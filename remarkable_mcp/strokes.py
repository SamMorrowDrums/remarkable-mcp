"""Pure ``.rm`` composition: append pen strokes onto an existing page.

This module is deliberately decoupled from any transport (no SSH/cloud/USB).
It takes the *original raw bytes* of a reMarkable v6 ``.rm`` page plus a list of
plain stroke dicts and returns new raw bytes with the strokes appended.

Why append-only?
----------------
On newer firmware (Paper Pro and friends) a full ``read_blocks`` ->
``write_blocks`` round-trip is *lossy*: rmscene does not understand every field
of the ``SceneInfo`` block and silently drops a few bytes, which can corrupt the
page. Instead we:

1. Parse the original bytes only to *discover* the target layer, paper size and
   the highest CRDT id already in use (read-only).
2. Serialize *only the new* ``SceneLineItemBlock``s with ``write_blocks``.
3. Strip the 43-byte ``HEADER_V6`` prefix from that fresh serialization.
4. Concatenate the headerless new-block bytes onto the *untouched* original
   bytes.

The original bytes are preserved exactly as a prefix, so nothing the device
wrote is ever rewritten. See ``test_server.py`` for the regression guard that
asserts this property (and that a naive round-trip does *not* byte-match).

Coordinate model
----------------
The canvas / model speaks normalized coordinates with a top-left origin in the
range ``[0, 1]``. ``.rm`` uses a center-origin X axis and a top origin Y axis,
both in *logical page units* given by the page's own ``SceneInfo.paper_size``
(falling back to the rM1/rM2 default ``1404 x 1872`` when absent). The mapping
is::

    rm_x = (nx - 0.5) * paper_width
    rm_y =  ny        * paper_height

This is device-agnostic: the page carries its own ``paper_size``, so a notebook
authored on a Paper Pro maps correctly even when edited from another device.
"""

from __future__ import annotations

import io
import logging
from typing import Iterable, Optional

from rmscene import HEADER_V6, read_blocks, write_blocks
from rmscene.scene_items import Line, Pen, PenColor, Point
from rmscene.scene_stream import (
    CrdtSequenceItem,
    SceneGroupItemBlock,
    SceneInfo,
    SceneLineItemBlock,
    TreeNodeBlock,
)
from rmscene.tagged_block_common import CrdtId

logger = logging.getLogger(__name__)

# rmscene write option that matches the v6 ("3.x") on-device format.
WRITE_VERSION = "3.1"

# Default logical page size when a page has no SceneInfo block (rM1/rM2).
DEFAULT_PAPER_SIZE = (1404, 1872)

# Stroke defaults.
DEFAULT_TOOL = "fineliner"
DEFAULT_COLOR = "black"
DEFAULT_WIDTH = 80
DEFAULT_PRESSURE = 90
DEFAULT_THICKNESS_SCALE = 1.0


class StrokeError(ValueError):
    """Raised when strokes cannot be composed onto a page."""


def _pen_aliases() -> dict[str, int]:
    out: dict[str, int] = {}
    for pen in Pen:
        out[pen.name.lower()] = int(pen.value)
    # Friendly aliases -> the "v2" tools the modern firmware uses by default.
    out.update(
        {
            "fineliner": int(Pen.FINELINER_2.value),
            "ballpoint": int(Pen.BALLPOINT_2.value),
            "marker": int(Pen.MARKER_2.value),
            "pencil": int(Pen.PENCIL_2.value),
            "mechanical_pencil": int(Pen.MECHANICAL_PENCIL_2.value),
            "mechanicalpencil": int(Pen.MECHANICAL_PENCIL_2.value),
            "paintbrush": int(Pen.PAINTBRUSH_2.value),
            "brush": int(Pen.PAINTBRUSH_2.value),
            "highlighter": int(Pen.HIGHLIGHTER_2.value),
            "calligraphy": int(Pen.CALIGRAPHY.value),
            "pen": int(Pen.FINELINER_2.value),
        }
    )
    return out


def _color_aliases() -> dict[str, int]:
    out: dict[str, int] = {}
    for color in PenColor:
        out[color.name.lower()] = int(color.value)
    out.update(
        {
            "grey": int(PenColor.GRAY.value),
            "highlight": int(PenColor.HIGHLIGHT.value),
        }
    )
    return out


PEN_IDS = _pen_aliases()
COLOR_IDS = _color_aliases()


def resolve_tool(tool) -> int:
    """Resolve a pen tool name or int to a valid Pen id."""
    if tool is None:
        return PEN_IDS[DEFAULT_TOOL]
    if isinstance(tool, int):
        return int(Pen(tool).value)
    key = str(tool).strip().lower().replace(" ", "_").replace("-", "_")
    if key in PEN_IDS:
        return PEN_IDS[key]
    raise StrokeError(
        f"Unknown pen tool {tool!r}. Valid names include: "
        "fineliner, ballpoint, marker, pencil, mechanical_pencil, paintbrush, "
        "highlighter, calligraphy."
    )


def resolve_color(color) -> int:
    """Resolve a color name or int to a valid PenColor id."""
    if color is None:
        return COLOR_IDS[DEFAULT_COLOR]
    if isinstance(color, int):
        return int(PenColor(color).value)
    key = str(color).strip().lower().replace(" ", "_").replace("-", "_")
    if key in COLOR_IDS:
        return COLOR_IDS[key]
    raise StrokeError(
        f"Unknown color {color!r}. Valid names include: black, gray/grey, white, "
        "yellow, green, pink, blue, red, cyan, magenta, highlight."
    )


def get_paper_size(blocks: Iterable[object]) -> tuple[int, int]:
    """Return the page's logical ``(width, height)`` from its SceneInfo block.

    Falls back to ``DEFAULT_PAPER_SIZE`` when no SceneInfo / paper_size exists
    (e.g. pages created from scratch that have no SceneInfo block yet).
    """
    for b in blocks:
        if isinstance(b, SceneInfo):
            size = getattr(b, "paper_size", None)
            if size:
                try:
                    w, h = size
                    if w and h:
                        return (int(w), int(h))
                except (TypeError, ValueError):
                    pass
    return DEFAULT_PAPER_SIZE


def _iter_crdt_ids(blocks: Iterable[object]):
    """Yield every CrdtId referenced by a block list (best effort)."""
    for b in blocks:
        for attr in ("tree_id", "node_id", "parent_id", "block_id"):
            v = getattr(b, attr, None)
            if isinstance(v, CrdtId):
                yield v
        item = getattr(b, "item", None)
        if item is not None:
            iid = getattr(item, "item_id", None)
            if isinstance(iid, CrdtId):
                yield iid
            val = getattr(item, "value", None)
            if isinstance(val, CrdtId):
                yield val
        group = getattr(b, "group", None)
        if group is not None:
            gid = getattr(group, "node_id", None)
            if isinstance(gid, CrdtId):
                yield gid


def find_target_layer(blocks: list) -> CrdtId:
    """Find the layer node that new strokes should hang off, dynamically.

    Strategy (most-specific first):
      1. The ``parent_id`` of the last existing ``SceneLineItemBlock`` — joins
         new strokes onto the same layer the page already draws into.
      2. The ``item.value`` (a layer node CrdtId) of the last
         ``SceneGroupItemBlock`` — these parent the visible layers under root.
      3. The ``node_id`` of the last labeled ``TreeNodeBlock`` (a layer group).

    Raises StrokeError with actionable guidance if no layer can be found, rather
    than guessing a hardcoded id that only happens to work on one device.
    """
    for b in reversed(blocks):
        if isinstance(b, SceneLineItemBlock) and isinstance(b.parent_id, CrdtId):
            return b.parent_id
    for b in reversed(blocks):
        if isinstance(b, SceneGroupItemBlock):
            val = getattr(b.item, "value", None)
            if isinstance(val, CrdtId):
                return val
    for b in reversed(blocks):
        if isinstance(b, TreeNodeBlock):
            group = getattr(b, "group", None)
            node_id = getattr(group, "node_id", None)
            label = getattr(group, "label", None)
            if isinstance(node_id, CrdtId) and label is not None:
                return node_id
    raise StrokeError(
        "Could not find a layer to draw on in this page. The .rm file may be "
        "empty or in an unsupported format. Open the page once on the device "
        "(which creates a default layer) and try again."
    )


def allocate_ids(blocks: list, count: int) -> list[CrdtId]:
    """Allocate ``count`` fresh, monotonically-increasing CrdtIds.

    Picks an id strictly greater (by ``part2``) than anything already present so
    the new sequence items never collide with existing content.
    """
    max_id = CrdtId(0, 0)
    for cid in _iter_crdt_ids(blocks):
        if (cid.part1, cid.part2) > (max_id.part1, max_id.part2):
            max_id = cid
    part1 = max_id.part1 if max_id.part1 else 0
    base = max_id.part2
    return [CrdtId(part1, base + 1 + i) for i in range(count)]


def _map_point(nx: float, ny: float, pressure, width, paper_w: int, paper_h: int) -> Point:
    nx = min(1.0, max(0.0, float(nx)))
    ny = min(1.0, max(0.0, float(ny)))
    rm_x = (nx - 0.5) * paper_w
    rm_y = ny * paper_h
    p = DEFAULT_PRESSURE if pressure is None else int(pressure)
    p = min(255, max(0, p))
    w = DEFAULT_WIDTH if width is None else int(width)
    return Point(x=rm_x, y=rm_y, speed=0, direction=0, width=w, pressure=p)


def build_line_block(
    layer_id: CrdtId,
    item_id: CrdtId,
    stroke: dict,
    paper_w: int,
    paper_h: int,
) -> SceneLineItemBlock:
    """Build a single ``SceneLineItemBlock`` from a plain stroke dict.

    Stroke dict contract::

        {
          "points": [[nx, ny], [nx, ny, pressure], ...],  # normalized [0,1] TL
          "tool":   "fineliner" | int | None,
          "color":  "black" | int | None,
          "width":  int | None,
          "thickness_scale": float | None,
        }
    """
    raw_points = stroke.get("points") or []
    if len(raw_points) < 1:
        raise StrokeError("A stroke needs at least one point.")

    width = stroke.get("width")
    pts: list[Point] = []
    for rp in raw_points:
        if len(rp) >= 3:
            nx, ny, pressure = rp[0], rp[1], rp[2]
        else:
            nx, ny, pressure = rp[0], rp[1], None
        pts.append(_map_point(nx, ny, pressure, width, paper_w, paper_h))

    line = Line(
        color=PenColor(resolve_color(stroke.get("color"))),
        tool=Pen(resolve_tool(stroke.get("tool"))),
        points=pts,
        thickness_scale=float(stroke.get("thickness_scale") or DEFAULT_THICKNESS_SCALE),
        starting_length=0.0,
        move_id=None,
    )
    item = CrdtSequenceItem(
        item_id=item_id,
        left_id=CrdtId(0, 0),
        right_id=CrdtId(0, 0),
        deleted_length=0,
        value=line,
    )
    return SceneLineItemBlock(
        parent_id=layer_id,
        item=item,
        extra_data=b"",
        extra_value_data=b"",
    )


def serialize_new_blocks(blocks: list) -> bytes:
    """Serialize blocks with write_blocks and strip the HEADER_V6 prefix."""
    buf = io.BytesIO()
    write_blocks(buf, blocks, options={"version": WRITE_VERSION})
    data = buf.getvalue()
    if data.startswith(HEADER_V6):
        return data[len(HEADER_V6) :]
    return data


def append_strokes(original_bytes: bytes, strokes: list[dict]) -> bytes:
    """Append ``strokes`` to an existing page, returning new raw ``.rm`` bytes.

    The original bytes are preserved verbatim as a prefix of the result.
    """
    if not strokes:
        raise StrokeError("No strokes provided to write.")

    blocks = list(read_blocks(io.BytesIO(original_bytes)))
    layer_id = find_target_layer(blocks)
    paper_w, paper_h = get_paper_size(blocks)
    ids = allocate_ids(blocks, len(strokes))

    new_blocks = [
        build_line_block(layer_id, ids[i], stroke, paper_w, paper_h)
        for i, stroke in enumerate(strokes)
    ]
    appended = serialize_new_blocks(new_blocks)
    return original_bytes + appended


def page_geometry(original_bytes: bytes) -> dict:
    """Return paper size + center-origin info for an existing page (read-only)."""
    blocks = list(read_blocks(io.BytesIO(original_bytes)))
    w, h = get_paper_size(blocks)
    has_scene_info = any(isinstance(b, SceneInfo) for b in blocks)
    layer: Optional[CrdtId] = None
    try:
        layer = find_target_layer(blocks)
    except StrokeError:
        layer = None
    return {
        "paper_width": w,
        "paper_height": h,
        "has_scene_info": has_scene_info,
        "has_layer": layer is not None,
    }
