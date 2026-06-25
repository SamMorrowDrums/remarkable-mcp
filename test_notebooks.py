"""Tests for native notebook byte builders."""

from io import BytesIO

import pytest

from rmscene import RootTextBlock, read_blocks
from rmscene.scene_items import ParagraphStyle
from rmscene.text import TextDocument

from remarkable_mcp.notebooks import markdown_page_rm_bytes, markdown_pages_rm_bytes


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


def test_markdown_pages_rm_bytes_rejects_bulk_markdown_for_official_client_safety():
    markdown = "# Long note\n" + "\n".join(f"Paragraph {i}" for i in range(1, 121))

    with pytest.raises(ValueError, match="PDF/EPUB"):
        markdown_pages_rm_bytes(markdown, max_lines=40, max_chars=10_000)


def test_markdown_page_rm_bytes_rejects_large_single_page_seed():
    markdown = "# Big blocks\n\n" + "\n\n".join("x" * 80 for _ in range(8))

    with pytest.raises(ValueError, match="native typed-text"):
        markdown_page_rm_bytes(markdown)
