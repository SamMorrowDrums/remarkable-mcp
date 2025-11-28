"""
reMarkable MCP Server initialization.
"""

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)


def _build_instructions() -> str:
    """Build server instructions based on current configuration."""
    # Check environment
    ssh_mode = os.environ.get("REMARKABLE_USE_SSH", "").lower() in ("1", "true", "yes")
    has_google_vision = bool(os.environ.get("GOOGLE_VISION_API_KEY"))

    instructions = """# reMarkable MCP Server

Access documents from your reMarkable tablet. All operations are read-only.

## Available Tools

- `remarkable_browse(path, query)` - Browse folders or search for documents
- `remarkable_read(document, content_type, page, grep)` - Read document content with pagination
- `remarkable_recent(limit)` - Get recently modified documents
- `remarkable_status()` - Check connection and diagnose issues

## Recommended Workflows

### Finding and Reading Documents
1. Use `remarkable_browse(query="keyword")` to search by name
2. Use `remarkable_read("Document Name")` to get content
3. Use `remarkable_read("Document", page=2)` to continue reading long documents
4. Use `remarkable_read("Document", grep="pattern")` to search within a document

### For Large Documents
Use pagination to avoid overwhelming context. The response includes:
- `page` / `total_pages` - current position
- `more` - true if more content exists
- `next_page` - page number to request next

### Combining Tools
- Browse → Read: Find documents first, then read them
- Recent → Read: Check what was recently modified, then read specific ones
- Read with grep: Search for specific content within large documents

## MCP Resources

Documents are registered as resources for direct access:
- `remarkable:///{path}.txt` - Get full extracted text content in one request
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

    # Add OCR instructions
    if has_google_vision:
        instructions += """
## OCR (Google Vision Active)

Google Vision API is configured for high-quality handwriting recognition.
Use `include_ocr=True` with `remarkable_read()` to extract handwritten content.
"""
    else:
        instructions += """
## OCR (Tesseract Fallback)

Google Vision is not configured. Tesseract will be used for OCR but works poorly
on handwriting. For better results, configure GOOGLE_VISION_API_KEY.
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
        logger.info("SSH mode: loading documents...")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, load_all_documents_sync)
        logger.info("SSH mode: documents loaded")
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
mcp = FastMCP("remarkable", instructions=_build_instructions(), lifespan=lifespan)

# Import tools, resources, and prompts to register them
from remarkable_mcp import (  # noqa: E402
    prompts,  # noqa: F401
    resources,  # noqa: F401
    tools,  # noqa: F401
)


def run():
    """Run the MCP server."""
    mcp.run()
