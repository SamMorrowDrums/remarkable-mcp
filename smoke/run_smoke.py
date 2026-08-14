#!/usr/bin/env python3
"""Deterministic, no-AI multi-transport MCP smoke test for remarkable-mcp.

This is the "first port of call" diagnostic: it drives the *real* MCP server over
the protocol (stdio, via the official ``mcp`` client SDK -- no AI, no models, no
mocks) and exercises EVERY available tool in EVERY available transport, in the
order:

    local  ->  cloud  ->  usb-web  ->  ssh

Why that order matters: local is read-only and side-effect-free so it runs
first; pushing files over SSH can reset the USB web interface, so SSH (the only
mode that writes to the device filesystem) runs LAST, after the USB-web checks
are done.

The server HIDES tools a transport cannot support (e.g. mkdir/move/rename/delete
are not registered over USB web; remarkable_author is SSH-only). The harness is
therefore ``tools/list``-driven: it asks each running server which tools it
actually exposes and only exercises those. Any tool in the master list that a
mode does not expose is reported as N/A for that mode -- which is the *correct*
behaviour, not a failure.

Each (mode, tool) cell is classified as one of:

    PASS  the tool ran and returned a sensible result for this transport
    N/A   the transport does not expose this tool (correctly hidden)
    SKIP  could not run (mode unavailable, or no target document to read)
    FAIL  the tool is exposed but errored or returned the wrong thing

A mode whose transport is not reachable (no cloud token, device unplugged, SSH
off) is reported as *unavailable* and all of its tools are SKIPped -- so a user
who only has one mode still gets a clean report for that mode.

Writes are confined to a unique per-run folder and cleaned up afterwards. Run
with ``--read-only`` to skip all write tools (pure connectivity/read check).

Usage:
    uv run python smoke/run_smoke.py                  # all available modes
    uv run python smoke/run_smoke.py --modes cloud    # one mode
    uv run python smoke/run_smoke.py --read-only      # no writes
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from mcp import ClientSession, StdioServerParameters

HERE = pathlib.Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
FIXTURE_PDF = HERE / "fixtures" / "smoke-doc.pdf"
# A known string embedded in the fixture PDF's text layer. The upload round-trip
# reads the freshly-uploaded document back and asserts this marker survived, so a
# "PASS" on upload means the bytes are actually retrievable -- not just that the
# upload call returned without error.
FIXTURE_MARKER = "reMarkable MCP Smoke Test"
SNAP_DIR = HERE / "snapshots"
USB_HOST = "10.11.99.1"

# Result states
PASS = "PASS"
FAIL = "FAIL"
NA = "N/A"
SKIP = "SKIP"

GLYPH = {PASS: "PASS  \u2705", FAIL: "FAIL  \u274c", NA: "N/A   \u26d4", SKIP: "SKIP  \u26a0\ufe0f"}

# The complete tool surface across all transports, in a stable display order.
# Not every mode exposes every tool; the harness discovers each mode's real
# surface via tools/list and marks the rest N/A.
ALL_TOOLS = [
    "remarkable_status",
    "remarkable_browse",
    "remarkable_recent",
    "remarkable_search",
    "remarkable_read",
    "remarkable_image",
    "remarkable_canvas",
    "remarkable_upload",
    "remarkable_mkdir",
    "remarkable_rename",
    "remarkable_move",
    "remarkable_author",
    "remarkable_refresh",
    "remarkable_delete",
]

READ_TOOLS = {
    "remarkable_browse",
    "remarkable_recent",
    "remarkable_search",
    "remarkable_read",
    "remarkable_image",
    "remarkable_canvas",
}
WRITE_TOOLS = {
    "remarkable_upload",
    "remarkable_mkdir",
    "remarkable_rename",
    "remarkable_move",
    "remarkable_author",
    "remarkable_refresh",
    "remarkable_delete",
}

# Per-mode launch spec. Every mode gets REMARKABLE_SKIP_CONFIRM=1 so the delete
# tool's confirmation (which refuses without MCP elicitation) does not block this
# non-interactive harness -- this also validates the documented automation escape
# hatch. usb/ssh disable cloud fallback so a dead device cannot silently pass via
# cloud.
MODES = {
    "local": {
        "transport": "local-dir",
        "args": ["--local-dir"],
        "env": {
            "REMARKABLE_USE_LOCAL_DIR": "1",
            "REMARKABLE_DISABLE_CLOUD_FALLBACK": "1",
        },
        "probe_port": None,
    },
    "cloud": {
        "transport": "cloud",
        "args": [],
        "env": {},
        "probe_port": None,
    },
    "usb": {
        "transport": "usb-web",
        "args": ["--usb"],
        "env": {
            "REMARKABLE_USE_USB_WEB": "1",
            "REMARKABLE_DISABLE_CLOUD_FALLBACK": "1",
        },
        "probe_port": 80,
    },
    "ssh": {
        "transport": "ssh",
        "args": ["--ssh"],
        "env": {
            "REMARKABLE_USE_SSH": "1",
            "REMARKABLE_DISABLE_CLOUD_FALLBACK": "1",
            # A smoke run is a write batch. Restart xochitl once after cleanup
            # rather than after every operation, which would force repeated
            # full-library rescans and obscure sequencing failures.
            "REMARKABLE_DEFER_RESTART": "1",
        },
        "probe_port": 22,
    },
}

# Per-call timeouts (seconds). Rendering and the on-device probe can be slow.
TIMEOUTS = {
    "remarkable_status": 60,
    "remarkable_image": 120,
    "remarkable_canvas": 120,
    "remarkable_author": 120,
    "remarkable_refresh": 120,
    "remarkable_upload": 120,
    "remarkable_delete": 90,
    "_default": 60,
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_text(path: pathlib.Path) -> str | None:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


def _process_tree(root_pid: int) -> set[int]:
    """Return the live Linux process tree rooted at ``root_pid``."""
    found = {root_pid}
    pending = [root_pid]
    while pending:
        pid = pending.pop()
        children = _read_text(pathlib.Path(f"/proc/{pid}/task/{pid}/children"))
        if not children:
            continue
        for value in children.split():
            child = int(value)
            if child not in found:
                found.add(child)
                pending.append(child)
    return found


def _process_sample(root_pid: int) -> dict[str, Any]:
    total_rss_kb = 0
    processes = []
    for pid in sorted(_process_tree(root_pid)):
        status = _read_text(pathlib.Path(f"/proc/{pid}/status"))
        if not status:
            continue
        name_match = re.search(r"^Name:\s+(.+)$", status, re.MULTILINE)
        rss_match = re.search(r"^VmRSS:\s+(\d+)\s+kB$", status, re.MULTILINE)
        rss_kb = int(rss_match.group(1)) if rss_match else 0
        total_rss_kb += rss_kb
        processes.append(
            {
                "pid": pid,
                "name": name_match.group(1) if name_match else "unknown",
                "rss_kb": rss_kb,
            }
        )
    return {"total_rss_kb": total_rss_kb, "processes": processes}


class ProcessDiagnostics:
    """Capture the stdio server process lifetime without changing the MCP SDK."""

    def __init__(self):
        self.process = None
        self._sampler = None
        self._stop = asyncio.Event()
        self.max_tree_rss_kb = 0
        self.max_tree_processes = []
        self.stderr = ""

    def attach(self, process) -> None:
        self.process = process
        self._sampler = asyncio.create_task(self._sample_loop())

    async def _sample_loop(self) -> None:
        while not self._stop.is_set():
            if self.process is not None:
                sample = _process_sample(self.process.pid)
                if sample["total_rss_kb"] > self.max_tree_rss_kb:
                    self.max_tree_rss_kb = sample["total_rss_kb"]
                    self.max_tree_processes = sample["processes"]
            try:
                await asyncio.wait_for(self._stop.wait(), 0.25)
            except asyncio.TimeoutError:
                pass

    async def finish(self, errlog) -> None:
        self._stop.set()
        if self._sampler is not None:
            await self._sampler
        errlog.flush()
        errlog.seek(0)
        self.stderr = errlog.read()

    def snapshot(self) -> dict[str, Any]:
        returncode = self.process.returncode if self.process is not None else None
        signal = -returncode if isinstance(returncode, int) and returncode < 0 else None
        stderr_lines = self.stderr.splitlines()
        return {
            "pid": self.process.pid if self.process is not None else None,
            "returncode": returncode,
            "signal": signal,
            "max_process_tree_rss_kb": self.max_tree_rss_kb,
            "max_process_tree": self.max_tree_processes,
            "stderr_tail": stderr_lines[-500:],
            "python_traceback": any(
                "Traceback (most recent call last)" in line for line in stderr_lines
            ),
        }


@asynccontextmanager
async def diagnostic_stdio_client(params):
    """Wrap MCP's stdio client while retaining its otherwise-hidden child process."""
    import mcp.client.stdio as stdio_module

    diagnostics = ProcessDiagnostics()
    original_create = stdio_module._create_platform_compatible_process

    async def _capture_process(**kwargs):
        process = await original_create(**kwargs)
        diagnostics.attach(process)
        return process

    stdio_module._create_platform_compatible_process = _capture_process
    try:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog:
            try:
                async with stdio_module.stdio_client(params, errlog=errlog) as streams:
                    yield (*streams, diagnostics)
            finally:
                await diagnostics.finish(errlog)
    finally:
        stdio_module._create_platform_compatible_process = original_create


def _run_diagnostic_command(args: list[str], timeout: int = 10) -> dict[str, Any]:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def _host_diagnostics() -> dict[str, Any]:
    meminfo = _read_text(pathlib.Path("/proc/meminfo")) or ""
    selected_mem = {}
    for key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
        match = re.search(rf"^{key}:\s+(.+)$", meminfo, re.MULTILINE)
        if match:
            selected_mem[key] = match.group(1)

    cgroup = {}
    for path in (
        pathlib.Path("/sys/fs/cgroup/memory.events"),
        pathlib.Path("/sys/fs/cgroup/memory.max"),
        pathlib.Path("/sys/fs/cgroup/memory.current"),
        pathlib.Path("/sys/fs/cgroup/memory/memory.oom_control"),
        pathlib.Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
        pathlib.Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"),
    ):
        value = _read_text(path)
        if value is not None:
            cgroup[str(path)] = value.strip()

    kernel = _run_diagnostic_command(
        [
            "journalctl",
            "-k",
            "--since",
            "10 minutes ago",
            "--no-pager",
            "--grep",
            "oom-kill|out of memory|killed process",
        ]
    )
    return {"memory": selected_mem, "cgroup": cgroup, "kernel_oom": kernel}


def _tablet_diagnostics() -> dict[str, Any]:
    """Collect read-only load/memory/SSH state in one additional connection."""
    command = (
        "printf 'uptime='; uptime; "
        "printf 'loadavg='; cat /proc/loadavg; "
        "printf 'memory_kb\\n'; sed -n "
        "'/MemTotal:/p;/MemAvailable:/p;/SwapTotal:/p;/SwapFree:/p' /proc/meminfo; "
        "printf 'processes\\n'; "
        "ps -eo pid,comm,rss,stat --sort=-rss 2>/dev/null | head -n 12; "
        "printf 'sshd='; systemctl is-active sshd 2>/dev/null || "
        "systemctl is-active dropbear 2>/dev/null || true; "
        "printf 'xochitl='; systemctl is-active xochitl 2>/dev/null || true; "
        "printf 'kernel_oom\\n'; "
        "journalctl -k --since '-10 min' --no-pager 2>/dev/null | "
        "grep -Ei 'oom-kill|out of memory|killed process' | tail -20 || true"
    )
    args = [
        "ssh",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]
    key_path = os.environ.get("REMARKABLE_SSH_KEY")
    if key_path:
        args.extend(["-i", os.path.expanduser(key_path), "-o", "IdentitiesOnly=yes"])
    args.extend(
        [
            "-p",
            os.environ.get("REMARKABLE_SSH_PORT", "22"),
            (
                f"{os.environ.get('REMARKABLE_SSH_USER', 'root')}@"
                f"{os.environ.get('REMARKABLE_SSH_HOST', USB_HOST)}"
            ),
            command,
        ]
    )
    password = os.environ.get("REMARKABLE_SSH_PASSWORD")
    if password:
        args = ["sshpass", "-p", password, *args]
    else:
        args[1:1] = ["-o", "BatchMode=yes"]
    return _run_diagnostic_command(args, timeout=15)


def _cloud_token_present() -> bool:
    if os.environ.get("REMARKABLE_TOKEN"):
        return True
    return (pathlib.Path.home() / ".rmapi").exists()


def _tcp_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _mode_prescreen(mode: str) -> tuple[bool, str]:
    """Cheap reachability check before spawning the server (avoids long hangs)."""
    spec = MODES[mode]
    if mode == "local":
        from remarkable_mcp.local_dir import check_local_dir_available

        base_dir = os.environ.get("REMARKABLE_LOCAL_DIR")
        if check_local_dir_available(base_dir):
            return True, "local data directory present"
        return False, (
            "no local data directory found (install/sign in to the reMarkable "
            "desktop app, or set REMARKABLE_LOCAL_DIR)"
        )
    if mode == "cloud":
        if _cloud_token_present():
            return True, "cloud token present"
        return False, "no cloud token (REMARKABLE_TOKEN unset and ~/.rmapi missing)"
    port = spec["probe_port"]
    if _tcp_open(USB_HOST, port):
        return True, f"{USB_HOST}:{port} reachable"
    label = "USB web interface" if mode == "usb" else "SSH"
    return False, f"{label} not reachable at {USB_HOST}:{port} (device unplugged or disabled)"


def _parse_payload(result) -> dict:
    """Extract the tool's JSON payload from a CallToolResult, if any."""
    for content in getattr(result, "content", []) or []:
        text = getattr(content, "text", None)
        if text:
            try:
                return json.loads(text)
            except (json.JSONDecodeError, TypeError):
                continue
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    return {}


def _error_type(payload: dict) -> str | None:
    err = payload.get("_error") if isinstance(payload, dict) else None
    if isinstance(err, dict):
        return err.get("type")
    return None


class Recorder:
    """Holds the per-mode/per-tool results plus run metadata."""

    def __init__(self):
        self.modes: dict[str, dict] = {}

    def mode_unavailable(self, mode: str, reason: str):
        prior = self.modes.get(mode, {})
        self.modes[mode] = {**prior, "available": False, "reason": reason, "tools": {}}
        for tool in ALL_TOOLS:
            self.modes[mode]["tools"][tool] = {"state": SKIP, "note": "mode unavailable"}

    def ensure_mode(self, mode: str, transport: str, doc_count, reason: str):
        prior = self.modes.get(mode, {})
        self.modes[mode] = {
            **prior,
            "available": True,
            "transport": transport,
            "document_count": doc_count,
            "reason": reason,
            "tools": {},
        }

    def record(self, mode: str, tool: str, state: str, note: str = "", detail=None):
        entry = {"state": state, "note": note}
        if detail is not None:
            entry["detail"] = detail
        self.modes[mode]["tools"][tool] = entry


async def call_tool(session, tool, args, timeout):
    """Call a tool, returning (payload, is_error, exception)."""
    try:
        result = await asyncio.wait_for(session.call_tool(tool, args), timeout)
    except asyncio.TimeoutError:
        return {}, True, f"timed out after {timeout}s"
    except Exception as exc:  # noqa: BLE001 - report any client/transport error
        return {}, True, f"{type(exc).__name__}: {exc}"
    payload = _parse_payload(result)
    return payload, bool(getattr(result, "isError", False)), None


def classify_ok(payload, is_error, exc, ok_error_types=()):
    """Classify a tool that is EXPECTED to work in this transport.

    ``ok_error_types`` lists error types that still count as PASS (e.g. a search
    with no matching documents legitimately returns ``no_documents_found``).
    Returns (state, note).
    """
    err_type = _error_type(payload)
    if exc:
        return FAIL, exc
    if err_type:
        if err_type in ok_error_types:
            return PASS, f"benign error: {err_type}"
        return FAIL, f"error: {err_type}"
    if is_error:
        return FAIL, "tool reported isError"
    return PASS, ""


def _detail_for(tool, payload):
    """A compact, human-useful snippet for the snapshot/report."""
    if not isinstance(payload, dict):
        return None
    if tool == "remarkable_status":
        keys = ("transport", "status", "document_count", "write_enabled", "fell_back_to_cloud")
        return {k: payload.get(k) for k in keys if k in payload}
    if tool in ("remarkable_browse", "remarkable_recent", "remarkable_search"):
        return {"count": payload.get("count")}
    err = _error_type(payload)
    if err:
        return {"error_type": err}
    return None


async def verify_upload_roundtrip(session, doc_path):
    """Read a just-uploaded fixture back and confirm its known text survived.

    Returns ``(ok, note)``. This makes the upload check end-to-end: the cloud
    client invalidates its in-memory document cache on every root commit and
    blobs are content-addressed (immutable), so a managed upload is readable in
    the same session with no eventual-consistency window to wait on.
    """
    payload, is_err, exc = await call_tool(
        session,
        "remarkable_read",
        {"document": doc_path, "content_type": "raw", "page": 1},
        TIMEOUTS["_default"],
    )
    if exc:
        return False, f"read-back errored: {exc}"
    err_type = _error_type(payload)
    if err_type:
        return False, f"read-back error: {err_type}"
    content = payload.get("content", "") if isinstance(payload, dict) else ""
    if FIXTURE_MARKER in content:
        return True, "round-trip OK (read-back content matched)"
    return False, "read-back found the doc but its known text was missing/garbled"


async def run_read_phase(session, mode, rec, registered):
    """browse, recent, search, read, image, canvas -- expected OK where exposed."""
    # browse
    if "remarkable_browse" in registered:
        payload, is_err, exc = await call_tool(
            session, "remarkable_browse", {"path": "/"}, TIMEOUTS["_default"]
        )
        state, note = classify_ok(payload, is_err, exc)
        rec.record(
            mode, "remarkable_browse", state, note, _detail_for("remarkable_browse", payload)
        )

    # recent -- also used to pick a read/render target
    target = None
    if "remarkable_recent" in registered:
        payload, is_err, exc = await call_tool(
            session, "remarkable_recent", {"limit": 5}, TIMEOUTS["_default"]
        )
        state, note = classify_ok(payload, is_err, exc)
        rec.record(
            mode, "remarkable_recent", state, note, _detail_for("remarkable_recent", payload)
        )
        docs = payload.get("documents") if isinstance(payload, dict) else None
        if docs:
            target = docs[0].get("path") or docs[0].get("name")

    # search -- use a unique no-match query so we validate the tool cheaply
    # (a match would trigger up to 5 full document downloads). Both a real match
    # and a clean "no documents found" prove the search path works.
    if "remarkable_search" in registered:
        query = f"zzzsmoke-no-match-{int(time.time())}"
        payload, is_err, exc = await call_tool(
            session, "remarkable_search", {"query": query}, TIMEOUTS["_default"]
        )
        state, note = classify_ok(payload, is_err, exc, ok_error_types=("no_documents_found",))
        rec.record(
            mode, "remarkable_search", state, note, _detail_for("remarkable_search", payload)
        )

    # read / image / canvas need a target document
    for tool, call_args in (
        ("remarkable_read", {"content_type": "text", "page": 1}),
        ("remarkable_image", {"page": 1}),
        ("remarkable_canvas", {"page": 1}),
    ):
        if tool not in registered:
            continue
        if not target:
            rec.record(mode, tool, SKIP, "no document available to read in this mode")
            continue
        a = {"document": target, **call_args}
        payload, is_err, exc = await call_tool(
            session, tool, a, TIMEOUTS.get(tool, TIMEOUTS["_default"])
        )
        state, note = classify_ok(payload, is_err, exc)
        if not note and target:
            note = f"target: {target}"
        rec.record(mode, tool, state, note, _detail_for(tool, payload))


async def run_write_phase(session, mode, rec, registered):
    """upload, mkdir, rename, move, author, delete -- only the exposed ones.

    All created items are confined to a unique per-run folder and cleaned up.
    Tools that are not exposed in this transport were already recorded N/A.

    In managed modes (cloud/ssh) the upload is verified end-to-end: the fixture
    is read back and its known text confirmed, so upload PASS means the bytes are
    actually retrievable. USB-web uploads land at the device root with the name
    ignored, so they can't be reliably re-targeted and are not round-tripped.
    """
    is_ssh = MODES[mode]["transport"] == "ssh"
    managed = "remarkable_mkdir" in registered  # folder ops exposed -> cloud/ssh
    runid = f"smoke-{mode}-{int(time.time())}"
    folder_path = f"/{runid}"

    created = []  # (kind, path) to clean up at the end, children before parents

    # Unique per-run upload payload so the USB-web upload (which lands at root and
    # ignores document_name) is identifiable for later SSH cleanup.
    tmp_pdf = pathlib.Path(tempfile.gettempdir()) / f"{runid}-doc.pdf"
    shutil.copyfile(FIXTURE_PDF, tmp_pdf)

    try:
        # --- mkdir -------------------------------------------------------
        upload_parent = "/"
        if "remarkable_mkdir" in registered:
            payload, is_err, exc = await call_tool(
                session,
                "remarkable_mkdir",
                {"folder_name": runid, "parent": "/"},
                TIMEOUTS["_default"],
            )
            state, note = classify_ok(payload, is_err, exc)
            rec.record(mode, "remarkable_mkdir", state, note)
            if state == PASS:
                created.append(("folder", folder_path))
                upload_parent = folder_path

        # --- upload ------------------------------------------------------
        if "remarkable_upload" in registered:
            up_args = {"file_path": str(tmp_pdf)}
            if managed:
                up_args["parent_folder"] = upload_parent
                up_args["document_name"] = f"{runid}-doc"
            payload, is_err, exc = await call_tool(
                session, "remarkable_upload", up_args, TIMEOUTS["remarkable_upload"]
            )
            state, note = classify_ok(payload, is_err, exc)
            uploaded_path = f"{upload_parent.rstrip('/')}/{runid}-doc"
            if state == PASS and not managed:
                note = (
                    "uploaded to device root (USB web ignores name/folder); "
                    "swept in the SSH phase if SSH is available this run"
                )
            elif state == PASS and managed:
                # End-to-end check: read the just-uploaded fixture back and
                # confirm its known text survived. A successful-but-unreadable
                # upload is a real failure worth surfacing on this row.
                ok, rt_note = await verify_upload_roundtrip(session, uploaded_path)
                if ok:
                    note = f"uploaded to {uploaded_path}; {rt_note}"
                else:
                    state = FAIL
                    note = f"upload call returned OK but {rt_note}"
            rec.record(
                mode, "remarkable_upload", state, note, _detail_for("remarkable_upload", payload)
            )
            if state == PASS:
                if managed:
                    created.append(("doc", uploaded_path))
                else:
                    rec.modes[mode].setdefault("usb_root_leftover", f"/{runid}-doc")
            elif managed and is_err is False and exc is None:
                # Upload itself succeeded (only the read-back failed), so the doc
                # still exists and must be cleaned up despite the FAIL verdict.
                created.append(("doc", uploaded_path))

        # --- rename ------------------------------------------------------
        renamed_path = None
        if "remarkable_rename" in registered:
            doc_entry = next((p for k, p in created if k == "doc"), None)
            if doc_entry:
                new_name = f"{runid}-renamed"
                payload, is_err, exc = await call_tool(
                    session,
                    "remarkable_rename",
                    {"document": doc_entry, "new_name": new_name},
                    TIMEOUTS["_default"],
                )
                state, note = classify_ok(payload, is_err, exc)
                rec.record(mode, "remarkable_rename", state, note)
                if state == PASS:
                    renamed_path = f"{upload_parent.rstrip('/')}/{new_name}"
                    created = [(k, renamed_path if k == "doc" else p) for k, p in created]
            else:
                rec.record(mode, "remarkable_rename", SKIP, "no uploaded document to rename")

        # --- move --------------------------------------------------------
        if "remarkable_move" in registered:
            if renamed_path:
                payload, is_err, exc = await call_tool(
                    session,
                    "remarkable_move",
                    {"document": renamed_path, "dest_folder": "/"},
                    TIMEOUTS["_default"],
                )
                state, note = classify_ok(payload, is_err, exc)
                rec.record(mode, "remarkable_move", state, note)
                if state == PASS:
                    moved_path = f"/{runid}-renamed"
                    created = [(k, moved_path if k == "doc" else p) for k, p in created]
            else:
                rec.record(mode, "remarkable_move", SKIP, "no document to move")

        # --- author (SSH only) ------------------------------------------
        if "remarkable_author" in registered:
            nb_name = f"{runid}-nb"
            payload, is_err, exc = await call_tool(
                session,
                "remarkable_author",
                {"method": "create_document", "name": nb_name, "folder": folder_path},
                TIMEOUTS["remarkable_author"],
            )
            state, note = classify_ok(payload, is_err, exc)
            nb_path = f"{folder_path}/{nb_name}"
            if state == PASS:
                created.insert(0, ("doc", nb_path))  # delete before its folder
                ap_payload, ap_err, ap_exc = await call_tool(
                    session,
                    "remarkable_author",
                    {"method": "add_page", "document": nb_path},
                    TIMEOUTS["remarkable_author"],
                )
                ap_state, _ = classify_ok(ap_payload, ap_err, ap_exc)
                dr_payload, dr_err, dr_exc = await call_tool(
                    session,
                    "remarkable_author",
                    {
                        "method": "draw",
                        "document": nb_path,
                        "page": 1,
                        "strokes": [
                            {
                                "points": [[0.1, 0.5], [0.9, 0.5]],
                                "tool": "fineliner",
                                "color": "black",
                            }
                        ],
                    },
                    TIMEOUTS["remarkable_author"],
                )
                dr_state, _ = classify_ok(dr_payload, dr_err, dr_exc)
                sub = f"create_document=PASS; add_page={ap_state}; draw={dr_state}"
                final = PASS if (ap_state == PASS and dr_state == PASS) else FAIL
                rec.record(mode, "remarkable_author", final, sub)
            else:
                rec.record(mode, "remarkable_author", state, note)

        # --- delete (cleanup) -------------------------------------------
        if "remarkable_delete" in registered:
            # Also sweep any USB-web leftover from an earlier usb phase this run.
            if is_ssh:
                for data in rec.modes.values():
                    leftover = data.get("usb_root_leftover")
                    if leftover:
                        created.append(("doc", leftover))
                        data.pop("usb_root_leftover", None)

            delete_states = []
            for _kind, path in created:
                payload, is_err, exc = await call_tool(
                    session, "remarkable_delete", {"document": path}, TIMEOUTS["remarkable_delete"]
                )
                st, _ = classify_ok(payload, is_err, exc)
                delete_states.append((path, st))
            if not delete_states:
                rec.record(mode, "remarkable_delete", SKIP, "nothing was created to delete")
            else:
                failed = [p for p, s in delete_states if s != PASS]
                if failed:
                    rec.record(mode, "remarkable_delete", FAIL, f"could not delete: {failed}")
                else:
                    rec.record(
                        mode, "remarkable_delete", PASS, f"deleted {len(delete_states)} item(s)"
                    )

        # SSH writes above are intentionally deferred. Refresh exactly once,
        # after cleanup metadata is on disk, then the mode-level tablet probe
        # confirms a fresh SSH session still succeeds after the xochitl restart.
        if is_ssh and "remarkable_refresh" in registered:
            payload, is_err, exc = await call_tool(
                session, "remarkable_refresh", {}, TIMEOUTS["remarkable_refresh"]
            )
            state, note = classify_ok(payload, is_err, exc)
            rec.record(
                mode, "remarkable_refresh", state, note, _detail_for("remarkable_refresh", payload)
            )
    finally:
        tmp_pdf.unlink(missing_ok=True)


def _mark_hidden_tools_na(mode, rec, registered):
    """Any master-list tool not exposed by this mode is correctly hidden -> N/A."""
    for tool in ALL_TOOLS:
        if tool == "remarkable_status":
            continue  # status is recorded explicitly once it succeeds
        if tool not in registered:
            rec.record(mode, tool, NA, "not exposed in this transport (correctly hidden)")


async def run_mode(mode: str, rec: Recorder, read_only: bool):
    ok, reason = _mode_prescreen(mode)
    if not ok:
        rec.mode_unavailable(mode, reason)
        return

    spec = MODES[mode]
    env = {
        **os.environ,
        "PYTHONFAULTHANDLER": "1",
        "REMARKABLE_SKIP_CONFIRM": "1",
        "REMARKABLE_SSH_TRACE": "1",
        **spec["env"],
    }
    server_args = ["server.py", *spec["args"]]
    if read_only:
        server_args.append("--read-only")
    params = StdioServerParameters(
        # The smoke harness itself runs under ``uv run``, so sys.executable is
        # already the project interpreter. Launch it directly rather than through
        # another uv wrapper: the process handle then exposes the Python server's
        # real return code/signal instead of uv's success-shaped wrapper status.
        command=sys.executable,
        args=server_args,
        env=env,
        cwd=str(REPO_ROOT),
    )

    diagnostics = None
    if mode == "ssh":
        rec.modes.setdefault(mode, {})["tablet_before"] = _tablet_diagnostics()
    try:
        async with diagnostic_stdio_client(params) as (read, write, diagnostics):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), 60)

                # status decides availability for real (handles fallback / auth).
                payload, is_err, exc = await call_tool(
                    session, "remarkable_status", {}, TIMEOUTS["remarkable_status"]
                )
                authed = isinstance(payload, dict) and payload.get("authenticated") is True
                transport = payload.get("transport") if isinstance(payload, dict) else None
                fell_back = (
                    bool(payload.get("fell_back_to_cloud")) if isinstance(payload, dict) else False
                )
                want_transport = spec["transport"]

                if exc or not authed or transport != want_transport or fell_back:
                    detail = exc or (
                        f"authenticated={authed} transport={transport} "
                        f"expected={want_transport} fell_back={fell_back}"
                    )
                    rec.mode_unavailable(mode, f"status check failed: {detail}")
                    return

                rec.ensure_mode(mode, transport, payload.get("document_count"), "connected")
                rec.record(
                    mode, "remarkable_status", PASS, "", _detail_for("remarkable_status", payload)
                )

                # tools/list is the source of truth for what this mode supports.
                tools_result = await asyncio.wait_for(session.list_tools(), 30)
                registered = {t.name for t in tools_result.tools}
                rec.modes[mode]["registered_tools"] = sorted(registered)
                _mark_hidden_tools_na(mode, rec, registered)

                await run_read_phase(session, mode, rec, registered)
                if read_only:
                    for tool in WRITE_TOOLS:
                        if tool in registered:
                            rec.record(mode, tool, SKIP, "write phase skipped (--read-only)")
                else:
                    await run_write_phase(session, mode, rec, registered)
    except Exception as exc:  # noqa: BLE001
        if mode not in rec.modes or not rec.modes[mode].get("available"):
            rec.mode_unavailable(mode, f"server launch/session error: {type(exc).__name__}: {exc}")
        else:
            rec.modes[mode]["session_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        data = rec.modes.setdefault(mode, {})
        if diagnostics is not None:
            data["server_process"] = diagnostics.snapshot()
        data["host_after"] = _host_diagnostics()
        if mode == "ssh":
            data["tablet_after"] = _tablet_diagnostics()


def print_report(rec: Recorder, modes: list[str], started: str) -> bool:
    """Print a human-readable report. Returns True if any FAIL occurred."""
    any_fail = False
    line = "=" * 72
    print(f"\n{line}\nreMarkable MCP -- multi-transport smoke test\nstarted: {started}\n{line}")

    for mode in modes:
        data = rec.modes.get(mode, {})
        print(f"\n### MODE: {mode}")
        process = data.get("server_process", {})
        returncode = process.get("returncode")
        process_failed = returncode not in (None, 0)
        if process_failed:
            any_fail = True
            signal = process.get("signal")
            suffix = f" (signal {signal})" if signal is not None else ""
            print(f"    SERVER PROCESS EXIT: returncode={returncode}{suffix}")

        tablet_after = data.get("tablet_after")
        tablet_probe_failed = isinstance(tablet_after, dict) and (
            tablet_after.get("error") is not None or tablet_after.get("returncode") not in (None, 0)
        )
        if tablet_probe_failed:
            any_fail = True
            detail = tablet_after.get("error") or tablet_after.get("stderr") or "unknown error"
            print(f"    POST-RUN TABLET PROBE FAILED: {detail}")

        if not data.get("available"):
            print(f"  UNAVAILABLE -- {data.get('reason', 'unknown')}  (all tools SKIPped)")
            continue
        print(
            f"  available -- transport={data.get('transport')} "
            f"documents={data.get('document_count')}"
        )
        for tool in ALL_TOOLS:
            entry = data["tools"].get(tool, {"state": SKIP, "note": "not run"})
            st = entry["state"]
            if st == FAIL:
                any_fail = True
            note = entry.get("note", "")
            print(f"    {GLYPH.get(st, st):10} {tool:22} {note}")
        session_error = data.get("session_error")
        if session_error:
            any_fail = True
            print(f"    SERVER SESSION ERROR: {session_error}")

    # Matrix summary
    print(f"\n{line}\nSUMMARY MATRIX (rows=tools, cols=modes)\n{line}")
    header = "  {:22}".format("tool") + "".join(f"{m:>10}" for m in modes)
    print(header)
    short = {PASS: "PASS", FAIL: "FAIL", NA: "N/A", SKIP: "SKIP"}
    for tool in ALL_TOOLS:
        row = "  {:22}".format(tool)
        for mode in modes:
            data = rec.modes.get(mode, {})
            st = data.get("tools", {}).get(tool, {}).get("state", SKIP)
            row += f"{short.get(st, st):>10}"
        print(row)

    print(line)
    verdict = "FAIL \u274c" if any_fail else "PASS \u2705"
    print(f"OVERALL: {verdict}  (FAIL = a tool misbehaved; N/A and SKIP are not failures)")
    print(line)
    return any_fail


async def main_async(args) -> int:
    rec = Recorder()
    started = _now()
    modes = args.modes
    for mode in modes:  # strict order: cloud -> usb -> ssh
        await run_mode(mode, rec, args.read_only)

    any_fail = print_report(rec, modes, started)

    SNAP_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snap = SNAP_DIR / f"smoke-{stamp}.json"
    snap.write_text(
        json.dumps(
            {
                "started": started,
                "finished": _now(),
                "read_only": args.read_only,
                "modes": modes,
                "results": rec.modes,
            },
            indent=2,
        )
    )
    print(f"\nFull snapshot: {snap.relative_to(REPO_ROOT)}")
    return 1 if any_fail else 0


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--modes",
        default="local,cloud,usb,ssh",
        help="Comma-separated subset of modes in run order (default: local,cloud,usb,ssh)",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Skip all write tools (upload/mkdir/rename/move/author/delete)",
    )
    args = parser.parse_args()

    # local runs first: it is read-only and side-effect-free. SSH still runs
    # last because its writes can reset the USB web interface.
    order = ["local", "cloud", "usb", "ssh"]
    requested = [m.strip() for m in args.modes.split(",") if m.strip()]
    unknown = [m for m in requested if m not in MODES]
    if unknown:
        parser.error(f"unknown mode(s): {unknown}. Choose from {list(MODES)}")
    # Always honour local -> cloud -> usb -> ssh ordering regardless of input order.
    args.modes = [m for m in order if m in requested]

    if not FIXTURE_PDF.exists():
        print(f"ERROR: fixture missing: {FIXTURE_PDF}", file=sys.stderr)
        return 2

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
