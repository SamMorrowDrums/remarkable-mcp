"""
Write tools for reMarkable tablet via cloud, SSH, or USB web interface.

The local-directory transport is always read-only and never registers these tools.

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
import shlex
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext
from datetime import datetime
from typing import Annotated, Optional

from mcp.server.mcpserver import Context, Elicit, ElicitationResult, Resolve
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from remarkable_mcp.api import (
    get_item_path,
    get_items_by_id,
    get_rmapi,
)
from remarkable_mcp.capabilities import client_supports_elicitation
from remarkable_mcp.responses import make_error, make_response
from remarkable_mcp.ssh import XOCHITL_PATH, Document, SSHClient
from remarkable_mcp.ssh_reliability import (
    SSHExecutionUnknownError,
    SSHPreExecutionError,
    SSHReliabilityError,
)

logger = logging.getLogger(__name__)
_write_context = threading.local()

# Tool annotations for write operations
WRITE_ANNOTATIONS = ToolAnnotations(read_only_hint=False)
UPLOAD_ANNOTATIONS = WRITE_ANNOTATIONS
MKDIR_ANNOTATIONS = WRITE_ANNOTATIONS
MOVE_ANNOTATIONS = WRITE_ANNOTATIONS
RENAME_ANNOTATIONS = WRITE_ANNOTATIONS
DELETE_ANNOTATIONS = ToolAnnotations(read_only_hint=False, destructive_hint=True)


def read_only_enabled() -> bool:
    """Check if read-only mode is enabled via the --read-only flag / env var."""
    return os.environ.get("REMARKABLE_READ_ONLY", "").lower() in (
        "1",
        "true",
        "yes",
    )


def write_enabled() -> bool:
    """Write tools are enabled by default; disabled only in read-only mode.

    The local-directory transport is unconditionally read-only: the directory
    is the desktop app's private sync cache, and writing to it directly would
    bypass sync and could corrupt the app's state.
    """
    if _is_local_dir_mode():
        return False
    return not read_only_enabled()


def _require_write_transport() -> Optional[str]:
    """Return an error string if writes are disabled, else None.

    Upload works in the three write-capable transports (cloud, SSH, USB web).
    Write tools are not registered in read-only or local-directory mode, so this
    is mostly a defensive check that returns a clear error if writes are somehow
    disabled.
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


def _is_local_dir_mode() -> bool:
    """Check if the local-directory transport is active."""
    return os.environ.get("REMARKABLE_USE_LOCAL_DIR", "").lower() in (
        "1",
        "true",
        "yes",
    ) or bool(os.environ.get("REMARKABLE_LOCAL_DIR"))


def _is_cloud_mode() -> bool:
    """Check if cloud transport is active (the default when other modes are off)."""
    return not _is_ssh_mode() and not _is_usb_web_mode() and not _is_local_dir_mode()


def _require_managed_write_mode() -> Optional[str]:
    """Return an error string unless in a mode that supports folder operations.

    mkdir, move, rename and delete work in cloud and SSH modes. USB web only
    supports uploads, while local-directory mode is read-only.
    """
    if _is_cloud_mode() or _is_ssh_mode():
        return None
    return make_error(
        error_type="unsupported_in_usb_web",
        message="This operation isn't available in USB web mode",
        suggestion=(
            "mkdir, move, rename and delete work in cloud mode (the "
            "default) and SSH mode. USB web supports upload only.\n"
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


def _permanently_delete_ssh_entry(ssh_client: SSHClient, doc_uuid: str) -> None:
    """Remove one resolved UUID and all of its sidecar files from the tablet."""
    entry = shlex.quote(f"{XOCHITL_PATH}/{doc_uuid}")
    sidecar_prefix = shlex.quote(f"{XOCHITL_PATH}/{doc_uuid}.")
    ssh_client._ssh_command(f"rm -rf {entry} {sidecar_prefix}*")


def _upload_file_bytes(ssh_client: SSHClient, local_path: str, remote_path: str) -> None:
    """Upload a local file to the tablet via SSH stdin pipe."""
    ssh_client._upload_file(local_path, remote_path)


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
    runner = getattr(ssh_client, "run_operation", None)
    in_dispatcher = getattr(ssh_client, "in_dispatcher_thread", lambda: False)
    if callable(runner) and not in_dispatcher():
        runner(
            "xochitl-refresh",
            lambda: _restart_xochitl_locked(ssh_client, wait_ready),
        )
        return
    _restart_xochitl_locked(ssh_client, wait_ready)


def _restart_xochitl_locked(ssh_client: SSHClient, wait_ready: bool) -> None:
    """Run restart/readiness/settle while excluding every other SSH operation."""
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
    if (
        defer_restart
        or _defer_restart_enabled()
        or getattr(_write_context, "suppress_restart", False)
    ):
        return False
    _restart_xochitl(ssh_client)
    return True


@contextmanager
def _suppress_automatic_restart():
    previous = getattr(_write_context, "suppress_restart", False)
    _write_context.suppress_restart = True
    try:
        yield
    finally:
        _write_context.suppress_restart = previous


def _response_persisted(response: str) -> bool:
    try:
        data = json.loads(response)
    except (TypeError, json.JSONDecodeError):
        return False
    return "_error" not in data


def _validate_author_request(
    method: str,
    document: Optional[str],
    page: Optional[int],
    strokes: Optional[list],
    name: Optional[str],
) -> Optional[str]:
    """Return transport-free validation errors before resolving an SSH client."""
    if method == "draw":
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
                    '[{"points": [[0.1,0.2],[0.8,0.2]], '
                    '"tool": "fineliner", "color": "black"}].'
                ),
            )
        return None
    if method == "add_page":
        if not document:
            return make_error(
                error_type="missing_parameter",
                message="add_page requires 'document'.",
                suggestion='Call remarkable_author(method="add_page", document=...).',
            )
        return None
    if method == "create_document":
        if not name:
            return make_error(
                error_type="missing_parameter",
                message="create_document requires 'name'.",
                suggestion=('Call remarkable_author(method="create_document", name="My notes").'),
            )
        return None
    return make_error(
        error_type="unknown_method",
        message=f"Unknown method: '{method}'.",
        suggestion='Use method="draw", "add_page", or "create_document".',
        did_you_mean=["draw", "add_page", "create_document"],
    )


def _mark_response_refreshed(response: str) -> str:
    try:
        data = json.loads(response)
    except (TypeError, json.JSONDecodeError):
        return response
    if "_error" in data:
        return response
    data["refresh_pending"] = False
    hint = data.get("_hint", "")
    deferred_text = "Restart deferred — call remarkable_refresh() once your batch is done."
    if deferred_text in hint:
        hint = hint.replace(
            deferred_text,
            "Concurrent writes were applied with one completed xochitl refresh.",
        )
    data["_hint"] = hint
    return json.dumps(data, indent=2)


def _mark_response_pending(response: str) -> str:
    try:
        data = json.loads(response)
    except (TypeError, json.JSONDecodeError):
        return response
    if "_error" in data:
        return response
    data["refresh_pending"] = True
    hint = data.get("_hint", "")
    if "remarkable_refresh" not in hint:
        hint = f"{hint} Restart deferred — call remarkable_refresh() once your batch is done."
    data["_hint"] = hint.strip()
    return json.dumps(data, indent=2)


def _pending_refresh_error(error_type: str, message: str, suggestion: str) -> str:
    data = json.loads(make_error(error_type, message, suggestion))
    data["refresh_pending"] = True
    return json.dumps(data, indent=2)


async def _refresh_ssh_client(ssh_client: SSHClient) -> None:
    try:
        await ssh_client.run_operation_async(
            "xochitl-refresh",
            lambda: _restart_xochitl(ssh_client),
        )
    except BaseException:
        _invalidate_client_cache(ssh_client)
        raise
    _invalidate_client_cache(ssh_client)


async def _run_ssh_tool_operation(
    ssh_client: SSHClient,
    operation: str,
    implementation,
    *,
    defer_restart: bool = False,
) -> str:
    deferred = defer_restart or _defer_restart_enabled()

    def mutation() -> str:
        with _suppress_automatic_restart():
            return implementation()

    try:
        response, refreshed = await ssh_client._refresh_coordinator.run_write(
            lambda: ssh_client.run_operation_async(operation, mutation),
            lambda: _refresh_ssh_client(ssh_client),
            deferred=deferred,
            persisted=_response_persisted,
        )
    except SSHPreExecutionError as e:
        return make_error(
            error_type="ssh_unavailable",
            message=str(e),
            suggestion="Wake and unlock the tablet, verify the USB cable, then try again.",
        )
    except SSHExecutionUnknownError as e:
        _invalidate_client_cache(ssh_client)
        return make_error(
            error_type="write_execution_unknown",
            message=str(e),
            suggestion=(
                "Do not repeat the write automatically. Run remarkable_refresh(), "
                "then browse the tablet to determine whether the change was applied."
            ),
        )
    except SSHReliabilityError as e:
        _invalidate_client_cache(ssh_client)
        return _pending_refresh_error(
            error_type="write_persisted_refresh_failed",
            message=str(e),
            suggestion=(
                "The write may already be on disk. Do not repeat it; restore the "
                "connection and call remarkable_refresh()."
            ),
        )
    except Exception as e:
        _invalidate_client_cache(ssh_client)
        return _pending_refresh_error(
            error_type="write_persisted_refresh_failed",
            message=f"Write persisted but the shared refresh failed: {e}",
            suggestion=(
                "Do not repeat the write. Restore the SSH connection and call "
                "remarkable_refresh() to make the pending change visible."
            ),
        )
    if refreshed:
        return _mark_response_refreshed(response)
    return _mark_response_pending(response) if deferred else response


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
    except SSHReliabilityError:
        raise
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


def _upload_filename(local_path: str, document_name: Optional[str] = None) -> str:
    """Return the multipart filename used by USB web uploads."""
    local_filename = os.path.basename(local_path)
    if not document_name or not document_name.strip():
        return local_filename

    requested = os.path.basename(document_name.strip().replace("\\", "/"))
    if not requested:
        return local_filename
    extension = os.path.splitext(local_filename)[1]
    if extension and not requested.lower().endswith(extension.lower()):
        requested += extension
    return requested


def _upload_via_usb_web(local_path: str, document_name: Optional[str] = None) -> dict:
    """Upload a file to the tablet via USB web interface POST /upload.

    Returns dict with upload result info.
    """
    import requests

    from remarkable_mcp.usb_web import DEFAULT_USB_HOST

    host = os.environ.get("REMARKABLE_USB_HOST", DEFAULT_USB_HOST).rstrip("/")
    url = f"{host}/upload"

    with open(local_path, "rb") as f:
        filename = _upload_filename(local_path, document_name)
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
    client_state = getattr(client, "__dict__", {})
    invalidate_metadata = getattr(type(client), "invalidate_metadata_cache", None)
    if callable(invalidate_metadata):
        invalidate_metadata(client)
    else:
        metadata_lock = client_state.get("_metadata_lock")
        context = metadata_lock if metadata_lock is not None else nullcontext()
        with context:
            client._documents = []
            client._documents_by_id = {}
            if "_metadata_loaded_all" in client_state:
                client._metadata_loaded_all = False
            if "_metadata_generation" in client_state:
                client._metadata_generation += 1

    file_type_lock = client_state.get("_file_type_lock")
    context = file_type_lock if file_type_lock is not None else nullcontext()
    with context:
        from remarkable_mcp.local_dir import LocalDirClient

        if isinstance(client, (SSHClient, LocalDirClient)):
            client._file_type_cache = None
        elif "_file_type_cache" in client_state:
            client._file_type_cache = {}
        if "_file_type_generation" in client_state:
            client._file_type_generation += 1


def _clear_document_extraction_cache(doc_id: str) -> None:
    from remarkable_mcp.extract import clear_extraction_cache

    clear_extraction_cache(doc_id)


def _update_deferred_ssh_cache(
    client: SSHClient,
    restarted: bool,
    *,
    document: Optional[Document] = None,
    remove_id: Optional[str] = None,
    file_type: Optional[str] = None,
) -> None:
    """Keep a deferred batch resolvable without rescanning the whole library."""
    if restarted:
        _invalidate_client_cache(client)
        return

    lock = getattr(client, "_metadata_lock", None)
    context = lock if lock is not None else nullcontext()
    with context:
        documents = getattr(client, "_documents", None)
        if not isinstance(documents, list):
            return
        if remove_id is not None:
            client._documents = [item for item in documents if item.ID != remove_id]
            client._documents_by_id.pop(remove_id, None)
        elif document is not None:
            existing = client._documents_by_id.get(document.ID)
            if existing is None:
                client._documents.append(document)
            elif existing is not document:
                client._documents = [
                    document if item.ID == document.ID else item for item in client._documents
                ]
            client._documents_by_id[document.ID] = document

    file_type_lock = getattr(client, "_file_type_lock", None)
    context = file_type_lock if file_type_lock is not None else nullcontext()
    with context:
        file_type_cache = getattr(client, "_file_type_cache", None)
        if remove_id is not None and file_type_cache is not None:
            file_type_cache.pop(remove_id, None)
        elif document is not None and file_type is not None and file_type_cache is not None:
            file_type_cache[document.ID] = file_type


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
        description="Set to true to confirm this destructive delete operation.",
    )


def _delete_confirmation_available(ctx: Context) -> bool:
    """Return whether deletion may proceed via elicitation or explicit bypass."""
    if os.environ.get("REMARKABLE_SKIP_CONFIRM", "").lower() in ("1", "true", "yes"):
        return True
    return client_supports_elicitation(ctx)


def _resolve_delete_confirmation(
    document: str,
    permanent: bool,
    confirmation_available: Annotated[bool, Resolve(_delete_confirmation_available)],
) -> _DeleteConfirmation | Elicit[_DeleteConfirmation]:
    """Resolve confirmation over a legacy back-channel or a modern input round."""
    skip = os.environ.get("REMARKABLE_SKIP_CONFIRM", "").lower() in (
        "1",
        "true",
        "yes",
    )
    if skip:
        return _DeleteConfirmation(confirm=True)
    if not confirmation_available:
        return _DeleteConfirmation(confirm=False)
    if permanent:
        message = (
            f"Permanently delete '{document}' from your reMarkable? "
            "This is irreversible and cannot be recovered from Trash."
        )
    else:
        message = f"Delete '{document}' from your reMarkable? This moves it to the trash."
    return Elicit(
        message,
        _DeleteConfirmation,
    )


def _delete_confirmation_abort(
    confirmation: ElicitationResult[_DeleteConfirmation],
    confirmation_available: bool,
    document: str,
) -> Optional[str]:
    """Return an abort response unless a destructive delete was confirmed.

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
    if not confirmation_available:
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
    if confirmation.action != "accept" or not getattr(confirmation.data, "confirm", False):
        return make_response(
            {"deleted": False, "cancelled": True, "document": document},
            "Delete cancelled — nothing was changed.",
        )
    return None


def _author_draw(
    document: str,
    page: int,
    strokes: list,
    ui_submitted: bool,
    defer_restart: bool = False,
) -> str:
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
    restarted = _maybe_restart_xochitl(ssh_client, defer_restart)
    _update_deferred_ssh_cache(ssh_client, restarted)
    _clear_document_extraction_cache(doc_uuid)

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
        "refresh_pending": not restarted,
    }
    if file_type.lower() == "epub":
        result["caveat"] = (
            "This is a reflowable EPUB; annotations are anchored to the "
            "current layout and may shift if the font size or layout changes."
        )
    hint = _write_result_message(
        restarted,
        (f"Appended {len(strokes)} stroke(s) to page {page} of '{target.VissibleName}'."),
        "Call remarkable_canvas(document, page) to view the updated page.",
    )
    return make_response(result, hint)


def _author_add_page(document: str, defer_restart: bool = False) -> str:
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
    restarted = _maybe_restart_xochitl(ssh_client, defer_restart)
    _update_deferred_ssh_cache(ssh_client, restarted)
    _clear_document_extraction_cache(doc_uuid)

    result = {
        "added": True,
        "document": target.VissibleName,
        "page_added": updated["page_index"],
        "total_pages": updated["total_pages"],
        "paper_size": list(nb.DEFAULT_PAPER),
        "transport": "ssh",
        "refresh_pending": not restarted,
    }
    hint = _write_result_message(
        restarted,
        (f"Added a blank page (now page {updated['page_index']} of {updated['total_pages']})."),
        (
            f"Call remarkable_canvas('{target.VissibleName}', "
            f"{updated['page_index']}) to draw on it."
        ),
    )
    return make_response(result, hint)


def _author_create_document(
    name: str,
    text: Optional[str],
    folder: Optional[str],
    defer_restart: bool = False,
) -> str:
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
    restarted = _maybe_restart_xochitl(ssh_client, defer_restart)
    cached_doc = Document(
        id=doc_uuid,
        hash=doc_uuid,
        name=name,
        doc_type="DocumentType",
        parent=parent_id,
        synced=False,
        last_modified=datetime.now(),
        local_path=f"{XOCHITL_PATH}/{doc_uuid}",
    )
    _update_deferred_ssh_cache(ssh_client, restarted, document=cached_doc, file_type="notebook")

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
        "refresh_pending": not restarted,
    }
    hint = _write_result_message(
        restarted,
        f"Created notebook '{name}'.",
        f"Call remarkable_canvas('{name}', 1) to view or draw on it.",
    )
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
        defer_restart: bool = False,
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
        - defer_restart: Skip the xochitl restart for this write. Call
          remarkable_refresh() once after the batch. The response sets
          refresh_pending=true while a refresh is owed.
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

        validation_error = _validate_author_request(
            method,
            document,
            page,
            strokes,
            name,
        )
        if validation_error:
            return validation_error

        def _impl() -> str:
            error = _require_write_transport()
            if error:
                return error

            if method == "draw":
                return _author_draw(document, page, strokes, ui_submitted, defer_restart)
            if method == "add_page":
                return _author_add_page(document, defer_restart)
            if method == "create_document":
                return _author_create_document(name, text, folder, defer_restart)
            raise AssertionError(f"Validated author method is unsupported: {method}")

        ssh_client = _get_ssh_client()
        if isinstance(ssh_client, SSHClient):
            return await _run_ssh_tool_operation(
                ssh_client,
                f"author-{method}",
                _impl,
                defer_restart=defer_restart,
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
        - USB web: uploaded via POST /upload; lands at the root (the multipart
          filename carries document_name because the endpoint has no rename field)

        Requires write mode (the default; disabled with --read-only).
        </instructions>
        <parameters>
        - file_path: Absolute path to the local PDF or EPUB file
        - parent_folder: Destination folder path on tablet (default: root "/").
          Honored in cloud and SSH modes; ignored by the USB web interface.
        - document_name: Display name on tablet (default: filename without
          extension). Honored in every mode; USB web uses it as the uploaded filename.
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
                    _upload_via_usb_web(file_path, document_name)

                    name = document_name or os.path.splitext(os.path.basename(file_path))[0]

                    # Clear cached documents
                    client = get_rmapi()
                    _invalidate_client_cache(client)

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

                cached_doc = Document(
                    id=doc_uuid,
                    hash=doc_uuid,
                    name=name,
                    doc_type="DocumentType",
                    parent=parent_id,
                    synced=False,
                    last_modified=datetime.now(),
                    local_path=f"{XOCHITL_PATH}/{doc_uuid}",
                )
                _update_deferred_ssh_cache(
                    ssh_client, restarted, document=cached_doc, file_type=ext
                )

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

            except SSHReliabilityError:
                raise
            except Exception as e:
                transport = "USB web" if _is_usb_web_mode() else "SSH"
                return make_error(
                    error_type="upload_failed",
                    message=f"Upload failed: {e}",
                    suggestion=f"Check {transport} connection and try again.",
                )

        if _is_ssh_mode():
            ssh_client = _get_ssh_client()
            if isinstance(ssh_client, SSHClient):
                return await _run_ssh_tool_operation(
                    ssh_client,
                    "upload",
                    _impl,
                    defer_restart=defer_restart,
                )
        if _is_usb_web_mode():
            usb_client = get_rmapi()
            run_operation = getattr(type(usb_client), "run_operation_async", None)
            if callable(run_operation):
                return await run_operation(usb_client, "upload", _impl)
        return await asyncio.to_thread(_impl)

    @mcp.tool(annotations=UPLOAD_ANNOTATIONS)
    async def remarkable_markdown_to_pdf(
        markdown: str,
        document_name: str = "Markdown",
        parent_folder: str = "/",
        defer_restart: bool = False,
    ) -> str:
        """
        <usecase>Render Markdown text as a PDF and upload it to the reMarkable tablet.</usecase>
        <instructions>
        Use this when the user wants notes, reports, summaries, or other Markdown
        content written back to the tablet without first creating a local file.
        Headings, lists, tables, links, blockquotes, and code blocks are rendered
        into a paginated A4 PDF. Raw HTML is escaped and image assets are omitted
        so rendering never reads local files or fetches remote content.

        Works in all three transports:
        - Cloud: sync upload with parent_folder and document_name
        - SSH: filesystem upload with parent_folder and document_name
        - USB web: root upload whose multipart filename is document_name + ".pdf"

        Requires write mode (the default; disabled with --read-only).
        </instructions>
        <parameters>
        - markdown: Markdown source text. Must not be empty.
        - document_name: Display name on the tablet (default: "Markdown").
        - parent_folder: Destination folder (default: root "/"). USB web always
          uploads to root because its firmware has no folder parameter.
        - defer_restart: SSH only. When True, skip the xochitl restart and call
          remarkable_refresh() once after the batch. Forwarded exactly like
          remarkable_upload; ignored in cloud and USB web modes.
        </parameters>
        <examples>
        - remarkable_markdown_to_pdf("# Meeting Notes\\n\\n- Follow up with Sam")
        - remarkable_markdown_to_pdf(
            "# Q4 Report\\n\\nRevenue increased **20%**.",
            document_name="Q4 Report",
            parent_folder="/Reports",
          )
        - remarkable_markdown_to_pdf(
            "# Batch item", document_name="Item 1", defer_restart=True
          )
          remarkable_refresh()
        </examples>
        """
        from remarkable_mcp.markdown_pdf import render_markdown_pdf

        try:
            pdf_bytes = await asyncio.to_thread(render_markdown_pdf, markdown)
        except Exception as e:
            return make_error(
                error_type="render_failed",
                message=f"Failed to render Markdown as PDF: {e}",
                suggestion="Provide non-empty Markdown text and try again.",
            )

        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = os.path.join(temp_dir, "document.pdf")
            with open(pdf_path, "wb") as pdf_file:
                pdf_file.write(pdf_bytes)
            return await remarkable_upload(
                pdf_path,
                parent_folder=parent_folder,
                document_name=document_name,
                defer_restart=defer_restart,
            )

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

                    cached_folder = Document(
                        id=doc_uuid,
                        hash=doc_uuid,
                        name=folder_name,
                        doc_type="CollectionType",
                        parent=parent_id,
                        synced=False,
                        last_modified=datetime.now(),
                        local_path=f"{XOCHITL_PATH}/{doc_uuid}",
                    )
                    _update_deferred_ssh_cache(ssh_client, restarted, document=cached_folder)

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

                except SSHReliabilityError:
                    raise
                except Exception as e:
                    return make_error(
                        error_type="mkdir_failed",
                        message=f"Failed to create folder: {e}",
                        suggestion="Check SSH connection and try again.",
                    )

            if _is_ssh_mode():
                ssh_client = _get_ssh_client()
                if isinstance(ssh_client, SSHClient):
                    return await _run_ssh_tool_operation(
                        ssh_client,
                        "mkdir",
                        _impl,
                        defer_restart=defer_restart,
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

                    target.parent = dest_id
                    target.last_modified = datetime.now()
                    _update_deferred_ssh_cache(ssh_client, restarted, document=target)

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

                except SSHReliabilityError:
                    raise
                except Exception as e:
                    return make_error(
                        error_type="move_failed",
                        message=f"Failed to move document: {e}",
                        suggestion="Check SSH connection and try again.",
                    )

            if _is_ssh_mode():
                ssh_client = _get_ssh_client()
                if isinstance(ssh_client, SSHClient):
                    return await _run_ssh_tool_operation(
                        ssh_client,
                        "move",
                        _impl,
                        defer_restart=defer_restart,
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

                    target.name = new_name
                    target.last_modified = datetime.now()
                    _update_deferred_ssh_cache(ssh_client, restarted, document=target)

                    return make_response(
                        {
                            "renamed": True,
                            "old_name": old_name,
                            "new_name": new_name,
                            "refresh_pending": not restarted,
                        },
                        _write_result_message(restarted, "Document renamed."),
                    )

                except SSHReliabilityError:
                    raise
                except Exception as e:
                    return make_error(
                        error_type="rename_failed",
                        message=f"Failed to rename document: {e}",
                        suggestion="Check SSH connection and try again.",
                    )

            if _is_ssh_mode():
                ssh_client = _get_ssh_client()
                if isinstance(ssh_client, SSHClient):
                    return await _run_ssh_tool_operation(
                        ssh_client,
                        "rename",
                        _impl,
                        defer_restart=defer_restart,
                    )
            return await asyncio.to_thread(_impl)

        @mcp.tool(annotations=DELETE_ANNOTATIONS)
        async def remarkable_delete(
            document: str,
            defer_restart: bool = False,
            permanent: bool = False,
            *,
            confirmation: Annotated[
                ElicitationResult[_DeleteConfirmation],
                Resolve(_resolve_delete_confirmation),
            ],
            confirmation_available: Annotated[bool, Resolve(_delete_confirmation_available)],
        ) -> str:
            """
            <usecase>Delete a document or folder on the reMarkable tablet.</usecase>
            <instructions>
            DESTRUCTIVE operation. By default the item is moved to the tablet's
            Trash and remains recoverable. In SSH mode only, permanent=True removes
            the resolved UUID and all sidecar files instead; use that only when the
            user explicitly asked for permanent deletion.

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
            - permanent: SSH only. Permanently remove the resolved document/folder
              files instead of moving the item to Trash (default: False).
            </parameters>
            <examples>
            - remarkable_delete("Old Notes")
            - remarkable_delete("/Books/Archive/Old Draft")
            </examples>
            """
            error = _require_managed_write_mode()
            if error:
                return error

            abort = _delete_confirmation_abort(confirmation, confirmation_available, document)
            if abort:
                return abort

            if _is_cloud_mode():
                if permanent:
                    return make_error(
                        error_type="permanent_delete_unsupported",
                        message="Permanent deletion is not supported in cloud mode.",
                        suggestion=(
                            "Delete normally to move the item to Trash, then empty "
                            "Trash from a reMarkable app or tablet."
                        ),
                    )
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

                    if permanent:
                        if target.is_folder and any(
                            item.Parent == target.ID for item in collection
                        ):
                            return make_error(
                                error_type="folder_not_empty",
                                message=(
                                    f"Cannot permanently delete non-empty folder "
                                    f"'{target.VissibleName}'."
                                ),
                                suggestion=(
                                    "Permanently delete its child documents and "
                                    "folders first, or omit permanent=True to move "
                                    "the whole folder to Trash."
                                ),
                            )
                        _permanently_delete_ssh_entry(ssh_client, target.ID)
                    else:
                        meta_content = ssh_client._scp_download(
                            f"{XOCHITL_PATH}/{target.ID}.metadata"
                        )
                        metadata = json.loads(meta_content.decode("utf-8"))
                        metadata["parent"] = "trash"
                        metadata["deleted"] = False
                        metadata["lastModified"] = str(int(time.time() * 1000))
                        metadata["metadatamodified"] = True
                        try:
                            metadata["version"] = int(metadata.get("version", 0)) + 1
                        except (TypeError, ValueError):
                            metadata["version"] = 1
                        _write_metadata(ssh_client, target.ID, metadata)

                    # Restart xochitl (unless deferred for a batch)
                    restarted = _maybe_restart_xochitl(ssh_client, defer_restart)

                    _update_deferred_ssh_cache(ssh_client, restarted, remove_id=target.ID)
                    _clear_document_extraction_cache(target.ID)

                    return make_response(
                        {
                            "deleted": True,
                            "name": target.VissibleName,
                            "path": doc_path,
                            "type": "folder" if target.is_folder else "document",
                            "permanent": permanent,
                            "refresh_pending": not restarted,
                        },
                        _write_result_message(
                            restarted,
                            (
                                "Document permanently deleted."
                                if permanent
                                else "Document moved to Trash."
                            ),
                            "It will no longer appear in the active library.",
                        ),
                    )

                except SSHReliabilityError:
                    raise
                except Exception as e:
                    return make_error(
                        error_type="delete_failed",
                        message=f"Failed to delete document: {e}",
                        suggestion="Check SSH connection and try again.",
                    )

            ssh_client = _get_ssh_client()
            if isinstance(ssh_client, SSHClient):
                return await _run_ssh_tool_operation(
                    ssh_client,
                    "delete",
                    _impl,
                    defer_restart=defer_restart,
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

        error = _require_write_transport()
        if error:
            return error

        ssh_client = None
        try:
            ssh_client = _get_ssh_client()
            if isinstance(ssh_client, SSHClient):
                await ssh_client._refresh_coordinator.refresh_explicit(
                    lambda: _refresh_ssh_client(ssh_client)
                )
            else:
                await asyncio.to_thread(_restart_xochitl, ssh_client)
                _invalidate_client_cache(ssh_client)
            return make_response(
                {"refreshed": True, "transport": "ssh", "refresh_pending": False},
                "xochitl restarted; any deferred changes are now visible. "
                "Use remarkable_browse() to verify.",
            )
        except Exception as e:
            if ssh_client is not None:
                _invalidate_client_cache(ssh_client)
            return make_error(
                error_type="refresh_failed",
                message=f"Failed to refresh tablet UI: {e}",
                suggestion="Check SSH connection and try again.",
            )

    # Refreshing is meaningful only for the SSH transport's xochitl restart;
    # cloud/USB clients have no equivalent, so the tool is hidden there.
    if _is_ssh_mode():
        mcp.tool(annotations=WRITE_ANNOTATIONS)(remarkable_refresh)
