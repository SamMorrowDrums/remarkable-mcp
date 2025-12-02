"""
reMarkable MCP Server initialization.
"""

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator
from urllib.parse import quote, unquote

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)


class RemarkableMCP(FastMCP):
    """Custom FastMCP server that handles VS Code's URI quirks.

    VS Code:
    - Appends ?version=... to resource URIs for cache busting
    - May send URIs with spaces or URL-encoded (%20)

    Pydantic's AnyUrl stores URIs with URL-encoded paths, so we need to
    normalize incoming URIs to match.
    """

    async def read_resource(self, uri):
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

        return await super().read_resource(uri_str)


def _build_instructions() -> str:
    """Build server instructions based on current configuration."""
    # Check environment
    ssh_mode = os.environ.get("REMARKABLE_USE_SSH", "").lower() in ("1", "true", "yes")
    has_google_vision = bool(os.environ.get("GOOGLE_VISION_API_KEY"))
    ocr_backend = os.environ.get("REMARKABLE_OCR_BACKEND", "auto").lower()

    instructions = """# reMarkable MCP Server

Access documents from your reMarkable tablet. All operations are read-only.

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

    # Add SSH-specific instructions
    if ssh_mode:
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
    else:
        instructions += """
## Cloud Mode (Active)

Connected via reMarkable Cloud API. Some features require SSH mode:
- Raw PDF/EPUB file downloads
- `content_type="raw"` parameter

For faster access and raw files, consider SSH mode: `uvx remarkable-mcp --ssh`
"""

    # Add OCR instructions based on configuration
    if ocr_backend == "sampling":
        instructions += """
## OCR (Sampling Mode Active)

OCR is configured to use this client's AI model via MCP sampling.
Use `remarkable_image("Document", include_ocr=True)` to extract text from images.
This requires no external API keys - it uses your client's capabilities.
"""
    elif has_google_vision:
        instructions += """
## OCR (Google Vision Active)

Google Vision API is configured for high-quality handwriting recognition.
Use `include_ocr=True` with `remarkable_read()` to extract handwritten content.
"""
    else:
        instructions += """
## OCR (Tesseract Fallback)

Google Vision is not configured. Tesseract will be used for OCR but works poorly
on handwriting. For better results, either:
- Configure GOOGLE_VISION_API_KEY for Google Vision
- Set REMARKABLE_OCR_BACKEND=sampling to use this client's AI for OCR
"""

    return instructions


@asynccontextmanager
async def lifespan(app: FastMCP) -> AsyncIterator[None]:
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
        # SSH mode: load all documents in executor to not block event loop
        # Wrap in try/except so server starts even if connection fails
        logger.info("SSH mode: loading documents...")
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, load_all_documents_sync)
            logger.info("SSH mode: documents loaded")
        except Exception as e:
            logger.warning(f"SSH mode: failed to load documents on startup: {e}")
            logger.warning("Server will start, but tools will show connection errors")
    else:
        # Cloud mode: load in background to not block startup
        logger.info("Cloud mode: starting background loader...")
        task = start_background_loader()

    try:
        yield
    finally:
        # Stop background loader on shutdown (if running)
        await stop_background_loader(task)


# Initialize FastMCP server with lifespan and instructions
mcp = RemarkableMCP("remarkable", instructions=_build_instructions(), lifespan=lifespan)

# Import tools, resources, and prompts to register them
from remarkable_mcp import (  # noqa: E402
    prompts,  # noqa: F401
    resources,  # noqa: F401
    tools,  # noqa: F401
)


def run():
    """Run the MCP server."""
    mcp.run()
