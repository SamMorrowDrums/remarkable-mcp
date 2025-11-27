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
    from remarkable_mcp.resources import start_background_loader, stop_background_loader

    # Start background document loader
    task = start_background_loader()

    try:
        yield
    finally:
        # Stop background loader on shutdown
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
