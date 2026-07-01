"""Unit tests for render_merged PDF page-index resolution.

Regression coverage for formatVersion 1 documents (flat ``pages`` list plus a
``redirectionPageMap``), which were previously misread as user-added pages so
``render_merged`` fell back to an annotation-only render for cloud-imported
PDFs. See ``_resolve_pdf_page_index`` in ``remarkable_mcp/extract.py``.
"""

import json
import tempfile
from pathlib import Path

from remarkable_mcp.extract import _resolve_pdf_page_index


def _write_content(tmp: Path, content: dict) -> None:
    (tmp / "doc.content").write_text(json.dumps(content))


def test_formatversion1_redirection_page_map():
    """formatVersion 1 imports map via redirectionPageMap (index -> PDF page)."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_content(tmp, {"formatVersion": 1, "redirectionPageMap": [0, 1, 2, 3]})
        assert _resolve_pdf_page_index(tmp, 1) == 0
        assert _resolve_pdf_page_index(tmp, 3) == 2


def test_formatversion1_user_added_page_is_none():
    """A -1 entry marks a user-added page with no PDF underlay."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_content(tmp, {"formatVersion": 1, "redirectionPageMap": [0, -1, 1]})
        assert _resolve_pdf_page_index(tmp, 2) is None


def test_formatversion2_cpages_redir_still_works():
    """The existing cPages.redir path (formatVersion 2) is unchanged."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_content(
            tmp,
            {
                "cPages": {
                    "pages": [
                        {"id": "a", "redir": {"value": 0}},
                        {"id": "b", "redir": {"value": 1}},
                    ]
                }
            },
        )
        assert _resolve_pdf_page_index(tmp, 1) == 0
        assert _resolve_pdf_page_index(tmp, 2) == 1


def test_no_mapping_returns_none():
    """Without cPages or redirectionPageMap the page has no resolvable underlay."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_content(tmp, {"formatVersion": 1, "pages": ["a", "b"]})
        assert _resolve_pdf_page_index(tmp, 1) is None
