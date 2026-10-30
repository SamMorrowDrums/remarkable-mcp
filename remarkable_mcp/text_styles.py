"""Native paragraph styles for typed text on generated notebook pages.

Markdown line prefixes select one of the paragraph styles a Paper Pro offers,
and the styles are written into the page's root text block. rmscene 0.8 does
not know the numbered-list code (10) and cannot write the two extra fields that
tell the larger heading from the smaller one, so the style entries are encoded
here. Their bytes match a page styled on a Paper Pro and read back from the
cloud; the meaning of the extra fields is unknown.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, replace

from rmscene.scene_stream import RootTextBlock, text_item_to_stream
from rmscene.tagged_block_common import CrdtId, LwwValue
from rmscene.tagged_block_writer import TaggedBlockWriter


class TextStyle(enum.Enum):
    """Paragraph styles the generated pages can carry.

    Each value is ``(format code, extra fields)``; an extra field is
    ``(tag index, "byte" | "int", value)`` written after the format code.
    """

    BODY = (1, ())
    TITLE = (2, ())
    HEADING = (3, ((2, "byte", 2), (3, "int", 3)))
    SUBHEADING = (3, ())
    BULLET = (4, ())
    BULLET2 = (5, ())
    CHECKBOX = (6, ())
    CHECKBOX_CHECKED = (7, ())
    NUMBERED = (10, ())

    @property
    def code(self) -> int:
        return self.value[0]

    @property
    def extra_fields(self) -> tuple:
        return self.value[1]


# Checked against in order, so longer prefixes come before the ones they
# start with ("- [ ] " before "- ", "### " before "# ").
LINE_PREFIXES = (
    ("- [ ] ", TextStyle.CHECKBOX),
    ("- [x] ", TextStyle.CHECKBOX_CHECKED),
    ("- [X] ", TextStyle.CHECKBOX_CHECKED),
    ("  - ", TextStyle.BULLET2),
    ("  * ", TextStyle.BULLET2),
    ("- ", TextStyle.BULLET),
    ("* ", TextStyle.BULLET),
    ("### ", TextStyle.SUBHEADING),
    ("## ", TextStyle.HEADING),
    ("# ", TextStyle.TITLE),
)
# The device numbers list items itself, so the typed number is dropped.
_NUMBERED_PREFIX = re.compile(r"\d+\. ")

# simple_text_document stores the whole text as one CRDT item whose characters
# get consecutive ids starting here; a paragraph's style is keyed by the id of
# the newline that opens it, and the first paragraph by the (0, 0) end marker.
_TEXT_FIRST_CHAR_ID = CrdtId(1, 16)
_STYLE_TIMESTAMP = CrdtId(1, 15)
# Marker byte that opens every style entry on the device.
_STYLE_ENTRY_MARKER = 17


def parse_paragraphs(text: str) -> list[tuple[TextStyle, str]]:
    """Split ``text`` on newlines into ``(style, content)`` paragraphs.

    A line starting with one of :data:`LINE_PREFIXES`, or with ``<digits>. ``
    for a numbered item, takes that style and loses the prefix; every other
    line is ``BODY``.
    """
    paragraphs = []
    for line in text.split("\n"):
        style, content = TextStyle.BODY, line
        for prefix, prefix_style in LINE_PREFIXES:
            if line.startswith(prefix):
                style, content = prefix_style, line[len(prefix) :]
                break
        else:
            numbered = _NUMBERED_PREFIX.match(line)
            if numbered:
                style, content = TextStyle.NUMBERED, line[numbered.end() :]
        paragraphs.append((style, content))
    return paragraphs


def paragraph_styles(paragraphs: list[tuple[TextStyle, str]]) -> dict:
    """Return the ``Text.styles`` map for paragraphs joined with newlines.

    Like ``simple_text_document``, later paragraphs get an entry only when they
    are not ``BODY``: an unstyled paragraph renders as body text, and plain
    input keeps the exact bytes of the unstyled builder.
    """
    styles = {CrdtId(0, 0): LwwValue(timestamp=_STYLE_TIMESTAMP, value=paragraphs[0][0])}
    offset = len(paragraphs[0][1]) + 1
    for style, content in paragraphs[1:]:
        if style != TextStyle.BODY:
            newline_id = CrdtId(_TEXT_FIRST_CHAR_ID.part1, _TEXT_FIRST_CHAR_ID.part2 + offset - 1)
            styles[newline_id] = LwwValue(timestamp=_STYLE_TIMESTAMP, value=style)
        offset += len(content) + 1
    return styles


def _write_style_entry(char_id: CrdtId, value: LwwValue, writer: TaggedBlockWriter) -> None:
    style = value.value
    writer.data.write_crdt_id(char_id)
    writer.write_id(1, value.timestamp)
    with writer.write_subblock(2):
        writer.data.write_uint8(_STYLE_ENTRY_MARKER)
        writer.data.write_uint8(style.code)
        for index, kind, field_value in style.extra_fields:
            if kind == "byte":
                writer.write_byte(index, field_value)
            else:
                writer.write_int(index, field_value)


@dataclass
class StyledRootTextBlock(RootTextBlock):
    """Root text block whose ``styles`` map to :class:`TextStyle` values.

    Serialised with the same layout as ``RootTextBlock.to_stream``; only the
    style entries are encoded differently. rmscene resolves block types on
    ``RootTextBlock`` before its subclasses, so reading is unaffected.
    """

    def to_stream(self, writer: TaggedBlockWriter) -> None:
        writer.write_id(1, self.block_id)
        with writer.write_subblock(2):
            text_items = self.value.items.sequence_items()
            with writer.write_subblock(1):
                with writer.write_subblock(1):
                    writer.data.write_varuint(len(text_items))
                    for item in text_items:
                        text_item_to_stream(item, writer)
            with writer.write_subblock(2):
                with writer.write_subblock(1):
                    writer.data.write_varuint(len(self.value.styles))
                    for char_id, value in self.value.styles.items():
                        _write_style_entry(char_id, value, writer)
        with writer.write_subblock(3):
            writer.data.write_float64(self.value.pos_x)
            writer.data.write_float64(self.value.pos_y)
        writer.write_float(4, self.value.width)


def styled_root_text(
    block: RootTextBlock, paragraphs: list[tuple[TextStyle, str]]
) -> StyledRootTextBlock:
    """Return ``block`` with its styles replaced by those of ``paragraphs``."""
    return StyledRootTextBlock(
        block_id=block.block_id,
        value=replace(block.value, styles=paragraph_styles(paragraphs)),
        extra_data=block.extra_data,
    )
