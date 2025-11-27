"""
reMarkable MCP Server initialization.
"""

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastMCP) -> AsyncIterator[None]:
    """Lifespan context manager for the MCP server."""
    # Import here to avoid circular imports
    from remarkable_mcp.resources import (
        _is_ssh_mode,
        load_all_documents_sync,
        start_background_loader,
        stop_background_loader,
    )

    task = None

    if _is_ssh_mode():
        # SSH mode: load all documents synchronously (fast over USB)
        logger.info("SSH mode: loading documents synchronously...")
        load_all_documents_sync()
    else:
        # Cloud mode: load in background to not block startup
        task = start_background_loader()

    try:
        yield
    finally:
        # Stop background loader on shutdown (if running)
        await stop_background_loader(task)


# Initialize FastMCP server with lifespan
mcp = FastMCP("remarkable", lifespan=lifespan)

# Import tools, resources, and prompts to register them
from remarkable_mcp import (  # noqa: E402
    prompts,  # noqa: F401
    resources,  # noqa: F401
    tools,  # noqa: F401
)


def run():
    """Run the MCP server."""
    mcp.run()
