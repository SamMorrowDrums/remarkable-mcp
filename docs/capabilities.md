# MCP Capability Negotiation

This server supports the MCP capability negotiation protocol. During the initialization handshake, clients declare their capabilities and the server responds with its supported features.

## Overview

When an MCP client connects, both sides exchange capability information:

1. **Client → Server**: Client sends its capabilities (sampling, elicitation, roots, etc.)
2. **Server → Client**: Server responds with its supported features
3. **Tools adapt**: Tools can check client capabilities and adjust behavior accordingly

This enables features like:
- **Sampling OCR**: Using the client's LLM for handwriting recognition
- **Adaptive responses**: Returning embedded resources or URIs based on client support

## Checking Client Capabilities

Tools can check what the connected client supports using the capability utilities:

```python
from mcp.server.fastmcp import Context
from remarkable_mcp.capabilities import (
    get_client_capabilities,
    client_supports_sampling,
    client_supports_elicitation,
    get_client_info,
)

@mcp.tool()
async def my_tool(ctx: Context) -> str:
    # Check if client supports specific features
    if client_supports_sampling(ctx):
        # Client can handle LLM sampling requests
        pass

    if client_supports_elicitation(ctx):
        # Client can handle interactive user prompts
        pass

    # Get full capabilities object
    caps = get_client_capabilities(ctx)

    # Get client info (name, version, protocol)
    info = get_client_info(ctx)

    return "result"
```

## Available Capability Checks

| Function | Description |
|----------|-------------|
| `get_client_capabilities(ctx)` | Get the full ClientCapabilities object |
| `client_supports_sampling(ctx)` | Check if client supports LLM sampling |
| `client_supports_elicitation(ctx)` | Check if client supports user prompts |
| `client_supports_roots(ctx)` | Check if client supports filesystem roots |
| `client_supports_experimental(ctx, feature)` | Check for experimental features |
| `get_client_info(ctx)` | Get client name, version, protocol |
| `get_protocol_version(ctx)` | Get negotiated protocol version |

## Sampling Capability

The most commonly used capability is **sampling**, which allows the server to request LLM completions from the client.

### How It Works

1. Server checks `client_supports_sampling(ctx)`
2. If supported, server can call `ctx.session.create_message(...)` 
3. Client's LLM processes the request and returns results
4. Server uses the response (e.g., OCR results from an image)

### Use in remarkable-mcp

When `REMARKABLE_OCR_BACKEND=sampling` is configured:

```python
# In remarkable_image tool
if include_ocr and client_supports_sampling(ctx):
    ocr_text = await ocr_via_sampling(ctx, png_data)
    # Returns handwriting transcription from client's LLM
```

This allows OCR without external API keys — the client's own AI model handles it.

## Embedded Resource Support

The MCP protocol does not have a specific capability flag for embedded resources in tool responses. Support for `EmbeddedResource` and `ImageContent` in tool results is part of the base protocol.

All clients supporting protocol version `2024-11-05` or later should handle embedded resources, though actual client implementations may vary. The `remarkable_image` tool includes a `compatibility` parameter to return resource URIs instead of embedded resources for clients that may not fully support them.

## Protocol Versions

| Version | Notable Features |
|---------|------------------|
| `2024-11-05` | Embedded resources, sampling |
| Earlier | Basic tool calls only |

Check the negotiated version:

```python
version = get_protocol_version(ctx)
```

## Best Practices

1. **Always check capabilities** before using advanced features
2. **Provide fallbacks** when capabilities are unavailable
3. **Use `compatibility` flags** for features that may not be universally supported
4. **Log capability info** for debugging connection issues
