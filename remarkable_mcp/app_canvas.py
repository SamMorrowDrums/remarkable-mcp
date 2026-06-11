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
  html, body { margin: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    display: flex; flex-direction: column; background: #1e1e1e; color: #e8e8e8;
  }
  header {
    display: flex; align-items: center; gap: .5rem; padding: .5rem .75rem;
    border-bottom: 1px solid rgba(255,255,255,.12); flex: 0 0 auto;
    position: sticky; top: 0; background: #1e1e1e; z-index: 2; flex-wrap: wrap;
  }
  header .title { font-weight: 600; flex: 1 1 auto; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  button {
    font: inherit; padding: .35rem .7rem; border-radius: .4rem; cursor: pointer;
    border: 1px solid rgba(255,255,255,.2); background: #2d2d2d; color: inherit;
  }
  button:disabled { opacity: .4; cursor: default; }
  .pageinfo { font-variant-numeric: tabular-nums; min-width: 5rem; text-align: center; }
  main { flex: 1 1 auto; display: flex; align-items: flex-start;
    justify-content: center; padding: 1rem; }
  img { display: block; max-width: 100%; height: auto; background: #fbfbfb;
    box-shadow: 0 1px 8px rgba(0,0,0,.4); border-radius: 2px; }
  .status { padding: 1rem; opacity: .8; }
  .stage { position: relative; display: inline-block; line-height: 0; }
  .stage canvas.ink { position: absolute; left: 0; top: 0;
    touch-action: none; cursor: crosshair; }
  .tools { display: flex; align-items: center; gap: .4rem; }
  .tools[hidden] { display: none; }
  select { font: inherit; padding: .3rem; border-radius: .4rem;
    background: #2d2d2d; color: inherit; border: 1px solid rgba(255,255,255,.2); }
  footer { flex: 0 0 auto; padding: .35rem .75rem; font-size: .8rem; opacity: .6;
    border-top: 1px solid rgba(255,255,255,.12); }
  footer .dirty { color: #ffd479; }
</style>
</head>
<body>
  <header>
    <span class="title" id="title">reMarkable Canvas</span>
    <button id="prev" disabled>&larr; Prev</button>
    <span class="pageinfo" id="pageinfo">--</span>
    <button id="next" disabled>Next &rarr;</button>
    <button id="full" hidden>Fullscreen</button>
    <button id="draw" hidden>Draw</button>
    <span class="tools" id="tools" hidden>
      <select id="tool" title="Pen">
        <option value="fineliner">Pen</option>
        <option value="highlighter">Highlighter</option>
      </select>
      <select id="color" title="Color">
        <option value="black">Black</option>
        <option value="gray">Gray</option>
        <option value="red">Red</option>
        <option value="blue">Blue</option>
        <option value="yellow">Yellow</option>
      </select>
      <button id="undo" disabled>Undo</button>
    </span>
    <button id="save" hidden disabled>Save</button>
    <button id="cancel" hidden disabled>Cancel</button>
  </header>
  <main><div class="status" id="status">Loading&hellip;</div></main>
  <footer id="footer">Read-only viewer</footer>
<script>
(function () {
  "use strict";
  var pending = {}, rpcId = 1;
  var state = { document: null, page: 1, total: 1, busy: false,
                displayMode: "inline", displayModes: [],
                writable: false, drawing: false, cache: {}, cur: null,
                img: null, canvas: null };

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
  function digText(payload) {
    // Tools that return a string (make_response / make_error) put JSON in the
    // first text content block. Parse it so the app can read success / _error.
    try {
      var r = payload && (payload.result || payload.toolResult || payload);
      var c = r && r.content;
      if (c && c.length) {
        for (var i = 0; i < c.length; i++) {
          if (c[i] && c[i].type === "text" && c[i].text) return JSON.parse(c[i].text);
        }
      }
    } catch (e) {}
    return null;
  }
  function render(data) {
    if (!data) return;
    if (data.error) { setStatus("Error: " + data.error); return; }
    if (data.document) state.document = data.document;
    if (data.page) state.page = data.page;
    if (data.total_pages) state.total = data.total_pages;
    if (typeof data.writable === "boolean") state.writable = data.writable;
    document.getElementById("title").textContent =
      data.document_name || data.document || "reMarkable Canvas";
    document.getElementById("pageinfo").textContent =
      state.page + " / " + state.total;
    if (data.png_data_uri) {
      var m = document.querySelector("main");
      m.innerHTML = "";
      var stage = document.createElement("div");
      stage.className = "stage";
      var img = document.createElement("img");
      img.alt = "Page " + state.page;
      var cv = document.createElement("canvas");
      cv.className = "ink";
      stage.appendChild(img);
      stage.appendChild(cv);
      m.appendChild(stage);
      state.img = img;
      state.canvas = cv;
      bindDrawing(cv);
      img.onload = function () { sizeOverlay(); redrawOverlay(); notifySize(); };
      img.src = data.png_data_uri;
    }
    refreshControls();
    notifySize();
  }
  function notifySize() {
    // Report an explicit height derived from the host-given width and the
    // page image's natural aspect ratio. Using document.body.scrollHeight is
    // circular here (the image is max-width:100%, so its height depends on the
    // width the host gives us, which the host is in turn deriving from our
    // reported height) and collapses the view to a thin strip in some hosts.
    var w = document.body.scrollWidth || document.documentElement.clientWidth;
    var h = document.body.scrollHeight;
    var img = state.img;
    if (img && img.naturalWidth && img.naturalHeight) {
      var head = document.querySelector("header");
      var foot = document.getElementById("footer");
      var chrome = (head ? head.offsetHeight : 0) + (foot ? foot.offsetHeight : 0);
      var availW = Math.max(80, (document.body.clientWidth || w) - 32); // main padding
      var dispW = Math.min(availW, img.naturalWidth);
      var dispH = img.naturalHeight * (dispW / img.naturalWidth);
      h = Math.ceil(chrome + dispH + 32);
    }
    notify("ui/notifications/size-changed", { width: w, height: h });
  }
  function updateDisplayModeButton() {
    // Per the MCP Apps spec, the View must only request a display mode the host
    // advertises in hostContext.availableDisplayModes. Hide the control entirely
    // when the host does not support fullscreen, rather than showing a button
    // whose request the host silently declines.
    var btn = document.getElementById("full");
    var canFull = state.displayModes.indexOf("fullscreen") !== -1;
    btn.hidden = !canFull;
    btn.textContent = state.displayMode === "fullscreen" ? "Exit fullscreen" : "Fullscreen";
  }
  function applyHostContext(hc) {
    if (!hc) return;
    if (Array.isArray(hc.availableDisplayModes)) state.displayModes = hc.availableDisplayModes;
    if (hc.displayMode) state.displayMode = hc.displayMode;
    updateDisplayModeButton();
  }

  // ---- Drawing: a client-side per-page stroke cache (the transaction buffer).
  // Strokes accumulate locally; nothing touches the device until Save flushes
  // them through remarkable_canvas_write. Cancel discards the cache.
  function pageStrokes() {
    if (!state.cache[state.page]) state.cache[state.page] = [];
    return state.cache[state.page];
  }
  function totalCached() {
    var n = 0;
    for (var k in state.cache) { if (state.cache[k]) n += state.cache[k].length; }
    return n;
  }
  function penTool() { return document.getElementById("tool").value; }
  function penColor() { return document.getElementById("color").value; }
  function colorCss(c) {
    return ({ black: "#111", gray: "#888", red: "#e23", blue: "#36c",
      yellow: "rgba(240,220,60,.45)" })[c] || "#111";
  }
  function ptOf(e) {
    var r = state.canvas.getBoundingClientRect();
    var nx = (e.clientX - r.left) / r.width;
    var ny = (e.clientY - r.top) / r.height;
    return [Math.max(0, Math.min(1, nx)), Math.max(0, Math.min(1, ny))];
  }
  function sizeOverlay() {
    var img = state.img, cv = state.canvas;
    if (!img || !cv) return;
    var w = img.clientWidth, h = img.clientHeight;
    cv.width = w; cv.height = h;
    cv.style.width = w + "px"; cv.style.height = h + "px";
    cv.style.pointerEvents = state.drawing ? "auto" : "none";
  }
  function drawPoly(ctx, stroke, w, h) {
    var pts = stroke.points;
    if (!pts || !pts.length) return;
    ctx.lineJoin = ctx.lineCap = "round";
    ctx.strokeStyle = colorCss(stroke.color);
    ctx.lineWidth = stroke.tool === "highlighter" ? 16 : 2.5;
    ctx.beginPath();
    for (var i = 0; i < pts.length; i++) {
      var x = pts[i][0] * w, y = pts[i][1] * h;
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
  }
  function redrawOverlay() {
    var cv = state.canvas;
    if (!cv) return;
    var ctx = cv.getContext("2d");
    ctx.clearRect(0, 0, cv.width, cv.height);
    var list = state.cache[state.page] || [];
    for (var i = 0; i < list.length; i++) drawPoly(ctx, list[i], cv.width, cv.height);
    if (state.cur) drawPoly(ctx, state.cur, cv.width, cv.height);
  }
  function bindDrawing(cv) {
    cv.onpointerdown = function (e) {
      if (!state.drawing || state.busy) return;
      try { cv.setPointerCapture(e.pointerId); } catch (err) {}
      state.cur = { points: [ptOf(e)], tool: penTool(), color: penColor() };
      e.preventDefault();
    };
    cv.onpointermove = function (e) {
      if (!state.cur) return;
      state.cur.points.push(ptOf(e));
      redrawOverlay();
    };
    function endStroke() {
      if (!state.cur) return;
      if (state.cur.points.length >= 2) pageStrokes().push(state.cur);
      state.cur = null;
      redrawOverlay();
      refreshControls();
    }
    cv.onpointerup = endStroke;
    cv.onpointercancel = endStroke;
  }
  function setDrawing(on) {
    state.drawing = on;
    if (state.canvas) state.canvas.style.pointerEvents = on ? "auto" : "none";
    refreshControls();
  }
  function undo() {
    var list = pageStrokes();
    if (list.length) { list.pop(); redrawOverlay(); refreshControls(); }
  }
  function cancelEdits() {
    state.cache = {};
    state.cur = null;
    redrawOverlay();
    refreshControls();
  }
  function saveEdits() {
    if (state.busy || totalCached() === 0) return;
    var pages = [];
    for (var k in state.cache) {
      if (state.cache[k] && state.cache[k].length) pages.push(parseInt(k, 10));
    }
    pages.sort(function (a, b) { return a - b; });
    state.busy = true;
    refreshControls();
    var i = 0;
    function step() {
      if (i >= pages.length) {
        state.cache = {};
        state.busy = false;
        goto(state.page); // re-render to show the strokes baked by the device render
        return;
      }
      var pg = pages[i++];
      rpc("tools/call", {
        name: "remarkable_canvas_write",
        arguments: {
          document: state.document, page: pg,
          strokes: state.cache[pg], ui_submitted: true,
        },
      }).then(function (result) {
        var out = digText(result);
        if (out && out._error) {
          state.busy = false;
          setStatus("Save failed: " + out._error.message);
          refreshControls();
          return;
        }
        step();
      }).catch(function (err) {
        state.busy = false;
        setStatus("Save failed: " + (err && err.message ? err.message : err));
        refreshControls();
      });
    }
    step();
  }
  function refreshControls() {
    var w = !!state.writable;
    var dirty = totalCached() > 0;
    document.getElementById("prev").disabled = state.busy || state.page <= 1;
    document.getElementById("next").disabled = state.busy || state.page >= state.total;
    var drawBtn = document.getElementById("draw");
    drawBtn.hidden = !w;
    drawBtn.textContent = state.drawing ? "Done drawing" : "Draw";
    document.getElementById("tools").hidden = !(w && state.drawing);
    document.getElementById("undo").disabled = state.busy || pageStrokes().length === 0;
    var save = document.getElementById("save");
    var cancel = document.getElementById("cancel");
    save.hidden = cancel.hidden = !w;
    save.disabled = state.busy || !dirty;
    cancel.disabled = state.busy || !dirty;
    var footer = document.getElementById("footer");
    if (!w) {
      footer.textContent = "Read-only viewer";
    } else if (dirty) {
      footer.innerHTML = '<span class="dirty">' + totalCached() +
        " unsaved stroke(s) — Save writes them to your reMarkable.</span>";
    } else {
      footer.textContent = "Draw mode — strokes are saved to your reMarkable on Save.";
    }
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
  document.getElementById("draw").onclick = function () { setDrawing(!state.drawing); };
  document.getElementById("undo").onclick = undo;
  document.getElementById("save").onclick = saveEdits;
  document.getElementById("cancel").onclick = cancelEdits;
  document.getElementById("full").onclick = function () {
    var want = state.displayMode === "fullscreen" ? "inline" : "fullscreen";
    if (want === "fullscreen" && state.displayModes.indexOf("fullscreen") === -1) return;
    rpc("ui/request-display-mode", { mode: want }).then(function (res) {
      // Host returns the mode actually set (may differ from the request).
      if (res && res.mode) state.displayMode = res.mode;
      updateDisplayModeButton();
    }).catch(function () {});
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
    // Host-initiated request: the spec sends ui/resource-teardown before
    // removing the View. We can't persist async work mid-teardown, so just
    // acknowledge so the host isn't left waiting. (The View cannot close itself.)
    if (msg.id !== undefined && msg.method === "ui/resource-teardown") {
      post({ jsonrpc: "2.0", id: msg.id, result: {} });
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
    if (msg.method === "ui/notifications/host-context-changed") {
      // Host pushes partial context updates (theme, displayMode, available
      // modes, resize). Merge display-mode fields and refresh the control.
      applyHostContext(msg.params || {});
      return;
    }
  });

  window.addEventListener("resize", function () { sizeOverlay(); redrawOverlay(); notifySize(); });

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
    // Capture host context (theme, supported display modes, dimensions) so the
    // fullscreen control is only offered when the host actually supports it.
    if (res && res.hostContext) applyHostContext(res.hostContext);
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
        render_page_full_page_from_document_zip,
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

        # Full-page render addressed by cPages index (coherent with the write
        # tool) so the overlay's normalized coordinates map exactly onto stroke
        # space and blank pages still render. paper_size is the page's own
        # coordinate extent, surfaced so the model can draw in-bounds.
        paper_size = None
        full = await run_blocking(
            render_page_full_page_from_document_zip, tmp_path, page, background_color=background
        )
        render_source = "strokes"
        if full is not None:
            png_data, paper_wh = full
            paper_size = [round(paper_wh[0]), round(paper_wh[1])]
        else:
            png_data = None
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

    from remarkable_mcp.write_tools import write_enabled

    transport = get_active_transport()
    # The canvas can write strokes back only over SSH (the filesystem transport),
    # and only when write mode is on. Surface that so the app shows/hides Save.
    writable = bool(write_enabled()) and transport == "ssh"

    structured = {
        "document": doc_path,
        "document_name": target_doc.VissibleName,
        "page": page,
        "total_pages": total_pages,
        "png_data_uri": data_uri,
        "page_width_px": width_px,
        "page_height_px": height_px,
        "paper_size": paper_size,
        "render_source": render_source,
        "transport": transport,
        "writable": writable,
    }

    resource_uri = f"remarkableimg:///{doc_path.lstrip('/')}.page-{page}.png"
    blob = types.BlobResourceContents(uri=resource_uri, mimeType="image/png", blob=png_base64)
    # Audience split (MCP content annotations): the rendered page image is for the
    # USER (it's large base64 the model never needs to read), while a lean text
    # digest carries the page/doc/writable facts the model may want — keeping the
    # model's context free of the image blob. When the page is writable we also
    # give the model the drawable geometry + coordinate convention so it can
    # compose in-bounds strokes for remarkable_canvas_write without seeing the
    # image (it draws blind, so these bounds are how it stays on the page).
    digest = (
        f"Page {page}/{total_pages} of '{target_doc.VissibleName}' "
        f"(transport={transport}, writable={'yes' if writable else 'no'})."
    )
    if writable and paper_size:
        digest += (
            f" Drawable page area is {paper_size[0]}x{paper_size[1]} in the page's own"
            " coordinate units. To draw, call remarkable_canvas_write(document, page, strokes)"
            " where each stroke's points are normalized [0,1] from the page's TOP-LEFT"
            " (x rightwards, y downwards); they map onto this exact page. Pass many strokes"
            " in one call."
        )
    elif writable:
        digest += (
            " To draw, call remarkable_canvas_write(document, page, strokes) with points"
            " normalized [0,1] from the page's TOP-LEFT."
        )
    digest += " Rendered in the interactive canvas; the page image is shown to the user."
    info = types.TextContent(
        type="text",
        text=digest,
        annotations=types.Annotations(audience=["assistant"]),
    )
    image = types.EmbeddedResource(
        type="resource",
        resource=blob,
        annotations=types.Annotations(audience=["user"]),
    )
    return types.CallToolResult(
        content=[info, image],
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
