"""
reMarkable Cloud Sync Client

A replacement for rmapy that uses the current reMarkable sync API (v3/v4).
rmapy is abandoned and uses deprecated endpoints that return 500 errors.

Based on the protocol used by ddvk/rmapi.
"""

import hashlib
import json
import logging
import math
import os
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)

# Thread-local HTTP sessions for connection pooling under parallel traversal.
_thread_local = threading.local()

# Retry configuration
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_DELAY = 2.0
MAX_RETRY_DELAY = 20.0
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Concurrency / cache configuration for metadata traversal.
# The cloud sync API is content-addressed (every blob is identified by its
# hash), so per-document blob/metadata fetches are independent and immutable:
# they parallelize cleanly and cache forever.
DEFAULT_SYNC_WORKERS = 16
MAX_SYNC_WORKERS = 64
# Only cache blobs at or below this size. Index/metadata blobs are tiny; this
# keeps large document content (PDF/.rm) out of the metadata cache by default.
DEFAULT_CACHE_MAX_BLOB_BYTES = 4 * 1024 * 1024


def _get_sync_workers() -> int:
    """Number of parallel workers for cloud metadata traversal (env-tunable)."""
    try:
        val = int(os.environ.get("REMARKABLE_SYNC_WORKERS", DEFAULT_SYNC_WORKERS))
    except (ValueError, TypeError):
        return DEFAULT_SYNC_WORKERS
    return max(1, min(val, MAX_SYNC_WORKERS))


def _cache_enabled() -> bool:
    """Whether the content-addressed blob cache is enabled (default: yes)."""
    return os.environ.get("REMARKABLE_DISABLE_CACHE", "").lower() not in ("1", "true", "yes")


def _cache_max_blob_bytes() -> int:
    try:
        return int(os.environ.get("REMARKABLE_CACHE_MAX_BLOB", DEFAULT_CACHE_MAX_BLOB_BYTES))
    except (ValueError, TypeError):
        return DEFAULT_CACHE_MAX_BLOB_BYTES


def _cache_dir() -> Path:
    """Directory for the content-addressed blob cache (env-overridable)."""
    override = os.environ.get("REMARKABLE_CACHE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".remarkable" / "cache" / "blobs"


# API endpoints
# Note: my.remarkable.com endpoints redirect to doesnotexist.remarkable.com
# So we use webapp-prod.cloud.remarkable.engineering for auth
AUTH_HOST = "https://webapp-prod.cloud.remarkable.engineering"
DEVICE_TOKEN_URL = f"{AUTH_HOST}/token/json/2/device/new"
USER_TOKEN_URL = f"{AUTH_HOST}/token/json/2/user/new"

SYNC_HOST = "https://internal.cloud.remarkable.com"
ROOT_URL = f"{SYNC_HOST}/sync/v4/root"  # GET current root (hash + generation)
ROOT_PUT_URL = f"{SYNC_HOST}/sync/v3/root"  # PUT to commit a new root (generation-gated)
FILES_URL = f"{SYNC_HOST}/sync/v3/files"

# Index serialization constants (reMarkable sync15 content-addressed store).
_SCHEMA_DOC = "3"  # document blob indexes are emitted as schema 3
_SCHEMA_ROOT = "4"  # root indexes must be emitted as schema 4 for writes
_TYPE_FILE = "0"  # entry type for a file inside a document index
# Max attempts to win the generation race when committing a new root.
_ROOT_COMMIT_ATTEMPTS = 5


def _get_retry_attempts() -> int:
    """Get the number of retry attempts from env or default."""
    try:
        val = int(os.environ.get("REMARKABLE_RETRY_ATTEMPTS", DEFAULT_RETRY_ATTEMPTS))
        return max(val, 1)
    except (ValueError, TypeError):
        return DEFAULT_RETRY_ATTEMPTS


def _get_retry_delay() -> float:
    """Get the base retry delay from env or default."""
    try:
        val = float(os.environ.get("REMARKABLE_RETRY_DELAY", DEFAULT_RETRY_DELAY))
        return max(val, 0.0)
    except (ValueError, TypeError):
        return DEFAULT_RETRY_DELAY


def _parse_retry_after(response: requests.Response) -> Optional[float]:
    """Parse the Retry-After header, returning seconds or None.

    Supports both forms defined by RFC 9110:
    - delay-seconds (e.g. ``120``)
    - HTTP-date (e.g. ``Wed, 21 Oct 2026 07:28:00 GMT``) — Cloudflare, which
      fronts the reMarkable cloud, often uses this on 429s. The delay is the
      time from now until that date.

    The result is clamped to ``[0, MAX_RETRY_DELAY]`` (consistent with the
    jittered backoff). Missing/invalid/negative values return None, so the
    caller falls back to exponential backoff with jitter.
    """
    header = response.headers.get("Retry-After")
    if header is None:
        return None

    # delay-seconds form.
    val: Optional[float]
    try:
        val = float(header)
    except (ValueError, TypeError):
        val = None

    # HTTP-date form.
    if val is None:
        try:
            retry_at = parsedate_to_datetime(header)
        except (TypeError, ValueError):
            return None
        if retry_at is None:
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        val = (retry_at - datetime.now(timezone.utc)).total_seconds()

    if not math.isfinite(val) or val < 0:
        return None
    return min(val, MAX_RETRY_DELAY)


def _compute_sleep(base_delay: float, attempt: int) -> float:
    """Compute sleep duration with exponential backoff and full jitter.

    Uses AWS's "full jitter" strategy: sleep uniformly in [0, cap] where
    cap = min(base * 2**attempt, MAX_RETRY_DELAY). This avoids
    thundering-herd retries from concurrent clients.
    """
    return random.uniform(0, min(base_delay * 2**attempt, MAX_RETRY_DELAY))


def _get_session() -> requests.Session:
    """Return a thread-local pooled HTTP session.

    Connection reuse (keep-alive) avoids a fresh TLS handshake on every request,
    which otherwise dominates cloud metadata traversal latency. Each worker
    thread gets its own session, so this is safe under the parallel traversal.
    """
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _thread_local.session = session
    return session


def _issue_request(method: str, url: str, **kwargs) -> requests.Response:
    """Issue a single HTTP request via the thread-local pooled session.

    This is the single seam through which all HTTP traffic flows (tests patch
    it to simulate responses).
    """
    return _get_session().request(method, url, **kwargs)


def _http_request_with_retry(method: str, url: str, **kwargs) -> requests.Response:
    """
    Make an HTTP request with exponential backoff and jitter.

    Retries on ConnectionError, Timeout, and retryable HTTP status codes
    (429, 500, 502, 503, 504). Does NOT retry on 401 or other 4xx errors.
    """
    max_attempts = _get_retry_attempts()
    base_delay = _get_retry_delay()
    last_exception: Optional[Exception] = None

    for attempt in range(max_attempts):
        try:
            response = _issue_request(method, url, **kwargs)

            if response.status_code not in RETRYABLE_STATUS_CODES:
                return response

            if attempt < max_attempts - 1:
                retry_after = _parse_retry_after(response)
                sleep_time = (
                    retry_after if retry_after is not None else _compute_sleep(base_delay, attempt)
                )
                logger.warning(
                    "Retryable HTTP %d from %s (attempt %d/%d), sleeping %.1fs",
                    response.status_code,
                    url,
                    attempt + 1,
                    max_attempts,
                    sleep_time,
                )
                time.sleep(sleep_time)
            else:
                return response

        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exception = exc
            if attempt < max_attempts - 1:
                sleep_time = _compute_sleep(base_delay, attempt)
                logger.warning(
                    "%s for %s (attempt %d/%d), sleeping %.1fs",
                    type(exc).__name__,
                    url,
                    attempt + 1,
                    max_attempts,
                    sleep_time,
                )
                time.sleep(sleep_time)

    raise last_exception  # noqa: TRY302  # exhausted retries, re-raise last connection error


def _hash_entries(files: List[Dict[str, Any]]) -> str:
    """Compute a document/root index hash from its entries.

    Mirrors ddvk/rmapi ``HashEntries``: sort entries by id, then SHA-256 over the
    concatenation of each entry's raw (hex-decoded) blob hash. Used for document
    index hashes (the hash a document is stored under in the root index).
    """
    hasher = hashlib.sha256()
    for entry in sorted(files, key=lambda e: e["id"]):
        hasher.update(bytes.fromhex(entry["hash"]))
    return hasher.hexdigest()


def _serialize_doc_index(files: List[Dict[str, Any]]) -> bytes:
    """Serialize a document's blob index (schema 3).

    Line 0 is the schema version; each subsequent line is
    ``hash:0:filename:0:size`` for one file blob.
    """
    lines = [_SCHEMA_DOC]
    for f in sorted(files, key=lambda e: e["id"]):
        lines.append(f"{f['hash']}:{_TYPE_FILE}:{f['id']}:0:{f['size']}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _serialize_root_index(entries: List[Dict[str, Any]]) -> bytes:
    """Serialize the root index (schema 4).

    Current servers reject new schema-3 root uploads, so writes always emit
    schema 4: line 0 is ``4``; line 1 is ``0:.:<numDocs>:<totalSize>``; each
    document line is ``hash:0:docId:<numFiles>:<size>``. The resulting root hash
    is ``sha256`` of this serialized body (not ``HashEntries``).
    """
    ordered = sorted(entries, key=lambda e: e["id"])
    total = sum(int(e["size"]) for e in ordered)
    lines = [_SCHEMA_ROOT, f"0:.:{len(ordered)}:{total}"]
    for e in ordered:
        lines.append(f"{e['hash']}:{_TYPE_FILE}:{e['id']}:{int(e['subfiles'])}:{int(e['size'])}")
    return ("\n".join(lines) + "\n").encode("utf-8")


class CloudWriteError(RuntimeError):
    """Raised when a cloud write cannot be completed."""


# CRC32C (Castagnoli) table for the GCS ``x-goog-hash`` upload integrity header.
# reMarkable's blob store sits behind Google Cloud Storage, which rejects PUTs
# without a matching ``crc32c`` checksum. stdlib ``zlib.crc32`` is IEEE, not
# Castagnoli, so we compute CRC32C ourselves (preferring the fast C extension).
_CRC32C_POLY = 0x82F63B78
_CRC32C_TABLE = []
for _i in range(256):
    _crc = _i
    for _ in range(8):
        _crc = (_crc >> 1) ^ (_CRC32C_POLY & -(_crc & 1))
    _CRC32C_TABLE.append(_crc & 0xFFFFFFFF)


def _crc32c_value(data: bytes) -> int:
    try:
        import google_crc32c

        return int.from_bytes(google_crc32c.Checksum(data).digest(), "big")
    except Exception:
        crc = 0xFFFFFFFF
        for byte in data:
            crc = (crc >> 8) ^ _CRC32C_TABLE[(crc ^ byte) & 0xFF]
        return crc ^ 0xFFFFFFFF


def _crc32c_header(data: bytes) -> str:
    """Return the ``x-goog-hash`` value (``crc32c=<base64>``) for ``data``."""
    import base64
    import struct

    digest = struct.pack(">I", _crc32c_value(data))
    return "crc32c=" + base64.b64encode(digest).decode("ascii")


@dataclass
class Document:
    """Represents a document or folder in the reMarkable cloud."""

    id: str
    hash: str
    name: str
    doc_type: str  # "DocumentType" or "CollectionType"
    parent: str = ""
    deleted: bool = False
    pinned: bool = False
    last_modified: Optional[datetime] = None
    size: int = 0
    files: List[Dict[str, Any]] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)

    @property
    def is_folder(self) -> bool:
        return self.doc_type == "CollectionType"

    @property
    def VissibleName(self) -> str:
        """Compatibility with rmapy naming."""
        return self.name

    @property
    def ID(self) -> str:
        """Compatibility with rmapy naming."""
        return self.id

    @property
    def Parent(self) -> str:
        """Compatibility with rmapy naming."""
        return self.parent

    @property
    def Type(self) -> str:
        """Compatibility with rmapy naming."""
        return self.doc_type

    @property
    def ModifiedClient(self) -> Optional[datetime]:
        """Compatibility with rmapy naming."""
        return self.last_modified


# Alias for backward compatibility with rmapy-style code
# In our sync module, both Document and Folder are the same class,
# distinguished by the is_folder property
Folder = Document


class RemarkableClient:
    """Client for reMarkable Cloud sync API."""

    def __init__(self, device_token: str = "", user_token: str = ""):
        self.device_token = device_token
        self.user_token = user_token
        self._documents: List[Document] = []
        self._documents_by_id: Dict[str, Document] = {}
        self._file_type_cache: Dict[str, Optional[str]] = {}

    def renew_token(self) -> str:
        """Exchange device token for a fresh user token."""
        if not self.device_token:
            raise RuntimeError("No device token available")

        headers = {"Authorization": f"Bearer {self.device_token}"}

        try:
            response = _http_request_with_retry("POST", USER_TOKEN_URL, headers=headers, timeout=30)
            if response.status_code == 200 and response.text:
                self.user_token = response.text.strip()
                return self.user_token
        except requests.RequestException as e:
            raise RuntimeError(f"Network error during token renewal: {e}")

        raise RuntimeError(
            f"Failed to renew user token (HTTP {response.status_code}).\n"
            "Your device may need to be re-registered.\n"
            "Get a new code from: https://my.remarkable.com/device/desktop/connect"
        )

    def _request(
        self,
        url: str,
        method: str = "GET",
        headers: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> requests.Response:
        """Make an authenticated request.

        Extra keyword arguments (e.g. ``data`` or ``json``) are forwarded to the
        underlying HTTP call, so the same auth/renew path serves writes too.
        """
        if not self.user_token:
            self.renew_token()

        request_headers = {"Authorization": f"Bearer {self.user_token}"}
        if headers:
            request_headers.update(headers)
        response = _http_request_with_retry(
            method, url, headers=request_headers, timeout=60, **kwargs
        )

        if response.status_code == 401:
            # Token expired, try to renew
            self.renew_token()
            request_headers = {"Authorization": f"Bearer {self.user_token}"}
            if headers:
                request_headers.update(headers)
            response = _http_request_with_retry(
                method, url, headers=request_headers, timeout=60, **kwargs
            )

        return response

    def _cache_read(self, file_hash: str) -> Optional[bytes]:
        """Return cached bytes for a content hash, or None on miss/disabled."""
        if not _cache_enabled():
            return None
        try:
            path = _cache_dir() / file_hash
            if path.is_file():
                return path.read_bytes()
        except Exception as e:  # pragma: no cover - cache must never break fetches
            logger.debug(f"Blob cache read failed for {file_hash}: {e}")
        return None

    def _cache_write(self, file_hash: str, content: bytes) -> None:
        """Persist bytes for a content hash (best-effort, atomic)."""
        if not _cache_enabled() or len(content) > _cache_max_blob_bytes():
            return
        try:
            cache_dir = _cache_dir()
            cache_dir.mkdir(parents=True, exist_ok=True)
            path = cache_dir / file_hash
            tmp = cache_dir / f".{file_hash}.{os.getpid()}.tmp"
            tmp.write_bytes(content)
            os.replace(tmp, path)
        except Exception as e:  # pragma: no cover - cache must never break fetches
            logger.debug(f"Blob cache write failed for {file_hash}: {e}")

    def _get_file(self, file_hash: str, file_name: str) -> bytes:
        """Download a file by its content hash.

        Blobs are content-addressed (immutable), so results are served from and
        stored in a local hash-keyed cache to make warm startups near-instant.
        """
        cached = self._cache_read(file_hash)
        if cached is not None:
            return cached
        response = self._request(f"{FILES_URL}/{file_hash}", headers={"rm-filename": file_name})
        response.raise_for_status()
        content = response.content
        self._cache_write(file_hash, content)
        return content

    def _parse_index(self, content: bytes) -> List[Dict[str, Any]]:
        """Parse an index file into entries."""
        lines = content.decode("utf-8").strip().split("\n")
        entries = []

        # First line is schema version
        for line in lines[1:]:
            parts = line.split(":")
            if len(parts) >= 5:
                entries.append(
                    {
                        "hash": parts[0],
                        "type": parts[1],
                        "id": parts[2],
                        "subfiles": int(parts[3]),
                        "size": int(parts[4]),
                    }
                )

        return entries

    def get_meta_items(self, limit: Optional[int] = None) -> List[Document]:
        """
        Fetch documents and folders from the cloud.

        Args:
            limit: Maximum number of documents to fetch. If None, fetches all.

        Returns a list of Document objects (compatible with rmapy Collection).
        """
        # Get root hash
        response = self._request(ROOT_URL)
        response.raise_for_status()

        # Handle empty or invalid JSON response
        if not response.text or not response.text.strip():
            raise RuntimeError(
                "Empty response from reMarkable API. Your token may have expired.\n"
                "Try re-registering: uvx remarkable-mcp --register <code>"
            )

        try:
            root_data = response.json()
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Invalid JSON from reMarkable API: {e}\nResponse was: {response.text[:200]}"
            )

        if "hash" not in root_data:
            raise RuntimeError(
                f"Unexpected API response format: {root_data}\nThe reMarkable API may have changed."
            )

        root_hash = root_data["hash"]

        # Get root index
        root_index = self._get_file(root_hash, "root.docSchema")
        entries = self._parse_index(root_index)

        # `limit` caps how many entries we fetch (used to bound work). Slicing
        # before fetching preserves the early-stop intent while letting us load
        # the rest in parallel.
        if limit is not None:
            entries = entries[:limit]

        # Each entry's blob index + metadata are independent, immutable fetches,
        # so load them in parallel. Results are placed back in entry order for
        # stable, deterministic output.
        documents_indexed: List[Optional[Document]] = [None] * len(entries)
        workers = _get_sync_workers()

        if workers <= 1 or len(entries) <= 1:
            for i, entry in enumerate(entries):
                documents_indexed[i] = self._load_document(entry)
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_to_idx = {
                    executor.submit(self._load_document, entry): i
                    for i, entry in enumerate(entries)
                }
                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    try:
                        documents_indexed[idx] = future.result()
                    except Exception as e:
                        logger.debug(f"Failed to load document at index {idx}: {e}")

        documents = [doc for doc in documents_indexed if doc is not None]

        self._documents = documents
        self._documents_by_id = {d.id: d for d in documents}

        return documents

    def _load_document(self, entry: Dict[str, Any]) -> Optional[Document]:
        """Load a single document's metadata from its index entry.

        Returns a Document, or None if the blob index can't be fetched or the
        document is marked deleted.
        """
        doc_id = entry["id"]
        doc_hash = entry["hash"]

        # Fetch the document's blob index
        try:
            blob_content = self._get_file(doc_hash, f"{doc_id}.docSchema")
            blob_entries = self._parse_index(blob_content)
        except Exception:
            return None

        # Find and fetch the metadata file
        metadata: Dict[str, Any] = {}
        files = []

        for blob_entry in blob_entries:
            files.append(blob_entry)
            if blob_entry["id"].endswith(".metadata"):
                try:
                    meta_content = self._get_file(blob_entry["hash"], blob_entry["id"])
                    metadata = json.loads(meta_content.decode("utf-8"))
                except Exception:
                    pass

        # Skip deleted documents
        if metadata.get("deleted", False):
            return None

        # Parse last modified timestamp
        last_modified = None
        if "lastModified" in metadata:
            try:
                ts = int(metadata["lastModified"]) / 1000  # Convert ms to seconds
                last_modified = datetime.fromtimestamp(ts)
            except (ValueError, TypeError):
                pass

        return Document(
            id=doc_id,
            hash=doc_hash,
            name=metadata.get("visibleName", doc_id),
            doc_type=metadata.get("type", "DocumentType"),
            parent=metadata.get("parent", ""),
            deleted=metadata.get("deleted", False),
            pinned=metadata.get("pinned", False),
            last_modified=last_modified,
            size=entry["size"],
            files=files,
            tags=metadata.get("tags", []),
        )

    def get_doc(self, doc_id: str) -> Optional[Document]:
        """Get a document by ID."""
        if not self._documents_by_id:
            self.get_meta_items()
        return self._documents_by_id.get(doc_id)

    def download(self, doc: Document) -> bytes:
        """Download a document's content as a zip file.

        Per-file blobs are fetched in parallel (and served from the
        content-addressed cache when present) so multi-page documents render
        without paying a sequential round-trip per page. The zip is assembled
        in the original blob order for deterministic output.
        """
        import io
        import zipfile

        blob_content = self._get_file(doc.hash, f"{doc.id}.docSchema")
        blob_entries = self._parse_index(blob_content)

        contents: List[Optional[bytes]] = [None] * len(blob_entries)

        def fetch(index: int, entry: Dict[str, Any]) -> None:
            try:
                contents[index] = self._get_file(entry["hash"], entry["id"])
            except Exception:
                contents[index] = None

        workers = _get_sync_workers()
        if workers > 1 and len(blob_entries) > 1:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(fetch, i, entry) for i, entry in enumerate(blob_entries)]
                for future in as_completed(futures):
                    future.result()
        else:
            for i, entry in enumerate(blob_entries):
                fetch(i, entry)

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_STORED) as zf:
            for entry, content in zip(blob_entries, contents):
                if content is not None:
                    zf.writestr(entry["id"], content)

        zip_buffer.seek(0)
        return zip_buffer.read()

    def _ensure_files(self, doc: Document) -> List[Dict[str, Any]]:
        """Return the document's blob index entries, fetching them if needed.

        Documents loaded via ``get_meta_items`` already carry ``files``; this is
        a defensive fallback for documents constructed without them.
        """
        if doc.files:
            return doc.files
        try:
            blob_content = self._get_file(doc.hash, f"{doc.id}.docSchema")
            doc.files = self._parse_index(blob_content)
        except Exception as e:
            logger.debug(f"Could not load blob index for {doc.id}: {e}")
            doc.files = []
        return doc.files

    def get_file_type(self, doc: Document) -> Optional[str]:
        """Return the document's source file type ('pdf', 'epub', or 'notebook').

        The cloud store keeps every blob of a document, including the original
        ``{id}.pdf`` / ``{id}.epub`` when present, so the type can be derived
        from the (already fetched) blob index without extra network calls. As a
        fallback the ``.content`` blob's ``fileType`` field is consulted.
        """
        if doc.id in self._file_type_cache:
            return self._file_type_cache[doc.id]

        file_type: Optional[str] = None
        content_hash: Optional[str] = None
        for entry in self._ensure_files(doc):
            entry_id = entry.get("id", "")
            if entry_id.endswith(".pdf"):
                file_type = "pdf"
                break
            if entry_id.endswith(".epub"):
                file_type = "epub"
                break
            if entry_id.endswith(".content"):
                content_hash = entry.get("hash")

        if file_type is None and content_hash:
            try:
                content = self._get_file(content_hash, f"{doc.id}.content")
                data = json.loads(content.decode("utf-8"))
                ft = data.get("fileType")
                if ft:
                    file_type = ft
            except Exception as e:
                logger.debug(f"Could not read .content fileType for {doc.id}: {e}")

        if file_type is None:
            file_type = "notebook"

        self._file_type_cache[doc.id] = file_type
        return file_type

    def download_raw_file(self, doc: Document, extension: str) -> Optional[bytes]:
        """Download the original source file (PDF or EPUB) for a document.

        Returns the raw bytes, or ``None`` if the document has no such source
        blob. The blob is content-addressed, so it is served from the local
        cache on warm reads.
        """
        ext = extension.lower().lstrip(".")
        suffix = f".{ext}"
        for entry in self._ensure_files(doc):
            if entry.get("id", "").endswith(suffix):
                try:
                    return self._get_file(entry["hash"], entry["id"])
                except Exception as e:
                    logger.debug(f"Failed to download {entry['id']} for {doc.id}: {e}")
                    return None
        return None

    def get_all_file_types(self) -> Dict[str, Optional[str]]:
        """Return a ``{doc_id: file_type}`` map for every loaded document.

        Types of PDF/EPUB documents are derived from each document's blob index
        with no extra network calls; the remaining documents (notebooks) consult
        their ``.content`` blob, so those reads are issued in parallel.
        """
        if not self._documents_by_id:
            self.get_meta_items()

        docs = list(self._documents_by_id.values())
        workers = _get_sync_workers()
        if workers > 1 and len(docs) > 1:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                list(executor.map(self.get_file_type, docs))
        return {doc.id: self.get_file_type(doc) for doc in docs}

    def check_connection(self) -> bool:
        """Return True if the cloud API is reachable and the token is valid."""
        try:
            response = self._request(ROOT_URL)
            return response.ok
        except Exception as e:
            logger.debug(f"Cloud connection check failed: {e}")
            return False

    # ------------------------------------------------------------------
    # Cloud write support
    #
    # The store is a content-addressed Merkle tree:
    #   * file blob  -> stored under sha256(content)
    #   * doc index  -> stored under HashEntries(files) (schema 3 body)
    #   * root index -> stored under sha256(body) (schema 4 body)
    # A write uploads any new blobs, re-serializes the root index, uploads it,
    # then commits it with the current generation (optimistic concurrency).
    # ------------------------------------------------------------------

    @staticmethod
    def _now_ms() -> str:
        return str(int(time.time() * 1000))

    def _put_blob(
        self,
        content: bytes,
        blob_hash: str,
        filename: str,
        content_type: str = "application/octet-stream",
    ) -> None:
        """Upload a blob's bytes, stored under ``blob_hash``.

        The store sits behind Google Cloud Storage, which requires a matching
        ``x-goog-hash`` (CRC32C) header on every upload.
        """
        headers = {
            "rm-filename": filename,
            "x-goog-hash": _crc32c_header(content),
            "content-type": content_type,
        }
        response = self._request(
            f"{FILES_URL}/{blob_hash}", method="PUT", headers=headers, data=content
        )
        response.raise_for_status()
        # The bytes are immutable under this hash, so prime the read cache.
        self._cache_write(blob_hash, content)

    def _upload_file_blob(self, content: bytes, filename: str) -> Dict[str, Any]:
        """Upload a file blob (hashed by content) and return its index entry."""
        blob_hash = hashlib.sha256(content).hexdigest()
        self._put_blob(content, blob_hash, filename)
        return {
            "hash": blob_hash,
            "type": _TYPE_FILE,
            "id": filename,
            "subfiles": 0,
            "size": len(content),
        }

    def _upload_doc_index(self, doc_id: str, files: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Upload a document blob index and return its root entry."""
        body = _serialize_doc_index(files)
        doc_hash = _hash_entries(files)
        self._put_blob(body, doc_hash, f"{doc_id}.docSchema")
        return {
            "hash": doc_hash,
            "type": _TYPE_FILE,
            "id": doc_id,
            "subfiles": len(files),
            "size": sum(int(f["size"]) for f in files),
        }

    def get_root(self) -> tuple:
        """Return the current ``(root_hash, generation)`` from the cloud."""
        response = self._request(ROOT_URL)
        response.raise_for_status()
        data = response.json()
        return data["hash"], data.get("generation", 0)

    def _read_root_entries(self) -> tuple:
        """Return ``(entries, generation)`` for the current root index."""
        root_hash, generation = self.get_root()
        root_index = self._get_file(root_hash, "root.docSchema")
        return self._parse_index(root_index), generation

    def _commit_root(self, root_hash: str, generation: int, broadcast: bool = True) -> int:
        """Commit a new root hash, gated on ``generation``.

        Returns the new generation. Raises ``CloudWriteError`` with ``conflict``
        set when the generation no longer matches (someone else wrote first).
        """
        body = {"broadcast": broadcast, "hash": root_hash, "generation": generation}
        response = self._request(
            ROOT_PUT_URL,
            method="PUT",
            headers={"rm-filename": "roothash"},
            json=body,
        )
        # 409/412/428 mean another client committed first (generation race) and
        # the write should be retried against the fresh root.
        if response.status_code in (409, 412, 428):
            err = CloudWriteError(
                f"Root generation conflict (HTTP {response.status_code}); will retry."
            )
            err.conflict = True
            raise err
        if not response.ok:
            raise CloudWriteError(
                f"Root commit failed (HTTP {response.status_code}): {response.text[:200]}"
            )
        data = response.json()
        if data.get("hash") != root_hash:
            raise CloudWriteError("Root commit returned a mismatched hash.")
        return data.get("generation", generation)

    def _sync_root(self, mutate) -> None:
        """Apply ``mutate(entries) -> new_entries`` and commit a new root.

        ``mutate`` may upload blobs as a side effect; it is re-invoked on a
        generation conflict after re-reading the remote root, so it must be
        idempotent with respect to blob uploads (content-addressed uploads are).
        On success all in-memory document caches are invalidated.
        """
        last_error: Optional[Exception] = None
        for attempt in range(_ROOT_COMMIT_ATTEMPTS):
            entries, generation = self._read_root_entries()
            new_entries = mutate(list(entries))
            root_body = _serialize_root_index(new_entries)
            root_hash = hashlib.sha256(root_body).hexdigest()
            self._put_blob(root_body, root_hash, "root.docSchema", "text/plain; charset=UTF-8")
            try:
                self._commit_root(root_hash, generation)
                self._invalidate_caches()
                return
            except CloudWriteError as e:
                if getattr(e, "conflict", False) and attempt < _ROOT_COMMIT_ATTEMPTS - 1:
                    last_error = e
                    time.sleep(_compute_sleep(_get_retry_delay(), attempt))
                    continue
                raise
        raise CloudWriteError(
            f"Could not commit root after {_ROOT_COMMIT_ATTEMPTS} attempts: {last_error}"
        )

    def _invalidate_caches(self) -> None:
        """Drop in-memory document/file-type caches after a mutation."""
        self._documents = []
        self._documents_by_id = {}
        self._file_type_cache = {}

    def _mutate_doc_metadata(self, entry: Dict[str, Any], apply: "Any") -> Dict[str, Any]:
        """Rewrite one document's ``.metadata`` blob and return its new root entry.

        ``apply(metadata: dict)`` mutates the metadata in place. The version is
        bumped and ``lastModified`` refreshed so the device picks up the change.
        """
        doc_id = entry["id"]
        files = self._parse_index(self._get_file(entry["hash"], f"{doc_id}.docSchema"))
        meta_entry = next((f for f in files if f["id"].endswith(".metadata")), None)
        if meta_entry is None:
            raise CloudWriteError(f"Document {doc_id} has no metadata blob.")

        metadata = json.loads(self._get_file(meta_entry["hash"], meta_entry["id"]).decode("utf-8"))
        apply(metadata)
        try:
            metadata["version"] = int(metadata.get("version", 1)) + 1
        except (TypeError, ValueError):
            metadata["version"] = 1
        metadata["lastModified"] = self._now_ms()
        metadata["metadatamodified"] = True

        new_meta = json.dumps(metadata, sort_keys=True).encode("utf-8")
        new_entry = self._upload_file_blob(new_meta, meta_entry["id"])
        files = [new_entry if f["id"] == meta_entry["id"] else f for f in files]
        return self._upload_doc_index(doc_id, files)

    def _replace_root_entry(self, entries, new_entry):
        """Return ``entries`` with the entry matching ``new_entry['id']`` replaced."""
        return [new_entry if e["id"] == new_entry["id"] else e for e in entries]

    def _require_entry(self, entries, doc_id):
        entry = next((e for e in entries if e["id"] == doc_id), None)
        if entry is None:
            raise CloudWriteError(f"Document {doc_id} not found in cloud root.")
        return entry

    def create_folder(self, name: str, parent_id: str = "") -> Document:
        """Create a folder (CollectionType) in the cloud and return it."""
        doc_id = str(uuid.uuid4())
        now = self._now_ms()
        metadata = {
            "createdTime": now,
            "lastModified": now,
            "new": False,
            "parent": parent_id or "",
            "pinned": False,
            "source": "",
            "type": "CollectionType",
            "visibleName": name,
        }
        meta_bytes = json.dumps(metadata, sort_keys=True).encode("utf-8")
        files = [self._upload_file_blob(meta_bytes, f"{doc_id}.metadata")]
        new_entry = self._upload_doc_index(doc_id, files)
        self._sync_root(lambda entries: entries + [new_entry])
        return Document(
            id=doc_id,
            hash=new_entry["hash"],
            name=name,
            doc_type="CollectionType",
            parent=parent_id or "",
        )

    def rename(self, doc_id: str, new_name: str) -> None:
        """Rename a document or folder in the cloud."""

        def apply(meta):
            meta["visibleName"] = new_name

        def mutate(entries):
            entry = self._require_entry(entries, doc_id)
            return self._replace_root_entry(entries, self._mutate_doc_metadata(entry, apply))

        self._sync_root(mutate)

    def move(self, doc_id: str, new_parent_id: str) -> None:
        """Move a document or folder under a new parent ("" = root)."""

        def apply(meta):
            meta["parent"] = new_parent_id or ""

        def mutate(entries):
            entry = self._require_entry(entries, doc_id)
            return self._replace_root_entry(entries, self._mutate_doc_metadata(entry, apply))

        self._sync_root(mutate)

    def delete(self, doc_id: str) -> None:
        """Move a document or folder to the trash (recoverable via ``restore``)."""
        self.move(doc_id, "trash")

    def restore(self, doc_id: str, parent_id: str = "") -> None:
        """Restore a trashed document or folder back to ``parent_id`` ("" = root)."""
        self.move(doc_id, parent_id or "")

    def upload_document(
        self,
        content: bytes,
        name: str,
        file_type: str,
        parent_id: str = "",
    ) -> Document:
        """Upload a PDF or EPUB document to the cloud and return it.

        Builds the four blobs a reMarkable document needs (``.content``,
        ``.metadata``, ``.pagedata`` and the source ``.pdf``/``.epub``), then
        adds the document to the root index.
        """
        ext = file_type.lower().lstrip(".")
        if ext not in ("pdf", "epub"):
            raise CloudWriteError(f"Unsupported upload type '{ext}'; use pdf or epub.")

        doc_id = str(uuid.uuid4())
        now = self._now_ms()

        page_count, page_uuids = self._page_layout(content, ext)
        content_json = {
            "coverPageNumber": -1,
            "documentMetadata": {},
            "dummyDocument": False,
            "extraMetadata": {},
            "fileType": ext,
            "fontName": "",
            "formatVersion": 1,
            "lineHeight": -1,
            "margins": 100,
            "orientation": "portrait",
            "pageCount": page_count,
            "pageTags": [],
            "pages": page_uuids,
            "tags": [],
            "textScale": 1,
        }
        metadata = {
            "createdTime": now,
            "deleted": False,
            "lastModified": now,
            "lastOpened": now,
            "lastOpenedPage": 0,
            "metadatamodified": False,
            "modified": False,
            "new": False,
            "parent": parent_id or "",
            "pinned": False,
            "source": "",
            "synced": False,
            "type": "DocumentType",
            "version": 1,
            "visibleName": name,
        }
        pagedata = ("Blank\n" * page_count) if page_count else "\n"

        files = [
            self._upload_file_blob(
                json.dumps(content_json, sort_keys=True).encode("utf-8"), f"{doc_id}.content"
            ),
            self._upload_file_blob(
                json.dumps(metadata, sort_keys=True).encode("utf-8"), f"{doc_id}.metadata"
            ),
            self._upload_file_blob(pagedata.encode("utf-8"), f"{doc_id}.pagedata"),
            self._upload_file_blob(content, f"{doc_id}.{ext}"),
        ]
        new_entry = self._upload_doc_index(doc_id, files)
        self._sync_root(lambda entries: entries + [new_entry])
        return Document(
            id=doc_id,
            hash=new_entry["hash"],
            name=name,
            doc_type="DocumentType",
            parent=parent_id or "",
        )

    @staticmethod
    def _page_layout(content: bytes, ext: str) -> tuple:
        """Return ``(page_count, page_uuids)`` for a source file.

        PDFs are measured with PyMuPDF so the device shows the right page count;
        EPUBs reflow on-device, so they start with no fixed pages.
        """
        if ext != "pdf":
            return 0, []
        try:
            import fitz

            with fitz.open(stream=content, filetype="pdf") as pdf:
                count = pdf.page_count
            return count, [str(uuid.uuid4()) for _ in range(count)]
        except Exception as e:  # pragma: no cover - defensive; upload still proceeds
            logger.debug(f"Could not count PDF pages: {e}")
            return 0, []


def register_device(one_time_code: str) -> Dict[str, str]:
    """
    Register a new device with reMarkable cloud.

    Args:
        one_time_code: Code from https://my.remarkable.com/device/desktop/connect

    Returns:
        Dict with devicetoken and usertoken keys
    """
    from uuid import uuid4

    body = {
        "code": one_time_code,
        "deviceDesc": "desktop-linux",
        "deviceID": str(uuid4()),
    }

    try:
        response = _http_request_with_retry("POST", DEVICE_TOKEN_URL, json=body, timeout=30)
        if response.status_code == 200 and response.text:
            device_token = response.text.strip()
            return {"devicetoken": device_token, "usertoken": ""}
    except requests.RequestException as e:
        raise RuntimeError(f"Network error during registration: {e}")

    raise RuntimeError(
        f"Registration failed (HTTP {response.status_code}). This usually means:\n"
        "  1. The code has expired (codes are single-use)\n"
        "  2. The code was already used\n"
        "  3. The code was typed incorrectly\n\n"
        "Get a new code from: https://my.remarkable.com/device/desktop/connect"
    )


def load_client_from_token(token_data: str) -> RemarkableClient:
    """
    Create a client from a token string.

    Args:
        token_data: Either:
            - JSON string with devicetoken and optional usertoken
            - Raw JWT device token (legacy format from rmapy)

    Returns:
        Configured RemarkableClient
    """
    token_data = token_data.strip()

    # Try to parse as JSON first
    if token_data.startswith("{"):
        try:
            data = json.loads(token_data)
            return RemarkableClient(
                device_token=data.get("devicetoken", ""),
                user_token=data.get("usertoken", ""),
            )
        except json.JSONDecodeError:
            pass

    # Treat as raw device token (legacy rmapy format - just the JWT)
    # JWT tokens start with "eyJ" (base64 encoded '{"')
    if token_data.startswith("eyJ"):
        return RemarkableClient(device_token=token_data, user_token="")

    raise ValueError(
        f"Invalid token format. Expected JSON or JWT token.\n"
        f"Token starts with: {token_data[:20]}..."
    )


def load_client_from_file(token_file: Path = Path.home() / ".rmapi") -> RemarkableClient:
    """
    Load a client from a token file.

    Args:
        token_file: Path to JSON token file (default: ~/.rmapi)

    Returns:
        Configured RemarkableClient
    """
    if not token_file.exists():
        raise RuntimeError(
            f"Token file not found: {token_file}\n"
            "Register first with: uvx remarkable-mcp --register <code>"
        )

    token_json = token_file.read_text()
    return load_client_from_token(token_json)
