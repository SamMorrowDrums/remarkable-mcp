"""Unit tests for render_merged PDF page-index resolution.

Regression coverage for formatVersion 1 documents (flat ``pages`` list plus a
``redirectionPageMap``), which were previously misread as user-added pages so
``render_merged`` fell back to an annotation-only render for cloud-imported
PDFs. See ``_resolve_pdf_page_index`` in ``remarkable_mcp/extract.py``.
"""

import json
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from remarkable_mcp.extract import (
    _annotation_page_viewbox,
    _extract_page_annotations,
    _get_ordered_rm_files,
    _points_per_unit,
    _resolve_pdf_page_index,
    _rewrite_svg_root,
    _select_rm_file_for_page,
    _v6_paths_from_blocks,
    extract_text_from_document_zip,
    render_mapped_pdf_page_from_document_zip,
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


@pytest.mark.parametrize("page", [0, -1, -100])
def test_pdf_page_resolution_rejects_non_positive_pages(page):
    """Invalid pages never wrap around to the final redirect entry."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_content(
            tmp,
            {
                "cPages": {
                    "pages": [
                        {"id": "a", "redir": {"value": 0}},
                        {"id": "b", "redir": {"value": 7}},
                    ]
                },
                "redirectionPageMap": [0, 7],
            },
        )
        assert _resolve_pdf_page_index(tmp, page) is None


def test_flat_pages_without_redirect_map_use_identity_mapping():
    """Server-uploaded v1 PDFs omit redirects, so their flat page order is identity."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_content(tmp, {"formatVersion": 1, "pages": ["a", "b"]})
        assert _resolve_pdf_page_index(tmp, 1) == 0
        assert _resolve_pdf_page_index(tmp, 2) == 1
        assert _resolve_pdf_page_index(tmp, 3) is None


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


def test_select_rm_file_by_page_id_for_formatversion1_sparse_document():
    """The legacy flat page list is authoritative even when .rm files are sparse."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_content(tmp, {"formatVersion": 1, "pages": ["p1", "p2", "p3"]})
        (tmp / "p1.rm").write_bytes(b"")
        (tmp / "p3.rm").write_bytes(b"")
        rm_files = _get_ordered_rm_files(tmp)

        assert _select_rm_file_for_page(tmp, rm_files, 1).stem == "p1"
        assert _select_rm_file_for_page(tmp, rm_files, 2) is None
        assert _select_rm_file_for_page(tmp, rm_files, 3).stem == "p3"


def test_device_scale_selection_and_annotation_viewbox():
    """SceneInfo paper grids select their calibrated per-device coordinate scale."""
    rm2_ppu = _points_per_unit((1404, 1872))
    paper_pro_ppu = _points_per_unit((1620, 2160))

    assert rm2_ppu == pytest.approx(0.3177)
    assert paper_pro_ppu == pytest.approx(0.3144)
    assert _points_per_unit((999, 999)) == pytest.approx(rm2_ppu)
    assert _points_per_unit(None) == pytest.approx(rm2_ppu)

    rm2_viewbox = _annotation_page_viewbox(612, 792, rm2_ppu)
    paper_pro_viewbox = _annotation_page_viewbox(612, 792, paper_pro_ppu)
    assert rm2_viewbox[0] == pytest.approx(-rm2_viewbox[2] / 2)
    assert paper_pro_viewbox[2] > rm2_viewbox[2]
    assert _annotation_page_viewbox(612, 792, rm2_ppu, centered_x=False)[0] == 0


def test_svg_root_rewrite_preserves_stroke_width():
    """Only root width/height change; path stroke-width remains untouched."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="1 2 3 4" '
        'width="3" height="4"><path stroke-width="3.5" width="9"/></svg>'
    )

    rewritten = _rewrite_svg_root(
        svg,
        viewbox=(-100.0, 0.0, 200.0, 300.0),
        width=1200,
        height=1800,
    )

    assert 'viewBox="-100.00 0.00 200.00 300.00"' in rewritten
    assert '<svg xmlns="http://www.w3.org/2000/svg"' in rewritten
    assert 'width="1200" height="1800"' in rewritten
    assert 'stroke-width="3.5"' in rewritten
    assert '<path stroke-width="3.5" width="9"' in rewritten


def test_glyph_range_rectangles_render_as_translucent_highlights():
    """GlyphRange rectangle geometry is emitted alongside pen paths."""
    rectangle = SimpleNamespace(x=10.0, y=20.0, w=30.0, h=8.0)
    glyph_range = SimpleNamespace(rectangles=[rectangle], color=3)
    block = SimpleNamespace(item=SimpleNamespace(value=glyph_range))

    paths, coords = _v6_paths_from_blocks([block])

    assert paths == [
        '<rect x="10.0" y="20.0" width="30.0" height="8.0" '
        'fill="#FFD700" opacity="0.35" stroke="none"/>'
    ]
    assert coords == [(10.0, 20.0), (40.0, 28.0)]


def test_glyph_range_rectangles_apply_text_anchor_offsets():
    """Highlights nested under anchored groups move with the corresponding text."""
    parent_id = object()
    rectangle = SimpleNamespace(x=10.0, y=20.0, w=30.0, h=8.0)
    glyph_range = SimpleNamespace(rectangles=[rectangle], color=3)
    block = SimpleNamespace(parent_id=parent_id, item=SimpleNamespace(value=glyph_range))

    with patch(
        "remarkable_mcp.extract._v6_group_offsets",
        return_value={parent_id: (4.0, 6.0)},
    ):
        paths, coords = _v6_paths_from_blocks([block], anchor_pos={"anchor": 1})

    assert 'x="14.0" y="26.0"' in paths[0]
    assert coords == [(14.0, 26.0), (44.0, 34.0)]


def test_extract_page_annotations_handles_unparseable_file():
    """A missing or non-.rm file yields no highlights and no strokes, not an error."""
    with tempfile.TemporaryDirectory() as td:
        bad = Path(td) / "not_real.rm"
        bad.write_bytes(b"this is not a valid rm file")
        assert _extract_page_annotations(bad) == ([], False)
        assert _extract_page_annotations(Path(td) / "missing.rm") == ([], False)


def test_extract_page_annotations_orders_highlights_and_detects_ink():
    """Stored highlight text is ordered by rectangle location and ink is indexed."""

    class GlyphRange:
        def __init__(self, text, x, y):
            self.text = text
            self.rectangles = [SimpleNamespace(x=x, y=y)]

    class Line:
        pass

    tree = SimpleNamespace(
        walk=lambda: [
            GlyphRange("second", 5, 50),
            Line(),
            GlyphRange("first", 20, 10),
        ]
    )
    with tempfile.TemporaryDirectory() as td:
        rm_file = Path(td) / "page.rm"
        rm_file.write_bytes(b"placeholder")
        with patch("rmscene.read_tree", return_value=tree):
            assert _extract_page_annotations(rm_file) == (["first", "second"], True)


def test_extraction_uses_authoritative_page_count_and_ids_with_blank_pages():
    """Annotated extraction reports all metadata pages, not only existing .rm files."""
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        zip_path = Path(tmp.name)
    try:
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                "doc.content",
                json.dumps({"cPages": {"pages": [{"id": "p1"}, {"id": "p2"}]}}),
            )
            zf.writestr("p1.rm", b"not-a-valid-rm")

        result = extract_text_from_document_zip(zip_path)
        assert result["pages"] == 2
        assert result["page_ids"] == ["p1", "p2"]
    finally:
        zip_path.unlink(missing_ok=True)


def test_mapped_pdf_fallback_uses_redirected_source_page():
    """Source-PDF fallback rasterizes the mapped PDF page, not the page ordinal."""
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        zip_path = Path(tmp.name)
    try:
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                "doc.content",
                json.dumps({"formatVersion": 1, "redirectionPageMap": [2, 0]}),
            )
            zf.writestr("doc.pdf", b"synthetic-pdf")

        with patch(
            "remarkable_mcp.extract.render_tablet_pdf_page_to_png",
            return_value=b"png",
        ) as render_pdf:
            assert render_mapped_pdf_page_from_document_zip(zip_path, page=1) == (
                b"png",
                True,
            )
        render_pdf.assert_called_once_with(
            b"synthetic-pdf",
            page=3,
            target_long_edge=2048,
        )
    finally:
        zip_path.unlink(missing_ok=True)


def test_mapped_pdf_fallback_does_not_invent_underlay_for_user_added_page():
    """A cPages entry without redir blocks stale/ordinal PDF fallback."""
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        zip_path = Path(tmp.name)
    try:
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                "doc.content",
                json.dumps(
                    {
                        "cPages": {"pages": [{"id": "added-page"}]},
                        "redirectionPageMap": [0],
                    }
                ),
            )
            zf.writestr("doc.pdf", b"synthetic-pdf")

        with patch("remarkable_mcp.extract.render_tablet_pdf_page_to_png") as render_pdf:
            assert render_mapped_pdf_page_from_document_zip(zip_path, page=1) == (
                None,
                True,
            )
        render_pdf.assert_not_called()
    finally:
        zip_path.unlink(missing_ok=True)
