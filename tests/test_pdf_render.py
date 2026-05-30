"""Tests for PDF-backed reMarkable document rendering via pymupdf.

These tests build a self-contained fixture: a minimal reMarkable-style zip that
contains a small multi-page PDF (generated with fitz) and the accompanying
.content / .metadata files.  No libcairo is required, so the tests run locally
even when cairosvg is absent.
"""

import io
import json
import zipfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_minimal_pdf(n_pages: int = 3) -> bytes:
    """Create a tiny N-page PDF using pymupdf and return the bytes."""
    fitz = pytest.importorskip("fitz", reason="pymupdf not installed")
    doc = fitz.open()
    for i in range(n_pages):
        page = doc.new_page(width=595, height=842)  # A4
        page.insert_text((72, 100), f"Page {i + 1} of {n_pages}", fontsize=24)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def _make_remarkable_pdf_zip(n_pages: int = 3, uuid: str = "test-doc-uuid") -> bytes:
    """Return bytes of a minimal reMarkable-style doc zip for a pure-PDF document."""
    pdf_bytes = _make_minimal_pdf(n_pages)

    content_json = json.dumps(
        {
            "fileType": "pdf",
            "pageCount": n_pages,
        }
    )
    metadata_json = json.dumps(
        {
            "visibleName": "Test PDF",
            "type": "DocumentType",
        }
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{uuid}.content", content_json)
        zf.writestr(f"{uuid}.metadata", metadata_json)
        zf.writestr(f"{uuid}.pdf", pdf_bytes)
    return buf.getvalue()


@pytest.fixture()
def pdf_zip_path(tmp_path: Path) -> Path:
    """Write a 3-page pure-PDF reMarkable zip to a temp file and return its path."""
    zip_bytes = _make_remarkable_pdf_zip(n_pages=3)
    p = tmp_path / "test_doc.zip"
    p.write_bytes(zip_bytes)
    return p


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_get_document_page_count_pdf(pdf_zip_path: Path) -> None:
    """get_document_page_count must return the PDF's real page count, not 0."""
    pytest.importorskip("fitz", reason="pymupdf not installed")
    from remarkable_mcp.extract import get_document_page_count

    count = get_document_page_count(pdf_zip_path)
    assert count == 3, f"Expected 3 pages, got {count}"


def test_render_pdf_page_returns_png(pdf_zip_path: Path) -> None:
    """render_pdf_page_from_document_zip must return valid PNG bytes for page 1."""
    pytest.importorskip("fitz", reason="pymupdf not installed")
    from remarkable_mcp.extract import render_pdf_page_from_document_zip

    result = render_pdf_page_from_document_zip(pdf_zip_path, page=1)
    assert result is not None, "render_pdf_page_from_document_zip returned None for page 1"
    assert result[:4] == b"\x89PNG", "Returned bytes do not start with PNG magic bytes"


def test_render_pdf_all_pages(pdf_zip_path: Path) -> None:
    """All pages of a pure-PDF doc must render successfully."""
    pytest.importorskip("fitz", reason="pymupdf not installed")
    from remarkable_mcp.extract import render_pdf_page_from_document_zip

    for page_num in (1, 2, 3):
        result = render_pdf_page_from_document_zip(pdf_zip_path, page=page_num)
        assert result is not None, f"Page {page_num} returned None"
        assert result[:4] == b"\x89PNG", f"Page {page_num} is not a PNG"


def test_render_pdf_page_out_of_range_returns_none(pdf_zip_path: Path) -> None:
    """Requesting a page beyond the PDF's range must return None (not crash)."""
    pytest.importorskip("fitz", reason="pymupdf not installed")
    from remarkable_mcp.extract import render_pdf_page_from_document_zip

    result = render_pdf_page_from_document_zip(pdf_zip_path, page=99)
    assert result is None, "Out-of-range page should return None"


def test_render_pdf_page_zero_returns_none(pdf_zip_path: Path) -> None:
    """Requesting page 0 (invalid 1-indexed) must return None."""
    pytest.importorskip("fitz", reason="pymupdf not installed")
    from remarkable_mcp.extract import render_pdf_page_from_document_zip

    result = render_pdf_page_from_document_zip(pdf_zip_path, page=0)
    assert result is None, "Page 0 (out of range) should return None"


def test_document_zip_is_pure_pdf(pdf_zip_path: Path) -> None:
    """document_zip_is_pure_pdf must return True for a pure-PDF zip."""
    from remarkable_mcp.extract import document_zip_is_pure_pdf

    assert document_zip_is_pure_pdf(pdf_zip_path) is True


def test_render_merged_pure_pdf_returns_png(pdf_zip_path: Path) -> None:
    """render_merged_page_from_document_zip must handle a pure-PDF zip gracefully."""
    pytest.importorskip("fitz", reason="pymupdf not installed")
    from remarkable_mcp.extract import render_merged_page_from_document_zip

    png_bytes, note = render_merged_page_from_document_zip(pdf_zip_path, page=1)
    assert png_bytes is not None, f"render_merged returned None; note={note!r}"
    assert png_bytes[:4] == b"\x89PNG", "Returned bytes do not start with PNG magic bytes"


def test_render_merged_pure_pdf_out_of_range(pdf_zip_path: Path) -> None:
    """render_merged_page_from_document_zip must return (None, error) for out-of-range page."""
    pytest.importorskip("fitz", reason="pymupdf not installed")
    from remarkable_mcp.extract import render_merged_page_from_document_zip

    png_bytes, note = render_merged_page_from_document_zip(pdf_zip_path, page=99)
    assert png_bytes is None, "Out-of-range page should produce None bytes"
    assert note is not None, "Out-of-range page should produce an error note"
    assert "out of range" in note.lower(), f"Unexpected note: {note!r}"
