"""Pure-logic builders for new reMarkable notebooks, pages, and text documents.

There is no transport / SSH here. These functions produce the bytes and JSON
dicts that the SSH write path uploads to the tablet:

- A drawable ``.rm`` page (blank, or seeded with typed text).
- A ``.content`` file describing a native notebook (the ``cPages`` page index).
- A ``.metadata`` file describing the document.
- Helpers to append a page entry to an existing notebook's ``.content``.

Pages are built with rmscene's blessed :func:`simple_text_document` builder so
every generated page contains a real drawable layer node. This is the same
mechanism validated by :mod:`remarkable_mcp.strokes` (``find_target_layer``
succeeds on the output, so strokes can be appended immediately afterwards).
``simple_text_document`` emits no ``SceneInfo`` block, so the page renders and
writes at the default paper size (see :data:`DEFAULT_PAPER`).
"""

from __future__ import annotations

import io
import time
import uuid as _uuid
from typing import Optional

from rmscene import PageInfoBlock, RootTextBlock, simple_text_document, write_blocks
from rmscene.crdt_sequence import CrdtSequence, CrdtSequenceItem
from rmscene.scene_items import END_MARKER, ParagraphStyle, Text
from rmscene.tagged_block_common import CrdtId, LwwValue

from remarkable_mcp.strokes import WRITE_VERSION

# reMarkable 1/2 portrait stroke space. simple_text_document emits no SceneInfo,
# so render + write both fall back to this; keep them consistent.
DEFAULT_PAPER = (1404, 1872)

# Official reMarkable clients currently render large native typed-text seeds
# unpredictably even when rmscene can parse the bytes back. Keep native styled
# authoring as a small seed feature; use PDF/EPUB upload for bulk documents.
MAX_SAFE_MARKDOWN_LINES = 6
MAX_SAFE_MARKDOWN_CHARS = 800


def new_uuid() -> str:
    """Return a fresh lowercase UUID string for a document or page id."""
    return str(_uuid.uuid4())


def _author_uuid(author_uuid: Optional[str | _uuid.UUID]):
    """Coerce an author uuid (str/UUID/None) into a ``uuid.UUID``.

    A fresh author uuid is generated when none is supplied so the ``.rm``
    ``AuthorIdsBlock`` and the ``.content`` ``cPages.uuids`` entry can be kept
    consistent by the caller.
    """
    if author_uuid is None:
        return _uuid.uuid4()
    if isinstance(author_uuid, _uuid.UUID):
        return author_uuid
    return _uuid.UUID(str(author_uuid))


def _plain_text_from_values(values: list[str | int]) -> str:
    """Return only user-visible text from text/formatting values."""
    return "".join(value for value in values if isinstance(value, str))


def _inline_markdown_values(text: str) -> list[str | int]:
    """Convert a small inline Markdown subset into reMarkable text items.

    reMarkable stores inline bold/italic as zero-width formatting items inside
    the root text CRDT sequence. rmscene decodes those integer codes as:
    1=bold on, 2=bold off, 3=italic on, 4=italic off.
    """
    values: list[str | int] = []
    buf: list[str] = []
    bold = False
    italic = False
    i = 0

    def flush() -> None:
        if buf:
            values.append("".join(buf))
            buf.clear()

    while i < len(text):
        if text.startswith("**", i):
            flush()
            values.append(2 if bold else 1)
            bold = not bold
            i += 2
            continue
        if text[i] == "*":
            flush()
            values.append(4 if italic else 3)
            italic = not italic
            i += 1
            continue
        buf.append(text[i])
        i += 1

    flush()
    if bold:
        values.append(2)
    if italic:
        values.append(4)
    return values


def _markdown_line_style(line: str) -> tuple[ParagraphStyle, str]:
    """Return native paragraph style and visible text for a Markdown-ish line."""
    if line.startswith("# "):
        return ParagraphStyle.HEADING, line[2:].strip()
    if line.startswith("## "):
        return ParagraphStyle.BOLD, line[3:].strip()

    stripped = line.lstrip(" \t")
    indent = len(line) - len(stripped)
    if stripped.startswith("- [ ] "):
        return ParagraphStyle.CHECKBOX, stripped[6:].strip()
    if stripped.lower().startswith("- [x] "):
        return ParagraphStyle.CHECKBOX_CHECKED, stripped[6:].strip()
    if stripped.startswith("- "):
        style = ParagraphStyle.BULLET2 if indent > 0 else ParagraphStyle.BULLET
        return style, stripped[2:].strip()
    return ParagraphStyle.PLAIN, line


def _styled_text_values(markdown: str) -> tuple[list[str | int], dict[CrdtId, LwwValue]]:
    """Build root text CRDT values and paragraph style map from Markdown."""
    values: list[str | int] = []
    styles: dict[CrdtId, LwwValue] = {}
    next_id = 16
    first_line = True

    for line in markdown.splitlines() or [""]:
        style, visible_text = _markdown_line_style(line)
        if first_line:
            styles[END_MARKER] = LwwValue(CrdtId(1, 15), style)
            first_line = False
        else:
            newline_id = CrdtId(1, next_id)
            values.append("\n")
            next_id += 1
            styles[newline_id] = LwwValue(CrdtId(1, next_id), style)

        for value in _inline_markdown_values(visible_text):
            values.append(value)
            next_id += 1 if isinstance(value, int) else max(1, len(value))

    return values, styles


def _crdt_sequence_from_values(values: list[str | int]) -> CrdtSequence:
    """Create a simple ordered CRDT sequence for text/formatting values."""
    items = []
    prev = END_MARKER
    next_id = 16
    for value in values:
        item_id = CrdtId(1, next_id)
        next_id += 1 if isinstance(value, int) else max(1, len(value))
        item = CrdtSequenceItem(
            item_id=item_id,
            left_id=prev,
            right_id=END_MARKER,
            deleted_length=0,
            value=value,
        )
        if items:
            items[-1].right_id = item_id
        items.append(item)
        prev = item_id
    return CrdtSequence(items)


def ensure_markdown_is_safe_native_seed(
    markdown: str,
    *,
    max_lines: int = MAX_SAFE_MARKDOWN_LINES,
    max_chars: int = MAX_SAFE_MARKDOWN_CHARS,
) -> None:
    """Reject bulk Markdown that official clients do not render reliably.

    Parser round-trips alone are not enough here: long native typed-text seeds
    can look valid to rmscene while the official reMarkable apps display only a
    trailing paragraph or otherwise mangle the page. For long note exports, make
    a PDF/EPUB instead of native typed text.
    """
    lines = markdown.splitlines() or [""]
    visible_lines = [line for line in lines if line.strip()]
    if len(visible_lines) > max_lines or len(markdown) > max_chars:
        raise ValueError(
            "content_markdown is too large for safe native typed-text seeding; "
            "upload a PDF/EPUB for bulk note exports instead"
        )


def split_markdown_pages(
    markdown: str,
    *,
    max_lines: int = 40,
    max_chars: int = 3500,
) -> list[str]:
    """Split Markdown-ish notebook text into page-sized chunks.

    Native typed-text pages are clipped to a single reMarkable page in the
    official clients. A huge CRDT text block can contain all text bytes while the
    tablet only shows the first page area. Split on whole lines so long exports
    become real `cPages` entries instead of one overflowing page.
    """
    if max_lines < 1:
        raise ValueError("max_lines must be at least 1")
    if max_chars < 1:
        raise ValueError("max_chars must be at least 1")

    lines = markdown.splitlines() or [""]
    pages: list[list[str]] = []
    current: list[str] = []
    current_chars = 0

    for line in lines:
        line_chars = len(line) + 1
        would_overflow_lines = len(current) >= max_lines
        would_overflow_chars = current_chars + line_chars > max_chars
        if current and (would_overflow_lines or would_overflow_chars):
            pages.append(current)
            current = []
            current_chars = 0
        current.append(line)
        current_chars += line_chars

    if current:
        pages.append(current)

    return ["\n".join(page).strip("\n") for page in pages] or [""]


def markdown_pages_rm_bytes(
    markdown: str,
    author_uuid: Optional[str | _uuid.UUID] = None,
    *,
    max_lines: int = 40,
    max_chars: int = 3500,
) -> list[bytes]:
    """Return one serialized `.rm` page per visible Markdown chunk."""
    au = _author_uuid(author_uuid)
    ensure_markdown_is_safe_native_seed(markdown, max_lines=max_lines, max_chars=max_chars)
    return [
        markdown_page_rm_bytes(page, author_uuid=au)
        for page in split_markdown_pages(markdown, max_lines=max_lines, max_chars=max_chars)
    ]


def markdown_page_rm_bytes(markdown: str, author_uuid: Optional[str | _uuid.UUID] = None) -> bytes:
    """Return serialized ``.rm`` bytes for styled native typed text.

    Supported Markdown subset:
    - ``# Heading`` -> native heading paragraph
    - ``## Subheading`` -> native bold paragraph
    - ``- item`` / indented ``- item`` -> bullet / nested bullet
    - ``- [ ] item`` / ``- [x] item`` -> unchecked / checked checkbox
    - inline ``**bold**`` and ``*italic*`` spans
    """
    au = _author_uuid(author_uuid)
    ensure_markdown_is_safe_native_seed(markdown)
    values, styles = _styled_text_values(markdown)
    visible_text = _plain_text_from_values(values)
    blocks = list(simple_text_document("", author_uuid=au))
    for i, block in enumerate(blocks):
        if isinstance(block, PageInfoBlock):
            blocks[i] = PageInfoBlock(
                loads_count=block.loads_count,
                merges_count=block.merges_count,
                text_chars_count=len(visible_text) + 1,
                text_lines_count=visible_text.count("\n") + 1,
                type_folio_use_count=block.type_folio_use_count,
            )
        elif isinstance(block, RootTextBlock):
            blocks[i] = RootTextBlock(
                block_id=CrdtId(0, 0),
                value=Text(
                    items=_crdt_sequence_from_values(values),
                    styles=styles,
                    pos_x=block.value.pos_x,
                    pos_y=block.value.pos_y,
                    width=block.value.width,
                ),
            )

    buf = io.BytesIO()
    write_blocks(buf, blocks, options={"version": WRITE_VERSION})
    return buf.getvalue()


def page_rm_bytes(
    text: str = "",
    author_uuid: Optional[str] = None,
    content_markdown: Optional[str] = None,
) -> bytes:
    """Return serialized ``.rm`` bytes for a single drawable page.

    With ``text=""`` this is a blank page; with text it is seeded with typed
    paragraphs (split on newlines). Pass ``content_markdown`` to seed styled
    native typed text using :func:`markdown_page_rm_bytes`. The result always
    contains a drawable layer node, so :func:`remarkable_mcp.strokes.append_strokes`
    works on it.
    """
    if content_markdown is not None:
        return markdown_page_rm_bytes(content_markdown, author_uuid=author_uuid)
    au = _author_uuid(author_uuid)
    blocks = list(simple_text_document(text, author_uuid=au))
    buf = io.BytesIO()
    write_blocks(buf, blocks, options={"version": WRITE_VERSION})
    return buf.getvalue()


def blank_page_rm_bytes(author_uuid: Optional[str] = None) -> bytes:
    """Return serialized ``.rm`` bytes for a blank drawable page."""
    return page_rm_bytes("", author_uuid=author_uuid)


def next_page_idx(existing_idx_values: list[str]) -> str:
    """Return a fractional ``idx.value`` that sorts after all existing ones.

    reMarkable orders pages by these lexicographically-sorted keys (the first
    pages of a notebook are ``ba``, ``bb``, ``bc``, ...). To append at the end
    we take the current maximum and produce the next strictly-greater key by
    incrementing its trailing character (or appending ``a`` when it is already
    ``z``, which still sorts after the original as a longer prefix-extension).
    """
    values = [v for v in existing_idx_values if isinstance(v, str) and v]
    if not values:
        return "ba"
    last = max(values)
    tail = last[-1]
    if tail < "z":
        return last[:-1] + chr(ord(tail) + 1)
    return last + "a"


def _page_entry(page_id: str, idx_value: str, template: str = "Blank") -> dict:
    """Build a single ``cPages.pages[]`` entry."""
    return {
        "id": page_id,
        "idx": {"timestamp": "1:2", "value": idx_value},
        "template": {"timestamp": "1:2", "value": template},
    }


def new_notebook_content(
    page_ids: list[str],
    author_uuid: str,
    paper: tuple[int, int] = DEFAULT_PAPER,
) -> dict:
    """Build a ``.content`` dict for a brand-new native notebook.

    Mirrors the schema xochitl itself writes (``formatVersion`` 2, ``cPages``
    page index, portrait zoom defaults). ``author_uuid`` must match the uuid
    used to build the page ``.rm`` bytes so CRDT author ids line up.
    """
    width, height = paper
    pages = []
    idx_values: list[str] = []
    for page_id in page_ids:
        idx_value = next_page_idx(idx_values)
        idx_values.append(idx_value)
        pages.append(_page_entry(page_id, idx_value))

    return {
        "cPages": {
            "lastOpened": {"timestamp": "1:1", "value": page_ids[0] if page_ids else ""},
            "original": {"timestamp": "0:0", "value": -1},
            "pages": pages,
            "uuids": [{"first": str(author_uuid), "second": 1}],
        },
        "coverPageNumber": -1,
        "customZoomCenterX": 0,
        "customZoomCenterY": height // 2,
        "customZoomOrientation": "portrait",
        "customZoomPageHeight": height,
        "customZoomPageWidth": width,
        "customZoomScale": 1,
        "documentMetadata": {},
        "extraMetadata": {},
        "fileType": "notebook",
        "fontName": "",
        "formatVersion": 2,
        "lineHeight": -1,
        "margins": 125,
        "orientation": "portrait",
        "pageCount": len(page_ids),
        "pageTags": [],
        "sizeInBytes": "0",
        "tags": [],
        "textAlignment": "justify",
        "textScale": 1,
        "zoomMode": "bestFit",
    }


def new_document_metadata(visible_name: str, parent: str = "") -> dict:
    """Build a ``.metadata`` dict for a new document.

    Includes the sync flags (``metadatamodified``/``modified``/``synced``/
    ``version``) so a freshly created document is picked up and synced, matching
    the proven ``remarkable_upload`` SSH path.
    """
    now = str(int(time.time() * 1000))
    return {
        "visibleName": visible_name,
        "type": "DocumentType",
        "parent": parent or "",
        "createdTime": now,
        "lastModified": now,
        "lastOpened": now,
        "lastOpenedPage": 0,
        "pinned": False,
        "deleted": False,
        "metadatamodified": True,
        "modified": True,
        "synced": False,
        "version": 0,
    }


def append_page_to_content(content_data: dict, new_page_id: str) -> dict:
    """Append a blank page entry to an existing notebook's ``.content`` dict.

    Returns ``{"content": <updated dict>, "idx": <new idx value>,
    "page_index": <1-based index>, "total_pages": <new count>}``. The input
    dict is updated in place. Raises :class:`ValueError` if the document is not
    a native ``cPages`` notebook (e.g. a PDF/EPUB with a flat ``pages`` list).
    """
    cpages = content_data.get("cPages")
    if not isinstance(cpages, dict) or not isinstance(cpages.get("pages"), list):
        raise ValueError(
            "Document is not a native notebook (no cPages page index); "
            "pages can only be added to notebooks."
        )

    pages = cpages["pages"]
    existing_idx = [
        p.get("idx", {}).get("value")
        for p in pages
        if isinstance(p, dict) and isinstance(p.get("idx"), dict)
    ]
    idx_value = next_page_idx([v for v in existing_idx if isinstance(v, str)])
    pages.append(_page_entry(new_page_id, idx_value))

    total = len(pages)
    content_data["pageCount"] = total
    return {
        "content": content_data,
        "idx": idx_value,
        "page_index": total,
        "total_pages": total,
    }
