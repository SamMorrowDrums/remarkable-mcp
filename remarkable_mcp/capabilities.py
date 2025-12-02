"""
Client capability checking utilities for reMarkable MCP Server.

This module provides utilities to check MCP client capabilities during
the request lifecycle. FastMCP supports capability negotiation through
the MCP protocol's initialize handshake.

## How MCP Capability Negotiation Works

When a client connects to an MCP server, the following handshake occurs:
1. Client sends `initialize` request with its capabilities
2. Server responds with its capabilities
3. Client sends `initialized` notification

## Client Capabilities (from client to server)

Clients declare what features they support:
- `sampling`: Server can request LLM completions from client
- `elicitation`: Server can request user input during tool execution
- `roots`: Server can query filesystem roots
- `experimental`: Dictionary of experimental feature support

## Server Capabilities (from server to client)

Servers declare what they offer:
- `tools`: Server provides tool definitions
- `resources`: Server provides resource access (subscribe, listChanged)
- `prompts`: Server provides prompt templates
- `logging`: Server can send log messages
- `completions`: Server supports argument completions

## Checking Client Capabilities

Use the Context object in tools to check what the client supports:

```python
from mcp.server.fastmcp import Context
from remarkable_mcp.capabilities import get_client_capabilities, client_supports_sampling

@mcp.tool()
async def my_tool(query: str, ctx: Context) -> str:
    # Check if client supports sampling
    if client_supports_sampling(ctx):
        # Can request LLM completions
        pass

    # Get full capabilities object
    caps = get_client_capabilities(ctx)
    if caps and caps.experimental:
        # Check experimental features
        pass

    return "result"
```

## Note on Embedded Resources

The MCP protocol does not have a specific capability flag for "embedded resources
in tool calls". Support for EmbeddedResource and ImageContent in tool responses
is part of the base protocol and determined by the protocol version. All clients
supporting protocol version 2024-11-05 or later should handle embedded resources.
"""

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from mcp.server.fastmcp import Context
    from mcp.types import ClientCapabilities


def get_client_capabilities(ctx: "Context") -> Optional["ClientCapabilities"]:
    """Get the client's declared capabilities from the MCP context.

    Returns None if the context is not available or client hasn't sent capabilities.

    Args:
        ctx: The FastMCP Context object from a tool or resource function

    Returns:
        ClientCapabilities object or None if not available

    Example:
        @mcp.tool()
        async def my_tool(ctx: Context) -> str:
            caps = get_client_capabilities(ctx)
            if caps and caps.sampling:
                # Client supports sampling
                pass
    """
    try:
        session = ctx.session
        if session and hasattr(session, "client_params") and session.client_params:
            return session.client_params.capabilities
    except (ValueError, AttributeError):
        # Context not available (e.g., outside of request lifecycle)
        pass
    return None


def client_supports_sampling(ctx: "Context") -> bool:
    """Check if the client supports LLM sampling requests.

    When true, the server can request the client's LLM to generate completions.

    Args:
        ctx: The FastMCP Context object

    Returns:
        True if client supports sampling, False otherwise
    """
    caps = get_client_capabilities(ctx)
    return caps is not None and caps.sampling is not None


def client_supports_elicitation(ctx: "Context") -> bool:
    """Check if the client supports user elicitation.

    When true, the server can request interactive user input during tool execution.

    Args:
        ctx: The FastMCP Context object

    Returns:
        True if client supports elicitation, False otherwise
    """
    caps = get_client_capabilities(ctx)
    return caps is not None and caps.elicitation is not None


def client_supports_roots(ctx: "Context") -> bool:
    """Check if the client supports filesystem roots.

    When true, the server can query for filesystem root directories.

    Args:
        ctx: The FastMCP Context object

    Returns:
        True if client supports roots, False otherwise
    """
    caps = get_client_capabilities(ctx)
    return caps is not None and caps.roots is not None


def client_supports_experimental(ctx: "Context", feature: str) -> bool:
    """Check if the client supports a specific experimental feature.

    Args:
        ctx: The FastMCP Context object
        feature: The experimental feature name to check

    Returns:
        True if client declares support for the experimental feature
    """
    caps = get_client_capabilities(ctx)
    if caps is None or caps.experimental is None:
        return False
    return feature in caps.experimental


def get_client_info(ctx: "Context") -> Optional[dict]:
    """Get information about the connected client.

    Returns client name, version, and other metadata from the initialize request.

    Args:
        ctx: The FastMCP Context object

    Returns:
        Dictionary with client info or None if not available
    """
    try:
        session = ctx.session
        if session and hasattr(session, "client_params") and session.client_params:
            params = session.client_params
            return {
                "name": params.clientInfo.name if params.clientInfo else None,
                "version": params.clientInfo.version if params.clientInfo else None,
                "protocol_version": params.protocolVersion,
            }
    except (ValueError, AttributeError):
        pass
    return None


def get_protocol_version(ctx: "Context") -> Optional[str]:
    """Get the MCP protocol version negotiated with the client.

    Args:
        ctx: The FastMCP Context object

    Returns:
        Protocol version string (e.g., "2024-11-05") or None
    """
    try:
        session = ctx.session
        if session and hasattr(session, "client_params") and session.client_params:
            return session.client_params.protocolVersion
    except (ValueError, AttributeError):
        pass
    return None
