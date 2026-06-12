"""
Text extraction helpers for reMarkable documents.
"""

import json
import logging
import os
import tempfile
import time
import zipfile
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape as _xml_escape

logger = logging.getLogger(__name__)


def _rm_to_svg(rm_file_path: Path, output_svg_path: Path) -> bool:
    """Convert a .rm file to SVG using the built-in rmscene renderers.

    The ``rmc`` dependency has been dropped: it transitively pinned
    ``rmscene<0.7.0``, which cannot parse current-firmware ``.rm`` scene
    blocks (e.g. ``SceneInfo`` ``block_type=13``). We render via ``rmscene``
    directly using the in-repo v6/v5 renderers. v6 is tried first since it is
    the current firmware format.

    Writes the SVG content to output_svg_path and returns True on success.
    Returns False if no renderer could handle the file.

    Credit: khalid-hasan (PR #97), ljdutel (#95)
    """
    svg_content = _render_rm_v6_to_svg(rm_file_path) or _render_rm_v5_to_svg(rm_file_path)
    if svg_content is not None:
        output_svg_path.write_text(svg_content)
        return True

    return False


# reMarkable tablet screen dimensions (in pixels) - used as fallback
REMARKABLE_WIDTH = 1404
REMARKABLE_HEIGHT = 1872

# Standard reMarkable background color (light cream/gray)
# Can be overridden via REMARKABLE_BACKGROUND_COLOR environment variable
_DEFAULT_BACKGROUND_COLOR = "#FBFBFB"


def get_background_color() -> str:
    """Get the background color, checking env var for override."""
    return os.environ.get("REMARKABLE_BACKGROUND_COLOR", _DEFAULT_BACKGROUND_COLOR)


# For backwards compatibility, expose as module constant (evaluated at import)
# Use get_background_color() for runtime evaluation of env var
REMARKABLE_BACKGROUND_COLOR = get_background_color()

# Margin around content when using content-based bounding box (in pixels)
CONTENT_MARGIN = 50

# Target long-edge resolution (px) for a full-page canvas render. The page is
# rasterised at this resolution preserving the page aspect; the displayed image
# is then scaled by the host, so this only sets crispness, not layout.
FULL_PAGE_TARGET_LONG_EDGE = 1872

# Cache TTL in seconds (5 minutes)
CACHE_TTL_SECONDS = 300

# Module-level cache for OCR results (full document)
# Key: doc_id
# Value: {"result": extraction_result, "include_ocr": bool, "timestamp": float}
_extraction_cache: Dict[str, Dict[str, Any]] = {}

# Per-page cache for sampling OCR results
# Key: (doc_id, page_number, backend)
# Value: {"text": str, "timestamp": float}
_page_ocr_cache: Dict[tuple, Dict[str, Any]] = {}


def _is_cache_valid(cached: Dict[str, Any]) -> bool:
    """Check if a cached entry is still valid based on TTL."""
    if "timestamp" not in cached:
        return True  # Old cache entries without timestamp are valid
    return (time.time() - cached["timestamp"]) < CACHE_TTL_SECONDS


def clear_extraction_cache(doc_id: Optional[str] = None) -> None:
    """
    Clear the extraction cache.

    Args:
        doc_id: If provided, only clear cache for this document.
                If None, clear the entire cache.
    """
    if doc_id:
        _extraction_cache.pop(doc_id, None)
        # Also clear per-page cache entries for this document
        keys_to_remove = [k for k in _page_ocr_cache if k[0] == doc_id]
        for key in keys_to_remove:
            _page_ocr_cache.pop(key, None)
    else:
        _extraction_cache.clear()
        _page_ocr_cache.clear()


def get_cached_page_ocr(
    doc_id: str,
    page: int,
    backend: str,
) -> Optional[str]:
    """
    Get cached OCR result for a specific page.

    Args:
        doc_id: Document ID
        page: Page number (1-indexed)
        backend: OCR backend used ("sampling", "google", "tesseract")

    Returns:
        Cached OCR text or None if not cached/expired
    """
    cache_key = (doc_id, page, backend)
    if cache_key in _page_ocr_cache:
        cached = _page_ocr_cache[cache_key]
        if _is_cache_valid(cached):
            return cached["text"]
        # Expired, remove it
        _page_ocr_cache.pop(cache_key, None)
    return None


def cache_page_ocr(
    doc_id: str,
    page: int,
    backend: str,
    text: str,
) -> None:
    """
    Cache OCR result for a specific page.

    Args:
        doc_id: Document ID
        page: Page number (1-indexed)
        backend: OCR backend used ("sampling", "google", "tesseract")
        text: OCR text result
    """
    cache_key = (doc_id, page, backend)
    _page_ocr_cache[cache_key] = {
        "text": text,
        "timestamp": time.time(),
    }


def get_cached_ocr_result(
    doc_id: str,
    include_ocr: bool = True,
    ocr_backend: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Get cached OCR result for a document if available and valid.

    Args:
        doc_id: Document ID to look up
        include_ocr: Whether OCR content is required
        ocr_backend: If specified, only return cache if it was produced by this backend.
                     Use "sampling", "google", or "tesseract". None accepts any backend.

    Returns:
        Cached result dict or None if not cached/expired/wrong backend
    """
    if doc_id in _extraction_cache:
        cached = _extraction_cache[doc_id]
        if (cached["include_ocr"] or not include_ocr) and _is_cache_valid(cached):
            # Check backend match if specified
            if ocr_backend is not None:
                cached_backend = cached["result"].get("ocr_backend")
                if cached_backend != ocr_backend:
                    return None
            return cached["result"]
    return None


def cache_ocr_result(
    doc_id: str,
    result: Dict[str, Any],
    include_ocr: bool = True,
) -> None:
    """
    Cache an OCR result for a document.

    Args:
        doc_id: Document ID
        result: Extraction result dict with keys: typed_text, highlights,
                handwritten_text, pages, page_ids, ocr_backend
        include_ocr: Whether this result includes OCR content
    """
    _extraction_cache[doc_id] = {
        "result": result,
        "include_ocr": include_ocr,
        "timestamp": time.time(),
    }


def find_similar_documents(query: str, documents: List, limit: int = 5) -> List[str]:
    """Find documents with similar names for 'did you mean' suggestions."""
    query_lower = query.lower()
    scored = []
    for doc in documents:
        name = doc.VissibleName
        # Use sequence matcher for fuzzy matching
        ratio = SequenceMatcher(None, query_lower, name.lower()).ratio()
        # Boost partial matches
        if query_lower in name.lower():
            ratio += 0.3
        scored.append((name, ratio))

    scored.sort(key=lambda x: x[1], reverse=True)
    return [name for name, score in scored[:limit] if score > 0.3]


def extract_text_from_pdf(pdf_path: Path) -> str:
    """
    Extract text from a PDF file using PyMuPDF.

    Returns the full text content of the PDF.
    """
    try:
        import fitz  # PyMuPDF

        text_parts = []
        with fitz.open(pdf_path) as doc:
            for page_num, page in enumerate(doc, 1):
                page_text = page.get_text()
                if page_text.strip():
                    text_parts.append(f"--- Page {page_num} ---\n{page_text.strip()}")

        return "\n\n".join(text_parts) if text_parts else ""
    except ImportError:
        return ""
    except Exception:
        return ""


def extract_text_from_epub(epub_path: Path) -> str:
    """
    Extract text from an EPUB file.

    Returns the full text content of the EPUB.
    """
    try:
        from bs4 import BeautifulSoup
        from ebooklib import ITEM_DOCUMENT, epub

        book = epub.read_epub(str(epub_path), options={"ignore_ncx": True})
        text_parts = []

        for item in book.get_items():
            if item.get_type() == ITEM_DOCUMENT:
                soup = BeautifulSoup(item.get_content(), "html.parser")
                # Get text, preserving some structure
                text = soup.get_text(separator="\n", strip=True)
                if text:
                    text_parts.append(text)

        return "\n\n".join(text_parts) if text_parts else ""
    except ImportError:
        return ""
    except Exception:
        return ""


def extract_text_from_rm_file(rm_file_path: Path) -> List[str]:
    """
    Extract typed text from a .rm file using rmscene.

    This extracts text that was typed via Type Folio or on-screen keyboard.
    Does NOT require OCR - text is stored natively in v6 .rm files.
    """
    try:
        from rmscene import read_blocks
        from rmscene.scene_items import Text
        from rmscene.scene_tree import SceneTree

        with open(rm_file_path, "rb") as f:
            tree = SceneTree()
            for block in read_blocks(f):
                tree.add_block(block)

        text_lines = []

        # Extract text from the scene tree
        for item in tree.root.children.values():
            if hasattr(item, "value") and isinstance(item.value, Text):
                text_obj = item.value
                if hasattr(text_obj, "items"):
                    for text_item in text_obj.items:
                        if hasattr(text_item, "value") and text_item.value:
                            text_lines.append(str(text_item.value))

        return text_lines

    except ImportError:
        return []  # rmscene not available
    except Exception:
        # Log but don't fail - file might be older format
        return []


def _parse_hex_color(hex_color: str) -> tuple:
    """Parse a hex color string to RGBA tuple.

    Supports #RRGGBB (RGB) and #RRGGBBAA (RGBA) formats.

    Args:
        hex_color: Hex color string (e.g., "#FFFFFF" or "#FFFFFF80")

    Returns:
        Tuple of (r, g, b, a) values (0-255)
    """
    if not hex_color.startswith("#"):
        return (255, 255, 255, 255)

    hex_str = hex_color.lstrip("#")
    if len(hex_str) == 6:
        r, g, b = tuple(int(hex_str[i : i + 2], 16) for i in (0, 2, 4))
        return (r, g, b, 255)
    elif len(hex_str) == 8:
        r, g, b, a = tuple(int(hex_str[i : i + 2], 16) for i in (0, 2, 4, 6))
        return (r, g, b, a)
    else:
        return (255, 255, 255, 255)


def _get_svg_content_bounds(svg_path: Path) -> Optional[tuple]:
    """
    Parse SVG file to get the content bounding box from viewBox.

    Args:
        svg_path: Path to the SVG file

    Returns:
        Tuple of (min_x, min_y, width, height) or None if not determinable
    """
    import xml.etree.ElementTree as ET

    try:
        tree = ET.parse(svg_path)
        root = tree.getroot()

        # Try to get viewBox attribute
        viewbox = root.get("viewBox")
        if viewbox:
            parts = viewbox.split()
            if len(parts) == 4:
                return (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))

        # Fallback to width/height attributes
        width = root.get("width")
        height = root.get("height")
        if width and height:
            # Remove 'px' suffix if present
            w = float(width.replace("px", ""))
            h = float(height.replace("px", ""))
            return (0, 0, w, h)

        return None
    except Exception:
        return None


CONTENT_PADDING = 20  # Padding around content bounds for SVG viewBox


def _svg_from_paths(paths: list, all_coords: list) -> Optional[str]:
    """Build SVG string with viewBox computed from actual content bounds."""
    if not paths or not all_coords:
        return None

    xs = [c[0] for c in all_coords]
    ys = [c[1] for c in all_coords]
    min_x = min(xs) - CONTENT_PADDING
    min_y = min(ys) - CONTENT_PADDING
    w = max(xs) - min_x + CONTENT_PADDING
    h = max(ys) - min_y + CONTENT_PADDING

    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{min_x:.0f} {min_y:.0f} {w:.0f} {h:.0f}" '
        f'width="{w:.0f}" height="{h:.0f}">'
        f"{''.join(paths)}</svg>"
    )


def _render_rm_v5_to_svg(rm_file_path: Path) -> Optional[str]:
    """
    Render a v5 .rm file (reMarkable .lines format) to SVG.

    The v5 binary format stores layers of strokes, where each stroke
    has pen/color metadata and a sequence of (x, y, speed, tilt, width,
    pressure) segments.
    """
    import struct

    # Pen IDs that are highlighters (both old and v5 mappings)
    HIGHLIGHTER_PENS = {5, 18}
    # Pen IDs that are erasers — skip rendering
    ERASER_PENS = {6, 7, 8}

    # Color mapping by pen type
    STROKE_COLORS = {0: "black", 1: "gray", 2: "white"}
    HIGHLIGHT_COLORS = {0: "#FFD700", 1: "#FFD700", 2: "#FFD700"}

    try:
        with open(rm_file_path, "rb") as f:
            header = f.read(43)
            if b"version=5" not in header:
                return None

            nlayers = struct.unpack("<I", f.read(4))[0]
            paths = []
            all_coords = []

            for _ in range(nlayers):
                nstrokes = struct.unpack("<I", f.read(4))[0]

                for _ in range(nstrokes):
                    pen, color, _pad, _w, _unk, nsegments = struct.unpack("<IIIIfI", f.read(24))

                    segments = []
                    for _ in range(nsegments):
                        x, y, speed, tilt, width, pressure = struct.unpack("<ffffff", f.read(24))
                        segments.append((x, y, width, pressure))

                    if not segments:
                        continue

                    # Skip eraser strokes
                    if pen in ERASER_PENS:
                        continue

                    is_highlighter = pen in HIGHLIGHTER_PENS
                    if is_highlighter:
                        stroke_color = HIGHLIGHT_COLORS.get(color, "#FFD700")
                        avg_width = sum(s[2] for s in segments) / len(segments)
                        stroke_width = max(10.0, min(avg_width * 2.0, 40.0))
                        opacity = ' opacity="0.35"'
                    else:
                        stroke_color = STROKE_COLORS.get(color, "black")
                        avg_width = sum(s[2] for s in segments) / len(segments)
                        stroke_width = max(0.5, min(avg_width * 0.8, 5.0))
                        opacity = ""

                    d = f"M {segments[0][0]:.1f} {segments[0][1]:.1f}"
                    d += "".join(f" L {s[0]:.1f} {s[1]:.1f}" for s in segments[1:])
                    all_coords.extend((s[0], s[1]) for s in segments)

                    paths.append(
                        f'<path d="{d}" stroke="{stroke_color}" '
                        f'stroke-width="{stroke_width:.1f}" '
                        f'fill="none" stroke-linecap="round" '
                        f'stroke-linejoin="round"{opacity}/>'
                    )

        return _svg_from_paths(paths, all_coords)
    except Exception:
        return None


def _v6_blocks(rm_file_path: Path) -> Optional[list]:
    """Read v6 .rm scene blocks, or None if the file is not v6 / unreadable."""
    try:
        from rmscene import read_blocks
    except ImportError:
        return None
    try:
        with open(rm_file_path, "rb") as f:
            header = f.read(43)
            if b"version=6" not in header:
                return None
            f.seek(0)
            return list(read_blocks(f))
    except Exception:
        return None


def _v6_paper_size(blocks: list) -> Tuple[float, float]:
    """Page extent (W, H) in stroke units, read from SceneInfo.paper_size.

    This is the authoritative stroke-coordinate extent for the page and the
    same value the write path (``strokes.page_geometry``) maps normalized
    coordinates into. Building a full-page render from it therefore makes the
    displayed image share ONE coordinate space with written strokes (so the
    drawing overlay and the write tool land ink in the same place). Falls back
    to the standard reMarkable page when no SceneInfo is present.
    """
    for b in blocks:
        ps = getattr(b, "paper_size", None)
        if ps and len(ps) == 2 and ps[0] and ps[1]:
            try:
                return float(ps[0]), float(ps[1])
            except (TypeError, ValueError):
                continue
    return float(REMARKABLE_WIDTH), float(REMARKABLE_HEIGHT)


def _v6_paths_from_blocks(blocks: list) -> Tuple[list, list]:
    """Build SVG ``<path>`` strings + a flat coordinate list from v6 blocks."""
    # Integer pen/color values (rmscene exposes ints on blocks).
    HIGHLIGHTER_PENS = {5, 18}  # HIGHLIGHTER_1, HIGHLIGHTER_2
    ERASER_PENS = {6, 8}  # ERASER, ERASER_AREA
    COLOR_MAP = {
        0: "black",  # BLACK
        1: "#808080",  # GRAY
        2: "white",  # WHITE
        3: "#FFD700",  # YELLOW
        4: "#00A000",  # GREEN
        5: "#FF69B4",  # PINK
        6: "#4169E1",  # BLUE
        7: "#E00000",  # RED
        8: "#A0A0A0",  # GRAY_OVERLAP
        9: "#FFD700",  # HIGHLIGHT
        10: "#00C000",  # GREEN_2
        11: "#00CED1",  # CYAN
        12: "#FF00FF",  # MAGENTA
        13: "#FFD700",  # YELLOW_2
    }

    paths: list = []
    all_coords: list = []
    for block in blocks:
        if not hasattr(block, "item") or not hasattr(block.item, "value"):
            continue
        line = block.item.value
        if not hasattr(line, "points") or not line.points:
            continue

        tool = line.tool if hasattr(line, "tool") else None
        color = line.color if hasattr(line, "color") else 0
        # Convert enums to int if needed
        tool = tool.value if hasattr(tool, "value") else tool
        color = color.value if hasattr(color, "value") else color

        if tool in ERASER_PENS:
            continue

        is_highlighter = tool in HIGHLIGHTER_PENS
        stroke_color = COLOR_MAP.get(color, "black")

        if is_highlighter:
            avg_width = (
                sum(p.width for p in line.points) / len(line.points)
                if all(hasattr(p, "width") for p in line.points)
                else 20.0
            )
            stroke_width = max(10.0, min(avg_width * 2.0, 40.0))
            opacity = ' opacity="0.35"'
        else:
            avg_width = (
                sum(p.width for p in line.points) / len(line.points)
                if all(hasattr(p, "width") for p in line.points)
                else 2.0
            )
            stroke_width = max(0.5, min(avg_width * 0.8, 5.0))
            opacity = ""

        d = f"M {line.points[0].x:.1f} {line.points[0].y:.1f}"
        d += "".join(f" L {p.x:.1f} {p.y:.1f}" for p in line.points[1:])
        all_coords.extend((p.x, p.y) for p in line.points)

        paths.append(
            f'<path d="{d}" stroke="{stroke_color}" '
            f'stroke-width="{stroke_width:.1f}" '
            f'fill="none" stroke-linecap="round" '
            f'stroke-linejoin="round"{opacity}/>'
        )
    return paths, all_coords


# reMarkable typed-text layout, in stroke/screen units. Mirrors the rmc
# exporter (github.com/ricklupton/rmc) so typed text (a RootTextBlock) renders
# in the full-page view at the same place the device draws it. rmc lays text
# out in a 72/226-DPI point space; our full-page SVG works in raw screen units,
# so point font sizes are converted to screen units via _PT_TO_SCREEN.
_TEXT_TOP_Y = -88
_PT_TO_SCREEN = 226.0 / 72.0


def _v6_text_svg_elements(blocks: list) -> list:
    """Build SVG ``<text>`` strings for typed text (a RootTextBlock) on a page.

    Returns an empty list when the page has no typed text (the common case for
    handwritten notebooks) or when rmscene's text helpers are unavailable.
    Coordinates are in the page's own stroke/screen units (center-origin X),
    matching :func:`_svg_full_page`, so text lands where the device shows it.
    """
    try:
        from rmscene.scene_items import ParagraphStyle, Text
        from rmscene.text import TextDocument
    except ImportError:
        return []

    text_item = next(
        (b.value for b in blocks if isinstance(getattr(b, "value", None), Text)),
        None,
    )
    if text_item is None:
        return []

    # Blank pages we synthesize carry an empty RootTextBlock; skip them so we
    # neither emit empty <text> nodes nor trigger rmscene's empty-item warning.
    try:
        if not any(isinstance(v, str) and v.strip() for v in text_item.items.values()):
            return []
    except Exception:
        pass

    line_heights = {
        ParagraphStyle.PLAIN: 70,
        ParagraphStyle.HEADING: 150,
        ParagraphStyle.BOLD: 70,
        ParagraphStyle.BULLET: 35,
        ParagraphStyle.BULLET2: 35,
        ParagraphStyle.CHECKBOX: 35,
        ParagraphStyle.CHECKBOX_CHECKED: 35,
    }
    font_sizes = {
        ParagraphStyle.HEADING: 14 * _PT_TO_SCREEN,
        ParagraphStyle.BOLD: 8 * _PT_TO_SCREEN,
    }
    default_font = 7 * _PT_TO_SCREEN

    try:
        doc = TextDocument.from_scene_item(text_item)
    except Exception:
        return []

    pos_x = float(getattr(text_item, "pos_x", 0.0) or 0.0)
    pos_y = float(getattr(text_item, "pos_y", 0.0) or 0.0)

    elements: list = []
    y_offset = _TEXT_TOP_Y
    for para in doc.contents:
        style = para.style.value if getattr(para, "style", None) is not None else None
        y_offset += line_heights.get(style, 70)
        text = str(para).strip()
        if not text:
            continue
        size = font_sizes.get(style, default_font)
        family = "serif" if style == ParagraphStyle.HEADING else "sans-serif"
        weight = (
            ' font-weight="bold"' if style in (ParagraphStyle.BOLD, ParagraphStyle.HEADING) else ""
        )
        elements.append(
            f'<text x="{pos_x:.1f}" y="{pos_y + y_offset:.1f}" '
            f'font-family="{family}" font-size="{size:.1f}"{weight} '
            f'fill="black" xml:space="preserve">{_xml_escape(text)}</text>'
        )
    return elements


def _render_rm_v6_to_svg(rm_file_path: Path) -> Optional[str]:
    """
    Render a v6 .rm file to a content-cropped SVG (read-only viewing).

    This handles newer reMarkable firmware that uses the v6 format with
    scene tree blocks, including proper highlighter and color support. The
    viewBox is cropped to the ink bounding box; for a full-page render that
    shares the page's own coordinate space, see ``render_rm_file_full_page_png``.
    """
    blocks = _v6_blocks(rm_file_path)
    if blocks is None:
        return None
    try:
        paths, all_coords = _v6_paths_from_blocks(blocks)
        return _svg_from_paths(paths, all_coords)
    except Exception:
        return None


def render_rm_file_to_png(
    rm_file_path: Path, background_color: Optional[str] = None
) -> Optional[bytes]:
    """
    Render a .rm file to PNG image bytes.

    Uses the rmscene renderers to convert .rm to SVG, then cairosvg to convert to PNG.
    The output is sized based on the SVG content bounds with a margin.

    Args:
        rm_file_path: Path to the .rm file
        background_color: Background color (e.g., "#FFFFFF", "transparent", None).
                         None means transparent. Use REMARKABLE_BACKGROUND_COLOR
                         for the standard reMarkable paper color.

    Returns:
        PNG image bytes, or None if rendering failed
    """
    import subprocess
    import tempfile

    tmp_svg_path = None
    tmp_png_path = None
    tmp_raw_path = None

    try:
        # Create temp files
        with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp_svg:
            tmp_svg_path = Path(tmp_svg.name)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_png:
            tmp_png_path = Path(tmp_png.name)

        # Convert .rm to SVG via the rmscene renderers
        if not _rm_to_svg(rm_file_path, tmp_svg_path):
            return None

        # Get content bounds from SVG
        bounds = _get_svg_content_bounds(tmp_svg_path)
        if bounds:
            # Use content bounds with margin
            _, _, content_width, content_height = bounds
            output_width = int(content_width) + 2 * CONTENT_MARGIN
            output_height = int(content_height) + 2 * CONTENT_MARGIN
        else:
            # Fallback to standard reMarkable dimensions
            output_width = REMARKABLE_WIDTH
            output_height = REMARKABLE_HEIGHT

        # Convert SVG to PNG
        try:
            import cairosvg
            from PIL import Image as PILImage

            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_raw:
                tmp_raw_path = Path(tmp_raw.name)

            # Use cairosvg with background_color if specified
            cairosvg.svg2png(
                url=str(tmp_svg_path),
                write_to=str(tmp_raw_path),
                output_width=output_width,
                output_height=output_height,
                background_color=background_color,
            )

            # If no background color specified (transparent), return as-is
            if background_color is None:
                with open(tmp_raw_path, "rb") as f:
                    return f.read()

            # If background color specified, ensure it's applied properly
            img = PILImage.open(tmp_raw_path)
            if img.mode == "RGBA" and background_color:
                # Parse hex color (supports #RRGGBB and #RRGGBBAA formats)
                r, g, b, a = _parse_hex_color(background_color)
                # Create background and composite foreground on top
                if a == 255:
                    # Fully opaque background - convert to RGB
                    bg = PILImage.new("RGB", img.size, (r, g, b))
                    bg.paste(img, mask=img.split()[3])
                    img = bg
                elif a > 0:
                    # Semi-transparent or transparent background
                    bg = PILImage.new("RGBA", img.size, (r, g, b, a))
                    img = PILImage.alpha_composite(bg, img)
                # If a == 0 (fully transparent), return as-is
            img.save(tmp_png_path)

            with open(tmp_png_path, "rb") as f:
                return f.read()

        except ImportError:
            # Fall back to inkscape
            result = subprocess.run(
                ["inkscape", str(tmp_svg_path), "--export-filename", str(tmp_png_path)],
                capture_output=True,
                timeout=30,
            )
            if result.returncode != 0:
                return None

            with open(tmp_png_path, "rb") as f:
                return f.read()

    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None
    finally:
        if tmp_svg_path:
            tmp_svg_path.unlink(missing_ok=True)
        if tmp_png_path:
            tmp_png_path.unlink(missing_ok=True)
        if tmp_raw_path:
            tmp_raw_path.unlink(missing_ok=True)


def _svg_full_page(paths: list, paper_w: float, paper_h: float) -> str:
    """Wrap paths in an SVG whose viewBox spans the WHOLE page in stroke units.

    The page coordinate system is center-origin in X (x in [-W/2, W/2]) and
    top-origin in Y (y in [0, H]) — the same mapping ``strokes._map_point``
    uses (``rm_x = (nx-0.5)*W``, ``rm_y = ny*H``). So a point drawn at
    normalized (nx, ny) over the rendered image lands at exactly the stroke
    coordinate the write tool will use. Empty ``paths`` yields a blank page.
    """
    min_x = -paper_w / 2.0
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{min_x:.0f} 0 {paper_w:.0f} {paper_h:.0f}" '
        f'width="{paper_w:.0f}" height="{paper_h:.0f}">'
        f"{''.join(paths)}</svg>"
    )


def _svg_string_to_png(
    svg: str, output_width: int, output_height: int, background_color: Optional[str]
) -> Optional[bytes]:
    """Rasterize an in-memory SVG string to PNG bytes via cairosvg."""
    try:
        import cairosvg
    except ImportError:
        return None
    try:
        return cairosvg.svg2png(
            bytestring=svg.encode("utf-8"),
            output_width=output_width,
            output_height=output_height,
            background_color=background_color,
        )
    except Exception:
        return None


def render_rm_file_full_page_png(
    rm_file_path: Path, background_color: Optional[str] = None
) -> Optional[Tuple[bytes, Tuple[float, float]]]:
    """Render a v6 .rm page to a FULL-PAGE PNG (not cropped to ink).

    Unlike ``render_rm_file_to_png`` (which crops the viewBox to the ink
    bounding box), this renders the whole page using a viewBox derived from the
    page's own ``SceneInfo.paper_size``. That makes the displayed image map
    linearly to the page's coordinate system, so the interactive drawing
    overlay places strokes exactly where the write tool will, and blank pages
    render as a blank page instead of returning ``None``.

    Returns ``(png_bytes, (paper_w, paper_h))`` or ``None`` if the file is not
    v6 or rendering dependencies are unavailable (callers should fall back).
    """
    blocks = _v6_blocks(rm_file_path)
    if blocks is None:
        return None
    try:
        paths, _ = _v6_paths_from_blocks(blocks)
        text_elements = _v6_text_svg_elements(blocks)
        paper_w, paper_h = _v6_paper_size(blocks)
    except Exception:
        return None

    # Typed text is drawn first so handwritten strokes layer on top of it,
    # matching the device's compositing order.
    svg = _svg_full_page(text_elements + paths, paper_w, paper_h)
    scale = FULL_PAGE_TARGET_LONG_EDGE / max(paper_w, paper_h)
    output_width = max(1, round(paper_w * scale))
    output_height = max(1, round(paper_h * scale))

    png = _svg_string_to_png(svg, output_width, output_height, background_color)
    if png is None:
        return None
    return png, (paper_w, paper_h)


def render_rm_file_to_svg(
    rm_file_path: Path, background_color: Optional[str] = None
) -> Optional[str]:
    """
    Render a .rm file to SVG string.

    Uses the rmscene renderers to convert .rm to SVG, optionally adding a background.

    Args:
        rm_file_path: Path to the .rm file
        background_color: Background color (e.g., "#FFFFFF", None for transparent).
                         Use REMARKABLE_BACKGROUND_COLOR for the standard paper color.

    Returns:
        SVG content as string, or None if rendering failed
    """
    import subprocess
    import tempfile

    tmp_svg_path = None

    try:
        # Create temp file for SVG output
        with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp_svg:
            tmp_svg_path = Path(tmp_svg.name)

        # Convert .rm to SVG via the rmscene renderers
        if not _rm_to_svg(rm_file_path, tmp_svg_path):
            return None

        # Read SVG content
        svg_content = tmp_svg_path.read_text()

        # Add background rectangle if color specified
        if background_color:
            svg_content = _add_svg_background(svg_content, background_color)

        return svg_content

    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None
    finally:
        if tmp_svg_path:
            tmp_svg_path.unlink(missing_ok=True)


def _add_svg_background(svg_content: str, background_color: str) -> str:
    """Add a background rectangle to an SVG.

    Inserts a rect element as the first child of the SVG to act as background.

    Args:
        svg_content: Original SVG content
        background_color: Background color (e.g., "#FFFFFF")

    Returns:
        SVG content with background added
    """
    import re

    # Find the opening <svg> tag and its attributes
    svg_match = re.search(r"(<svg[^>]*>)", svg_content, re.IGNORECASE)
    if not svg_match:
        return svg_content

    svg_tag = svg_match.group(1)

    # Extract viewBox or width/height for the background rect dimensions
    viewbox_match = re.search(r'viewBox="([^"]*)"', svg_tag)
    if viewbox_match:
        viewbox = viewbox_match.group(1)
        parts = viewbox.split()
        if len(parts) == 4:
            x, y, width, height = parts
            bg_rect = (
                f'<rect x="{x}" y="{y}" width="{width}" '
                f'height="{height}" fill="{background_color}"/>'
            )
        else:
            # Fallback to full page
            bg_rect = f'<rect x="0" y="0" width="100%" height="100%" fill="{background_color}"/>'
    else:
        # No viewBox, use 100% dimensions
        bg_rect = f'<rect x="0" y="0" width="100%" height="100%" fill="{background_color}"/>'

    # Insert background rect right after the opening svg tag
    insert_pos = svg_match.end()
    return svg_content[:insert_pos] + bg_rect + svg_content[insert_pos:]


def _get_ordered_rm_files(tmpdir_path: Path) -> List[Path]:
    """Extract and order .rm files from an extracted document directory.

    Reads the .content file to determine page order and returns .rm files
    sorted accordingly. Falls back to filesystem order if no page order found.

    Args:
        tmpdir_path: Path to the extracted document directory

    Returns:
        List of .rm file paths in correct page order
    """
    # Get page order from .content file
    page_order = []
    for content_file in tmpdir_path.glob("*.content"):
        try:
            data = json.loads(content_file.read_text())
            # New format: cPages.pages array
            if "cPages" in data and "pages" in data["cPages"]:
                page_order = [p["id"] for p in data["cPages"]["pages"]]
            # Fallback: pages array directly
            elif "pages" in data and isinstance(data["pages"], list):
                page_order = data["pages"]
        except Exception:
            # Ignore errors reading/parsing .content file; fallback to default page order
            pass
        break

    rm_files = list(tmpdir_path.glob("**/*.rm"))

    # Sort rm_files by page order if available
    if page_order:
        rm_by_id = {}
        for rm_file in rm_files:
            page_id = rm_file.stem
            rm_by_id[page_id] = rm_file

        ordered_rm_files = []
        for page_id in page_order:
            if page_id in rm_by_id:
                ordered_rm_files.append(rm_by_id[page_id])
        # Add any remaining files not in page order
        for rm_file in rm_files:
            if rm_file not in ordered_rm_files:
                ordered_rm_files.append(rm_file)
        return ordered_rm_files

    return rm_files


def render_page_from_document_zip_svg(
    zip_path: Path, page: int = 1, background_color: Optional[str] = None
) -> Optional[str]:
    """
    Render a specific page from a reMarkable document zip to SVG.

    Args:
        zip_path: Path to the document zip file
        page: Page number (1-indexed)
        background_color: Background color (e.g., "#FFFFFF", None for transparent).
                         Use REMARKABLE_BACKGROUND_COLOR for the standard paper color.

    Returns:
        SVG content as string, or None if rendering failed or page doesn't exist
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmpdir_path)

        rm_files = _get_ordered_rm_files(tmpdir_path)

        # Validate page number
        if page < 1 or page > len(rm_files):
            return None

        # Render the requested page
        target_rm_file = rm_files[page - 1]
        return render_rm_file_to_svg(target_rm_file, background_color=background_color)


def render_page_from_document_zip(
    zip_path: Path, page: int = 1, background_color: Optional[str] = None
) -> Optional[bytes]:
    """
    Render a specific page from a reMarkable document zip to PNG.

    Args:
        zip_path: Path to the document zip file
        page: Page number (1-indexed)
        background_color: Background color (e.g., "#FFFFFF", None for transparent).
                         Use REMARKABLE_BACKGROUND_COLOR for the standard paper color.

    Returns:
        PNG image bytes, or None if rendering failed or page doesn't exist
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmpdir_path)

        rm_files = _get_ordered_rm_files(tmpdir_path)

        # Validate page number
        if page < 1 or page > len(rm_files):
            return None

        # Render the requested page
        target_rm_file = rm_files[page - 1]
        return render_rm_file_to_png(target_rm_file, background_color=background_color)


def _document_paper_size(rm_files: List[Path]) -> Tuple[float, float]:
    """Best-effort page extent for a document by scanning its .rm files.

    Used to render a blank page (one with no .rm of its own) at the same size
    as the rest of the document. Falls back to the standard reMarkable page.
    """
    for p in rm_files:
        blocks = _v6_blocks(p)
        if blocks:
            return _v6_paper_size(blocks)
    return float(REMARKABLE_WIDTH), float(REMARKABLE_HEIGHT)


def render_page_full_page_from_document_zip(
    zip_path: Path, page: int = 1, background_color: Optional[str] = None
) -> Optional[Tuple[bytes, Tuple[float, float]]]:
    """Full-page render of a page, addressed by cPages index.

    This is the render used by the interactive canvas. It differs from
    ``render_page_from_document_zip`` in two ways that matter for write-back:

    1. Pages are addressed by the ``.content`` cPages order — the SAME index
       the write tool (``_page_ids_from_content``) uses — so "page N" means the
       same page in the viewer and the writer even when blank pages (which may
       have no ``.rm`` file) are present.
    2. The page is rendered full-bleed using its own ``SceneInfo.paper_size``,
       so the overlay's normalized coordinates map exactly onto stroke space.

    Returns ``(png_bytes, (paper_w, paper_h))`` or ``None`` (caller falls back
    to PDF rasterization or an error).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmpdir_path)

        rm_glob = list(tmpdir_path.glob("**/*.rm"))

        entries = _read_cpages_entries(tmpdir_path)
        page_ids = [e.get("id") for e in entries if isinstance(e, dict) and e.get("id")]

        if page_ids:
            if page < 1 or page > len(page_ids):
                return None
            page_id = page_ids[page - 1]
            rm_file = next((p for p in rm_glob if p.stem == page_id), None)
        else:
            # No cPages metadata: fall back to filesystem/page order of .rm files.
            ordered = _get_ordered_rm_files(tmpdir_path)
            if page < 1 or page > len(ordered):
                return None
            rm_file = ordered[page - 1]

        if rm_file is not None and rm_file.exists():
            return render_rm_file_full_page_png(rm_file, background_color=background_color)

        # The page exists in cPages but has no .rm layer yet (a blank page):
        # render a blank full page at the document's paper size so the viewer
        # still shows it. (The write tool will return no_page_layer until the
        # page has a drawable layer / has been added via remarkable_add_page.)
        paper_w, paper_h = _document_paper_size(rm_glob)
        svg = _svg_full_page([], paper_w, paper_h)
        scale = FULL_PAGE_TARGET_LONG_EDGE / max(paper_w, paper_h)
        png = _svg_string_to_png(
            svg, max(1, round(paper_w * scale)), max(1, round(paper_h * scale)), background_color
        )
        if png is None:
            return None
        return png, (paper_w, paper_h)


def document_zip_has_pdf_underlay(zip_path: Path) -> bool:
    """Check if a reMarkable document zip contains a PDF underlay.

    Args:
        zip_path: Path to the document zip file

    Returns:
        True if the zip contains a .pdf file
    """
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            return any(name.endswith(".pdf") for name in zf.namelist())
    except Exception:
        return False


def _read_cpages_entries(tmpdir_path: Path) -> List[Dict[str, Any]]:
    """Read cPages.pages entries from the .content metadata file.

    Args:
        tmpdir_path: Path to the extracted document directory

    Returns:
        List of cPages page entries, or empty list if not found
    """
    content_file = next(tmpdir_path.glob("*.content"), None)
    if content_file is None:
        return []
    try:
        data = json.loads(content_file.read_text())
        if "cPages" in data and "pages" in data["cPages"]:
            return data["cPages"]["pages"]
    except Exception:
        pass
    return []


def _pdf_page_index_for_cpages_entry(entry: Dict[str, Any]) -> Optional[int]:
    """Get the 0-based PDF page index from a cPages entry's redir field.

    The redir.value field in a cPages entry maps the reMarkable page to
    the original PDF page number (0-based).

    Args:
        entry: A single cPages page entry dict

    Returns:
        0-based PDF page index, or None if no redirect exists
    """
    redir = entry.get("redir", {})
    if isinstance(redir, dict) and "value" in redir:
        try:
            return int(redir["value"])
        except (ValueError, TypeError):
            return None
    return None


def _render_pdf_page_to_png(
    pdf_bytes: bytes, page_index: int, width: int, height: int
) -> Optional[bytes]:
    """Rasterize a single PDF page to PNG bytes using PyMuPDF (fitz).

    Args:
        pdf_bytes: Raw PDF file bytes
        page_index: 0-based page index
        width: Target output width in pixels
        height: Target output height in pixels

    Returns:
        PNG image bytes, or None on failure
    """
    try:
        import fitz

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            if page_index < 0 or page_index >= len(doc):
                return None

            pdf_page = doc[page_index]
            # Scale to fill the target dimensions
            mat = fitz.Matrix(width / pdf_page.rect.width, height / pdf_page.rect.height)
            pix = pdf_page.get_pixmap(matrix=mat, alpha=False)
            return pix.tobytes("png")
        finally:
            doc.close()
    except Exception:
        return None


def render_tablet_pdf_page_to_png(
    pdf_bytes: bytes, page: int = 1, target_long_edge: int = 2048
) -> Optional[bytes]:
    """Rasterize one page of the tablet's native PDF export to PNG bytes.

    This is the portable fallback used when the local stroke renderer cannot
    produce an image. The reMarkable's own firmware renders every notebook (and
    annotated PDF/EPUB) to a PDF served at ``/download/<uuid>/pdf``, so this path
    works regardless of the .rm block format, handles empty pages, and—crucially
    for portability—does not depend on a working ``cairo``/``libcairo`` for
    ``cairosvg`` (PyMuPDF bundles its own renderer). Credit: ljdutel (#95).

    Args:
        pdf_bytes: Raw bytes of the tablet-exported PDF.
        page: 1-based page number (matches notebook page ordering 1:1).
        target_long_edge: Target pixel size for the longest page edge.

    Returns:
        PNG image bytes, or None on failure / out-of-range page.
    """
    try:
        import fitz
    except ImportError:
        return None

    # The tablet's PDFs sometimes reference graphics-state resources MuPDF
    # considers malformed; it still rasterizes them correctly, so suppress the
    # noisy non-fatal error output.
    try:
        fitz.TOOLS.mupdf_display_errors(False)
    except Exception:
        pass

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return None

    try:
        index = page - 1
        if index < 0 or index >= len(doc):
            return None
        pdf_page = doc[index]
        longest_edge = max(pdf_page.rect.width, pdf_page.rect.height) or 1.0
        zoom = target_long_edge / longest_edge
        pix = pdf_page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        return pix.tobytes("png")
    except Exception:
        return None
    finally:
        doc.close()


def render_merged_page_from_document_zip(
    zip_path: Path,
    page: int = 1,
    background_color: Optional[str] = None,
    canvas_width: Optional[int] = None,
    canvas_height: Optional[int] = None,
) -> tuple[Optional[bytes], Optional[str]]:
    """Render a page with the PDF underlay composited with the annotation layer.

    Extracts the zip, determines which PDF page corresponds to the requested
    reMarkable page, rasterizes the PDF page, renders the annotation layer,
    and alpha-composites them into a single image.

    Credit: Re-implementation inspired by PR #79 from @ColinSha.

    Args:
        zip_path: Path to the document zip file
        page: Page number (1-indexed)
        background_color: Background color for annotation layer
        canvas_width: Output canvas width (default: derived from PDF page)
        canvas_height: Output canvas height (default: derived from PDF page)

    Returns:
        Tuple of (png_bytes, note) where note is an informational message
        or None. Returns (None, error_note) on failure.
    """
    import io
    import re

    import fitz
    from PIL import Image as PILImage

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(tmpdir_path)
        except Exception:
            # Fall back to annotation-only
            png = render_page_from_document_zip(zip_path, page, background_color)
            return png, "Could not extract zip; returned annotation-only render."

        # Find the PDF file in the extracted directory. If multiple are present
        # (rare), prefer the one whose stem matches the .content document id so
        # selection is deterministic.
        pdf_files = list(tmpdir_path.glob("**/*.pdf"))
        if not pdf_files:
            rm_files = _get_ordered_rm_files(tmpdir_path)
            if page < 1 or page > len(rm_files):
                return None, f"Page {page} out of range (document has {len(rm_files)} pages)."
            png = render_rm_file_to_png(rm_files[page - 1], background_color=background_color)
            return png, "No PDF underlay found; returned annotation-only render."

        content_stems = {p.stem for p in tmpdir_path.glob("*.content")}
        matching = [p for p in pdf_files if p.stem in content_stems]
        pdf_path = matching[0] if matching else sorted(pdf_files)[0]
        pdf_bytes = pdf_path.read_bytes()

        # Read cPages to find PDF page mapping
        cpages = _read_cpages_entries(tmpdir_path)
        rm_files = _get_ordered_rm_files(tmpdir_path)

        if page < 1 or page > len(rm_files):
            return None, f"Page {page} out of range (document has {len(rm_files)} pages)."

        target_rm_file = rm_files[page - 1]

        # Determine which PDF page this reMarkable page maps to
        pdf_page_index: Optional[int] = None
        if cpages and page <= len(cpages):
            pdf_page_index = _pdf_page_index_for_cpages_entry(cpages[page - 1])

        if pdf_page_index is None:
            # No redirect — this page may be a user-added blank page
            png = render_rm_file_to_png(target_rm_file, background_color=background_color)
            return png, "Page has no PDF underlay (user-added page); annotation-only render."

        # Get PDF page dimensions to set annotation viewBox correctly
        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            try:
                if pdf_page_index >= len(doc):
                    png = render_rm_file_to_png(target_rm_file, background_color=background_color)
                    return png, "PDF page index out of range; annotation-only render."

                pdf_page = doc[pdf_page_index]
                pdf_w_pt = pdf_page.rect.width  # PDF width in points
                pdf_h_pt = pdf_page.rect.height  # PDF height in points
            finally:
                doc.close()
        except Exception:
            png = render_rm_file_to_png(target_rm_file, background_color=background_color)
            return png, "Could not read PDF dimensions; annotation-only render."

        # Determine output canvas size
        out_w = canvas_width or int(pdf_w_pt * 2)  # 2x for decent resolution
        out_h = canvas_height or int(pdf_h_pt * 2)

        # 1. Rasterize the PDF page
        pdf_png = _render_pdf_page_to_png(pdf_bytes, pdf_page_index, out_w, out_h)
        if pdf_png is None:
            png = render_rm_file_to_png(target_rm_file, background_color=background_color)
            return png, "PDF rasterization failed; annotation-only render."

        # 2. Render annotation layer to SVG, then to PNG with transparent background
        ann_svg_path = None
        ann_png_bytes = None

        try:
            with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp_svg:
                ann_svg_path = Path(tmp_svg.name)

            if _rm_to_svg(target_rm_file, ann_svg_path):
                # Read the SVG and adjust viewBox to match PDF page bounds.
                #
                # rmscene's v6 renderer emits stroke coordinates with the
                # origin at the top of the page and x=0 in the horizontal
                # center (so x ranges roughly from -W/2 to +W/2). Setting the
                # viewBox to (-W_pt/2, 0, W_pt, H_pt) maps that coordinate
                # system to the PDF page bounds so annotations align. If the
                # upstream coordinate convention changes, this alignment will
                # need to be revisited.
                svg_content = ann_svg_path.read_text()

                svg_content = re.sub(
                    r'viewBox="[^"]*"',
                    f'viewBox="{-pdf_w_pt / 2:.1f} 0 {pdf_w_pt:.1f} {pdf_h_pt:.1f}"',
                    svg_content,
                )
                # Also set explicit width/height to match output canvas
                svg_content = re.sub(r'width="[^"]*"', f'width="{out_w}"', svg_content)
                svg_content = re.sub(r'height="[^"]*"', f'height="{out_h}"', svg_content)

                # Render SVG to PNG with transparent background
                import cairosvg

                ann_png_data = cairosvg.svg2png(
                    bytestring=svg_content.encode("utf-8"),
                    output_width=out_w,
                    output_height=out_h,
                )
                ann_png_bytes = ann_png_data
        except Exception as exc:
            # Annotation rendering failed; we'll just return the PDF, but
            # record the failure so callers can surface it as a note.
            logger.debug("Annotation overlay rendering failed: %s", exc)
            ann_render_error = exc
        else:
            ann_render_error = None
        finally:
            if ann_svg_path:
                ann_svg_path.unlink(missing_ok=True)

        # 3. Composite: PDF base + annotation overlay
        try:
            pdf_img = PILImage.open(io.BytesIO(pdf_png)).convert("RGBA")

            if ann_png_bytes:
                ann_img = PILImage.open(io.BytesIO(ann_png_bytes)).convert("RGBA")
                # Resize annotation to match PDF if needed
                if ann_img.size != pdf_img.size:
                    ann_img = ann_img.resize(pdf_img.size, PILImage.LANCZOS)
                composite = PILImage.alpha_composite(pdf_img, ann_img)
                merged_note = None
            else:
                composite = pdf_img
                merged_note = (
                    "Annotation overlay failed to render; returned PDF page without annotations."
                    if ann_render_error is not None
                    else None
                )

            # Convert to RGB for PNG output (no alpha needed in final)
            composite = composite.convert("RGB")
            buf = io.BytesIO()
            composite.save(buf, format="PNG")
            return buf.getvalue(), merged_note
        except Exception:
            # Last resort: return annotation-only from already-extracted .rm file
            png = render_rm_file_to_png(target_rm_file, background_color=background_color)
            return png, "Compositing failed; annotation-only render."


def get_document_page_count(zip_path: Path) -> int:
    """
    Get the number of pages in a reMarkable document zip.

    Uses the .content metadata file for accurate page count (includes
    user-added pages in PDFs). Falls back to counting .rm files.

    Args:
        zip_path: Path to the document zip file

    Returns:
        Number of pages (0 if unable to determine)
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmpdir_path)

        # Try .content metadata first — it's the authoritative page list
        for content_file in tmpdir_path.glob("*.content"):
            try:
                data = json.loads(content_file.read_text())
                if "cPages" in data and "pages" in data["cPages"]:
                    return len(data["cPages"]["pages"])
                if "pages" in data and isinstance(data["pages"], list):
                    return len(data["pages"])
            except Exception:
                pass
            break

        # Fallback to counting .rm files
        return len(list(tmpdir_path.glob("**/*.rm")))


def get_document_file_type(zip_path: Path) -> str:
    """
    Read the ``fileType`` from a document zip's ``.content`` file.

    Returns one of "notebook", "pdf", "epub", or "" when it cannot be
    determined. Reads only the ``.content`` entry from the zip (no full
    extraction) so it is cheap to call alongside get_document_page_count.
    """
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in zf.namelist():
                if name.endswith(".content"):
                    try:
                        data = json.loads(zf.read(name).decode("utf-8"))
                    except Exception:
                        return ""
                    return str(data.get("fileType", "") or "")
    except Exception:
        pass
    return ""


def extract_text_from_document_zip(
    zip_path: Path, include_ocr: bool = False, doc_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Extract all text content from a reMarkable document zip.

    Args:
        zip_path: Path to the document zip file
        include_ocr: Whether to run OCR on handwritten content
        doc_id: Optional document ID for caching OCR results

    Returns:
        {
            "typed_text": [...],      # From rmscene parsing (list of strings)
            "highlights": [...],       # From PDF annotations
            "handwritten_text": [...], # From OCR (if enabled) - one per page, in order
            "pages": int,
            "page_ids": [...],         # Page UUIDs in order
            "ocr_backend": str,        # Which OCR backend was used (if any)
        }
    """
    # Check cache if doc_id provided
    if doc_id and doc_id in _extraction_cache:
        cached = _extraction_cache[doc_id]
        # Return cached result if OCR requirement is satisfied and cache is valid
        # (cached with OCR can satisfy no-OCR request, but not vice versa)
        if (cached["include_ocr"] or not include_ocr) and _is_cache_valid(cached):
            return cached["result"]

    result: Dict[str, Any] = {
        "typed_text": [],
        "highlights": [],
        "handwritten_text": None,
        "pages": 0,
        "page_ids": [],
        "ocr_backend": None,
        "tags": [],
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmpdir_path)

        # Get page order from .content file
        page_order = []
        for content_file in tmpdir_path.glob("*.content"):
            try:
                data = json.loads(content_file.read_text())
                # New format: cPages.pages array
                if "cPages" in data and "pages" in data["cPages"]:
                    page_order = [p["id"] for p in data["cPages"]["pages"]]
                # Fallback: pages array directly
                elif "pages" in data and isinstance(data["pages"], list):
                    page_order = data["pages"]
            except Exception:
                # Malformed .content file - continue without page order
                pass
            break  # Only process first .content file

        rm_files = list(tmpdir_path.glob("**/*.rm"))

        # If we have page order, sort rm_files accordingly
        if page_order:
            # Create mapping of page_id -> rm_file
            rm_by_id = {}
            for rm_file in rm_files:
                page_id = rm_file.stem  # filename without extension
                rm_by_id[page_id] = rm_file

            # Sort rm_files by page order
            ordered_rm_files = []
            for page_id in page_order:
                if page_id in rm_by_id:
                    ordered_rm_files.append(rm_by_id[page_id])
            # Add any remaining files not in page order
            for rm_file in rm_files:
                if rm_file not in ordered_rm_files:
                    ordered_rm_files.append(rm_file)
            rm_files = ordered_rm_files
            result["page_ids"] = [f.stem for f in rm_files]

        result["pages"] = len(rm_files)

        # Extract typed text from .rm files using rmscene
        for rm_file in rm_files:
            text_lines = extract_text_from_rm_file(rm_file)
            result["typed_text"].extend(text_lines)

        # Extract text from .txt and .md files
        for txt_file in tmpdir_path.glob("**/*.txt"):
            try:
                content = txt_file.read_text(errors="ignore")
                if content.strip():
                    result["typed_text"].append(content)
            except Exception:
                # File read failed - skip this file and continue
                pass

        for md_file in tmpdir_path.glob("**/*.md"):
            try:
                content = md_file.read_text(errors="ignore")
                if content.strip():
                    result["typed_text"].append(content)
            except Exception:
                # File read failed - skip this file and continue
                pass

        # Extract from .content files (metadata with text and tags)
        for content_file in tmpdir_path.glob("**/*.content"):
            try:
                data = json.loads(content_file.read_text())
                if "text" in data:
                    result["typed_text"].append(data["text"])
                if "tags" in data and data["tags"]:
                    result["tags"] = data["tags"]
            except Exception:
                # Malformed JSON or read error - skip this file
                pass

        # Extract PDF highlights
        for json_file in tmpdir_path.glob("**/*.json"):
            try:
                data = json.loads(json_file.read_text())
                if isinstance(data, dict) and "highlights" in data:
                    for h in data.get("highlights", []):
                        if "text" in h and h["text"]:
                            result["highlights"].append(h["text"])
            except Exception:
                # Malformed JSON - skip this file
                pass

        # OCR for handwritten content (optional)
        if include_ocr and rm_files:
            ocr_result, ocr_backend = extract_handwriting_ocr(rm_files)
            result["handwritten_text"] = ocr_result
            result["ocr_backend"] = ocr_backend

    # Cache result if doc_id provided
    if doc_id:
        _extraction_cache[doc_id] = {
            "result": result,
            "include_ocr": include_ocr,
            "timestamp": time.time(),
        }

    return result


def extract_handwriting_ocr(rm_files: List[Path]) -> tuple[Optional[List[str]], Optional[str]]:
    """
    Extract handwritten text using OCR.

    Supports multiple backends (set REMARKABLE_OCR_BACKEND env var):
    - "sampling": Uses client's LLM via MCP sampling (requires async context, tools only)
    - "google": Google Cloud Vision - best for handwriting
    - "tesseract": pytesseract - basic OCR, requires cairosvg (or inkscape)
    - "auto" (default): Google if API key provided, else Tesseract

    Note: "sampling" backend requires async context and is only available via tools,
    not via MCP resources. When sampling is configured but this sync function is called
    (e.g., from resources), it falls back to the auto-detection logic.

    Returns:
        Tuple of (ocr_results, backend_used) where backend_used is "google" or "tesseract"
    """
    import os

    backend = os.environ.get("REMARKABLE_OCR_BACKEND", "auto").lower()

    # Sampling backend requires async context - can't be used from sync functions
    # Fall back to auto-detection for resources and other sync callers
    if backend == "sampling":
        backend = "auto"

    # Auto-detect best available backend
    if backend == "auto":
        # Check for Google Vision API key first (simplest auth method)
        if os.environ.get("GOOGLE_VISION_API_KEY"):
            backend = "google"
        else:
            backend = "tesseract"

    if backend == "google":
        result = _ocr_google_vision(rm_files)
        return (result, "google")
    else:
        result = _ocr_tesseract(rm_files)
        return (result, "tesseract")


def _ocr_google_vision(rm_files: List[Path]) -> Optional[List[str]]:
    """
    OCR using Google Cloud Vision API.
    Best quality for handwriting recognition.

    Supports two authentication methods:
    1. GOOGLE_VISION_API_KEY env var (simplest - just an API key)
    2. GOOGLE_APPLICATION_CREDENTIALS or default credentials (service account)
    """
    import os

    api_key = os.environ.get("GOOGLE_VISION_API_KEY")

    if api_key:
        # Use REST API with API key (simpler, no SDK needed)
        return _ocr_google_vision_rest(rm_files, api_key)
    else:
        # Use SDK with service account credentials
        return _ocr_google_vision_sdk(rm_files)


def _ocr_google_vision_rest(rm_files: List[Path], api_key: str) -> Optional[List[str]]:
    """
    OCR using Google Cloud Vision REST API with API key.
    """
    import base64
    import subprocess
    import tempfile

    import requests

    ocr_results = []

    for rm_file in rm_files:
        tmp_svg_path = None
        tmp_png_path = None
        tmp_raw_path = None
        try:
            # Create temp files
            with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp_svg:
                tmp_svg_path = Path(tmp_svg.name)
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_png:
                tmp_png_path = Path(tmp_png.name)

            # Convert .rm to SVG via the rmscene renderers
            if not _rm_to_svg(rm_file, tmp_svg_path):
                continue
            # Convert SVG to PNG
            try:
                import cairosvg
                from PIL import Image as PILImage

                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_raw:
                    tmp_raw_path = Path(tmp_raw.name)

                cairosvg.svg2png(
                    url=str(tmp_svg_path),
                    write_to=str(tmp_raw_path),
                    output_width=REMARKABLE_WIDTH,
                    output_height=REMARKABLE_HEIGHT,
                )

                # Add white background
                img = PILImage.open(tmp_raw_path)
                if img.mode == "RGBA":
                    bg = PILImage.new("RGB", img.size, (255, 255, 255))
                    bg.paste(img, mask=img.split()[3])
                    img = bg
                img.save(tmp_png_path)
                tmp_raw_path.unlink(missing_ok=True)
                tmp_raw_path = None
            except ImportError:
                result = subprocess.run(
                    ["inkscape", str(tmp_svg_path), "--export-filename", str(tmp_png_path)],
                    capture_output=True,
                    timeout=30,
                )
                if result.returncode != 0:
                    continue

            # Read and encode image
            with open(tmp_png_path, "rb") as f:
                image_content = base64.b64encode(f.read()).decode("utf-8")

            # Call Google Vision REST API
            url = f"https://vision.googleapis.com/v1/images:annotate?key={api_key}"
            payload = {
                "requests": [
                    {
                        "image": {"content": image_content},
                        "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                    }
                ]
            }

            response = requests.post(url, json=payload, timeout=60)
            if response.status_code == 200:
                data = response.json()
                if "responses" in data and data["responses"]:
                    resp = data["responses"][0]
                    if "fullTextAnnotation" in resp:
                        text = resp["fullTextAnnotation"]["text"]
                        if text.strip():
                            ocr_results.append(text.strip())
            elif response.status_code in (401, 403):
                # API key invalid or API not enabled - fall back to Tesseract
                return _ocr_tesseract(rm_files)

        except subprocess.TimeoutExpired:
            # Page rendering timed out - skip this page and continue
            pass
        except Exception:
            # API call or rendering failed - skip this page and continue
            pass
        finally:
            if tmp_svg_path:
                tmp_svg_path.unlink(missing_ok=True)
            if tmp_png_path:
                tmp_png_path.unlink(missing_ok=True)
            if tmp_raw_path:
                tmp_raw_path.unlink(missing_ok=True)

    return ocr_results if ocr_results else None


def _ocr_google_vision_sdk(rm_files: List[Path]) -> Optional[List[str]]:
    """
    OCR using Google Cloud Vision SDK with service account credentials.
    """
    try:
        import subprocess
        import tempfile

        from google.cloud import vision

        client = vision.ImageAnnotatorClient()
        ocr_results = []

        for rm_file in rm_files:
            tmp_svg_path = None
            tmp_png_path = None
            tmp_raw_path = None
            try:
                # Create temp files
                with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp_svg:
                    tmp_svg_path = Path(tmp_svg.name)
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_png:
                    tmp_png_path = Path(tmp_png.name)

                # Convert .rm to SVG via the rmscene renderers
                if not _rm_to_svg(rm_file, tmp_svg_path):
                    continue
                try:
                    import cairosvg
                    from PIL import Image as PILImage

                    # Convert to PNG (comes out with transparent background)
                    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_raw:
                        tmp_raw_path = Path(tmp_raw.name)

                    cairosvg.svg2png(
                        url=str(tmp_svg_path),
                        write_to=str(tmp_raw_path),
                        output_width=REMARKABLE_WIDTH,
                        output_height=REMARKABLE_HEIGHT,
                    )

                    # Add white background (SVG renders as black-on-transparent)
                    img = PILImage.open(tmp_raw_path)
                    if img.mode == "RGBA":
                        bg = PILImage.new("RGB", img.size, (255, 255, 255))
                        bg.paste(img, mask=img.split()[3])
                        img = bg
                    img.save(tmp_png_path)
                    tmp_raw_path.unlink(missing_ok=True)
                    tmp_raw_path = None
                except ImportError:
                    # Fall back to inkscape
                    result = subprocess.run(
                        ["inkscape", str(tmp_svg_path), "--export-filename", str(tmp_png_path)],
                        capture_output=True,
                        timeout=30,
                    )
                    if result.returncode != 0:
                        continue

                # Send to Google Vision API
                with open(tmp_png_path, "rb") as f:
                    content = f.read()

                image = vision.Image(content=content)

                # Use DOCUMENT_TEXT_DETECTION for best handwriting results
                response = client.document_text_detection(image=image)

                if response.error.message:
                    continue

                if response.full_text_annotation.text:
                    ocr_results.append(response.full_text_annotation.text.strip())

            except subprocess.TimeoutExpired:
                # Page rendering timed out - skip this page and continue
                pass
            except Exception:
                # Rendering or API error - skip this page and continue
                pass
            finally:
                if tmp_svg_path:
                    tmp_svg_path.unlink(missing_ok=True)
                if tmp_png_path:
                    tmp_png_path.unlink(missing_ok=True)
                if tmp_raw_path:
                    tmp_raw_path.unlink(missing_ok=True)

        return ocr_results if ocr_results else None

    except ImportError:
        # google-cloud-vision not installed, fall back to tesseract
        return _ocr_tesseract(rm_files)
    except Exception:
        # API error, fall back to tesseract
        return _ocr_tesseract(rm_files)


def _ocr_tesseract(rm_files: List[Path]) -> Optional[List[str]]:
    """
    OCR using Tesseract.
    Basic quality - designed for printed text, not handwriting.

    Requires: pytesseract, cairosvg (or inkscape)
    """
    try:
        import subprocess
        import tempfile

        import pytesseract
        from PIL import Image, ImageFilter, ImageOps

        ocr_results = []

        for rm_file in rm_files:
            tmp_svg_path = None
            tmp_png_path = None
            tmp_raw_path = None
            try:
                # Create temp files
                with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp_svg:
                    tmp_svg_path = Path(tmp_svg.name)
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_png:
                    tmp_png_path = Path(tmp_png.name)

                # Convert .rm to SVG via the rmscene renderers
                if not _rm_to_svg(rm_file, tmp_svg_path):
                    continue

                # Convert SVG to PNG with higher resolution for better OCR
                try:
                    import cairosvg

                    # Convert to PNG (comes out with transparent background)
                    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_raw:
                        tmp_raw_path = Path(tmp_raw.name)

                    # Use 1.5x resolution for better OCR (2x is too slow)
                    cairosvg.svg2png(
                        url=str(tmp_svg_path),
                        write_to=str(tmp_raw_path),
                        output_width=2106,  # 1.5x reMarkable width
                        output_height=2808,  # 1.5x reMarkable height
                    )

                    # Add white background (SVG renders as black-on-transparent)
                    img = Image.open(tmp_raw_path)
                    if img.mode == "RGBA":
                        bg = Image.new("RGB", img.size, (255, 255, 255))
                        bg.paste(img, mask=img.split()[3])
                        img = bg
                    img.save(tmp_png_path)
                    tmp_raw_path.unlink(missing_ok=True)
                    tmp_raw_path = None
                except ImportError:
                    result = subprocess.run(
                        ["inkscape", str(tmp_svg_path), "--export-filename", str(tmp_png_path)],
                        capture_output=True,
                        timeout=30,
                    )
                    if result.returncode != 0:
                        continue

                # Preprocess image for better OCR
                img = Image.open(tmp_png_path)

                # Convert to grayscale
                img = img.convert("L")

                # Increase contrast
                img = ImageOps.autocontrast(img, cutoff=2)

                # Slight sharpening
                img = img.filter(ImageFilter.SHARPEN)

                # Run OCR with optimized settings for sparse handwriting
                # PSM 11 = Sparse text - find as much text as possible
                # PSM 6 = Uniform block of text (alternative)
                custom_config = r"--psm 11 --oem 3"
                text = pytesseract.image_to_string(img, config=custom_config)

                if text.strip():
                    ocr_results.append(text.strip())

            except subprocess.TimeoutExpired:
                # Page rendering timed out - skip this page and continue
                pass
            except Exception:
                # Rendering or OCR error - skip this page and continue
                pass
            finally:
                if tmp_svg_path:
                    tmp_svg_path.unlink(missing_ok=True)
                if tmp_png_path:
                    tmp_png_path.unlink(missing_ok=True)
                if tmp_raw_path:
                    tmp_raw_path.unlink(missing_ok=True)

        return ocr_results if ocr_results else None

    except ImportError:
        # OCR dependencies not installed
        return None
