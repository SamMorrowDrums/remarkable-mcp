"""Tests for native notebook byte builders."""

from io import BytesIO

from rmscene import RootTextBlock, read_blocks
from rmscene.scene_items import ParagraphStyle
from rmscene.text import TextDocument

from remarkable_mcp.notebooks import markdown_page_rm_bytes


def _read_text_document(raw: bytes) -> TextDocument:
    root = next(block for block in read_blocks(BytesIO(raw)) if isinstance(block, RootTextBlock))
    return TextDocument.from_scene_item(root.value)


def test_markdown_page_rm_bytes_round_trips_native_paragraph_styles():
    raw = markdown_page_rm_bytes(
        "# Title\n"
        "Plain paragraph with **bold** and *italic*.\n"
        "- Bullet item\n"
        "  - Nested bullet\n"
        "- [ ] Open item\n"
        "- [x] Done item"
    )

    doc = _read_text_document(raw)

    assert [paragraph.style.value for paragraph in doc.contents] == [
        ParagraphStyle.HEADING,
        ParagraphStyle.PLAIN,
        ParagraphStyle.BULLET,
        ParagraphStyle.BULLET2,
        ParagraphStyle.CHECKBOX,
        ParagraphStyle.CHECKBOX_CHECKED,
    ]
    assert [str(paragraph) for paragraph in doc.contents] == [
        "Title",
        "Plain paragraph with bold and italic.",
        "Bullet item",
        "Nested bullet",
        "Open item",
        "Done item",
    ]


def test_markdown_page_rm_bytes_round_trips_inline_bold_and_italic_spans():
    raw = markdown_page_rm_bytes("Plain **bold** and *italic* text")

    doc = _read_text_document(raw)
    spans = doc.contents[0].contents

    assert [(span.s, span.properties) for span in spans] == [
        ("Plain ", {"font-weight": "normal", "font-style": "normal"}),
        ("bold", {"font-weight": "bold", "font-style": "normal"}),
        (" and ", {"font-weight": "normal", "font-style": "normal"}),
        ("italic", {"font-weight": "normal", "font-style": "italic"}),
        (" text", {"font-weight": "normal", "font-style": "normal"}),
    ]
