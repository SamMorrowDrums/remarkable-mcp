"""
reMarkable MCP Server

An MCP server that provides access to reMarkable tablet data through the reMarkable Cloud API.
"""

from remarkable_mcp.server import mcp

__version__ = "0.1.0"
__all__ = ["mcp", "__version__"]
