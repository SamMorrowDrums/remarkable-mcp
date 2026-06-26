"""
Write tools for reMarkable tablet via cloud, SSH, or USB web interface.

These tools are enabled by default. Disable them (expose a read-only server) via:
- CLI flag: remarkable-mcp --read-only  (works in any transport)
- Environment variable: REMARKABLE_READ_ONLY=1

The legacy --write flag and REMARKABLE_ENABLE_WRITE variable are still accepted
for backward compatibility but are now no-ops (write is the default). --write and
--read-only are mutually exclusive.

Write support by transport:
- Cloud mode: upload, mkdir, move, rename, delete (-> trash)
- SSH mode:   upload, mkdir, move, rename, delete
- USB web mode: upload only (via POST /upload endpoint)

Inspired by PR #70 from @McSchnizzle. SSH operations use the tablet filesystem,
USB uses the web upload endpoint, and cloud uses the sync v3/v4 blob protocol —
no external Go binary required.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Optional

from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from remarkable_mcp.api import (
    get_item_path,
    get_items_by_id,
    get_rmapi,
)
from remarkable_mcp.capabilities import client_supports_elicitation
from remarkable_mcp.responses import make_error, make_response
from remarkable_mcp.ssh import XOCHITL_PATH, SSHClient

logger = logging.getLogger(__name__)

# Tool annotations for write operations
WRITE_ANNOTATIONS = ToolAnnotations(readOnlyHint=False)
UPLOAD_ANNOTATIONS = WRITE_ANNOTATIONS
MKDIR_ANNOTATIONS = WRITE_ANNOTATIONS
MOVE_ANNOTATIONS = WRITE_ANNOTATIONS
RENAME_ANNOTATIONS = WRITE_ANNOTATIONS
DELETE_ANNOTATIONS = ToolAnnotations(readOnlyHint=False, destructiveHint=True)


def read_only_enabled() -> bool:
    """Check if read-only mode is enabled via the --read-only flag / env var."""
    return os.environ.get("REMARKABLE_READ_ONLY", "").lower() in (
        "1",
        "true",
        "yes",
    )


def write_enabled() -> bool:
    """Write tools are enabled by default; disabled only in read-only mode."""
    return not read_only_enabled()


def _require_write_transport() -> Optional[str]:
    """Return an error string if writes are disabled, else None.

    Upload works in all three transports (cloud, SSH, USB web). Write tools are
    not registered when REMARKABLE_READ_ONLY is set, so this is mostly a
    defensive check that returns a clear error if writes are somehow disabled.
    """
    if not write_enabled():
        return make_error(
            error_type="write_disabled",
            message="Write operations are disabled (read-only mode)",
            suggestion=(
                "Write tools are enabled by default. Remove the --read-only flag "
                "(or unset REMARKABLE_READ_ONLY) and restart the server to enable "
                "upload, mkdir, move, rename, and delete."
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


def _is_cloud_mode() -> bool:
    """Check if cloud transport is active (the default when SSH/USB are off)."""
    return not _is_ssh_mode() and not _is_usb_web_mode()


def _require_managed_write_mode() -> Optional[str]:
    """Return an error string unless in a mode that supports folder operations.

    mkdir, move, rename and delete work in cloud and SSH modes. USB web
    mode only supports uploads, so it is rejected here with guidance.
    """
    if _is_cloud_mode() or _is_ssh_mode():
        return None
    return make_error(
        error_type="unsupported_in_usb_web",
        message="This operation isn't available in USB web mode",
        suggestion=(
            "mkdir, move, rename and delete work in cloud mode (the "
            "default) and SSH mode. Upload works in all three modes.\n"
            "Run with: remarkable-mcp  (cloud)  or  remarkable-mcp --ssh"
        ),
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


# Tunables for the post-restart readiness wait (see _restart_xochitl). All are
# overridable by env var so the wait can be tightened or relaxed per device
# without code changes.
_RESTART_READY_TIMEOUT = float(os.environ.get("REMARKABLE_RESTART_TIMEOUT", "30"))
_RESTART_POLL_INTERVAL = float(os.environ.get("REMARKABLE_RESTART_POLL_INTERVAL", "1"))
_RESTART_SETTLE_SECONDS = float(os.environ.get("REMARKABLE_RESTART_SETTLE", "3"))


def _restart_xochitl(ssh_client: SSHClient, wait_ready: bool = True) -> None:
    """Restart the xochitl UI service so it picks up metadata changes.

    Restarting forces xochitl to rescan the entire document store. On the
    resource-constrained tablet that briefly starves the SSH daemon, so a
    follow-up write issued immediately can land mid-rescan and have its
    connection refused or torn down — the "race" seen during bulk operations.

    To avoid that, after issuing the restart we poll ``systemctl is-active``
    until xochitl reports ``active`` again, then add a short settle delay so the
    rescan-driven CPU/IO spike has subsided before we return. The call therefore
    only completes once the tablet is ready for the next operation. Pass
    ``wait_ready=False`` to restore the old fire-and-return behaviour.
    """
    ssh_client._ssh_command("systemctl restart xochitl", timeout=30)
    if not wait_ready:
        return

    deadline = time.monotonic() + _RESTART_READY_TIMEOUT
    while time.monotonic() < deadline:
        try:
            state = ssh_client._ssh_command("systemctl is-active xochitl", timeout=10).strip()
        except RuntimeError:
            # While the unit is still activating, `is-active` exits non-zero
            # (surfaced as RuntimeError) and the SSH layer itself may be briefly
            # refusing connections. Either way it isn't ready yet — keep polling.
            state = "activating"
        if state == "active":
            break
        time.sleep(_RESTART_POLL_INTERVAL)

    # `systemctl restart` returns once the process is respawned, but the store
    # rescan continues afterwards; settle briefly so the next reconnect lands
    # after the spike rather than during it.
    if _RESTART_SETTLE_SECONDS > 0:
        time.sleep(_RESTART_SETTLE_SECONDS)


def _defer_restart_enabled() -> bool:
    """Whether xochitl restarts should be deferred by default (env override).

    Set REMARKABLE_DEFER_RESTART=1 to make every write skip its own restart, so
    a batch of writes pays the restart cost (and its race surface) exactly once
    via remarkable_refresh instead of once per write.
    """
    return os.environ.get("REMARKABLE_DEFER_RESTART", "").lower() in (
        "1",
        "true",
        "yes",
    )


def _maybe_restart_xochitl(ssh_client: SSHClient, defer_restart: bool = False) -> bool:
    """Restart xochitl unless deferral is requested (per-call arg or env).

    Returns True if a restart was performed, False if it was deferred. When
    deferred the on-disk metadata has changed but xochitl has not yet rescanned,
    so the caller must run remarkable_refresh once the batch is complete for the
    changes to become visible in the UI.
    """
    if defer_restart or _defer_restart_enabled():
        return False
    _restart_xochitl(ssh_client)
    return True


def _write_result_message(
    restarted: bool,
    done: str,
    verify: str = "Use remarkable_browse() to verify.",
) -> str:
    """Build a write tool's success message, accounting for restart deferral.

    ``done`` is the past-tense summary of what happened (e.g. "Document
    moved."). When the restart ran, ``verify`` tells the user how to confirm the
    change; when it was deferred, the user is instead reminded to call
    remarkable_refresh once their batch is complete.
    """
    if restarted:
        return f"{done} {verify}"
    return f"{done} Restart deferred — call remarkable_refresh() once your batch is done."


def _page_ids_from_content(content_data: dict) -> list:
    """Return ordered page ids from a parsed ``.content`` file.

    Handles both the modern ``cPages.pages[].id`` schema (native notebooks) and
    the legacy flat ``pages`` UUID list (PDF/EPUB-backed documents).
    """
    cpages = content_data.get("cPages")
    if isinstance(cpages, dict) and isinstance(cpages.get("pages"), list):
        ids = [p.get("id") for p in cpages["pages"] if isinstance(p, dict) and p.get("id")]
        if ids:
            return ids
    pages = content_data.get("pages")
    if isinstance(pages, list) and all(isinstance(p, str) for p in pages):
        return list(pages)
    return []


def _read_remote_bytes(ssh_client: SSHClient, remote_path: str) -> Optional[bytes]:
    """Download a remote file's bytes, or None if it does not exist / fails."""
    try:
        return ssh_client._scp_download(remote_path)
    except Exception as e:
        logger.debug(f"Remote read failed for {remote_path}: {e}")
        return None


def _remote_file_exists(ssh_client: SSHClient, remote_path: str) -> bool:
    """Return True if a file exists on the tablet."""
    out = ssh_client._ssh_command(f"test -f '{remote_path}' && echo yes || echo no")
    return out.strip().endswith("yes")


def _write_remote_bytes(ssh_client: SSHClient, remote_path: str, data: bytes) -> None:
    """Write bytes to a remote path by staging a local temp file and uploading."""
    import tempfile

    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        _upload_file_bytes(ssh_client, tmp_path, remote_path)
    finally:
        os.unlink(tmp_path)


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


def _invalidate_client_cache(client) -> None:
    """Drop the in-memory document cache so the next read reflects the write."""
    client._documents = []
    client._documents_by_id = {}


def _cloud_mkdir(folder_name: str, parent: str) -> str:
    """Create a folder in the reMarkable cloud."""
    client = get_rmapi()
    collection = client.get_meta_items()
    items_by_id = get_items_by_id(collection)
    parent_id = _resolve_parent_id(parent, items_by_id, collection)
    if parent_id is None:
        return make_error(
            error_type="folder_not_found",
            message=f"Parent folder not found: '{parent}'",
            suggestion="Use remarkable_browse('/') to see available folders.",
        )
    doc = client.create_folder(folder_name, parent_id)
    _invalidate_client_cache(client)
    return make_response(
        {
            "created": True,
            "folder_name": folder_name,
            "uuid": doc.id,
            "parent": parent,
            "transport": "cloud",
        },
        "Folder created in the reMarkable cloud. Use remarkable_browse() to verify.",
    )


def _cloud_move(document: str, dest_folder: str) -> str:
    """Move a document or folder to another folder in the cloud."""
    client = get_rmapi()
    collection = client.get_meta_items()
    items_by_id = get_items_by_id(collection)

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

    dest_id = _resolve_parent_id(dest_folder, items_by_id, collection)
    if dest_id is None:
        return make_error(
            error_type="folder_not_found",
            message=f"Destination folder not found: '{dest_folder}'",
            suggestion="Use remarkable_browse('/') to see available folders.",
        )

    # Prevent moving a folder into itself or one of its descendants
    if target.is_folder:
        if dest_id == target.ID:
            return make_error(
                error_type="invalid_move",
                message="Cannot move a folder into itself",
                suggestion="Choose a different destination folder.",
            )
        check_id = dest_id
        while check_id and check_id in items_by_id:
            if check_id == target.ID:
                return make_error(
                    error_type="invalid_move",
                    message="Cannot move a folder into one of its subfolders",
                    suggestion="Choose a destination that is not inside the folder being moved.",
                )
            check_id = getattr(items_by_id[check_id], "Parent", "")

    old_path = get_item_path(target, items_by_id)
    client.move(target.ID, dest_id)
    _invalidate_client_cache(client)
    return make_response(
        {
            "moved": True,
            "name": target.VissibleName,
            "from": old_path,
            "to": dest_folder,
            "transport": "cloud",
        },
        "Document moved in the reMarkable cloud. Use remarkable_browse() to verify.",
    )


def _cloud_rename(document: str, new_name: str) -> str:
    """Rename a document or folder in the cloud."""
    client = get_rmapi()
    collection = client.get_meta_items()
    items_by_id = get_items_by_id(collection)

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

    old_name = target.VissibleName
    client.rename(target.ID, new_name)
    _invalidate_client_cache(client)
    return make_response(
        {
            "renamed": True,
            "old_name": old_name,
            "new_name": new_name,
            "transport": "cloud",
        },
        "Document renamed in the reMarkable cloud. Use remarkable_browse() to verify.",
    )


def _cloud_delete(document: str) -> str:
    """Move a document or folder to the cloud trash (recoverable on-device)."""
    client = get_rmapi()
    collection = client.get_meta_items()
    items_by_id = get_items_by_id(collection)

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
    client.delete(target.ID)
    _invalidate_client_cache(client)
    return make_response(
        {
            "deleted": True,
            "name": target.VissibleName,
            "path": doc_path,
            "type": "folder" if target.is_folder else "document",
            "transport": "cloud",
        },
        "Moved to the trash on your reMarkable (recoverable from the device's Trash).",
    )


class _DeleteConfirmation(BaseModel):
    """Schema for confirming a destructive delete via elicitation."""

    confirm: bool = Field(
        default=False,
        description="Set to true to permanently move this item to the trash.",
    )


async def _confirm_delete(ctx: Optional[Context], document: str) -> Optional[str]:
    """Confirm a destructive delete before it runs.

    Returns None to proceed with the delete, or a response string (a
    cancellation or a refusal) to abort.

    Delete is destructive, so confirmation is required. It is bypassed only when
    REMARKABLE_SKIP_CONFIRM=1 (for headless automation). When the client cannot
    show a confirmation prompt — no elicitation support, or elicitation fails at
    runtime — the delete is REFUSED rather than performed silently, so a client
    that can't prompt the user cannot delete documents unattended. Write tools
    are on by default, and most MCP clients don't support elicitation yet, so
    this refusal path is the common case for unattended setups.
    """
    if os.environ.get("REMARKABLE_SKIP_CONFIRM", "").lower() in ("1", "true", "yes"):
        return None
    if ctx is None or not client_supports_elicitation(ctx):
        return make_error(
            "confirmation_unavailable",
            (
                f"Refused to delete '{document}': this client cannot show a "
                "confirmation prompt (no MCP elicitation support) and delete is "
                "a destructive operation."
            ),
            (
                "Confirm the delete from a client that supports elicitation, or "
                "set REMARKABLE_SKIP_CONFIRM=1 to allow deletes without a prompt "
                "(e.g. for headless automation)."
            ),
        )
    try:
        result = await ctx.elicit(
            message=f"Delete '{document}' from your reMarkable? This moves it to the trash.",
            schema=_DeleteConfirmation,
        )
    except Exception as e:  # elicitation advertised but failed — refuse, don't delete
        logger.debug(f"Elicitation failed, refusing delete without confirmation: {e}")
        return make_error(
            "confirmation_unavailable",
            (
                f"Refused to delete '{document}': the confirmation prompt could "
                "not be shown and delete is a destructive operation."
            ),
            (
                "Retry from a client that can display confirmation prompts, or "
                "set REMARKABLE_SKIP_CONFIRM=1 to allow deletes without a prompt."
            ),
        )
    if result.action != "accept" or not getattr(result.data, "confirm", False):
        return make_response(
            {"deleted": False, "cancelled": True, "document": document},
            "Delete cancelled — nothing was changed.",
        )
    return None


def _author_draw(document: str, page: int, strokes: list, ui_submitted: bool) -> str:
    """Append strokes to a page, auto-creating a blank drawable layer if absent."""
    if not document or page is None:
        return make_error(
            error_type="missing_parameter",
            message="draw requires 'document' and 'page'.",
            suggestion=(
                'Call remarkable_author(method="draw", document=..., page=1, strokes=[...]).'
            ),
        )
    if not strokes:
        return make_error(
            error_type="no_strokes",
            message="No strokes provided to write.",
            suggestion=(
                "Pass a non-empty list of stroke dicts, e.g. "
                '[{"points": [[0.1,0.2],[0.8,0.2]], "tool": "fineliner", "color": "black"}].'
            ),
        )

    from remarkable_mcp import notebooks as nb
    from remarkable_mcp import strokes as strokes_mod

    ssh_client = _get_ssh_client()
    collection = ssh_client.get_meta_items()
    items_by_id = get_items_by_id(collection)
    target = _resolve_document(document, collection, items_by_id)
    if not target:
        from remarkable_mcp.extract import find_similar_documents

        similar = find_similar_documents(document, collection)
        return make_error(
            error_type="document_not_found",
            message=f"Document not found: '{document}'",
            suggestion="Use remarkable_browse() to find the correct name.",
            did_you_mean=similar or None,
        )

    doc_uuid = target.ID
    content_raw = _read_remote_bytes(ssh_client, f"{XOCHITL_PATH}/{doc_uuid}.content")
    try:
        content_data = json.loads(content_raw) if content_raw else {}
    except (ValueError, TypeError):
        content_data = {}
    page_ids = _page_ids_from_content(content_data)
    if not page_ids:
        return make_error(
            error_type="no_pages",
            message=f"Could not read the page list for '{target.VissibleName}'.",
            suggestion="The document may have no pages, or an unexpected .content format.",
        )
    if page < 1 or page > len(page_ids):
        return make_error(
            error_type="page_out_of_range",
            message=f"Page {page} does not exist. Document has {len(page_ids)} page(s).",
            suggestion=f"Use page=1 to {len(page_ids)}.",
        )

    page_id = page_ids[page - 1]
    file_type = str(content_data.get("fileType", "") or "")
    rm_path = f"{XOCHITL_PATH}/{doc_uuid}/{page_id}.rm"
    original = _read_remote_bytes(ssh_client, rm_path)
    # A native notebook page that has never been drawn on (or a PDF/EPUB page
    # with no annotation overlay yet) has no .rm file. Seed a blank drawable
    # layer so the strokes have somewhere to land.
    page_existed = original is not None
    if not page_existed:
        original = nb.blank_page_rm_bytes()

    try:
        geom = strokes_mod.page_geometry(original)
        new_bytes = strokes_mod.append_strokes(original, strokes)
    except strokes_mod.StrokeError as e:
        return make_error(
            error_type="stroke_write_failed",
            message=str(e),
            suggestion=(
                "Check the stroke format (points normalized [0,1]) and that "
                "the page has a drawable layer."
            ),
        )
    except Exception as e:  # noqa: BLE001 - surface as structured guidance
        return make_error(
            error_type="stroke_write_failed",
            message=f"Failed to build strokes for page {page}: {e}",
            suggestion="Verify the stroke payload and retry.",
        )

    # Preserve the pristine original once (only if the page pre-existed), then
    # write the appended bytes.
    if page_existed:
        bak_path = rm_path + ".bak"
        if not _remote_file_exists(ssh_client, bak_path):
            _write_remote_bytes(ssh_client, bak_path, original)
    _write_remote_bytes(ssh_client, rm_path, new_bytes)
    _maybe_restart_xochitl(ssh_client)

    result = {
        "written": True,
        "document": target.VissibleName,
        "page": page,
        "total_pages": len(page_ids),
        "strokes_added": len(strokes),
        "paper_size": [geom["paper_width"], geom["paper_height"]],
        "file_type": file_type or "notebook",
        "created_overlay": not page_existed,
        "writable": True,
        "transport": "ssh",
        "ui_submitted": bool(ui_submitted),
    }
    if file_type.lower() == "epub":
        result["caveat"] = (
            "This is a reflowable EPUB; annotations are anchored to the "
            "current layout and may shift if the font size or layout changes."
        )
    hint = (
        f"Appended {len(strokes)} stroke(s) to page {page} of "
        f"'{target.VissibleName}'. Call remarkable_canvas(document, page) to "
        "view the updated page."
    )
    return make_response(result, hint)


def _author_add_page(document: str) -> str:
    """Append a blank, drawable page to the end of a native notebook."""
    if not document:
        return make_error(
            error_type="missing_parameter",
            message="add_page requires 'document'.",
            suggestion='Call remarkable_author(method="add_page", document=...).',
        )

    from remarkable_mcp import notebooks as nb

    ssh_client = _get_ssh_client()
    collection = ssh_client.get_meta_items()
    items_by_id = get_items_by_id(collection)
    target = _resolve_document(document, collection, items_by_id)
    if not target:
        from remarkable_mcp.extract import find_similar_documents

        similar = find_similar_documents(document, collection)
        return make_error(
            error_type="document_not_found",
            message=f"Document not found: '{document}'",
            suggestion="Use remarkable_browse() to find the correct name.",
            did_you_mean=similar or None,
        )

    doc_uuid = target.ID
    content_raw = _read_remote_bytes(ssh_client, f"{XOCHITL_PATH}/{doc_uuid}.content")
    try:
        content_data = json.loads(content_raw) if content_raw else {}
    except (ValueError, TypeError):
        content_data = {}
    if not content_data:
        return make_error(
            error_type="no_content",
            message=f"Could not read the .content file for '{target.VissibleName}'.",
            suggestion="The document may be missing or have an unexpected format.",
        )

    # Reuse the notebook's existing author uuid so CRDT author ids stay aligned.
    author_uuid = None
    uuids = content_data.get("cPages", {}).get("uuids")
    if isinstance(uuids, list) and uuids and isinstance(uuids[0], dict):
        author_uuid = uuids[0].get("first")

    new_page_id = nb.new_uuid()
    try:
        updated = nb.append_page_to_content(content_data, new_page_id)
    except ValueError as e:
        return make_error(
            error_type="not_a_notebook",
            message=str(e),
            suggestion=(
                "Pages can only be added to native notebooks. PDF/EPUB pages "
                "already exist — open the page and draw on it directly."
            ),
        )

    try:
        page_bytes = nb.blank_page_rm_bytes(author_uuid=author_uuid)
    except (ValueError, TypeError):
        page_bytes = nb.blank_page_rm_bytes()

    _write_remote_bytes(ssh_client, f"{XOCHITL_PATH}/{doc_uuid}/{new_page_id}.rm", page_bytes)
    _write_content_file(ssh_client, doc_uuid, updated["content"])
    _maybe_restart_xochitl(ssh_client)
    _invalidate_client_cache(ssh_client)

    result = {
        "added": True,
        "document": target.VissibleName,
        "page_added": updated["page_index"],
        "total_pages": updated["total_pages"],
        "paper_size": list(nb.DEFAULT_PAPER),
        "transport": "ssh",
    }
    hint = (
        f"Added a blank page (now page {updated['page_index']} of "
        f"{updated['total_pages']}). Call remarkable_canvas('{target.VissibleName}', "
        f"{updated['page_index']}) to draw on it."
    )
    return make_response(result, hint)


def _author_create_document(name: str, text: Optional[str], folder: Optional[str]) -> str:
    """Create a new native notebook (blank, or seeded with typed text)."""
    if not name:
        return make_error(
            error_type="missing_parameter",
            message="create_document requires 'name'.",
            suggestion='Call remarkable_author(method="create_document", name="My notes").',
        )

    from remarkable_mcp import notebooks as nb

    ssh_client = _get_ssh_client()
    collection = ssh_client.get_meta_items()
    items_by_id = get_items_by_id(collection)
    parent_id = _resolve_parent_id(folder or "/", items_by_id, collection)
    if parent_id is None:
        folders = [get_item_path(i, items_by_id) for i in collection if i.is_folder]
        return make_error(
            error_type="folder_not_found",
            message=f"Folder not found: '{folder}'",
            suggestion="Use remarkable_browse('/') to see available folders.",
            did_you_mean=folders[:5] if folders else None,
        )

    doc_uuid = nb.new_uuid()
    page_id = nb.new_uuid()
    author_uuid = nb.new_uuid()
    page_bytes = nb.page_rm_bytes(text or "", author_uuid=author_uuid)
    content_data = nb.new_notebook_content([page_id], author_uuid)
    metadata = nb.new_document_metadata(name, parent=parent_id)

    ssh_client._ssh_command(f"mkdir -p '{XOCHITL_PATH}/{doc_uuid}'")
    _write_remote_bytes(ssh_client, f"{XOCHITL_PATH}/{doc_uuid}/{page_id}.rm", page_bytes)
    _write_content_file(ssh_client, doc_uuid, content_data)
    _write_metadata(ssh_client, doc_uuid, metadata)
    _maybe_restart_xochitl(ssh_client)
    _invalidate_client_cache(ssh_client)

    result = {
        "created": True,
        "document": name,
        "document_id": doc_uuid,
        "page_id": page_id,
        "total_pages": 1,
        "paper_size": list(nb.DEFAULT_PAPER),
        "has_text": bool(text),
        "folder": folder or "/",
        "transport": "ssh",
    }
    hint = f"Created notebook '{name}'. Call remarkable_canvas('{name}', 1) to view or draw on it."
    return make_response(result, hint)


def register_write_tools():
    """Register all write tools with the MCP server."""
    # Imported lazily to break a circular import: server.py imports this module
    # at load time and immediately calls register_write_tools(), so importing
    # `mcp` at module top would re-enter a partially-initialized server module.
    from remarkable_mcp.server import mcp

    async def remarkable_author(
        method: str,
        document: Optional[str] = None,
        page: Optional[int] = None,
        strokes: Optional[list] = None,
        name: Optional[str] = None,
        text: Optional[str] = None,
        folder: Optional[str] = None,
        ui_submitted: bool = False,
        ctx: Context = None,
    ) -> str:
        """
        <usecase>Author native reMarkable ink and notebooks. ONE compound write
        primitive with three methods: "draw" appends pen/highlighter strokes to a
        page, "add_page" appends a blank drawable page to a notebook, and
        "create_document" creates a new notebook (optionally seeded with typed
        text). This is the single tool behind the interactive canvas (Save → draw,
        ＋Page → add_page, new notebook → create_document) AND model-driven markup
        (highlighting, underlining, marking) — the model composes the strokes.</usecase>
        <instructions>
        Requires SSH mode (the default tablet-filesystem transport for write-back)
        and write mode (the default; disabled with --read-only). All methods are
        non-destructive (draw appends to existing ink and backs the page up to
        {pageId}.rm.bak the first time), so no confirmation prompt is required.

        Pick the method, then pass only that method's parameters:

        - method="draw": requires `document` + `page` + `strokes`. Strokes are
          APPENDED to the page's ink. If the page has no ink layer yet (a fresh
          notebook page, or a PDF/EPUB page never annotated), a blank drawable
          layer is created automatically. Coordinates: each point is [nx, ny] or
          [nx, ny, pressure] with nx, ny in [0,1] from the page's TOP-LEFT; the
          tool maps them to the page's own paper_size so the same payload works on
          any device. (PDF-backed pages currently use the same map; precise PDF
          overlay placement is a follow-up.)

        - method="add_page": requires `document`. Appends one blank page to the END
          of a native notebook and returns its 1-based page number. Only native
          notebooks support this (PDF/EPUB pages already exist).

        - method="create_document": requires `name`. Creates a new native notebook
          in `folder` (default root). Leave `text` EMPTY for a blank notebook —
          that is the default and the correct choice. Only pass `text` when the
          user EXPLICITLY asks for specific typed content; never invent a title,
          placeholder, or "created by" line. Returns the new document id and
          first-page geometry.

        The interactive canvas calls this exact tool via the MCP Apps bridge,
        passing ui_submitted=true for draw (the human already chose Save). A model
        calling it directly should omit ui_submitted.
        </instructions>
        <parameters>
        - method: "draw" | "add_page" | "create_document".
        - document: Target document name/path/id (draw, add_page).
        - page: 1-based page number (draw).
        - strokes: List of stroke dicts (draw). Each:
            {"points": [[nx, ny], [nx, ny, pressure], ...],
             "tool": "fineliner" | "highlighter" | int,
             "color": "black" | "yellow" | ...,
             "width": <optional float>, "thickness_scale": <optional float>}
        - name: Title of the new notebook (create_document).
        - text: Optional typed text for the first page (create_document). Omit it
            unless the user explicitly requested specific content; never fabricate
            placeholder text. Paragraphs split on newlines.
        - folder: Destination folder path (create_document; default "/").
        - ui_submitted: Set by the canvas app when the user clicked Save. Models omit it.
        </parameters>
        <examples>
        - remarkable_author(method="draw", document="Ideas", page=1,
            strokes=[{"points": [[0.1,0.2],[0.8,0.2]], "tool": "highlighter", "color": "yellow"}])
        - remarkable_author(method="add_page", document="Ideas")
        - remarkable_author(method="create_document", name="Sketches")
        - remarkable_author(method="create_document", name="Meeting notes",
            text="Agenda\nFollow-ups")  # only when the user supplied that text
        </examples>
        """

        def _impl() -> str:
            error = _require_write_transport()
            if error:
                return error

            if method == "draw":
                return _author_draw(document, page, strokes, ui_submitted)
            if method == "add_page":
                return _author_add_page(document)
            if method == "create_document":
                return _author_create_document(name, text, folder)
            return make_error(
                error_type="unknown_method",
                message=f"Unknown method: '{method}'.",
                suggestion='Use method="draw", "add_page", or "create_document".',
                did_you_mean=["draw", "add_page", "create_document"],
            )

        return await asyncio.to_thread(_impl)

    # Native ink/notebook authoring is SSH-only today (cloud/USB-web write-back
    # is not implemented yet), so only expose the tool in SSH mode rather than
    # registering it everywhere and erroring at call time. Clients on other
    # transports simply don't see a tool they couldn't use.
    if _is_ssh_mode():
        mcp.tool(annotations=WRITE_ANNOTATIONS)(remarkable_author)

    @mcp.tool(annotations=UPLOAD_ANNOTATIONS)
    async def remarkable_upload(
        file_path: str,
        parent_folder: str = "/",
        document_name: Optional[str] = None,
        defer_restart: bool = False,
    ) -> str:
        """
        <usecase>Upload a PDF or EPUB file to the reMarkable tablet.</usecase>
        <instructions>
        Uploads a local file to the tablet. Only PDF and EPUB formats
        are supported. Works in all three transports:
        - Cloud: uploaded via the sync protocol; supports parent_folder + document_name
        - SSH: transferred over SSH, metadata created; supports parent_folder + document_name
        - USB web: uploaded via POST /upload; lands at the root (the firmware's
          upload endpoint has no folder or rename field)

        Requires write mode (the default; disabled with --read-only).
        </instructions>
        <parameters>
        - file_path: Absolute path to the local PDF or EPUB file
        - parent_folder: Destination folder path on tablet (default: root "/").
          Honored in cloud and SSH modes; ignored by the USB web interface.
        - document_name: Display name on tablet (default: filename without
          extension). Honored in cloud and SSH modes; ignored by the USB web interface.
        - defer_restart: SSH only. When True, skip the xochitl restart that
          normally makes the upload visible. Use this for every upload in a
          batch, then call remarkable_refresh() ONCE at the end. Each restart
          forces a full document-store rescan that briefly destabilises the SSH
          link, so deferring turns N restarts (and N races) into one. The
          response sets "refresh_pending": true while a refresh is owed. Ignored
          in cloud/USB modes (no xochitl).
        </parameters>
        <examples>
        - remarkable_upload("/tmp/paper.pdf")
        - remarkable_upload("/tmp/book.epub", parent_folder="/Books")
        - remarkable_upload("/tmp/report.pdf", document_name="Q4 Report")
        - # batch import: defer every upload, refresh once at the end
          remarkable_upload("/tmp/a.pdf", defer_restart=True)
          remarkable_upload("/tmp/b.pdf", defer_restart=True)
          remarkable_refresh()
        </examples>
        """

        def _impl() -> str:
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

                # Cloud mode: upload via the sync v3/v4 blob protocol
                if _is_cloud_mode():
                    client = get_rmapi()
                    collection = client.get_meta_items()
                    items_by_id = get_items_by_id(collection)
                    parent_id = _resolve_parent_id(parent_folder, items_by_id, collection)
                    if parent_id is None:
                        folders = [get_item_path(i, items_by_id) for i in collection if i.is_folder]
                        return make_error(
                            error_type="folder_not_found",
                            message=f"Folder not found: '{parent_folder}'",
                            suggestion="Use remarkable_browse('/') to see available folders.",
                            did_you_mean=folders[:5] if folders else None,
                        )

                    name = document_name or os.path.splitext(os.path.basename(file_path))[0]
                    with open(file_path, "rb") as f:
                        data = f.read()
                    doc = client.upload_document(data, name, ext, parent_id)
                    return make_response(
                        {
                            "uploaded": True,
                            "name": name,
                            "uuid": doc.id,
                            "format": ext,
                            "parent_folder": parent_folder,
                            "transport": "cloud",
                        },
                        "Document uploaded to the reMarkable cloud. "
                        "Use remarkable_browse() to verify it appears.",
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
                            "USB web upload places files at root. "
                            "Use SSH mode for folder placement."
                        )
                    return make_response(
                        result,
                        "Document uploaded via USB web interface. "
                        "Use remarkable_browse() to verify.",
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

                # Restart xochitl to pick up changes (unless deferred for a batch)
                restarted = _maybe_restart_xochitl(ssh_client, defer_restart)

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
                        "refresh_pending": not restarted,
                    },
                    _write_result_message(
                        restarted,
                        "Document uploaded successfully.",
                        "Use remarkable_browse() to verify it appears.",
                    ),
                )

            except Exception as e:
                transport = "USB web" if _is_usb_web_mode() else "SSH"
                return make_error(
                    error_type="upload_failed",
                    message=f"Upload failed: {e}",
                    suggestion=f"Check {transport} connection and try again.",
                )

        return await asyncio.to_thread(_impl)

    if _is_ssh_mode() or _is_cloud_mode():

        @mcp.tool(annotations=MKDIR_ANNOTATIONS)
        async def remarkable_mkdir(
            folder_name: str,
            parent: str = "/",
            defer_restart: bool = False,
        ) -> str:
            """
            <usecase>Create a new folder on the reMarkable tablet.</usecase>
            <instructions>
            Creates a folder in the tablet's document hierarchy. In SSH mode the
            folder appears after xochitl restarts; in cloud mode it syncs to all
            your devices.

            Works in cloud and SSH modes (default; --read-only disables). Not available over the
            USB web interface (the firmware exposes no folder-create endpoint).
            </instructions>
            <parameters>
            - folder_name: Name of the new folder
            - parent: Parent folder path (default: root "/")
            - defer_restart: SSH only. Skip the xochitl restart so a batch of
              writes can refresh once via remarkable_refresh() instead of once
              per write. Sets "refresh_pending": true. Ignored in cloud mode.
            </parameters>
            <examples>
            - remarkable_mkdir("Projects")
            - remarkable_mkdir("2024", parent="/Archive")
            </examples>
            """

            def _impl() -> str:
                error = _require_managed_write_mode()
                if error:
                    return error

                if _is_cloud_mode():
                    return _cloud_mkdir(folder_name, parent)

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

                    # Restart xochitl (unless deferred for a batch)
                    restarted = _maybe_restart_xochitl(ssh_client, defer_restart)

                    # Clear cache
                    ssh_client._documents = []
                    ssh_client._documents_by_id = {}

                    return make_response(
                        {
                            "created": True,
                            "folder_name": folder_name,
                            "uuid": doc_uuid,
                            "parent": parent,
                            "refresh_pending": not restarted,
                        },
                        _write_result_message(restarted, "Folder created."),
                    )

                except Exception as e:
                    return make_error(
                        error_type="mkdir_failed",
                        message=f"Failed to create folder: {e}",
                        suggestion="Check SSH connection and try again.",
                    )

            return await asyncio.to_thread(_impl)

        @mcp.tool(annotations=MOVE_ANNOTATIONS)
        async def remarkable_move(
            document: str,
            dest_folder: str,
            defer_restart: bool = False,
        ) -> str:
            """
            <usecase>Move a document or folder to a different location.</usecase>
            <instructions>
            Moves a document or folder by updating its parent reference in the metadata.
            Find the document name with remarkable_browse() first.

            Works in cloud and SSH modes (default; --read-only disables). Not available over the
            USB web interface.
            </instructions>
            <parameters>
            - document: Name or path of the document/folder to move
            - dest_folder: Destination folder path (use "/" for root)
            - defer_restart: SSH only. Skip the xochitl restart so a batch of
              writes can refresh once via remarkable_refresh() instead of once
              per write. Sets "refresh_pending": true. Ignored in cloud mode.
            </parameters>
            <examples>
            - remarkable_move("Meeting Notes", "/Archive")
            - remarkable_move("Old Project", "/Archive/2023")
            </examples>
            """

            def _impl() -> str:
                error = _require_managed_write_mode()
                if error:
                    return error

                if _is_cloud_mode():
                    return _cloud_move(document, dest_folder)

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
                                        "Choose a destination that is not inside the folder "
                                        "being moved."
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

                    # Restart xochitl (unless deferred for a batch)
                    restarted = _maybe_restart_xochitl(ssh_client, defer_restart)

                    # Clear cache
                    ssh_client._documents = []
                    ssh_client._documents_by_id = {}

                    return make_response(
                        {
                            "moved": True,
                            "name": target.VissibleName,
                            "from": old_path,
                            "to": dest_folder,
                            "refresh_pending": not restarted,
                        },
                        _write_result_message(restarted, "Document moved."),
                    )

                except Exception as e:
                    return make_error(
                        error_type="move_failed",
                        message=f"Failed to move document: {e}",
                        suggestion="Check SSH connection and try again.",
                    )

            return await asyncio.to_thread(_impl)

        @mcp.tool(annotations=RENAME_ANNOTATIONS)
        async def remarkable_rename(
            document: str,
            new_name: str,
            defer_restart: bool = False,
        ) -> str:
            """
            <usecase>Rename a document or folder on the reMarkable tablet.</usecase>
            <instructions>
            Changes the display name of a document or folder by updating its metadata.
            Find the document name with remarkable_browse() first.

            Works in cloud and SSH modes (default; --read-only disables). Not available over the
            USB web interface.
            </instructions>
            <parameters>
            - document: Current name or path of the document/folder
            - new_name: New display name
            - defer_restart: SSH only. Skip the xochitl restart so a batch of
              writes can refresh once via remarkable_refresh() instead of once
              per write. Sets "refresh_pending": true. Ignored in cloud mode.
            </parameters>
            <examples>
            - remarkable_rename("Untitled", "Meeting Notes 2024-01-15")
            - remarkable_rename("/Work/Draft", "Final Report")
            </examples>
            """

            def _impl() -> str:
                error = _require_managed_write_mode()
                if error:
                    return error

                if _is_cloud_mode():
                    return _cloud_rename(document, new_name)

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

                    # Restart xochitl (unless deferred for a batch)
                    restarted = _maybe_restart_xochitl(ssh_client, defer_restart)

                    # Clear cache
                    ssh_client._documents = []
                    ssh_client._documents_by_id = {}

                    return make_response(
                        {
                            "renamed": True,
                            "old_name": old_name,
                            "new_name": new_name,
                            "refresh_pending": not restarted,
                        },
                        _write_result_message(restarted, "Document renamed."),
                    )

                except Exception as e:
                    return make_error(
                        error_type="rename_failed",
                        message=f"Failed to rename document: {e}",
                        suggestion="Check SSH connection and try again.",
                    )

            return await asyncio.to_thread(_impl)

        @mcp.tool(annotations=DELETE_ANNOTATIONS)
        async def remarkable_delete(
            document: str,
            defer_restart: bool = False,
            ctx: Optional[Context] = None,
        ) -> str:
            """
            <usecase>Delete a document or folder on the reMarkable tablet.</usecase>
            <instructions>
            DESTRUCTIVE operation. In cloud mode the item is moved to the trash
            (recoverable from the device's Trash). In SSH mode it is marked deleted
            in its metadata and disappears from the tablet UI after restart.

            Works in cloud and SSH modes (default; --read-only disables). Not available over the
            USB web interface.

            If the client supports elicitation, this tool asks the user to confirm
            before deleting. If the client cannot show a confirmation prompt, the
            delete is refused unless REMARKABLE_SKIP_CONFIRM=1 is set (for headless
            automation).
            </instructions>
            <parameters>
            - document: Name or path of the document/folder to delete
            - defer_restart: SSH only. Skip the xochitl restart so a batch of
              deletes can refresh once via remarkable_refresh() instead of once
              per delete. Sets "refresh_pending": true. Ignored in cloud mode.
            </parameters>
            <examples>
            - remarkable_delete("Old Notes")
            - remarkable_delete("/Books/Archive/Old Draft")
            </examples>
            """
            error = _require_managed_write_mode()
            if error:
                return error

            abort = await _confirm_delete(ctx, document)
            if abort:
                return abort

            if _is_cloud_mode():
                return await asyncio.to_thread(_cloud_delete, document)

            def _impl() -> str:
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

                    # Read existing metadata
                    meta_content = ssh_client._scp_download(f"{XOCHITL_PATH}/{target.ID}.metadata")
                    metadata = json.loads(meta_content.decode("utf-8"))

                    # Mark as deleted
                    metadata["deleted"] = True
                    metadata["lastModified"] = str(int(time.time() * 1000))
                    metadata["metadatamodified"] = True
                    _write_metadata(ssh_client, target.ID, metadata)

                    # Restart xochitl (unless deferred for a batch)
                    restarted = _maybe_restart_xochitl(ssh_client, defer_restart)

                    # Clear cache
                    ssh_client._documents = []
                    ssh_client._documents_by_id = {}

                    return make_response(
                        {
                            "deleted": True,
                            "name": target.VissibleName,
                            "path": doc_path,
                            "type": "folder" if target.is_folder else "document",
                            "refresh_pending": not restarted,
                        },
                        _write_result_message(
                            restarted,
                            "Document deleted.",
                            "It will no longer appear on the tablet.",
                        ),
                    )

                except Exception as e:
                    return make_error(
                        error_type="delete_failed",
                        message=f"Failed to delete document: {e}",
                        suggestion="Check SSH connection and try again.",
                    )

            return await asyncio.to_thread(_impl)

    async def remarkable_refresh() -> str:
        """
        <usecase>Restart xochitl once to apply writes made with
        defer_restart=True (or with REMARKABLE_DEFER_RESTART set).</usecase>
        <instructions>
        SSH only. Each write normally restarts xochitl so the change becomes
        visible, but every restart forces a full document-store rescan that
        briefly destabilises the SSH link. For a batch of writes, pass
        defer_restart=True to each one and then call this tool ONCE at the end:
        it pays the restart cost a single time instead of once per write,
        eliminating the per-write race and the redundant rescans.

        Calling it when nothing is pending is harmless — it just restarts the UI.
        Requires write mode (the default; disabled with --read-only).
        </instructions>
        <parameters>(none)</parameters>
        <examples>
        - remarkable_upload("/tmp/a.pdf", defer_restart=True)
          remarkable_upload("/tmp/b.pdf", defer_restart=True)
          remarkable_refresh()
        </examples>
        """

        def _impl() -> str:
            error = _require_write_transport()
            if error:
                return error

            try:
                ssh_client = _get_ssh_client()
                _restart_xochitl(ssh_client)
                # Drop any cache populated while restarts were deferred so the
                # next read reflects the just-applied changes.
                ssh_client._documents = []
                ssh_client._documents_by_id = {}
                return make_response(
                    {"refreshed": True, "transport": "ssh"},
                    "xochitl restarted; any deferred changes are now visible. "
                    "Use remarkable_browse() to verify.",
                )
            except Exception as e:
                return make_error(
                    error_type="refresh_failed",
                    message=f"Failed to refresh tablet UI: {e}",
                    suggestion="Check SSH connection and try again.",
                )

        return await asyncio.to_thread(_impl)

    # Refreshing is meaningful only for the SSH transport's xochitl restart;
    # cloud/USB clients have no equivalent, so the tool is hidden there.
    if _is_ssh_mode():
        mcp.tool(annotations=WRITE_ANNOTATIONS)(remarkable_refresh)
