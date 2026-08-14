# MCP Protocol and Capability Compatibility

remarkable-mcp uses stable MCP Python SDK 2.x. A single `MCPServer` serves both
protocol eras:

- `2026-07-28` clients use `server/discover`, sessionless requests, and
  multi-round input results.
- Clients negotiating `2025-11-25` or any earlier revision supported by the SDK
  continue to use the initialize handshake and legacy sessions.

The same stdio process or Streamable HTTP `/mcp` endpoint handles both. No
protocol-version flag or separate deployment is required.

## Client capabilities

Handlers receive the SDK v2 `Context` explicitly and can inspect the
capabilities attached to that request:

```python
from mcp.server.mcpserver import Context

from remarkable_mcp.capabilities import (
    client_supports_elicitation,
    client_supports_sampling,
    get_client_capabilities,
    get_client_info,
)


@mcp.tool()
async def my_tool(ctx: Context) -> str:
    if client_supports_sampling(ctx):
        ...
    if client_supports_elicitation(ctx):
        ...

    capabilities = get_client_capabilities(ctx)
    client = get_client_info(ctx)
    return "result"
```

| Function | Description |
|----------|-------------|
| `get_client_capabilities(ctx)` | Return the request's `ClientCapabilities` |
| `client_supports_sampling(ctx)` | Check for LLM sampling |
| `client_supports_elicitation(ctx)` | Check for form/user elicitation |
| `client_supports_roots(ctx)` | Check for filesystem roots |
| `client_supports_experimental(ctx, feature)` | Check an experimental capability |
| `get_client_info(ctx)` | Return client name, version, and protocol when supplied |
| `get_protocol_version(ctx)` | Return the negotiated protocol revision |
| `get_client_extensions(ctx)` | Return protocol extensions such as MCP Apps |
| `client_supports_apps(ctx)` | Check for the MCP Apps UI extension |

## Multi-round compatibility

MCP `2026-07-28` removes server-initiated requests. Sampling, roots, and
elicitation therefore cannot be sent back over a modern request's transport.
MCP SDK 2.x provides `Resolve` dependencies with `Sample`, `ListRoots`, and
`Elicit` results:

- On a modern connection, the server returns an `InputRequiredResult`; the
  client answers it and retries the original tool with sealed `requestState`.
- On a legacy connection, the same resolver uses the established
  server-to-client request.

remarkable-mcp uses this compatibility layer for sampling OCR and destructive
delete confirmation. Tool schemas do not expose the hidden resolver parameters.
If the client does not advertise the required capability, OCR falls back to its
configured local/provider backend and delete fails closed.

`MCPServer` protects multi-round request state with a process-local key by
default. This is appropriate for stdio and the supported single-process HTTP
runner. A future multi-worker deployment must configure shared
`RequestStateSecurity` keys and sticky routing for legacy sessions.

## Embedded resources and structured output

Embedded text/image resources are part of the base protocol and require no
separate capability. `remarkable_image` returns `EmbeddedResource` content by
default and retains `compatibility=True` for clients that need a JSON/data-URI
fallback.

The MCP Apps canvas keeps both:

- embedded PNG content for ordinary clients;
- `structuredContent` on the wire (`structured_content` in Python) for the app.

The `io.modelcontextprotocol/ui` extension controls whether a client renders the
`ui://remarkable/canvas` HTML resource; clients without it still receive the
image.

## Authentication and process state

MCP HTTP authorization headers are not used to select a reMarkable account or
transport. `REMARKABLE_TOKEN`, CLI flags, and the documented reMarkable
environment variables configure one process-local client. That client and its
transport resolution are protected by locks and shared safely by modern
stateless requests and legacy sessions in the same process.

Streamable HTTP continues to bind to loopback by default and enforces strict
Host/Origin allowlists. See the README's reverse-proxy guidance before exposing
it remotely.
