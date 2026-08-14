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
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
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
    synced: bool = True  # False means cloud-archived (not on device)
    last_modified: Optional[datetime] = None
    size: int = 0
    files: List[Dict[str, Any]] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    # SSH-specific: local path to the document folder
    local_path: Optional[str] = None

    @property
    def is_folder(self) -> bool:
        return self.doc_type == "CollectionType"

    @property
    def is_cloud_archived(self) -> bool:
        """True if document is in the trash.

        Previously this also checked ``not self.synced``, but the metadata
        ``synced`` field means "local changes have been pushed to the cloud"
        — not "the document is physically present on this device".  Documents
        added via the Chrome extension or cloud sync arrive with
        ``synced: false`` even though their content is on the device.

        See: https://github.com/SamMorrowDrums/remarkable-mcp/issues/65
        """
        return self.parent == "trash"

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
        password: Optional[str] = None,
        key_path: Optional[str] = None,
    ):
        self.host = host
        self.user = user
        self.port = port
        self.password = password
        self.key_path = os.path.expanduser(key_path) if key_path else None
        self._documents: List[Document] = []
        self._documents_by_id: Dict[str, Document] = {}
        self._metadata_lock = threading.RLock()
        self._file_type_lock = threading.RLock()
        self._io_lock = threading.RLock()
        self._file_type_cache: Optional[dict[str, Optional[str]]] = None
        self._trace_lock = threading.Lock()
        self._trace_sequence = 0
        self._trace_active = 0

    def _trace_start(self, operation: str) -> tuple[int, float]:
        if os.environ.get("REMARKABLE_SSH_TRACE", "").lower() not in ("1", "true", "yes"):
            return 0, 0.0
        with self._trace_lock:
            self._trace_sequence += 1
            self._trace_active += 1
            sequence = self._trace_sequence
            active = self._trace_active
        logger.warning("SSH trace start id=%d operation=%s active=%d", sequence, operation, active)
        return sequence, time.monotonic()

    def _trace_finish(
        self,
        trace: tuple[int, float],
        operation: str,
        returncode: Optional[int],
        output_bytes: int,
    ) -> None:
        sequence, started = trace
        if not sequence:
            return
        with self._trace_lock:
            self._trace_active -= 1
            active = self._trace_active
        logger.warning(
            "SSH trace finish id=%d operation=%s active=%d duration=%.3fs returncode=%s bytes=%d",
            sequence,
            operation,
            active,
            time.monotonic() - started,
            returncode,
            output_bytes,
        )

    @staticmethod
    def _command_operation(command: str) -> str:
        if "*.metadata" in command:
            return "metadata-preload"
        if "*.content" in command:
            return "file-type-preload"
        if command.startswith("find "):
            return "document-file-list"
        if command.startswith("test -f "):
            return "file-exists"
        if "systemctl restart xochitl" in command:
            return "xochitl-restart"
        if "systemctl is-active xochitl" in command:
            return "xochitl-readiness"
        if command == "echo ok":
            return "connection-check"
        return "command"

    def _auth_ssh_options(self) -> List[str]:
        """SSH options for key-based auth.

        When a password is set, auth is handled by wrapping the command with
        ``sshpass`` (see callers), so no options are added here.

        Otherwise we use ``BatchMode=yes`` (never prompt) and, if an explicit
        key is configured, pin it with ``-i`` plus ``IdentitiesOnly=yes``. The
        latter makes ssh use only that on-disk key and ignore any ssh-agent
        (e.g. 1Password), so signing never blocks on interactive approval in a
        headless MCP server.
        """
        if self.password:
            return []
        opts = ["-o", "BatchMode=yes"]
        if self.key_path:
            opts += ["-i", self.key_path, "-o", "IdentitiesOnly=yes"]
        return opts

    def _ssh_command(self, command: str, timeout: int = 30) -> str:
        """Execute a command on the tablet via SSH."""
        with self._io_lock:
            return self._ssh_command_unlocked(command, timeout)

    def _ssh_command_unlocked(self, command: str, timeout: int) -> str:
        operation = self._command_operation(command)
        trace = self._trace_start(operation)
        returncode = None
        output_bytes = 0
        ssh_args = [
            "ssh",
            *self._auth_ssh_options(),
            "-o",
            "ConnectTimeout=5",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-p",
            str(self.port),
            f"{self.user}@{self.host}",
            command,
        ]

        # Use sshpass for password authentication
        if self.password:
            ssh_args = ["sshpass", "-p", self.password] + ssh_args

        try:
            result = subprocess.run(
                ssh_args,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            returncode = result.returncode
            output_bytes = len(result.stdout.encode()) + len(result.stderr.encode())
            if result.returncode != 0:
                raise RuntimeError(f"SSH command failed: {result.stderr}")
            return result.stdout
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"SSH command timed out after {timeout}s")
        except FileNotFoundError as e:
            if self.password and "sshpass" in str(e):
                raise RuntimeError(
                    "sshpass not found. Install it with: "
                    "apt install sshpass (Debian/Ubuntu), "
                    "brew install hudochenkov/sshpass/sshpass (macOS), "
                    "or set up SSH key authentication instead."
                )
            raise RuntimeError(
                "SSH client ('ssh') not found on PATH. Install OpenSSH: "
                "Windows (Settings → Optional Features → OpenSSH Client, or it is "
                "built in on Windows 10+), macOS (preinstalled), "
                "Linux (apt install openssh-client / equivalent)."
            )
        finally:
            self._trace_finish(trace, operation, returncode, output_bytes)

    def _scp_download(self, remote_path: str, timeout: int = 60) -> bytes:
        """Download a file from the tablet via SSH cat (more reliable than SCP)."""
        with self._io_lock:
            return self._scp_download_unlocked(remote_path, timeout)

    def _scp_download_unlocked(self, remote_path: str, timeout: int) -> bytes:
        operation = f"download-{remote_path.rsplit('.', 1)[-1]}"
        trace = self._trace_start(operation)
        returncode = None
        output_bytes = 0
        # Use SSH + cat instead of SCP for binary file transfer
        # This avoids issues with /dev/stdout on various platforms
        ssh_args = [
            "ssh",
            *self._auth_ssh_options(),
            "-o",
            "ConnectTimeout=5",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-p",
            str(self.port),
            f"{self.user}@{self.host}",
            f"cat '{remote_path}'",
        ]

        # Use sshpass for password authentication
        if self.password:
            ssh_args = ["sshpass", "-p", self.password] + ssh_args

        try:
            result = subprocess.run(
                ssh_args,
                capture_output=True,
                timeout=timeout,
            )
            returncode = result.returncode
            output_bytes = len(result.stdout) + len(result.stderr)
            if result.returncode != 0:
                raise RuntimeError(f"SSH cat failed: {result.stderr.decode()}")
            return result.stdout
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"SSH cat timed out after {timeout}s")
        finally:
            self._trace_finish(trace, operation, returncode, output_bytes)

    def _upload_file(self, local_path: str, remote_path: str, timeout: int = 120) -> None:
        """Upload one file while participating in the per-client SSH I/O queue."""
        with self._io_lock:
            operation = f"upload-{remote_path.rsplit('.', 1)[-1]}"
            trace = self._trace_start(operation)
            returncode = None
            output_bytes = 0
            ssh_args = [
                "ssh",
                *self._auth_ssh_options(),
                "-o",
                "ConnectTimeout=5",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-p",
                str(self.port),
                f"{self.user}@{self.host}",
                f"cat > '{remote_path}'",
            ]
            if self.password:
                ssh_args = ["sshpass", "-p", self.password] + ssh_args

            try:
                with open(local_path, "rb") as source:
                    result = subprocess.run(
                        ssh_args,
                        stdin=source,
                        capture_output=True,
                        timeout=timeout,
                    )
                returncode = result.returncode
                output_bytes = len(result.stdout) + len(result.stderr)
                if result.returncode != 0:
                    raise RuntimeError(f"Upload failed: {result.stderr.decode()}")
            except FileNotFoundError as e:
                if self.password and "sshpass" in str(e):
                    raise RuntimeError(
                        "sshpass not found. Install it with: "
                        "apt install sshpass (Debian/Ubuntu), "
                        "brew install hudochenkov/sshpass/sshpass (macOS), "
                        "or set up SSH key authentication instead."
                    )
                raise RuntimeError("SSH client not found. Install openssh-client.")
            except subprocess.TimeoutExpired:
                raise RuntimeError(f"SSH upload timed out after {timeout}s")
            finally:
                self._trace_finish(trace, operation, returncode, output_bytes)

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
        with self._metadata_lock:
            # Re-check inside the lock: startup resource loading and the first
            # status/tool request can arrive together on separate worker threads.
            # Exactly one of them should scan the tablet; the other reuses its cache.
            if self._documents and limit is None:
                return self._documents
            if self._documents and limit is not None and len(self._documents) >= limit:
                return self._documents[:limit]

            try:
                output = self._ssh_command(
                    f"for f in {XOCHITL_PATH}/*.metadata; do "
                    f'echo "===FILE===$(basename $f .metadata)"; cat "$f" 2>/dev/null; '
                    f"done",
                    timeout=60,
                )
            except Exception as e:
                raise RuntimeError(f"Failed to read metadata: {e}")

            documents = []
            current_id = None
            current_content = []

            for line in output.split("\n"):
                if line.startswith("===FILE==="):
                    if current_id and current_content:
                        self._parse_and_add_document(
                            current_id, "\n".join(current_content), documents, limit
                        )
                        if limit is not None and len(documents) >= limit:
                            break
                    current_id = line.replace("===FILE===", "").strip()
                    current_content = []
                else:
                    current_content.append(line)

            if current_id and current_content:
                if limit is None or len(documents) < limit:
                    self._parse_and_add_document(
                        current_id, "\n".join(current_content), documents, limit
                    )

            self._documents = documents
            self._documents_by_id = {d.id: d for d in documents}

            logger.info(f"Loaded {len(documents)} documents via SSH")
            return documents

    def _parse_and_add_document(
        self,
        doc_id: str,
        content: str,
        documents: List[Document],
        limit: Optional[int],
    ) -> None:
        """Parse metadata JSON and add document to list."""
        if limit is not None and len(documents) >= limit:
            return

        try:
            metadata = json.loads(content.strip())

            # Skip deleted documents
            if metadata.get("deleted", False):
                return

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
                synced=metadata.get("synced", True),
                last_modified=last_modified,
                size=0,
                tags=metadata.get("tags", []),
                local_path=f"{XOCHITL_PATH}/{doc_id}",
            )

            documents.append(doc)

        except Exception as e:
            logger.debug(f"Failed to parse metadata for {doc_id}: {e}")

    def get_doc(self, doc_id: str) -> Optional[Document]:
        """Get a document by ID."""
        if not self._documents_by_id:
            self.get_meta_items()
        return self._documents_by_id.get(doc_id)

    def download(self, doc: Document) -> bytes:
        """
        Download a document's content as a zip file.

        Creates a zip archive with the same structure as the cloud API.
        Includes the document folder contents, .content metadata, and
        any associated PDF/EPUB file for merged rendering support.
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

        # Include the associated PDF or EPUB file for merged rendering
        for ext in ("pdf", "epub"):
            raw_file = f"{XOCHITL_PATH}/{doc.id}.{ext}"
            try:
                self._ssh_command(f"test -f '{raw_file}' && echo exists")
                file_list.append(raw_file)
            except Exception:
                pass

        # Create zip archive. This archive is an internal transport container that
        # is extracted in-memory by the caller and never written to disk, so we use
        # ZIP_STORED (no compression) to avoid pointless compress/decompress CPU cost.
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_STORED) as zf:
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

    def download_raw_file(self, doc: Document, extension: str) -> Optional[bytes]:
        """
        Download a raw file (PDF or EPUB) for a document.

        Args:
            doc: The document to download
            extension: File extension without dot (e.g., 'pdf', 'epub')

        Returns:
            Raw file bytes, or None if file doesn't exist
        """
        file_path = f"{XOCHITL_PATH}/{doc.id}.{extension}"

        try:
            # Check if file exists first
            self._ssh_command(f"test -f '{file_path}'", timeout=5)
            # Download the file
            return self._scp_download(file_path, timeout=120)
        except Exception as e:
            logger.debug(f"Raw file not found: {file_path}: {e}")
            return None

    def get_file_type(self, doc: Document) -> Optional[str]:
        """
        Get the file type (pdf, epub, etc.) for a document.

        Returns the extension without dot, or None if not a file-based document.
        """
        with self._file_type_lock:
            # If the background all-file preload owns this lock, wait for it to
            # finish instead of starting another SSH connection in parallel.
            if self._file_type_cache is not None and doc.id in self._file_type_cache:
                return self._file_type_cache[doc.id]

            content_file = f"{XOCHITL_PATH}/{doc.id}.content"
            try:
                content = self._scp_download(content_file, timeout=10)
                data = json.loads(content.decode("utf-8"))
                return data.get("fileType")
            except Exception:
                return None

    def get_all_file_types(self) -> dict[str, Optional[str]]:
        """
        Get file types for all documents in a single SSH command.

        Returns a dict mapping document ID to file type (pdf, epub, or None).
        Much more efficient than calling get_file_type() for each document.
        """
        with self._file_type_lock:
            if self._file_type_cache is not None:
                return self._file_type_cache

            cache: dict[str, Optional[str]] = {}
            try:
                output = self._ssh_command(
                    f"for f in {XOCHITL_PATH}/*.content; do "
                    f'echo "===FILE===$(basename $f .content)"; cat "$f" 2>/dev/null; '
                    f"done",
                    timeout=60,
                )

                current_id = None
                current_content = []

                for line in output.split("\n"):
                    if line.startswith("===FILE==="):
                        if current_id and current_content:
                            try:
                                data = json.loads("\n".join(current_content))
                                cache[current_id] = data.get("fileType")
                            except json.JSONDecodeError:
                                cache[current_id] = None

                        current_id = line.replace("===FILE===", "").strip()
                        current_content = []
                    else:
                        current_content.append(line)

                if current_id and current_content:
                    try:
                        data = json.loads("\n".join(current_content))
                        cache[current_id] = data.get("fileType")
                    except json.JSONDecodeError:
                        cache[current_id] = None

            except Exception as e:
                logger.warning(f"Failed to batch-load file types: {e}")

            self._file_type_cache = cache
            return cache


def check_ssh_available(
    host: str = DEFAULT_SSH_HOST,
    user: str = DEFAULT_SSH_USER,
    port: int = DEFAULT_SSH_PORT,
) -> bool:
    """Check if SSH connection to reMarkable tablet is available."""
    client = create_ssh_client(host=host, user=user, port=port)
    return client.check_connection()


def create_ssh_client(
    host: Optional[str] = None,
    user: Optional[str] = None,
    port: Optional[int] = None,
    password: Optional[str] = None,
    key_path: Optional[str] = None,
) -> SSHClient:
    """
    Create an SSH client with settings from environment or defaults.

    Environment variables:
    - REMARKABLE_SSH_HOST: SSH host (default: 10.11.99.1)
    - REMARKABLE_SSH_USER: SSH user (default: root)
    - REMARKABLE_SSH_PORT: SSH port (default: 22)
    - REMARKABLE_SSH_PASSWORD: SSH password (optional, requires sshpass)
    - REMARKABLE_SSH_KEY: path to a private key for key-based auth (optional).
      When set, ssh uses only this identity (IdentitiesOnly) and ignores any
      ssh-agent, avoiding hangs with interactive agents such as 1Password in a
      headless server.
    """
    return SSHClient(
        host=host or os.environ.get("REMARKABLE_SSH_HOST", DEFAULT_SSH_HOST),
        user=user or os.environ.get("REMARKABLE_SSH_USER", DEFAULT_SSH_USER),
        port=port or int(os.environ.get("REMARKABLE_SSH_PORT", str(DEFAULT_SSH_PORT))),
        password=password or os.environ.get("REMARKABLE_SSH_PASSWORD"),
        key_path=key_path or os.environ.get("REMARKABLE_SSH_KEY"),
    )
