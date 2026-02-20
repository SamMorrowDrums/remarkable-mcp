"""
reMarkable USB Web Interface Client

Direct access to reMarkable tablet via USB Web Interface (HTTP API).
No SSH or developer mode required - just enable "USB web interface" in Settings.

Default connection: http://10.11.99.1 (USB connection)

The USB web interface provides:
- /documents/ - List all documents and folders
- /documents/{guid} - List documents in a folder
- /download/{guid}/rmdoc - Download raw document archive (firmware v3.9+)
- /download/{guid}/pdf - Download as PDF
- /upload - Upload documents
- /thumbnail/{guid} - Get document thumbnail

Benefits:
- No developer mode required (no factory reset)
- No reMarkable Connect subscription required
- Works over USB connection only (offline)
- Officially supported by reMarkable
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# Default USB web interface settings
DEFAULT_USB_HOST = "http://10.11.99.1"

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

    def _request(self, endpoint: str, method: str = "GET") -> requests.Response:
        """Make an HTTP request to the USB web interface."""
        url = f"{self.host}{endpoint}"
        try:
            response = requests.request(method, url, timeout=self.timeout)
            response.raise_for_status()
            return response
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
        )

    def get_meta_items(self, limit: Optional[int] = None) -> List[Document]:
        """
        Fetch documents and folders from the tablet via USB web interface.

        Args:
            limit: Maximum number of documents to fetch. If None, fetches all.

        Returns a list of Document objects.
        """
        # Return cached documents if available and no limit specified
        if self._documents and limit is None:
            return self._documents

        # If we have cached docs and limit is within cache, return slice
        if self._documents and limit is not None and len(self._documents) >= limit:
            return self._documents[:limit]

        documents = []
        folders_to_process = [("", DOCUMENTS_URL)]  # (parent_id, url)
        processed_folders = set()

        # Recursively fetch all documents from all folders
        while folders_to_process:
            parent_id, url = folders_to_process.pop(0)

            # Skip if already processed
            if url in processed_folders:
                continue
            processed_folders.add(url)

            try:
                response = self._request(url)
                entries = response.json()

                for entry in entries:
                    doc = self._parse_document_entry(entry, parent=parent_id)
                    documents.append(doc)

                    # If it's a folder, add it to the queue
                    if doc.is_folder:
                        folder_url = f"/documents/{doc.id}"
                        folders_to_process.append((doc.id, folder_url))

                    # Check limit
                    if limit is not None and len(documents) >= limit:
                        break

                if limit is not None and len(documents) >= limit:
                    break

            except Exception as e:
                logger.warning(f"Failed to fetch documents from {url}: {e}")
                continue

        self._documents = documents
        self._documents_by_id = {d.id: d for d in documents}

        logger.info(f"Loaded {len(documents)} documents via USB web interface")
        return documents

    def get_doc(self, doc_id: str) -> Optional[Document]:
        """Get a document by ID."""
        if not self._documents_by_id:
            self.get_meta_items()
        return self._documents_by_id.get(doc_id)

    def download(self, doc: Document) -> bytes:
        """
        Download a document's content as a zip file.

        Uses the /download/{guid}/rmdoc endpoint (requires firmware v3.9+).
        Returns the raw .rmdoc archive which is essentially a zip file.
        """
        endpoint = DOWNLOAD_URL.format(guid=doc.id)
        try:
            response = self._request(endpoint)
            return response.content
        except RuntimeError as e:
            # If rmdoc format fails, try PDF as fallback
            if "404" in str(e) or "Not Found" in str(e):
                logger.debug("rmdoc format not available, trying PDF fallback")
                try:
                    pdf_endpoint = DOWNLOAD_PDF_URL.format(guid=doc.id)
                    response = self._request(pdf_endpoint)
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

        For USB web interface, we use the /download/{guid}/pdf endpoint.
        Note: EPUB download may not be supported via USB web interface.
        """
        if extension.lower() == "pdf":
            endpoint = DOWNLOAD_PDF_URL.format(guid=doc.id)
            try:
                response = self._request(endpoint)
                return response.content
            except Exception as e:
                logger.debug(f"Failed to download PDF for {doc.id}: {e}")
                return None
        else:
            # EPUB and other formats may not be directly supported
            logger.debug(f"Format {extension} may not be supported via USB web interface")
            return None

    def get_file_type(self, doc: Document) -> Optional[str]:
        """
        Get the file type (pdf, epub, notebook) for a document.

        For USB web interface, we infer from the document name.
        A more sophisticated implementation could inspect the downloaded content.
        """
        name = doc.VissibleName.lower()
        if name.endswith(".pdf"):
            return "pdf"
        elif name.endswith(".epub"):
            return "epub"
        return "notebook"

    def get_all_file_types(self) -> dict[str, Optional[str]]:
        """
        Get file types for all documents.

        For USB web interface, we infer from document names.
        """
        if not self._documents_by_id:
            self.get_meta_items()

        return {doc_id: self.get_file_type(doc) for doc_id, doc in self._documents_by_id.items()}


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
    import os

    return USBWebClient(
        host=host or os.environ.get("REMARKABLE_USB_HOST", DEFAULT_USB_HOST),
        timeout=timeout or int(os.environ.get("REMARKABLE_USB_TIMEOUT", "10")),
    )
