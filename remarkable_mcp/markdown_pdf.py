"""Render Markdown as a paginated PDF for reMarkable uploads."""

from html import escape

import pymupdf
from markdown_it import MarkdownIt

_PAGE_MARGIN = 54
_USER_CSS = """
body {
  color: #111;
  font-family: sans-serif;
  font-size: 11pt;
  line-height: 1.45;
}
h1 { font-size: 24pt; margin: 0 0 14pt; }
h2 { font-size: 18pt; margin: 18pt 0 10pt; }
h3 { font-size: 14pt; margin: 14pt 0 8pt; }
p, ul, ol, blockquote, pre, table { margin: 0 0 10pt; }
blockquote {
  border-left: 3px solid #777;
  color: #444;
  padding-left: 10pt;
}
code, pre { font-family: monospace; }
pre {
  background-color: #f2f2f2;
  padding: 8pt;
}
table { border-collapse: collapse; width: 100%; }
th, td {
  border: 1px solid #888;
  padding: 4pt;
  text-align: left;
}
th { background-color: #e8e8e8; }
"""


def _render_image(_renderer, tokens, index, options, env) -> str:
    """Replace images with alt text instead of reading local or remote assets."""
    del options, env
    token = tokens[index]
    alt = escape(token.content or "image")
    return f"<p><em>[Image omitted: {alt}]</em></p>"


def render_markdown_pdf(markdown: str) -> bytes:
    """Render Markdown into A4 PDF bytes without loading external assets."""
    if not markdown or not markdown.strip():
        raise ValueError("Markdown content cannot be empty")

    renderer = MarkdownIt("commonmark", {"html": False}).enable("table")
    renderer.add_render_rule("image", _render_image)
    html = renderer.render(markdown)

    page = pymupdf.paper_rect("a4")
    content = pymupdf.Rect(
        page.x0 + _PAGE_MARGIN,
        page.y0 + _PAGE_MARGIN,
        page.x1 - _PAGE_MARGIN,
        page.y1 - _PAGE_MARGIN,
    )
    story = pymupdf.Story(html=html, user_css=_USER_CSS)

    def page_rect(_rect_number, _filled):
        return page, content, None

    document = story.write_with_links(page_rect)
    try:
        return document.tobytes(garbage=3, deflate=True)
    finally:
        document.close()
