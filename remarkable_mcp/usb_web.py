"""
reMarkable USB Web Interface Client

Direct access to reMarkable tablet via USB Web Interface (HTTP API).
Just enable "USB web interface" in Settings → Storage.

Default connection: http://10.11.99.1 (USB connection)

The USB web interface provides:
- /documents/ - List all documents and folders
- /documents/{guid} - List documents in a folder
- /download/{guid}/rmdoc - Download raw document archive (firmware v3.9+)
- /download/{guid}/pdf - Download as PDF
- /upload - Upload documents
- /thumbnail/{guid} - Get document thumbnail

Benefits:
- No subscription required
- No reMarkable Connect subscription required
- Works over USB connection only (offline)
- Officially supported by reMarkable
"""

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from remarkable_mcp.operation_queue import OperationDispatcher

logger = logging.getLogger(__name__)

# Default USB web interface settings
DEFAULT_USB_HOST = "http://10.11.99.1"
GET_408_MAX_ATTEMPTS = 3
GET_408_BACKOFF_SECONDS = 0.25

# API endpoints
DOCUMENTS_URL = "/documents/"
DOWNLOAD_URL = "/download/{guid}/rmdoc"
DOWNLOAD_PDF_URL = "/download/{guid}/pdf"
THUMBNAIL_URL = "/thumbnail/{guid}"


@dataclass
class Document:
    """Represents a document or folder on the reMarkable tablet."""

    id: str
    hash: str
    name: str
    doc_type: str  # "DocumentType" or "CollectionType"
    parent: str = ""
    deleted: bool = False
    pinned: bool = False
    synced: bool = True
    last_modified: Optional[datetime] = None
    size: int = 0
    file_type: Optional[str] = None  # "pdf", "epub", "notebook" — from API response
    bookmarked: bool = False
    current_page: int = 0
    tags: List[str] = field(default_factory=list)
    files: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def is_folder(self) -> bool:
        return self.doc_type == "CollectionType"

    @property
    def is_cloud_archived(self) -> bool:
        """False for USB - all documents are on device."""
        return False

    @property
    def VissibleName(self) -> str:
        """Compatibility with cloud client naming."""
        return self.name

    @property
    def ID(self) -> str:
        """Compatibility with cloud client naming."""
        return self.id

    @property
    def Parent(self) -> str:
        """Compatibility with cloud client naming."""
        return self.parent

    @property
    def Type(self) -> str:
        """Compatibility with cloud client naming."""
        return self.doc_type

    @property
    def ModifiedClient(self) -> Optional[datetime]:
        """Compatibility with cloud client naming."""
        return self.last_modified


# Alias for compatibility
Folder = Document


class USBWebClient:
    """Client for accessing reMarkable tablet via USB web interface."""

    def __init__(self, host: str = DEFAULT_USB_HOST, timeout: int = 10):
        """
        Initialize USB web interface client.

        Args:
            host: Base URL for the USB web interface (default: http://10.11.99.1)
            timeout: Request timeout in seconds (default: 10)
        """
        self.host = host.rstrip("/")
        self.timeout = timeout
        self._documents: List[Document] = []
        self._documents_by_id: Dict[str, Document] = {}
        self._metadata_loaded_all = False
        self._metadata_lock = threading.RLock()
        self._metadata_condition = threading.Condition(self._metadata_lock)
        self._metadata_loading = False
        self._metadata_generation = 0
        try:
            max_concurrency = int(os.environ.get("REMARKABLE_USB_MAX_CONCURRENCY", "2"))
        except ValueError as exc:
            raise ValueError("REMARKABLE_USB_MAX_CONCURRENCY must be an integer") from exc
        if not 1 <= max_concurrency <= 16:
            raise ValueError("REMARKABLE_USB_MAX_CONCURRENCY must be between 1 and 16")
        self._dispatcher = OperationDispatcher(
            name="USB HTTP",
            max_concurrency=max_concurrency,
        )

    def _request(
        self, endpoint: str, method: str = "GET", timeout: int | None = None
    ) -> requests.Response:
        """Make an HTTP request to the USB web interface."""
        normalized_method = method.upper()
        return self._dispatcher.call(
            f"{normalized_method} {endpoint}",
            lambda: self._request_unqueued(endpoint, normalized_method, timeout),
        )

    def _request_unqueued(
        self, endpoint: str, method: str, timeout: int | None
    ) -> requests.Response:
        url = f"{self.host}{endpoint}"
        request_timeout = self.timeout if timeout is None else timeout

        try:
            for attempt in range(GET_408_MAX_ATTEMPTS):
                response = requests.request(method, url, timeout=request_timeout)
                if response.status_code != 408 or method != "GET":
                    response.raise_for_status()
                    return response

                if attempt == GET_408_MAX_ATTEMPTS - 1:
                    response.close()
                    raise RuntimeError(
                        f"USB web interface returned HTTP 408 after "
                        f"{GET_408_MAX_ATTEMPTS} GET attempts for {endpoint}. "
                        "The tablet's xochitl HTTP handler did not complete the request in time. "
                        "The tablet may be temporarily busy; wait a moment and try again."
                    )

                response.close()
                delay = GET_408_BACKOFF_SECONDS * (2**attempt)
                logger.warning(
                    "USB web interface returned HTTP 408 for %s (attempt %d/%d); retrying in %.2fs",
                    endpoint,
                    attempt + 1,
                    GET_408_MAX_ATTEMPTS,
                    delay,
                )
                time.sleep(delay)
        except requests.Timeout:
            raise RuntimeError(
                "USB web interface request timed out. "
                "Make sure USB web interface is enabled on your reMarkable "
                "(Settings → Storage → USB web interface)"
            )
        except requests.ConnectionError:
            raise RuntimeError(
                f"Cannot connect to USB web interface at {self.host}. "
                f"Make sure:\n"
                f"  1. Your reMarkable is connected via USB\n"
                f"  2. USB web interface is enabled (Settings → Storage)\n"
                f"  3. The device is on and unlocked"
            )
        except requests.HTTPError as e:
            raise RuntimeError(f"USB web interface request failed: {e}")

    async def run_method_async(self, func, *args, **kwargs):
        """Await USB operations while keeping HTTP I/O on the bounded dispatcher."""
        if func.__name__ == "get_meta_items":
            limit = args[0] if args else kwargs.get("limit")
            if self._get_cached_meta_items(limit) is not None:
                return func(*args, **kwargs)
            # Metadata waiters must not occupy dispatcher workers needed by
            # independent downloads. The traversal's _request calls remain bounded.
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self._dispatcher.call_async(
            func.__name__.replace("_", "-"),
            lambda: func(*args, **kwargs),
        )

    async def run_operation_async(self, operation: str, callback):
        return await self._dispatcher.call_async(operation, callback)

    def reliability_status(self) -> dict:
        return {"operations": self._dispatcher.diagnostics()}

    def close(self) -> None:
        self._dispatcher.close()

    async def aclose(self) -> None:
        await asyncio.to_thread(self._dispatcher.close)

    def check_connection(self) -> bool:
        """Check if USB web interface is accessible."""
        try:
            self._request(DOCUMENTS_URL)
            return True
        except Exception as e:
            logger.debug(f"USB web interface check failed: {e}")
            return False

    def _parse_document_entry(self, entry: Dict[str, Any], parent: str = "") -> Document:
        """Parse a document entry from the USB web interface response."""
        # USB web interface returns entries like:
        # {"ID": "guid", "VissibleName": "name", "Type": "DocumentType"}
        doc_id = entry.get("ID", "")
        name = entry.get("VissibleName", doc_id)
        doc_type = entry.get("Type", "DocumentType")

        # Try to parse modification time if available
        last_modified = None
        if "ModifiedClient" in entry:
            try:
                # Try parsing ISO format
                last_modified = datetime.fromisoformat(
                    entry["ModifiedClient"].replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                pass

        return Document(
            id=doc_id,
            hash=doc_id,
            name=name,
            doc_type=doc_type,
            parent=parent,
            last_modified=last_modified,
            file_type=entry.get("fileType"),
            bookmarked=entry.get("Bookmarked", False),
            current_page=entry.get("CurrentPage", 0),
        )

    def _get_cached_meta_items_locked(self, limit: Optional[int]) -> Optional[List[Document]]:
        if self._metadata_loaded_all:
            return self._documents if limit is None else self._documents[:limit]
        if limit is not None and len(self._documents) >= limit:
            return self._documents[:limit]
        return None

    def _get_cached_meta_items(self, limit: Optional[int]) -> Optional[List[Document]]:
        with self._metadata_lock:
            return self._get_cached_meta_items_locked(limit)

    def _publish_metadata_locked(self, documents: List[Document], *, loaded_all: bool) -> None:
        self._documents = documents
        self._documents_by_id = {document.id: document for document in documents}
        self._metadata_loaded_all = loaded_all

    def invalidate_metadata_cache(self) -> None:
        """Clear metadata atomically and reject any traversal already in flight."""
        with self._metadata_condition:
            self._documents = []
            self._documents_by_id = {}
            self._metadata_loaded_all = False
            self._metadata_generation += 1
            self._metadata_condition.notify_all()

    def _load_meta_items(self, limit: Optional[int]) -> tuple[List[Document], bool]:
        documents = []
        folders_to_process = [("", DOCUMENTS_URL)]  # (parent_id, url)
        processed_folders = set()

        while folders_to_process:
            parent_id, url = folders_to_process.pop(0)
            if url in processed_folders:
                continue
            processed_folders.add(url)

            try:
                response = self._request(url)
                entries = response.json()
            except Exception as exc:
                raise RuntimeError(f"Failed to fetch documents from {url}: {exc}") from exc

            for index, entry in enumerate(entries):
                doc = self._parse_document_entry(entry, parent=parent_id)
                documents.append(doc)

                if doc.is_folder:
                    folder_url = f"/documents/{doc.id}"
                    folders_to_process.append((doc.id, folder_url))

                if limit is not None and len(documents) >= limit:
                    loaded_all = index == len(entries) - 1 and not folders_to_process
                    return documents, loaded_all

        return documents, True

    def get_meta_items(self, limit: Optional[int] = None) -> List[Document]:
        """
        Fetch documents and folders from the tablet via USB web interface.

        Args:
            limit: Maximum number of documents to fetch. If None, fetches all.

        Returns a list of Document objects.
        """
        with self._metadata_condition:
            while True:
                cached = self._get_cached_meta_items_locked(limit)
                if cached is not None:
                    return cached
                if not self._metadata_loading:
                    self._metadata_loading = True
                    generation = self._metadata_generation
                    break
                self._metadata_condition.wait()

        try:
            documents, loaded_all = self._load_meta_items(limit)
        except BaseException:
            with self._metadata_condition:
                self._metadata_loading = False
                self._metadata_condition.notify_all()
            raise

        with self._metadata_condition:
            try:
                if generation == self._metadata_generation:
                    self._publish_metadata_locked(documents, loaded_all=loaded_all)
            finally:
                self._metadata_loading = False
                self._metadata_condition.notify_all()

        logger.info(f"Fetched {len(documents)} documents via USB web interface")
        return documents

    def get_doc(self, doc_id: str) -> Optional[Document]:
        """Get a document by ID."""
        while True:
            with self._metadata_lock:
                document = self._documents_by_id.get(doc_id)
                if document is not None or self._metadata_loaded_all:
                    return document
            self.get_meta_items()

    # Downloads can be large — use a longer timeout
    DOWNLOAD_TIMEOUT = 120

    def download(self, doc: Document) -> bytes:
        """
        Download a document's content as a zip file.

        Uses the /download/{guid}/rmdoc endpoint (requires firmware v3.9+).
        Returns the raw .rmdoc archive which is essentially a zip file.
        """
        endpoint = DOWNLOAD_URL.format(guid=doc.id)
        try:
            response = self._request(endpoint, timeout=self.DOWNLOAD_TIMEOUT)
            return response.content
        except RuntimeError as e:
            # If rmdoc format fails, try PDF as fallback
            if "404" in str(e) or "Not Found" in str(e):
                logger.debug("rmdoc format not available, trying PDF fallback")
                try:
                    pdf_endpoint = DOWNLOAD_PDF_URL.format(guid=doc.id)
                    response = self._request(pdf_endpoint, timeout=self.DOWNLOAD_TIMEOUT)
                    return response.content
                except Exception as pdf_e:
                    raise RuntimeError(
                        f"Failed to download document {doc.id}. "
                        f"rmdoc error: {e}, PDF error: {pdf_e}"
                    )
            raise

    def download_raw_file(self, doc: Document, extension: str) -> Optional[bytes]:
        """
        Download a raw file (PDF or EPUB) for a document.

        The .rmdoc archive contains the original source files (.pdf, .epub)
        alongside the .rm notebook data, so we extract from the archive.
        Falls back to the /download/{guid}/pdf endpoint for PDF.
        """
        ext = extension.lower().lstrip(".")
        # Try extracting from the .rmdoc archive first
        try:
            rmdoc_data = self.download(doc)
            import io
            import zipfile

            with zipfile.ZipFile(io.BytesIO(rmdoc_data)) as z:
                for name in z.namelist():
                    if name.endswith(f".{ext}"):
                        return z.read(name)
        except Exception as e:
            logger.debug(f"Failed to extract .{ext} from rmdoc for {doc.id}: {e}")

        # Fall back to /download/{guid}/pdf endpoint for PDF
        if ext == "pdf":
            try:
                endpoint = DOWNLOAD_PDF_URL.format(guid=doc.id)
                response = self._request(endpoint, timeout=self.DOWNLOAD_TIMEOUT)
                return response.content
            except Exception as e:
                logger.debug(f"Failed to download PDF for {doc.id}: {e}")

        return None

    def get_file_type(self, doc: Document) -> Optional[str]:
        """
        Get the file type (pdf, epub, notebook) for a document.

        Uses the fileType field returned by the USB web API.
        """
        if doc.file_type:
            return doc.file_type
        return "notebook"

    def get_all_file_types(self) -> dict[str, Optional[str]]:
        """
        Get file types for all documents.

        Uses the fileType field from the USB web API response.
        """
        documents = self.get_meta_items()
        return {doc.id: self.get_file_type(doc) for doc in documents}


def check_usb_web_available(host: str = DEFAULT_USB_HOST) -> bool:
    """Check if USB web interface is accessible."""
    client = USBWebClient(host=host)
    return client.check_connection()


def create_usb_web_client(
    host: Optional[str] = None, timeout: Optional[int] = None
) -> USBWebClient:
    """
    Create a USB web interface client.

    Environment variables:
    - REMARKABLE_USB_HOST: USB web interface host (default: http://10.11.99.1)
    - REMARKABLE_USB_TIMEOUT: Request timeout in seconds (default: 10)
    """
    return USBWebClient(
        host=host or os.environ.get("REMARKABLE_USB_HOST", DEFAULT_USB_HOST),
        timeout=timeout or int(os.environ.get("REMARKABLE_USB_TIMEOUT", "10")),
    )
