"""
reMarkable SSH Client

Direct access to reMarkable tablet via SSH when connected over USB.
Default connection: root@10.11.99.1 (USB connection)

The tablet stores documents at:
/home/root/.local/share/remarkable/xochitl/

Each document is a folder with:
- {uuid}.metadata - JSON with visibleName, type, parent, etc.
- {uuid}.content - JSON with file info
- {uuid}/ - folder with .rm files (pages), .pdf, etc.
"""

import io
import json
import logging
import os
import subprocess
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Default SSH settings for USB connection
DEFAULT_SSH_HOST = "10.11.99.1"
DEFAULT_SSH_USER = "root"
DEFAULT_SSH_PORT = 22

# Document storage path on the tablet
XOCHITL_PATH = "/home/root/.local/share/remarkable/xochitl"


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
    last_modified: Optional[datetime] = None
    size: int = 0
    files: List[Dict[str, Any]] = field(default_factory=list)
    # SSH-specific: local path to the document folder
    local_path: Optional[str] = None

    @property
    def is_folder(self) -> bool:
        return self.doc_type == "CollectionType"

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


class SSHClient:
    """Client for accessing reMarkable tablet via SSH."""

    def __init__(
        self,
        host: str = DEFAULT_SSH_HOST,
        user: str = DEFAULT_SSH_USER,
        port: int = DEFAULT_SSH_PORT,
    ):
        self.host = host
        self.user = user
        self.port = port
        self._documents: List[Document] = []
        self._documents_by_id: Dict[str, Document] = {}

    def _ssh_command(self, command: str, timeout: int = 30) -> str:
        """Execute a command on the tablet via SSH."""
        ssh_args = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-p",
            str(self.port),
            f"{self.user}@{self.host}",
            command,
        ]

        try:
            result = subprocess.run(
                ssh_args,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode != 0:
                raise RuntimeError(f"SSH command failed: {result.stderr}")
            return result.stdout
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"SSH command timed out after {timeout}s")
        except FileNotFoundError:
            raise RuntimeError("SSH client not found. Install openssh-client.")

    def _scp_download(self, remote_path: str, timeout: int = 60) -> bytes:
        """Download a file from the tablet via SCP."""
        scp_args = [
            "scp",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-P",
            str(self.port),
            f"{self.user}@{self.host}:{remote_path}",
            "/dev/stdout",
        ]

        try:
            result = subprocess.run(
                scp_args,
                capture_output=True,
                timeout=timeout,
            )
            if result.returncode != 0:
                raise RuntimeError(f"SCP failed: {result.stderr.decode()}")
            return result.stdout
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"SCP timed out after {timeout}s")

    def check_connection(self) -> bool:
        """Check if SSH connection to tablet is available."""
        try:
            self._ssh_command("echo ok", timeout=5)
            return True
        except Exception as e:
            logger.debug(f"SSH connection check failed: {e}")
            return False

    def get_meta_items(self, limit: Optional[int] = None) -> List[Document]:
        """
        Fetch documents and folders from the tablet via SSH.

        Args:
            limit: Maximum number of documents to fetch. If None, fetches all.

        Returns a list of Document objects.
        """
        # List all .metadata files
        try:
            output = self._ssh_command(f"ls -1 {XOCHITL_PATH}/*.metadata 2>/dev/null || true")
        except Exception as e:
            raise RuntimeError(f"Failed to list documents: {e}")

        metadata_files = [f.strip() for f in output.strip().split("\n") if f.strip()]

        documents = []

        for meta_path in metadata_files:
            if limit is not None and len(documents) >= limit:
                break

            try:
                # Extract UUID from path
                doc_id = Path(meta_path).stem  # removes .metadata

                # Read metadata
                meta_content = self._ssh_command(f"cat '{meta_path}'")
                metadata = json.loads(meta_content)

                # Skip deleted documents
                if metadata.get("deleted", False):
                    continue

                # Parse last modified timestamp
                last_modified = None
                if "lastModified" in metadata:
                    try:
                        ts = int(metadata["lastModified"]) / 1000
                        last_modified = datetime.fromtimestamp(ts)
                    except (ValueError, TypeError):
                        pass

                doc = Document(
                    id=doc_id,
                    hash=doc_id,  # Use ID as hash for SSH
                    name=metadata.get("visibleName", doc_id),
                    doc_type=metadata.get("type", "DocumentType"),
                    parent=metadata.get("parent", ""),
                    deleted=metadata.get("deleted", False),
                    pinned=metadata.get("pinned", False),
                    last_modified=last_modified,
                    size=0,
                    local_path=f"{XOCHITL_PATH}/{doc_id}",
                )

                documents.append(doc)

            except Exception as e:
                logger.debug(f"Failed to parse metadata {meta_path}: {e}")
                continue

        self._documents = documents
        self._documents_by_id = {d.id: d for d in documents}

        return documents

    def get_doc(self, doc_id: str) -> Optional[Document]:
        """Get a document by ID."""
        if not self._documents_by_id:
            self.get_meta_items()
        return self._documents_by_id.get(doc_id)

    def download(self, doc: Document) -> bytes:
        """
        Download a document's content as a zip file.

        Creates a zip archive with the same structure as the cloud API.
        """
        doc_path = f"{XOCHITL_PATH}/{doc.id}"

        # List files in the document folder
        try:
            output = self._ssh_command(f"find '{doc_path}' -type f 2>/dev/null || true")
        except Exception:
            output = ""

        file_list = [f.strip() for f in output.strip().split("\n") if f.strip()]

        # Also include the .content file if it exists
        content_file = f"{XOCHITL_PATH}/{doc.id}.content"
        try:
            self._ssh_command(f"test -f '{content_file}' && echo exists")
            file_list.append(content_file)
        except Exception:
            pass

        # Create zip archive
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for remote_path in file_list:
                try:
                    content = self._scp_download(remote_path)
                    # Use relative path in zip
                    rel_path = os.path.basename(remote_path)
                    if "/" in remote_path.replace(f"{XOCHITL_PATH}/{doc.id}", ""):
                        # Preserve subdirectory structure
                        rel_path = remote_path.replace(f"{XOCHITL_PATH}/{doc.id}/", "")
                    zf.writestr(rel_path, content)
                except Exception as e:
                    logger.debug(f"Failed to download {remote_path}: {e}")
                    continue

        zip_buffer.seek(0)
        return zip_buffer.read()


def check_ssh_available(
    host: str = DEFAULT_SSH_HOST,
    user: str = DEFAULT_SSH_USER,
    port: int = DEFAULT_SSH_PORT,
) -> bool:
    """Check if SSH connection to reMarkable tablet is available."""
    client = SSHClient(host=host, user=user, port=port)
    return client.check_connection()


def create_ssh_client(
    host: Optional[str] = None,
    user: Optional[str] = None,
    port: Optional[int] = None,
) -> SSHClient:
    """
    Create an SSH client with settings from environment or defaults.

    Environment variables:
    - REMARKABLE_SSH_HOST: SSH host (default: 10.11.99.1)
    - REMARKABLE_SSH_USER: SSH user (default: root)
    - REMARKABLE_SSH_PORT: SSH port (default: 22)
    """
    return SSHClient(
        host=host or os.environ.get("REMARKABLE_SSH_HOST", DEFAULT_SSH_HOST),
        user=user or os.environ.get("REMARKABLE_SSH_USER", DEFAULT_SSH_USER),
        port=port or int(os.environ.get("REMARKABLE_SSH_PORT", str(DEFAULT_SSH_PORT))),
    )
