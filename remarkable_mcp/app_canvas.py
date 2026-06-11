"""
Interactive MCP App canvas for reMarkable documents (SEP-1865 "MCP Apps").

The server always registers:
- An HTML app resource at ``ui://remarkable/canvas`` (MIME
  ``text/html;profile=mcp-app``) that the client renders in a sandboxed iframe.
- A ``remarkable_canvas`` tool whose ``_meta.ui.resourceUri`` points at that
  resource, so app-capable clients open the viewer and feed it page data over
  the MCP Apps postMessage bridge.

There is no separate feature flag: the MCP Apps capability is negotiated at the
``initialize`` handshake. Clients that advertise the
``io.modelcontextprotocol/ui`` extension render the interactive canvas; clients
that don't simply ignore ``_meta.ui``/``ui://`` and receive the rendered page as
an embedded image. The tool therefore degrades gracefully and is useful
everywhere — the canvas is just inert UI metadata to clients that can't use it.

This phase is a **read-only** viewer (render + page navigation). Pen capture,
local undo, and an explicit Save button that writes strokes back to the device
are tracked as later, device-validated phases and are intentionally not wired
here. Write-back, when it lands, will ride the existing write gate
(``write_enabled()`` / ``--read-only``) alongside the other write tools rather
than introducing a new flag.
"""

import base64
import logging
from typing import Optional

from mcp import types
from mcp.server.fastmcp import Context

from remarkable_mcp.capabilities import APP_UI_MIME, client_supports_apps

logger = logging.getLogger(__name__)

# Resource URI for the canvas app (referenced by the tool's _meta.ui.resourceUri).
CANVAS_RESOURCE_URI = "ui://remarkable/canvas"

# Read-only viewer annotations (no device mutation in this phase).
CANVAS_ANNOTATIONS = types.ToolAnnotations(readOnlyHint=True, openWorldHint=False)


# The canvas app: a self-contained, dependency-free HTML/JS surface that speaks
# the MCP Apps postMessage bridge (JSON-RPC 2.0). It renders the page PNG that
# the server delivers in the tool result's structuredContent and lets the user
# page through the document by calling the remarkable_canvas tool back over the
# bridge. Kept vanilla on purpose: no build step, no external assets, no CSP
# headaches. The bridge wiring follows SEP-1865 and still needs validation
# against a real app host (ChatGPT/Claude/VS Code/Inspector).
_CANVAS_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>reMarkable Canvas</title>
<style>
  :root { color-scheme: light dark; }
  html, body { margin: 0; height: 100%; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    display: flex; flex-direction: column; background: #1e1e1e; color: #e8e8e8;
  }
  header {
    display: flex; align-items: center; gap: .5rem; padding: .5rem .75rem;
    border-bottom: 1px solid rgba(255,255,255,.12); flex: 0 0 auto;
  }
  header .title { font-weight: 600; flex: 1 1 auto; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  button {
    font: inherit; padding: .35rem .7rem; border-radius: .4rem; cursor: pointer;
    border: 1px solid rgba(255,255,255,.2); background: #2d2d2d; color: inherit;
  }
  button:disabled { opacity: .4; cursor: default; }
  .pageinfo { font-variant-numeric: tabular-nums; min-width: 5rem; text-align: center; }
  main { flex: 1 1 auto; overflow: auto; display: flex; align-items: flex-start;
    justify-content: center; padding: 1rem; }
  img { max-width: 100%; height: auto; background: #fbfbfb;
    box-shadow: 0 1px 8px rgba(0,0,0,.4); border-radius: 2px; }
  .status { padding: 1rem; opacity: .8; }
  footer { flex: 0 0 auto; padding: .35rem .75rem; font-size: .8rem; opacity: .6;
    border-top: 1px solid rgba(255,255,255,.12); }
</style>
</head>
<body>
  <header>
    <span class="title" id="title">reMarkable Canvas</span>
    <button id="prev" disabled>&larr; Prev</button>
    <span class="pageinfo" id="pageinfo">--</span>
    <button id="next" disabled>Next &rarr;</button>
    <button id="full">Fullscreen</button>
  </header>
  <main><div class="status" id="status">Loading&hellip;</div></main>
  <footer id="footer">Read-only viewer</footer>
<script>
(function () {
  "use strict";
  var pending = {}, rpcId = 1;
  var state = { document: null, page: 1, total: 1, busy: false };

  function post(msg) {
    try { window.parent.postMessage(msg, "*"); } catch (e) {}
  }
  function rpc(method, params) {
    return new Promise(function (resolve, reject) {
      var id = rpcId++;
      pending[id] = { resolve: resolve, reject: reject };
      post({ jsonrpc: "2.0", id: id, method: method, params: params || {} });
    });
  }
  function notify(method, params) {
    post({ jsonrpc: "2.0", method: method, params: params || {} });
  }

  function setStatus(text) {
    document.querySelector("main").innerHTML =
      '<div class="status">' + text + "</div>";
  }
  function digOut(payload) {
    // The host may deliver the CallToolResult directly or wrapped in params.
    if (!payload) return null;
    if (payload.structuredContent) return payload.structuredContent;
    if (payload.result && payload.result.structuredContent)
      return payload.result.structuredContent;
    if (payload.toolResult && payload.toolResult.structuredContent)
      return payload.toolResult.structuredContent;
    return null;
  }
  function render(data) {
    if (!data) return;
    if (data.error) { setStatus("Error: " + data.error); return; }
    if (data.document) state.document = data.document;
    if (data.page) state.page = data.page;
    if (data.total_pages) state.total = data.total_pages;
    document.getElementById("title").textContent =
      data.document_name || data.document || "reMarkable Canvas";
    document.getElementById("pageinfo").textContent =
      state.page + " / " + state.total;
    document.getElementById("prev").disabled = state.busy || state.page <= 1;
    document.getElementById("next").disabled =
      state.busy || state.page >= state.total;
    if (data.png_data_uri) {
      var m = document.querySelector("main");
      m.innerHTML = "";
      var img = document.createElement("img");
      img.src = data.png_data_uri;
      img.alt = "Page " + state.page;
      m.appendChild(img);
    }
    notifySize();
  }
  function notifySize() {
    var h = document.body.scrollHeight, w = document.body.scrollWidth;
    notify("ui/notifications/size-changed", { width: w, height: h });
  }

  function goto(page) {
    if (state.busy || !state.document) return;
    if (page < 1 || page > state.total) return;
    state.busy = true;
    render({});
    rpc("tools/call", {
      name: "remarkable_canvas",
      arguments: { document: state.document, page: page },
    }).then(function (result) {
      state.busy = false;
      render(digOut(result) || {});
    }).catch(function (err) {
      state.busy = false;
      setStatus("Failed to load page: " + (err && err.message ? err.message : err));
    });
  }

  document.getElementById("prev").onclick = function () { goto(state.page - 1); };
  document.getElementById("next").onclick = function () { goto(state.page + 1); };
  document.getElementById("full").onclick = function () {
    rpc("ui/request-display-mode", { mode: "fullscreen" }).catch(function () {});
  };

  window.addEventListener("message", function (event) {
    var msg = event.data;
    if (!msg || msg.jsonrpc !== "2.0") return;
    if (msg.id !== undefined && (("result" in msg) || ("error" in msg))) {
      var p = pending[msg.id];
      if (p) {
        delete pending[msg.id];
        if (msg.error) p.reject(msg.error); else p.resolve(msg.result);
      }
      return;
    }
    // Input/result asymmetry (per the MCP Apps spec): tool-input params are
    // wrapped as { arguments: {...} } (the document/page the tool was called
    // with), while tool-result params are a bare CallToolResult carrying the
    // rendered page in structuredContent. Handle them separately.
    if (msg.method === "ui/notifications/tool-input" ||
        msg.method === "ui/notifications/tool-input-partial") {
      var args = (msg.params && msg.params.arguments) || {};
      if (args.document) state.document = args.document;
      if (args.page) state.page = args.page;
      return;
    }
    if (msg.method === "ui/notifications/tool-result") {
      render(digOut(msg.params) || {});
      return;
    }
  });

  // Handshake: announce the app (with protocol version + client info, as the
  // spec requires) and the display modes it can use, then signal that we are
  // ready to receive tool input/results.
  rpc("ui/initialize", {
    protocolVersion: "2026-01-26",
    capabilities: {},
    clientInfo: { name: "remarkable-canvas", version: "1" },
    appCapabilities: { availableDisplayModes: ["inline", "fullscreen"] },
  }).then(function (res) {
    notify("ui/notifications/initialized", {});
    var d = digOut(res);
    if (d) render(d);
  }).catch(function () {
    // Some hosts push tool-input/result without a handshake reply; that's fine.
    notify("ui/notifications/initialized", {});
  });
})();
</script>
</body>
</html>
"""


async def _render_canvas_page(document: str, page: int, ctx: Optional[Context]):
    """Resolve a document, render the requested page, and build the app payload.

    Returns a CallToolResult carrying both an embedded PNG (so non-app clients
    still see the page) and structuredContent (the data the canvas app reads).
    On failure returns a make_error JSON string. Mirrors remarkable_image by
    converting unexpected transport/auth errors into structured guidance rather
    than letting them surface as a bare exception string.
    """
    from remarkable_mcp.responses import make_error

    try:
        return await _render_canvas_page_impl(document, page, ctx)
    except Exception as e:
        logger.exception("Canvas render failed")
        return make_error(
            error_type="canvas_failed",
            message=f"Failed to open canvas for '{document}': {e}",
            suggestion=(
                "Check remarkable_status() for connection/auth, then retry. "
                "You can also use remarkable_image to view the page directly."
            ),
        )


async def _render_canvas_page_impl(document: str, page: int, ctx: Optional[Context]):
    """Implementation behind _render_canvas_page (see that wrapper for errors)."""
    # Imported lazily to avoid any import-order coupling with tools.py, which is
    # imported before this module is registered.
    import tempfile
    from pathlib import Path

    from remarkable_mcp.api import (
        download_raw_file,
        get_active_transport,
        get_item_path,
        get_items_by_id,
        get_rmapi,
    )
    from remarkable_mcp.concurrency import run_blocking
    from remarkable_mcp.extract import (
        find_similar_documents,
        get_background_color,
        get_document_page_count,
        render_page_from_document_zip,
        render_tablet_pdf_page_to_png,
    )
    from remarkable_mcp.responses import make_error
    from remarkable_mcp.tools import (
        _apply_root_filter,
        _get_root_path,
        _is_within_root,
        _resolve_root_path,
    )

    background = await run_blocking(get_background_color)
    client = get_rmapi()
    collection = await run_blocking(client.get_meta_items)
    items_by_id = get_items_by_id(collection)

    root = _get_root_path()
    actual_document = _resolve_root_path(document) if document.startswith("/") else document

    documents = [item for item in collection if not item.is_folder]
    target_doc = None
    document_lower = actual_document.lower().strip("/")
    for doc in documents:
        doc_path = get_item_path(doc, items_by_id)
        if not _is_within_root(doc_path, root):
            continue
        if doc.VissibleName.lower() == document_lower:
            target_doc = doc
            break
        if doc_path.lower().strip("/") == document_lower:
            target_doc = doc
            break

    if not target_doc:
        filtered_docs = [
            doc for doc in documents if _is_within_root(get_item_path(doc, items_by_id), root)
        ]
        similar = find_similar_documents(document, filtered_docs)
        search_term = document.split()[0] if document else "notes"
        return make_error(
            error_type="document_not_found",
            message=f"Document not found: '{document}'",
            suggestion=(
                f"Try remarkable_browse(query='{search_term}') to search, "
                "or remarkable_browse('/') to list all files."
            ),
            did_you_mean=similar if similar else None,
        )

    raw_doc = await run_blocking(client.download, target_doc)
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp.write(raw_doc)
        tmp_path = Path(tmp.name)

    try:
        total_pages = await run_blocking(get_document_page_count, tmp_path)
        if total_pages == 0:
            return make_error(
                error_type="no_pages",
                message=f"Document '{target_doc.VissibleName}' has no renderable pages.",
                suggestion=(
                    "This may be a PDF/EPUB without annotations. "
                    "Use remarkable_read() to extract text content instead."
                ),
            )
        if page < 1 or page > total_pages:
            return make_error(
                error_type="page_out_of_range",
                message=f"Page {page} does not exist. Document has {total_pages} page(s).",
                suggestion=f"Use page=1 to {total_pages} to view different pages.",
            )

        png_data = await run_blocking(
            render_page_from_document_zip, tmp_path, page, background_color=background
        )
        render_source = "strokes"
        if png_data is None:
            pdf_bytes = await run_blocking(download_raw_file, client, target_doc, "pdf")
            if pdf_bytes:
                png_data = await run_blocking(render_tablet_pdf_page_to_png, pdf_bytes, page)
                if png_data is not None:
                    render_source = "tablet_pdf"

        if png_data is None:
            return make_error(
                error_type="render_failed",
                message="Failed to render page to image.",
                suggestion=(
                    "The page may be empty, or local rendering dependencies may be "
                    "missing. Try remarkable_read() to extract text instead."
                ),
            )
    finally:
        tmp_path.unlink(missing_ok=True)

    doc_path = _apply_root_filter(get_item_path(target_doc, items_by_id))
    png_base64 = base64.b64encode(png_data).decode("utf-8")
    data_uri = f"data:image/png;base64,{png_base64}"

    width_px = height_px = None
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(png_data)) as im:
            width_px, height_px = im.size
    except Exception:
        pass

    structured = {
        "document": doc_path,
        "document_name": target_doc.VissibleName,
        "page": page,
        "total_pages": total_pages,
        "png_data_uri": data_uri,
        "page_width_px": width_px,
        "page_height_px": height_px,
        "render_source": render_source,
        "transport": get_active_transport(),
        "writable": False,
    }

    resource_uri = f"remarkableimg:///{doc_path.lstrip('/')}.page-{page}.png"
    blob = types.BlobResourceContents(uri=resource_uri, mimeType="image/png", blob=png_base64)
    info = types.TextContent(
        type="text",
        text=(
            f"Page {page}/{total_pages} of '{target_doc.VissibleName}'. "
            "Open in an MCP Apps-capable client to view the interactive canvas."
        ),
    )
    return types.CallToolResult(
        content=[info, types.EmbeddedResource(type="resource", resource=blob)],
        structuredContent=structured,
    )


def register_app_tools():
    """Register the interactive canvas app resource and tool with the server.

    ``mcp`` is imported here rather than at module load so that importing
    ``app_canvas`` never triggers ``server`` (which imports this module and
    registers these tools). That avoids a circular import when ``app_canvas``
    happens to be imported before ``server``.
    """
    from remarkable_mcp.server import mcp

    @mcp.resource(CANVAS_RESOURCE_URI, mime_type=APP_UI_MIME)
    def remarkable_canvas_ui() -> str:
        """HTML for the reMarkable interactive canvas app (MCP Apps surface)."""
        return _CANVAS_HTML

    @mcp.tool(
        structured_output=False,
        annotations=CANVAS_ANNOTATIONS,
        meta={
            "ui": {
                "resourceUri": CANVAS_RESOURCE_URI,
                "visibility": ["model", "app"],
                "preferredDisplayMode": "inline",
            },
            # ChatGPT/OpenAI Apps SDK legacy alias for the output template.
            "openai/outputTemplate": CANVAS_RESOURCE_URI,
        },
    )
    async def remarkable_canvas(
        document: str,
        page: int = 1,
        ctx: Optional[Context] = None,
    ):
        """
        <usecase>Open a reMarkable page in an interactive canvas viewer.</usecase>
        <instructions>
        Renders a notebook or document page and, in clients that support MCP Apps
        interactive UI, opens a canvas viewer that lets the user page through the
        document. This is the entry point for the interactive viewer.

        The canvas is currently a read-only viewer (render + page navigation).
        For plain image extraction without the interactive surface, use
        remarkable_image instead.

        Clients that do not support MCP Apps still get the rendered page back as
        an embedded PNG image, so this tool is useful everywhere; it just won't
        open the interactive panel.
        </instructions>
        <parameters>
        - document: Document name or path (use remarkable_browse to find documents)
        - page: Page number to open (default: 1, 1-indexed)
        </parameters>
        <examples>
        - remarkable_canvas("Meeting Notes")
        - remarkable_canvas("/Work/Sketches/Wireframe", page=3)
        </examples>
        """
        result = await _render_canvas_page(document, page, ctx)
        # When the client can't render the app, a short note is helpful; the
        # embedded image is already included for those clients.
        if isinstance(result, types.CallToolResult) and ctx is not None:
            if not client_supports_apps(ctx):
                logger.debug("Client does not advertise MCP Apps UI; returning image only.")
        return result

    logger.info("Registered interactive canvas app (ui=%s).", CANVAS_RESOURCE_URI)
