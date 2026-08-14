"""
reMarkable MCP Server initialization.
"""

import logging
import os
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from ipaddress import ip_address
from typing import AsyncIterator
from urllib.parse import quote, unquote

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings

from remarkable_mcp.extract import get_ocr_backend

logger = logging.getLogger(__name__)

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


class RemarkableMCP(MCPServer):
    """Custom MCP server that handles VS Code's URI quirks.

    VS Code:
    - Appends ?version=... to resource URIs for cache busting
    - May send URIs with spaces or URL-encoded (%20)

    Pydantic's AnyUrl stores URIs with URL-encoded paths, so we need to
    normalize incoming URIs to match.
    """

    async def read_resource(self, uri, context: Context | None = None):
        """Read a resource, normalizing the URI for lookup.

        Handles:
        - Query parameters: ?version=timestamp -> stripped
        - Spaces in path: encode to %20 to match stored URIs
        """
        uri_str = str(uri)

        # Strip query parameters (e.g., ?version=1764625282944)
        if "?" in uri_str:
            uri_str = uri_str.split("?")[0]
            logger.debug("Stripped query params from resource URI")

        # Normalize path encoding - Pydantic AnyUrl stores with %20 for spaces
        # VS Code may send either spaces or %20, so normalize to %20
        if ":///" in uri_str:
            scheme_end = uri_str.index(":///") + 4
            scheme = uri_str[:scheme_end]
            path = uri_str[scheme_end:]

            # First decode any existing encoding, then re-encode consistently
            # This handles both "November 2025" and "November%202025" inputs
            decoded_path = unquote(path)
            # quote with safe='/' preserves path separators but encodes spaces
            encoded_path = quote(decoded_path, safe="/:")
            uri_str = scheme + encoded_path
            logger.debug(f"Normalized resource URI path: {path} -> {encoded_path}")

        return await super().read_resource(uri_str, context)


def _build_instructions() -> str:
    """Build server instructions based on current configuration."""
    # Check environment
    ssh_mode = os.environ.get("REMARKABLE_USE_SSH", "").lower() in ("1", "true", "yes")
    usb_web_mode = os.environ.get("REMARKABLE_USE_USB_WEB", "").lower() in (
        "1",
        "true",
        "yes",
    )
    local_dir_mode = os.environ.get("REMARKABLE_USE_LOCAL_DIR", "").lower() in (
        "1",
        "true",
        "yes",
    ) or bool(os.environ.get("REMARKABLE_LOCAL_DIR"))
    has_google_vision = bool(os.environ.get("GOOGLE_VISION_API_KEY"))
    ocr_backend = get_ocr_backend()

    read_only_mode = os.environ.get("REMARKABLE_READ_ONLY", "").lower() in (
        "1",
        "true",
        "yes",
    )
    # Local-directory mode is unconditionally read-only (the folder is the
    # desktop app's private sync cache).
    write_mode = not read_only_mode and not local_dir_mode

    read_only_note = "" if write_mode else " All operations are read-only."

    instructions = (
        "# reMarkable MCP Server\n\n"
        f"Access documents from your reMarkable tablet.{read_only_note}\n"
        """
## Available Tools

- `remarkable_browse(path, query)` - Browse folders or search for documents
- `remarkable_read(document, content_type, page, grep)` - Read document content with pagination
- `remarkable_recent(limit)` - Get recently modified documents
- `remarkable_status()` - Check connection and diagnose issues
- `remarkable_image(document, page, include_ocr)` - Get a PNG image with optional OCR

## Recommended Workflows

### Finding and Reading Documents
1. Use `remarkable_browse(query="keyword")` to search by name
2. Use `remarkable_read("Document Name")` to get content
3. Use `remarkable_read("Document", page=2)` to continue reading long documents
4. Use `remarkable_read("Document", grep="pattern")` to search within a document

### Getting Page Images
Use `remarkable_image` when you need visual context:
- Hand-drawn diagrams, sketches, or UI mockups
- Content that text extraction might miss
- Implementing designs based on hand-drawn wireframes

Example: `remarkable_image("UI Mockup", page=1)` returns a PNG image
Example: `remarkable_image("Notes", include_ocr=True)` returns image with extracted text

### For Large Documents
Use pagination to avoid overwhelming context. The response includes:
- `page` / `total_pages` - current position
- `more` - true if more content exists
- `next_page` - page number to request next

### Combining Tools
- Browse → Read: Find documents first, then read them
- Recent → Read: Check what was recently modified, then read specific ones
- Read with grep: Search for specific content within large documents
- Browse → Image: Find a document then get its visual representation

## MCP Resources

Documents are registered as resources for direct access:
- `remarkable:///{path}.txt` - Get full extracted text content in one request
- `remarkableimg:///{path}.page-{N}.png` - Get PNG image of page N (notebooks only)
- Use resources when you need complete document content without pagination
"""
    )

    # Add transport-specific instructions
    if local_dir_mode:
        instructions += """
## Local Directory Mode (Active)

Reading from a local reMarkable data directory (typically the official
desktop app's sync folder). Fully offline and device-free:
- **Raw file access**: Use `content_type="raw"` to get original PDF/EPUB text
- **Read-only**: the folder is the desktop app's sync cache, so write tools
  are disabled in this mode. Content freshness depends on the desktop app
  syncing (keep it running/signed in).

### Content Types for remarkable_read
- `"text"` (default) - Full content: raw PDF/EPUB text + annotations
- `"raw"` - Only original PDF/EPUB text (no annotations)
- `"annotations"` - Only typed text, highlights, and OCR content
"""
    elif ssh_mode:
        instructions += """
## SSH Mode (Active)

You're connected directly to the tablet via SSH. This enables:
- **Raw file access**: Use `content_type="raw"` to get original PDF/EPUB text
- **Raw resources**: `remarkableraw:///{path}.pdf` or `.epub` for original files
- **Faster operations**: Direct tablet access is 10-100x faster than cloud

### Content Types for remarkable_read
- `"text"` (default) - Full content: raw PDF/EPUB text + annotations
- `"raw"` - Only original PDF/EPUB text (no annotations)
- `"annotations"` - Only typed text, highlights, and OCR content
"""
        if write_mode:
            instructions += """
## Write Tools (Active)

Write operations are enabled. These tools modify your tablet's filesystem:

- `remarkable_upload(file_path, parent_folder, document_name)` - Upload a PDF/EPUB
- `remarkable_markdown_to_pdf(markdown, document_name, parent_folder, defer_restart)` -
  Render Markdown as a PDF and upload it
- `remarkable_mkdir(folder_name, parent)` - Create a folder
- `remarkable_move(document, dest_folder)` - Move a document/folder
- `remarkable_rename(document, new_name)` - Rename a document/folder
- `remarkable_delete(document)` - Delete a document/folder (destructive)
- `remarkable_refresh()` - Restart xochitl once to apply writes deferred with `defer_restart=True`

### Safety
- **Delete is destructive** and immediate — the MCP client should confirm with the user first
- After each write operation, the tablet UI restarts automatically (the call waits for it
  to settle). For batches, pass `defer_restart=True` to each write (or set
  `REMARKABLE_DEFER_RESTART=1`) and call `remarkable_refresh()` once at the end, to restart
  a single time instead of once per write
- Use `remarkable_browse()` to verify changes after write operations
- Run with `--read-only` to disable all write tools
"""
    elif usb_web_mode:
        if write_mode:
            instructions += """
## Write Tools (Active — USB Web)

Upload is available via the USB web interface:

- `remarkable_upload(file_path)` - Upload a PDF/EPUB file
- `remarkable_markdown_to_pdf(markdown, document_name)` - Render Markdown as a
  named PDF and upload it to the root folder

Note: mkdir, move, rename, and delete require SSH mode.
"""
    else:
        instructions += """
## Cloud Mode (Active)

Connected via reMarkable Cloud API. Some features require SSH mode:
- Raw PDF/EPUB file downloads
- `content_type="raw"` parameter

For faster access and raw files, consider SSH mode: `uvx remarkable-mcp --ssh`
"""
        if write_mode:
            instructions += """
## Write Tools (Active)

Write operations are enabled (the default). These tools modify your cloud library
and sync to all your devices:

- `remarkable_upload(file_path, parent_folder, document_name)` - Upload a PDF/EPUB
- `remarkable_markdown_to_pdf(markdown, document_name, parent_folder)` - Render
  Markdown as a PDF and upload it
- `remarkable_mkdir(folder_name, parent)` - Create a folder
- `remarkable_move(document, dest_folder)` - Move a document/folder
- `remarkable_rename(document, new_name)` - Rename a document/folder
- `remarkable_delete(document)` - Delete a document/folder (destructive)

### Safety
- **Delete is destructive** — the MCP client should confirm with the user first
- Changes sync to your devices; use `remarkable_browse()` to verify them
- Run with `--read-only` to disable all write tools
"""

    # Add OCR instructions based on configuration
    uses_google_vision = ocr_backend == "google" or (ocr_backend == "auto" and has_google_vision)
    if uses_google_vision:
        instructions += """
## OCR (Google Vision Selected)

Google Vision will be tried first. If it is unavailable or detects no text,
OCR falls back to local Tesseract.
Use `include_ocr=True` with `remarkable_read()` to extract handwritten content.
"""
    else:
        instructions += """
## OCR (Tesseract Active)

Tesseract will be used for local OCR but works poorly on handwriting.
For better handwriting recognition, configure GOOGLE_VISION_API_KEY and use
REMARKABLE_OCR_BACKEND=auto (the default) or google.
"""

    return instructions


@asynccontextmanager
async def lifespan(app: MCPServer) -> AsyncIterator[None]:
    """Lifespan context manager for the MCP server."""
    import asyncio
    import os

    # Import here to avoid circular imports
    from remarkable_mcp.resources import (
        _is_ssh_mode,
        load_all_documents_sync,
        start_background_loader,
        stop_background_loader,
    )

    task = None
    ssh_mode = _is_ssh_mode()
    logger.info(f"REMARKABLE_USE_SSH env: {os.environ.get('REMARKABLE_USE_SSH')}")
    logger.info(f"SSH mode detected: {ssh_mode}")

    if ssh_mode:
        # SSH mode: load documents in the background so we do NOT block the MCP
        # initialize handshake. Tools query the device live, so they work before
        # the load finishes; this load only pre-populates browsable resources.
        logger.info("SSH mode: starting background document load...")

        async def _ssh_background_load():
            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(None, load_all_documents_sync)
                logger.info("SSH mode: documents loaded")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"SSH mode: failed to load documents on startup: {e}")
                logger.warning("Server will start, but tools will show connection errors")

        task = asyncio.create_task(_ssh_background_load())
    else:
        # Cloud/USB/local-directory mode: load in background to not block startup
        logger.info("Cloud/USB/local-directory mode: starting background loader...")
        task = start_background_loader()

    try:
        yield
    finally:
        # Stop background loader on shutdown (if running)
        await stop_background_loader(task)


try:
    _SERVER_VERSION = version("remarkable-mcp")
except PackageNotFoundError:
    _SERVER_VERSION = ""

# One MCPServer serves modern 2026-07-28 requests and legacy initialize/session
# clients concurrently. reMarkable credentials and transport selection remain
# process configuration; MCP HTTP authorization headers are intentionally unused.
mcp = RemarkableMCP(
    "remarkable",
    instructions=_build_instructions(),
    version=_SERVER_VERSION,
    lifespan=lifespan,
)

# Import tools, resources, and prompts to register them
from remarkable_mcp import (  # noqa: E402
    prompts,  # noqa: F401
    resources,  # noqa: F401
    tools,  # noqa: F401
)

# Conditionally register write tools when enabled.
# Write works in three transports: cloud (default), SSH, and USB web.
# Cloud and SSH support the full set (upload/mkdir/move/rename/delete); the USB
# web interface only supports upload, so only that tool registers in USB mode
# (see the per-tool gating in write_tools.register_write_tools). Local-directory
# mode is strictly read-only and registers no write tools.
from remarkable_mcp import write_tools as _write_tools  # noqa: E402

if _write_tools.write_enabled():
    _write_tools.register_write_tools()

# Register the interactive MCP App canvas (remarkable_canvas + ui:// resource).
# There is no feature flag: app-capable clients (those advertising the MCP Apps
# UI extension at initialize) open an interactive viewer, while other clients
# ignore the UI metadata and receive the rendered page as an embedded image.
from remarkable_mcp import app_canvas as _app_canvas  # noqa: E402

_app_canvas.register_app_tools()


def _transport_security_for_host(host: str) -> TransportSecuritySettings:
    """Build a strict Host/Origin allowlist for the actual HTTP bind address."""
    normalized = host.strip().strip("[]")
    if normalized in ("0.0.0.0", "::"):
        raise ValueError("Wildcard HTTP bind addresses are not supported; use a concrete address.")
    if normalized in _LOOPBACK_HOSTS:
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=[
                "http://127.0.0.1:*",
                "http://localhost:*",
                "http://[::1]:*",
            ],
        )

    try:
        address = ip_address(normalized)
    except ValueError:
        authority = normalized
    else:
        authority = f"[{normalized}]" if address.version == 6 else normalized

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[authority, f"{authority}:*"],
        allowed_origins=[
            f"http://{authority}",
            f"http://{authority}:*",
            f"https://{authority}",
            f"https://{authority}:*",
        ],
    )


def run(
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8000,
):
    """Run the MCP server over stdio or Streamable HTTP."""
    if transport == "streamable-http":
        mcp.run(
            transport=transport,
            host=host,
            port=port,
            transport_security=_transport_security_for_host(host),
        )
        return
    mcp.run(transport=transport)
