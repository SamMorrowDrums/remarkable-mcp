"""Unit tests for render_merged PDF page-index resolution.

Regression coverage for formatVersion 1 documents (flat ``pages`` list plus a
``redirectionPageMap``), which were previously misread as user-added pages so
``render_merged`` fell back to an annotation-only render for cloud-imported
PDFs. See ``_resolve_pdf_page_index`` in ``remarkable_mcp/extract.py``.
"""

import json
import tempfile
from pathlib import Path

from remarkable_mcp.extract import (
    _extract_page_annotations,
    _get_ordered_rm_files,
    _resolve_pdf_page_index,
    _select_rm_file_for_page,
)


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


def test_cpages_is_authoritative_over_stale_redirection_page_map():
    """A v2 user-added page (cPages entry without redir) must resolve to None.

    A v1->v2 migrated document can retain a stale redirectionPageMap whose
    order-shifted indices would otherwise composite a wrong PDF underlay.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_content(
            tmp,
            {
                "cPages": {
                    "pages": [
                        {"id": "a", "redir": {"value": 0}},
                        {"id": "b"},  # user-added page, no underlay
                    ]
                },
                "redirectionPageMap": [0, 1],  # stale: would wrongly map page 2
            },
        )
        assert _resolve_pdf_page_index(tmp, 1) == 0
        assert _resolve_pdf_page_index(tmp, 2) is None


def test_no_mapping_returns_none():
    """Without cPages or redirectionPageMap the page has no resolvable underlay."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_content(tmp, {"formatVersion": 1, "pages": ["a", "b"]})
        assert _resolve_pdf_page_index(tmp, 1) is None


def test_select_rm_file_by_page_id_on_sparse_document():
    """The .rm is chosen by page id, not positional index in the compacted list.

    An 8-page doc annotated on pages 2,3,4,6,7,8 must render page 4's own strokes
    (p4.rm), not the 4th entry of the compacted rm list (p6.rm).
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ids = [f"p{i}" for i in range(1, 9)]
        _write_content(tmp, {"cPages": {"pages": [{"id": i} for i in ids]}})
        for pid in ("p2", "p3", "p4", "p6", "p7", "p8"):
            (tmp / f"{pid}.rm").write_bytes(b"")
        rm_files = _get_ordered_rm_files(tmp)

        assert _select_rm_file_for_page(tmp, rm_files, 4).stem == "p4"
        assert _select_rm_file_for_page(tmp, rm_files, 8).stem == "p8"
        # Un-annotated pages have no stroke layer.
        assert _select_rm_file_for_page(tmp, rm_files, 1) is None
        assert _select_rm_file_for_page(tmp, rm_files, 5) is None
        # Out of range.
        assert _select_rm_file_for_page(tmp, rm_files, 9) is None


def test_select_rm_file_positional_without_page_order():
    """With no .content page order, fall back to positional selection."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "a.rm").write_bytes(b"")
        (tmp / "b.rm").write_bytes(b"")
        rm_files = _get_ordered_rm_files(tmp)
        assert _select_rm_file_for_page(tmp, rm_files, 1) is not None
        assert _select_rm_file_for_page(tmp, rm_files, 5) is None


def test_extract_page_annotations_handles_unparseable_file():
    """A missing or non-.rm file yields no highlights and no strokes, not an error."""
    with tempfile.TemporaryDirectory() as td:
        bad = Path(td) / "not_real.rm"
        bad.write_bytes(b"this is not a valid rm file")
        assert _extract_page_annotations(bad) == ([], False)
        assert _extract_page_annotations(Path(td) / "missing.rm") == ([], False)
