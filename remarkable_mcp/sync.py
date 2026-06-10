"""
reMarkable Cloud Sync Client

A replacement for rmapy that uses the current reMarkable sync API (v3/v4).
rmapy is abandoned and uses deprecated endpoints that return 500 errors.

Based on the protocol used by ddvk/rmapi.
"""

import json
import logging
import math
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
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
ROOT_URL = f"{SYNC_HOST}/sync/v4/root"
FILES_URL = f"{SYNC_HOST}/sync/v3/files"


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

    Only the numeric form (seconds) is supported; HTTP-date values
    and invalid/negative/non-finite values fall back to jittered backoff.
    """
    header = response.headers.get("Retry-After")
    if header is None:
        return None
    try:
        val = float(header)
    except (ValueError, TypeError):
        return None
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
        self, url: str, method: str = "GET", headers: Optional[Dict[str, str]] = None
    ) -> requests.Response:
        """Make an authenticated request."""
        if not self.user_token:
            self.renew_token()

        request_headers = {"Authorization": f"Bearer {self.user_token}"}
        if headers:
            request_headers.update(headers)
        response = _http_request_with_retry(method, url, headers=request_headers, timeout=60)

        if response.status_code == 401:
            # Token expired, try to renew
            self.renew_token()
            request_headers = {"Authorization": f"Bearer {self.user_token}"}
            if headers:
                request_headers.update(headers)
            response = _http_request_with_retry(method, url, headers=request_headers, timeout=60)

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
