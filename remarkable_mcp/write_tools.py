"""
Write tools for reMarkable MCP server.

These tools enable mutation of the reMarkable cloud library: uploading
documents, creating folders, moving, renaming and deleting items.

Implementation strategy
-----------------------
The reMarkable cloud sync v3/v4 protocol uses a content-addressed blob store
where every mutation requires computing SHA256 hashes of new index trees and
performing a conditional PUT against the root with generation matching. A pure
Python reimplementation of that protocol is large (rmapi is ~2000 lines of Go
just for the sync layer) and easy to corrupt a user's library with on the
first bug.

Instead, these tools shell out to the well known and battle tested
`ddvk/rmapi` Go CLI (https://github.com/ddvk/rmapi). The existing
`remarkable-mcp` README already credits rmapi as the inspiration for the read
path, and rmapi shares the exact same `~/.rmapi` token file that this server
uses, so no extra auth setup is needed.

Activation
----------
Write tools are OFF by default. They are registered only when:

    REMARKABLE_ENABLE_WRITE=1

is set in the environment, OR the server was launched with `--write` (handled
in cli.py). This keeps the default install read-only and matches the existing
server `instructions` text.

The location of the rmapi binary can be overridden with `RMAPI_BIN`
(default: `rmapi` from PATH).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from mcp.types import ToolAnnotations

from remarkable_mcp.responses import make_error as _make_error
from remarkable_mcp.responses import make_response as _make_response
from remarkable_mcp.server import mcp


def make_response(data: dict, hint: str = "Write operation completed.") -> str:
    return _make_response(data, hint)


def make_error(message: str) -> str:
    return _make_error(
        "WriteOperationFailed",
        message,
        "Check that rmapi is installed, the file exists, and the remote path is correct.",
    )


def write_enabled() -> bool:
    """Return True if write tools should be registered."""
    return os.environ.get("REMARKABLE_ENABLE_WRITE", "").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _rmapi_bin() -> Optional[str]:
    """Locate the rmapi binary."""
    explicit = os.environ.get("RMAPI_BIN")
    if explicit and Path(explicit).is_file():
        return explicit
    found = shutil.which("rmapi")
    if found:
        return found
    # Common install locations
    for candidate in (
        Path.home() / ".local" / "bin" / "rmapi",
        Path("/usr/local/bin/rmapi"),
        Path("/opt/homebrew/bin/rmapi"),
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def _run_rmapi(args: list[str], input_text: Optional[str] = None) -> tuple[int, str, str]:
    """Run rmapi with the given subcommand args.

    Returns (returncode, stdout, stderr).

    Inherits the user's environment so RMAPI_CONFIG, HOME, etc. work as
    expected. The rmapi binary uses ~/.rmapi (or $RMAPI_CONFIG) as its token
    store, the same file the read path uses.
    """
    binary = _rmapi_bin()
    if not binary:
        raise RuntimeError(
            "rmapi binary not found. Install ddvk/rmapi from "
            "https://github.com/ddvk/rmapi/releases and ensure it is on PATH "
            "or set RMAPI_BIN to its absolute path."
        )

    # rmapi reads its config from ~/.rmapi by default, but the read path of
    # remarkable-mcp historically uses that same file to store a raw JWT
    # device token (legacy rmapy format) which rmapi cannot parse. If the
    # caller hasn't set RMAPI_CONFIG and a YAML config exists at the
    # canonical location ~/.config/rmapi/rmapi.conf, point rmapi at it.
    env = os.environ.copy()
    if "RMAPI_CONFIG" not in env:
        canonical = Path.home() / ".config" / "rmapi" / "rmapi.conf"
        if canonical.is_file():
            env["RMAPI_CONFIG"] = str(canonical)

    cmd = [binary, *args]
    proc = subprocess.run(
        cmd,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _normalise_remote_path(path: str) -> str:
    """Normalise a remote reMarkable path: ensure leading /, strip trailing /."""
    if not path:
        return "/"
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return path


def _join_remote(parent: str, name: str) -> str:
    parent = _normalise_remote_path(parent)
    if parent == "/":
        return "/" + name
    return f"{parent}/{name}"


# ---------------------------------------------------------------------------
# Tool annotations
# ---------------------------------------------------------------------------

UPLOAD_ANNOTATIONS = ToolAnnotations(
    title="Upload File to reMarkable",
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)

MKDIR_ANNOTATIONS = ToolAnnotations(
    title="Create reMarkable Folder",
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
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


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def register_write_tools() -> None:
    """Register all write tools with the MCP server.

    Called from server.py during startup IF write mode is enabled.
    """

    @mcp.tool(annotations=UPLOAD_ANNOTATIONS)
    def remarkable_upload(
        file_path: str,
        parent_folder: str = "/",
        document_name: Optional[str] = None,
    ) -> dict:
        """Upload a local PDF or EPUB to the reMarkable cloud.

        Args:
            file_path: Absolute path to a local .pdf or .epub file.
            parent_folder: Remote folder to upload into. Use "/" for the root
                of the library. The folder must already exist; create it
                first with `remarkable_mkdir` if needed.
            document_name: Optional new name for the document on the tablet.
                If omitted, the local filename (without extension) is used.

        Returns:
            On success: { ok: True, remote_path, message }
            On failure: { ok: False, error }

        Notes:
            - Uses the same auth as the read tools (~/.rmapi).
            - The document appears on the tablet after the next sync (a few
              seconds when the tablet is online).
        """
        try:
            local = Path(file_path).expanduser().resolve()
            if not local.is_file():
                return make_error(f"Local file not found: {local}")
            if local.suffix.lower() not in (".pdf", ".epub"):
                return make_error(
                    f"Unsupported file type {local.suffix!r}. Only .pdf and .epub are supported."
                )

            parent = _normalise_remote_path(parent_folder)

            # rmapi `put` uploads the file into the given remote directory,
            # using the local filename (sans extension) as the document name.
            # If a custom document_name is requested, we stage a renamed copy
            # in a temp dir so the upload picks up the new name, then optionally
            # rename after the fact for safety.
            if document_name:
                import tempfile

                with tempfile.TemporaryDirectory() as tmp:
                    staged = Path(tmp) / f"{document_name}{local.suffix.lower()}"
                    shutil.copy2(local, staged)
                    rc, out, err = _run_rmapi(["put", str(staged), parent])
            else:
                rc, out, err = _run_rmapi(["put", str(local), parent])

            if rc != 0:
                return make_error(
                    f"rmapi put failed (exit {rc}): {err.strip() or out.strip()}"
                )

            final_name = document_name or local.stem
            remote_path = _join_remote(parent, final_name)
            return make_response(
                {
                    "ok": True,
                    "remote_path": remote_path,
                    "parent_folder": parent,
                    "document_name": final_name,
                    "message": f"Uploaded to {remote_path}",
                    "rmapi_output": out.strip(),
                }
            )
        except Exception as e:
            return make_error(f"remarkable_upload failed: {e}")

    @mcp.tool(annotations=MKDIR_ANNOTATIONS)
    def remarkable_mkdir(folder_name: str, parent: str = "/") -> dict:
        """Create a folder on the reMarkable tablet.

        Args:
            folder_name: Name of the new folder (no slashes).
            parent: Remote parent folder. Use "/" for the library root.

        Returns:
            { ok: True, remote_path } on success, or { ok: False, error }.

        If the folder already exists, rmapi will report an error which is
        surfaced unchanged.
        """
        try:
            if "/" in folder_name:
                return make_error("folder_name must not contain '/'.")
            parent_norm = _normalise_remote_path(parent)
            target = _join_remote(parent_norm, folder_name)
            rc, out, err = _run_rmapi(["mkdir", target])
            if rc != 0:
                return make_error(
                    f"rmapi mkdir failed (exit {rc}): {err.strip() or out.strip()}"
                )
            return make_response(
                {"ok": True, "remote_path": target, "message": f"Created {target}"}
            )
        except Exception as e:
            return make_error(f"remarkable_mkdir failed: {e}")

    @mcp.tool(annotations=MOVE_ANNOTATIONS)
    def remarkable_move(document: str, dest_folder: str) -> dict:
        """Move a document or folder to a different folder.

        Args:
            document: Full remote path of the item to move (e.g. "/Inbox/Foo").
            dest_folder: Full remote path of the destination folder.

        Returns:
            { ok: True, new_path } on success, or { ok: False, error }.
        """
        try:
            src = _normalise_remote_path(document)
            dst = _normalise_remote_path(dest_folder)
            rc, out, err = _run_rmapi(["mv", src, dst])
            if rc != 0:
                return make_error(
                    f"rmapi mv failed (exit {rc}): {err.strip() or out.strip()}"
                )
            new_path = _join_remote(dst, src.rsplit("/", 1)[-1])
            return make_response(
                {"ok": True, "new_path": new_path, "message": f"Moved {src} -> {new_path}"}
            )
        except Exception as e:
            return make_error(f"remarkable_move failed: {e}")

    @mcp.tool(annotations=RENAME_ANNOTATIONS)
    def remarkable_rename(document: str, new_name: str) -> dict:
        """Rename a document or folder.

        Args:
            document: Full remote path of the item to rename.
            new_name: New display name (no slashes).

        Returns:
            { ok: True, new_path } on success, or { ok: False, error }.

        Implementation note: rmapi does not expose a top-level `rename`
        subcommand on every release; this implementation moves the item to
        a sibling path with the new name, which is the rmapi-supported way
        to rename.
        """
        try:
            if "/" in new_name:
                return make_error("new_name must not contain '/'.")
            src = _normalise_remote_path(document)
            parent = src.rsplit("/", 1)[0] or "/"
            dst = _join_remote(parent, new_name)
            # rmapi mv accepts "mv SRC DST" where DST may be a new name in same dir
            rc, out, err = _run_rmapi(["mv", src, dst])
            if rc != 0:
                return make_error(
                    f"rmapi rename (mv) failed (exit {rc}): {err.strip() or out.strip()}"
                )
            return make_response(
                {"ok": True, "new_path": dst, "message": f"Renamed {src} -> {dst}"}
            )
        except Exception as e:
            return make_error(f"remarkable_rename failed: {e}")

    @mcp.tool(annotations=DELETE_ANNOTATIONS)
    def remarkable_delete(document: str, confirm: bool = False) -> dict:
        """Delete a document or folder from the reMarkable cloud.

        DESTRUCTIVE. Pass `confirm=True` to actually delete. Without `confirm`
        this tool returns a dry-run response describing what would happen.

        Args:
            document: Full remote path of the item to delete.
            confirm: Must be True to actually perform the delete.

        Returns:
            { ok: True, deleted } on success, { ok: False, error } on failure,
            or { ok: True, dry_run: True } when confirm is not set.
        """
        try:
            target = _normalise_remote_path(document)
            if not confirm:
                return make_response(
                    {
                        "ok": True,
                        "dry_run": True,
                        "would_delete": target,
                        "message": (
                            f"Dry run: would delete {target}. "
                            "Re-call with confirm=True to actually delete."
                        ),
                    }
                )
            rc, out, err = _run_rmapi(["rm", target])
            if rc != 0:
                return make_error(
                    f"rmapi rm failed (exit {rc}): {err.strip() or out.strip()}"
                )
            return make_response(
                {"ok": True, "deleted": target, "message": f"Deleted {target}"}
            )
        except Exception as e:
            return make_error(f"remarkable_delete failed: {e}")
