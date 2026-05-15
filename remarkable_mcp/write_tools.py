"""
Write tools for reMarkable tablet via SSH or USB web interface.

These tools are opt-in — disabled by default. Enable via:
- CLI flag: remarkable-mcp --ssh --write  (or --usb --write)
- Environment variable: REMARKABLE_ENABLE_WRITE=1

Available in SSH mode and USB web mode. Cloud mode returns a clear error.
- SSH mode: full write support (upload, mkdir, move, rename, delete)
- USB web mode: upload only (via POST /upload endpoint)

Inspired by PR #70 from @McSchnizzle. Implemented purely via SSH filesystem
operations and USB web HTTP endpoints — no external Go binary required.
"""

import json
import logging
import os
import time
import uuid
from typing import Optional

from mcp.types import ToolAnnotations

from remarkable_mcp.api import (
    get_item_path,
    get_items_by_id,
    get_rmapi,
)
from remarkable_mcp.responses import make_error, make_response
from remarkable_mcp.server import mcp
from remarkable_mcp.ssh import XOCHITL_PATH, SSHClient

logger = logging.getLogger(__name__)

# Tool annotations for write operations
UPLOAD_ANNOTATIONS = ToolAnnotations(
    title="Upload Document to reMarkable",
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)

MKDIR_ANNOTATIONS = ToolAnnotations(
    title="Create Folder on reMarkable",
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)

MOVE_ANNOTATIONS = ToolAnnotations(
    title="Move reMarkable Document",
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)

RENAME_ANNOTATIONS = ToolAnnotations(
    title="Rename reMarkable Document",
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)

DELETE_ANNOTATIONS = ToolAnnotations(
    title="Delete reMarkable Document",
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
)


def write_enabled() -> bool:
    """Check if write tools are enabled via environment variable."""
    return os.environ.get("REMARKABLE_ENABLE_WRITE", "").lower() in (
        "1",
        "true",
        "yes",
    )


def _require_write_transport() -> Optional[str]:
    """Return an error string if not in a write-capable mode, or None if OK."""
    ssh_enabled = os.environ.get("REMARKABLE_USE_SSH", "").lower() in (
        "1",
        "true",
        "yes",
    )
    usb_web_enabled = os.environ.get("REMARKABLE_USE_USB_WEB", "").lower() in (
        "1",
        "true",
        "yes",
    )
    if not ssh_enabled and not usb_web_enabled:
        return make_error(
            error_type="write_transport_required",
            message="Write operations require SSH or USB web mode",
            suggestion=(
                "Write tools work with SSH or USB web access to your tablet. "
                "Run with: remarkable-mcp --ssh --write  (or --usb --write)\n"
                "See: https://github.com/SamMorrowDrums/remarkable-mcp/blob/main/docs/ssh-setup.md"
            ),
        )
    return None


def _require_ssh_mode() -> Optional[str]:
    """Return an error string if not in SSH mode, or None if OK.

    Used for operations that only work via SSH (mkdir, move, rename, delete).
    """
    ssh_enabled = os.environ.get("REMARKABLE_USE_SSH", "").lower() in (
        "1",
        "true",
        "yes",
    )
    if not ssh_enabled:
        return make_error(
            error_type="ssh_required",
            message="This operation requires SSH mode",
            suggestion=(
                "mkdir, move, rename, and delete only work with SSH access. "
                "Upload works with both SSH and USB web mode.\n"
                "Run with: remarkable-mcp --ssh --write\n"
                "See: https://github.com/SamMorrowDrums/remarkable-mcp/blob/main/docs/ssh-setup.md"
            ),
        )
    return None


def _is_ssh_mode() -> bool:
    """Check if SSH transport is active."""
    return os.environ.get("REMARKABLE_USE_SSH", "").lower() in (
        "1",
        "true",
        "yes",
    )


def _is_usb_web_mode() -> bool:
    """Check if USB web transport is active."""
    return os.environ.get("REMARKABLE_USE_USB_WEB", "").lower() in (
        "1",
        "true",
        "yes",
    )


def _get_ssh_client():
    """Get the current API client (expected to be SSHClient in SSH mode)."""
    return get_rmapi()


def _write_metadata(ssh_client: SSHClient, doc_uuid: str, metadata: dict) -> None:
    """Write a metadata JSON file to the tablet."""
    content = json.dumps(metadata, indent=2)
    remote_path = f"{XOCHITL_PATH}/{doc_uuid}.metadata"
    ssh_client._ssh_command(f"cat > '{remote_path}' << 'REMARKABLE_EOF'\n{content}\nREMARKABLE_EOF")


def _write_content_file(ssh_client: SSHClient, doc_uuid: str, content_data: dict) -> None:
    """Write a .content JSON file to the tablet."""
    content = json.dumps(content_data, indent=2)
    remote_path = f"{XOCHITL_PATH}/{doc_uuid}.content"
    ssh_client._ssh_command(f"cat > '{remote_path}' << 'REMARKABLE_EOF'\n{content}\nREMARKABLE_EOF")


def _upload_file_bytes(ssh_client: SSHClient, local_path: str, remote_path: str) -> None:
    """Upload a local file to the tablet via SSH stdin pipe."""
    import subprocess

    ssh_args = [
        "ssh",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-p",
        str(ssh_client.port),
        f"{ssh_client.user}@{ssh_client.host}",
        f"cat > '{remote_path}'",
    ]

    if not ssh_client.password:
        ssh_args.insert(1, "-o")
        ssh_args.insert(2, "BatchMode=yes")
    else:
        ssh_args = ["sshpass", "-p", ssh_client.password] + ssh_args

    with open(local_path, "rb") as f:
        try:
            result = subprocess.run(
                ssh_args,
                stdin=f,
                capture_output=True,
                timeout=120,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Upload failed: {result.stderr.decode()}")
        except FileNotFoundError as e:
            if ssh_client.password and "sshpass" in str(e):
                raise RuntimeError(
                    "sshpass not found. Install it with: "
                    "apt install sshpass (Debian/Ubuntu), "
                    "brew install hudochenkov/sshpass/sshpass (macOS), "
                    "or set up SSH key authentication instead."
                )
            raise RuntimeError("SSH client not found. Install openssh-client.")
        except subprocess.TimeoutExpired:
            raise RuntimeError("SSH upload timed out after 120s")


def _restart_xochitl(ssh_client: SSHClient) -> None:
    """Restart the xochitl UI service on the tablet."""
    ssh_client._ssh_command("systemctl restart xochitl", timeout=15)


def _upload_via_usb_web(local_path: str) -> dict:
    """Upload a file to the tablet via USB web interface POST /upload.

    Returns dict with upload result info.
    """
    import requests

    from remarkable_mcp.usb_web import DEFAULT_USB_HOST

    host = os.environ.get("REMARKABLE_USB_HOST", DEFAULT_USB_HOST).rstrip("/")
    url = f"{host}/upload"

    with open(local_path, "rb") as f:
        filename = os.path.basename(local_path)
        files = {"file": (filename, f)}
        try:
            response = requests.post(url, files=files, timeout=120)
            response.raise_for_status()
            return {"status": response.status_code, "ok": True}
        except requests.Timeout:
            raise RuntimeError("USB web upload timed out. Check USB connection.")
        except requests.ConnectionError:
            raise RuntimeError(
                f"Cannot connect to USB web interface at {host}. "
                "Make sure USB cable is connected and web interface is enabled."
            )
        except requests.HTTPError as e:
            raise RuntimeError(f"USB web upload failed: {e}")


def _resolve_parent_id(parent_path: str, items_by_id: dict, collection: list) -> Optional[str]:
    """Resolve a folder path to its UUID.

    Args:
        parent_path: Path like "/" or "/Folder/Subfolder"
        items_by_id: Dict mapping item IDs to items
        collection: Full list of items

    Returns:
        The UUID of the folder, "" for root, or None if not found
    """
    if not parent_path or parent_path == "/":
        return ""

    # Normalize
    parent_path = parent_path.strip("/")

    for item in collection:
        if not item.is_folder:
            continue
        item_path = get_item_path(item, items_by_id).strip("/")
        if item_path.lower() == parent_path.lower():
            return item.ID

    return None


def _resolve_document(
    name_or_path: str, collection: list, items_by_id: dict, folders_only: bool = False
) -> Optional[object]:
    """Find a document or folder by name or path.

    Args:
        name_or_path: Document name or full path
        collection: Full list of items
        items_by_id: Dict mapping item IDs to items
        folders_only: If True, only match folders

    Returns:
        The matching item, or None
    """
    target = name_or_path.lower().strip("/")

    for item in collection:
        if folders_only and not item.is_folder:
            continue
        # Match by name
        if item.VissibleName.lower() == target:
            return item
        # Match by path
        item_path = get_item_path(item, items_by_id).strip("/")
        if item_path.lower() == target:
            return item

    return None


def register_write_tools():
    """Register all write tools with the MCP server."""

    @mcp.tool(annotations=UPLOAD_ANNOTATIONS)
    async def remarkable_upload(
        file_path: str,
        parent_folder: str = "/",
        document_name: Optional[str] = None,
    ) -> str:
        """
        <usecase>Upload a PDF or EPUB file to the reMarkable tablet.</usecase>
        <instructions>
        Uploads a local file to the tablet. Only PDF and EPUB formats
        are supported.

        Works in both SSH and USB web mode:
        - SSH: file is transferred via SSH, metadata is created, xochitl restarts
        - USB web: file is uploaded via POST /upload HTTP endpoint

        Requires --write flag.
        </instructions>
        <parameters>
        - file_path: Absolute path to the local PDF or EPUB file
        - parent_folder: Destination folder path on tablet (default: root "/").
          Note: parent_folder is only supported in SSH mode.
        - document_name: Display name on tablet (default: filename without
          extension). Note: document_name is only supported in SSH mode.
        </parameters>
        <examples>
        - remarkable_upload("/tmp/paper.pdf")
        - remarkable_upload("/tmp/book.epub", parent_folder="/Books")
        - remarkable_upload("/tmp/report.pdf", document_name="Q4 Report")
        </examples>
        """
        error = _require_write_transport()
        if error:
            return error

        try:
            # Validate file
            if not os.path.isfile(file_path):
                return make_error(
                    error_type="file_not_found",
                    message=f"File not found: '{file_path}'",
                    suggestion="Provide an absolute path to an existing PDF or EPUB file.",
                )

            ext = os.path.splitext(file_path)[1].lower().lstrip(".")
            if ext not in ("pdf", "epub"):
                return make_error(
                    error_type="unsupported_format",
                    message=f"Unsupported file format: '.{ext}'",
                    suggestion="Only PDF and EPUB files can be uploaded to reMarkable.",
                )

            # USB web mode: simple HTTP upload
            if _is_usb_web_mode():
                _upload_via_usb_web(file_path)

                name = document_name or os.path.splitext(os.path.basename(file_path))[0]

                # Clear cached documents
                client = get_rmapi()
                client._documents = []
                client._documents_by_id = {}

                result = {
                    "uploaded": True,
                    "name": name,
                    "format": ext,
                    "transport": "usb-web",
                }
                if parent_folder != "/":
                    result["note"] = (
                        "USB web upload places files at root. Use SSH mode for folder placement."
                    )
                return make_response(
                    result,
                    "Document uploaded via USB web interface. Use remarkable_browse() to verify.",
                )

            # SSH mode: full upload with metadata
            ssh_client = _get_ssh_client()
            collection = ssh_client.get_meta_items()
            items_by_id = get_items_by_id(collection)

            # Resolve parent folder
            parent_id = _resolve_parent_id(parent_folder, items_by_id, collection)
            if parent_id is None:
                folders = [get_item_path(i, items_by_id) for i in collection if i.is_folder]
                return make_error(
                    error_type="folder_not_found",
                    message=f"Folder not found: '{parent_folder}'",
                    suggestion="Use remarkable_browse('/') to see available folders.",
                    did_you_mean=folders[:5] if folders else None,
                )

            # Generate UUID and set name
            doc_uuid = str(uuid.uuid4())
            name = document_name or os.path.splitext(os.path.basename(file_path))[0]
            timestamp_ms = str(int(time.time() * 1000))

            # Upload the file
            remote_file = f"{XOCHITL_PATH}/{doc_uuid}.{ext}"
            _upload_file_bytes(ssh_client, file_path, remote_file)

            # Create metadata
            metadata = {
                "visibleName": name,
                "type": "DocumentType",
                "parent": parent_id,
                "deleted": False,
                "pinned": False,
                "lastModified": timestamp_ms,
                "metadatamodified": True,
                "modified": True,
                "synced": False,
                "version": 0,
            }
            _write_metadata(ssh_client, doc_uuid, metadata)

            # Create content file
            content_data = {
                "fileType": ext,
            }
            _write_content_file(ssh_client, doc_uuid, content_data)

            # Create the document directory (required by xochitl)
            ssh_client._ssh_command(f"mkdir -p '{XOCHITL_PATH}/{doc_uuid}'")

            # Restart xochitl to pick up changes
            _restart_xochitl(ssh_client)

            # Clear cached documents so next read picks up the new doc
            ssh_client._documents = []
            ssh_client._documents_by_id = {}

            return make_response(
                {
                    "uploaded": True,
                    "name": name,
                    "uuid": doc_uuid,
                    "format": ext,
                    "parent_folder": parent_folder,
                    "remote_path": remote_file,
                    "transport": "ssh",
                },
                "Document uploaded successfully. Use remarkable_browse() to verify it appears.",
            )

        except Exception as e:
            transport = "USB web" if _is_usb_web_mode() else "SSH"
            return make_error(
                error_type="upload_failed",
                message=f"Upload failed: {e}",
                suggestion=f"Check {transport} connection and try again.",
            )

    @mcp.tool(annotations=MKDIR_ANNOTATIONS)
    async def remarkable_mkdir(
        folder_name: str,
        parent: str = "/",
    ) -> str:
        """
        <usecase>Create a new folder on the reMarkable tablet.</usecase>
        <instructions>
        Creates a folder in the tablet's document hierarchy. The folder appears
        in the tablet UI after xochitl restarts.

        Requires SSH mode and --write flag. Not available in USB web mode.
        </instructions>
        <parameters>
        - folder_name: Name of the new folder
        - parent: Parent folder path (default: root "/")
        </parameters>
        <examples>
        - remarkable_mkdir("Projects")
        - remarkable_mkdir("2024", parent="/Archive")
        </examples>
        """
        error = _require_ssh_mode()
        if error:
            return error

        try:
            ssh_client = _get_ssh_client()
            collection = ssh_client.get_meta_items()
            items_by_id = get_items_by_id(collection)

            # Resolve parent
            parent_id = _resolve_parent_id(parent, items_by_id, collection)
            if parent_id is None:
                return make_error(
                    error_type="folder_not_found",
                    message=f"Parent folder not found: '{parent}'",
                    suggestion="Use remarkable_browse('/') to see available folders.",
                )

            # Generate UUID
            doc_uuid = str(uuid.uuid4())
            timestamp_ms = str(int(time.time() * 1000))

            # Create metadata for folder
            metadata = {
                "visibleName": folder_name,
                "type": "CollectionType",
                "parent": parent_id,
                "deleted": False,
                "pinned": False,
                "lastModified": timestamp_ms,
                "metadatamodified": True,
                "modified": True,
                "synced": False,
                "version": 0,
            }
            _write_metadata(ssh_client, doc_uuid, metadata)

            # Restart xochitl
            _restart_xochitl(ssh_client)

            # Clear cache
            ssh_client._documents = []
            ssh_client._documents_by_id = {}

            return make_response(
                {
                    "created": True,
                    "folder_name": folder_name,
                    "uuid": doc_uuid,
                    "parent": parent,
                },
                "Folder created. Use remarkable_browse() to verify.",
            )

        except Exception as e:
            return make_error(
                error_type="mkdir_failed",
                message=f"Failed to create folder: {e}",
                suggestion="Check SSH connection and try again.",
            )

    @mcp.tool(annotations=MOVE_ANNOTATIONS)
    async def remarkable_move(
        document: str,
        dest_folder: str,
    ) -> str:
        """
        <usecase>Move a document or folder to a different location.</usecase>
        <instructions>
        Moves a document or folder by updating its parent reference in the metadata.
        Find the document name with remarkable_browse() first.

        Requires SSH mode and --write flag. Not available in USB web mode.
        </instructions>
        <parameters>
        - document: Name or path of the document/folder to move
        - dest_folder: Destination folder path (use "/" for root)
        </parameters>
        <examples>
        - remarkable_move("Meeting Notes", "/Archive")
        - remarkable_move("Old Project", "/Archive/2023")
        </examples>
        """
        error = _require_ssh_mode()
        if error:
            return error

        try:
            ssh_client = _get_ssh_client()
            collection = ssh_client.get_meta_items()
            items_by_id = get_items_by_id(collection)

            # Find the document
            target = _resolve_document(document, collection, items_by_id)
            if not target:
                from remarkable_mcp.extract import find_similar_documents

                similar = find_similar_documents(document, collection)
                return make_error(
                    error_type="document_not_found",
                    message=f"Document not found: '{document}'",
                    suggestion="Use remarkable_browse() to find the correct name.",
                    did_you_mean=similar if similar else None,
                )

            # Find the destination folder
            dest_id = _resolve_parent_id(dest_folder, items_by_id, collection)
            if dest_id is None:
                return make_error(
                    error_type="folder_not_found",
                    message=f"Destination folder not found: '{dest_folder}'",
                    suggestion="Use remarkable_browse('/') to see available folders.",
                )

            # Prevent moving a folder into itself or a descendant
            if target.is_folder:
                if dest_id == target.ID:
                    return make_error(
                        error_type="invalid_move",
                        message="Cannot move a folder into itself",
                        suggestion="Choose a different destination folder.",
                    )
                # Walk up from dest to check for cycles
                check_id = dest_id
                while check_id and check_id in items_by_id:
                    if check_id == target.ID:
                        return make_error(
                            error_type="invalid_move",
                            message="Cannot move a folder into one of its subfolders",
                            suggestion=(
                                "Choose a destination that is not inside the folder being moved."
                            ),
                        )
                    parent_item = items_by_id[check_id]
                    check_id = parent_item.Parent if hasattr(parent_item, "Parent") else ""

            # Read existing metadata
            meta_content = ssh_client._scp_download(f"{XOCHITL_PATH}/{target.ID}.metadata")
            metadata = json.loads(meta_content.decode("utf-8"))

            old_path = get_item_path(target, items_by_id)

            # Update parent
            metadata["parent"] = dest_id
            metadata["lastModified"] = str(int(time.time() * 1000))
            metadata["metadatamodified"] = True
            _write_metadata(ssh_client, target.ID, metadata)

            # Restart xochitl
            _restart_xochitl(ssh_client)

            # Clear cache
            ssh_client._documents = []
            ssh_client._documents_by_id = {}

            return make_response(
                {
                    "moved": True,
                    "name": target.VissibleName,
                    "from": old_path,
                    "to": dest_folder,
                },
                "Document moved. Use remarkable_browse() to verify.",
            )

        except Exception as e:
            return make_error(
                error_type="move_failed",
                message=f"Failed to move document: {e}",
                suggestion="Check SSH connection and try again.",
            )

    @mcp.tool(annotations=RENAME_ANNOTATIONS)
    async def remarkable_rename(
        document: str,
        new_name: str,
    ) -> str:
        """
        <usecase>Rename a document or folder on the reMarkable tablet.</usecase>
        <instructions>
        Changes the display name of a document or folder by updating its metadata.
        Find the document name with remarkable_browse() first.

        Requires SSH mode and --write flag. Not available in USB web mode.
        </instructions>
        <parameters>
        - document: Current name or path of the document/folder
        - new_name: New display name
        </parameters>
        <examples>
        - remarkable_rename("Untitled", "Meeting Notes 2024-01-15")
        - remarkable_rename("/Work/Draft", "Final Report")
        </examples>
        """
        error = _require_ssh_mode()
        if error:
            return error

        try:
            ssh_client = _get_ssh_client()
            collection = ssh_client.get_meta_items()
            items_by_id = get_items_by_id(collection)

            # Find the document
            target = _resolve_document(document, collection, items_by_id)
            if not target:
                from remarkable_mcp.extract import find_similar_documents

                similar = find_similar_documents(document, collection)
                return make_error(
                    error_type="document_not_found",
                    message=f"Document not found: '{document}'",
                    suggestion="Use remarkable_browse() to find the correct name.",
                    did_you_mean=similar if similar else None,
                )

            # Read existing metadata
            meta_content = ssh_client._scp_download(f"{XOCHITL_PATH}/{target.ID}.metadata")
            metadata = json.loads(meta_content.decode("utf-8"))

            old_name = metadata.get("visibleName", target.VissibleName)

            # Update name
            metadata["visibleName"] = new_name
            metadata["lastModified"] = str(int(time.time() * 1000))
            metadata["metadatamodified"] = True
            _write_metadata(ssh_client, target.ID, metadata)

            # Restart xochitl
            _restart_xochitl(ssh_client)

            # Clear cache
            ssh_client._documents = []
            ssh_client._documents_by_id = {}

            return make_response(
                {
                    "renamed": True,
                    "old_name": old_name,
                    "new_name": new_name,
                },
                "Document renamed. Use remarkable_browse() to verify.",
            )

        except Exception as e:
            return make_error(
                error_type="rename_failed",
                message=f"Failed to rename document: {e}",
                suggestion="Check SSH connection and try again.",
            )

    @mcp.tool(annotations=DELETE_ANNOTATIONS)
    async def remarkable_delete(
        document: str,
        confirm: bool = False,
    ) -> str:
        """
        <usecase>Delete a document or folder on the reMarkable tablet.</usecase>
        <instructions>
        DESTRUCTIVE operation — marks a document as deleted in its metadata.
        The document won't appear in the tablet UI after restart.

        Safety: requires confirm=True to actually delete. Without it, returns
        a dry-run preview showing what would be deleted.

        Requires SSH mode and --write flag. Not available in USB web mode.
        </instructions>
        <parameters>
        - document: Name or path of the document/folder to delete
        - confirm: Must be True to actually delete (default: False for dry-run)
        </parameters>
        <examples>
        - remarkable_delete("Old Notes")  # Dry-run preview
        - remarkable_delete("Old Notes", confirm=True)  # Actually delete
        </examples>
        """
        error = _require_ssh_mode()
        if error:
            return error

        try:
            ssh_client = _get_ssh_client()
            collection = ssh_client.get_meta_items()
            items_by_id = get_items_by_id(collection)

            # Find the document
            target = _resolve_document(document, collection, items_by_id)
            if not target:
                from remarkable_mcp.extract import find_similar_documents

                similar = find_similar_documents(document, collection)
                return make_error(
                    error_type="document_not_found",
                    message=f"Document not found: '{document}'",
                    suggestion="Use remarkable_browse() to find the correct name.",
                    did_you_mean=similar if similar else None,
                )

            doc_path = get_item_path(target, items_by_id)

            if not confirm:
                # Dry-run: show what would be deleted
                return make_response(
                    {
                        "dry_run": True,
                        "would_delete": {
                            "name": target.VissibleName,
                            "path": doc_path,
                            "type": "folder" if target.is_folder else "document",
                            "uuid": target.ID,
                        },
                        "confirm_required": True,
                    },
                    "This is a dry-run preview. To actually delete, call "
                    f'remarkable_delete("{document}", confirm=True).',
                )

            # Read existing metadata
            meta_content = ssh_client._scp_download(f"{XOCHITL_PATH}/{target.ID}.metadata")
            metadata = json.loads(meta_content.decode("utf-8"))

            # Mark as deleted
            metadata["deleted"] = True
            metadata["lastModified"] = str(int(time.time() * 1000))
            metadata["metadatamodified"] = True
            _write_metadata(ssh_client, target.ID, metadata)

            # Restart xochitl
            _restart_xochitl(ssh_client)

            # Clear cache
            ssh_client._documents = []
            ssh_client._documents_by_id = {}

            return make_response(
                {
                    "deleted": True,
                    "name": target.VissibleName,
                    "path": doc_path,
                    "type": "folder" if target.is_folder else "document",
                },
                "Document deleted. It will no longer appear on the tablet.",
            )

        except Exception as e:
            return make_error(
                error_type="delete_failed",
                message=f"Failed to delete document: {e}",
                suggestion="Check SSH connection and try again.",
            )
