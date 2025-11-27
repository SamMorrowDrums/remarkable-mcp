"""
reMarkable MCP Server initialization.
"""

from mcp.server.fastmcp import FastMCP

# Initialize FastMCP server
mcp = FastMCP("remarkable")

# Import tools, resources, and prompts to register them
from remarkable_mcp import (  # noqa: E402
    prompts,  # noqa: F401
    resources,  # noqa: F401
    tools,  # noqa: F401
)


def run():
    """Run the MCP server."""
    mcp.run()
