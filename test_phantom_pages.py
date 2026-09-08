"""Regression tests for deleted-page (tombstone) handling in page counts.

reMarkable firmware never removes entries from ``cPages.pages`` when a page
is deleted; it writes a last-writer-wins ``deleted`` register into the entry
(``{"timestamp": ..., "value": 1}``) and hides it in the tablet UI. Reading
the raw array length therefore overcounts ``total_pages`` for any document
whose pages were ever deleted, and positional page addressing resolves
deleted slots. Verified against a real document: 61 raw entries, 25
tombstones, 36 visible pages (firmware's own top-level ``pageCount``: 36).

See ``_is_page_deleted`` / ``_visible_cpages_entries`` in
``remarkable_mcp/extract.py`` and issue #184.
"""

import json
import tempfile
import zipfile
from pathlib import Path

import pytest

from remarkable_mcp.extract import (
    _get_page_order,
    _is_page_deleted,
    _read_cpages_entries,
    _resolve_pdf_page_index,
    _visible_cpages_entries,
    extract_text_from_document_zip,
    get_document_page_count,
)


def _entry(page_id: str, deleted: bool = False) -> dict:
    """Build a cPages.pages entry shaped like firmware output."""
    entry = {
        "id": page_id,
        "idx": {"timestamp": "1:2", "value": "ba"},
        "template": {"timestamp": "1:2", "value": "Blank"},
    }
    if deleted:
        entry["deleted"] = {"timestamp": "3:1", "value": 1}
    return entry


def _make_zip(tmp: Path, content: dict, rm_ids=()) -> Path:
    """Create a document zip holding ``content`` and placeholder .rm files."""
    src = tmp / "doc"
    src.mkdir(exist_ok=True)
    (src / "doc.content").write_text(json.dumps(content))
    for page_id in rm_ids:
        (src / f"{page_id}.rm").write_bytes(b"\x00\x01")
    zip_path = tmp / "doc.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for f in sorted(src.iterdir()):
            zf.write(f, f.name)
    return zip_path


def _extract(zip_path: Path) -> Path:
    """Extract a document zip into a fresh directory and return it."""
    out = zip_path.parent / zip_path.stem
    out.mkdir(exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(out)
    return out


# ---------------------------------------------------------------------------
# _is_page_deleted / _visible_cpages_entries
# ---------------------------------------------------------------------------


def test_is_page_deleted_register_forms():
    """Truthy register value marks deletion; missing or zero keeps the page."""
    assert _is_page_deleted(_entry("a", deleted=True)) is True
    assert _is_page_deleted(_entry("a")) is False
    undo = _entry("a")
    undo["deleted"] = {"timestamp": "3:1", "value": 0}
    assert _is_page_deleted(undo) is False
    assert _is_page_deleted({"id": "a", "deleted": 1}) is True


def test_visible_cpages_entries_filters_only_deleted():
    """Only tombstoned entries are dropped; order is preserved."""
    entries = [_entry("p1"), _entry("d1", deleted=True), _entry("p2")]
    assert [e["id"] for e in _visible_cpages_entries(entries)] == ["p1", "p2"]


# ---------------------------------------------------------------------------
# Page counts
# ---------------------------------------------------------------------------


def test_page_count_excludes_tombstones():
    """get_document_page_count reports visible pages, not raw array length."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        entries = [_entry(f"p{i}") for i in range(4)] + [
            _entry(f"d{i}", deleted=True) for i in range(3)
        ]
        content = {"formatVersion": 2, "cPages": {"pages": entries}}
        zip_path = _make_zip(tmp, content)
        assert get_document_page_count(zip_path) == 4


def test_page_count_matches_firmware_pagecount_on_real_document_shape():
    """Replica of issue #184: 36 kept + 25 tombstones -> 36 (not 61).

    The firmware's top-level ``pageCount`` (visible pages) is the oracle the
    fixed count must agree with.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        entries = [_entry(f"id-{i:02d}") for i in range(36)] + [
            _entry(f"del-{i:02d}", deleted=True) for i in range(25)
        ]
        content = {
            "formatVersion": 2,
            "fileType": "notebook",
            "pageCount": 36,
            "cPages": {"pages": entries},
        }
        zip_path = _make_zip(tmp, content)
        assert get_document_page_count(zip_path) == content["pageCount"]
        assert get_document_page_count(zip_path) == 36


def test_page_count_unchanged_without_tombstones():
    """Documents with no deletions report the same count as before."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        entries = [_entry(f"p{i}") for i in range(5)]
        content = {"formatVersion": 2, "cPages": {"pages": entries}}
        assert get_document_page_count(_make_zip(tmp, content)) == 5


def test_page_count_fallback_counts_rm_files():
    """Without cPages/pages the .rm fallback is unchanged."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        content = {"formatVersion": 2}
        assert get_document_page_count(_make_zip(tmp, content, rm_ids=["a", "b"])) == 2


def test_read_cpages_entries_filters_by_default_keeps_raw_on_opt_in():
    """_read_cpages_entries filters tombstones unless include_deleted=True."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        entries = [_entry("p1"), _entry("d1", deleted=True), _entry("p2")]
        (tmp / "doc.content").write_text(
            json.dumps({"formatVersion": 2, "cPages": {"pages": entries}})
        )
        assert [e["id"] for e in _read_cpages_entries(tmp)] == ["p1", "p2"]
        assert len(_read_cpages_entries(tmp, include_deleted=True)) == 3


# ---------------------------------------------------------------------------
# Page ordering and addressing
# ---------------------------------------------------------------------------


def test_get_page_order_excludes_tombstones():
    """_get_page_order returns the visible page sequence."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        entries = [_entry("p1"), _entry("d1", deleted=True), _entry("p2")]
        (tmp / "doc.content").write_text(
            json.dumps({"formatVersion": 2, "cPages": {"pages": entries}})
        )
        assert _get_page_order(tmp) == ["p1", "p2"]


def test_resolve_pdf_page_index_addresses_visible_pages():
    """PDF-page resolution indexes the visible sequence, not raw slots."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        p1, p3 = _entry("p1"), _entry("p3")
        p1["redir"] = {"timestamp": "1:1", "value": 0}
        p3["redir"] = {"timestamp": "1:1", "value": 2}
        entries = [p1, _entry("d1", deleted=True), p3]
        (tmp / "doc.content").write_text(
            json.dumps({"formatVersion": 2, "cPages": {"pages": entries}})
        )
        # Page 2 (visible) is p3 with PDF page 2. The tombstone in raw slot 2
        # must not be addressed, nor reported as a user-added page (None).
        assert _resolve_pdf_page_index(tmp, 2) == 2


def test_extract_document_content_reports_visible_pages():
    """extract_text_from_document_zip derives pages/page_ids post-filter."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        visible_ids = [f"p{i}" for i in range(3)]
        entries = [_entry(visible_ids[0]), _entry("d1", deleted=True)] + [
            _entry(pid) for pid in visible_ids[1:]
        ]
        content = {"formatVersion": 2, "cPages": {"pages": entries}}
        zip_path = _make_zip(tmp, content, rm_ids=visible_ids)
        result = extract_text_from_document_zip(zip_path)
        assert result["pages"] == 3
        assert result["page_ids"] == visible_ids


def test_flat_formatversion1_pages_untouched():
    """formatVersion 1 flat page-id lists have no tombstones; unchanged."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        content = {"formatVersion": 1, "pages": ["a", "b", "c"]}
        zip_path = _make_zip(tmp, content)
        assert get_document_page_count(zip_path) == 3


# ---------------------------------------------------------------------------
# Write tools and notebooks
# ---------------------------------------------------------------------------


def test_page_ids_from_content_excludes_tombstones():
    """write_tools page addressing sees the visible sequence."""
    from remarkable_mcp.write_tools import _page_ids_from_content

    content = {"cPages": {"pages": [_entry("p1"), _entry("d1", deleted=True), _entry("p2")]}}
    assert _page_ids_from_content(content) == ["p1", "p2"]


def test_append_page_reports_visible_count_with_tombstones():
    """Appending after deletions: index/count track visible pages."""
    from remarkable_mcp.notebooks import append_page_to_content

    content = {
        "cPages": {"pages": [_entry("p1"), _entry("p2"), _entry("d1", deleted=True)]},
        "pageCount": 2,
    }
    updated = append_page_to_content(content, "new-id")
    assert updated["total_pages"] == 3
    assert updated["page_index"] == 3
    assert content["pageCount"] == 3


def test_append_page_unchanged_without_tombstones():
    """Clean documents keep the previous append arithmetic."""
    from remarkable_mcp.notebooks import append_page_to_content

    content = {"cPages": {"pages": [_entry("p1"), _entry("p2")]}, "pageCount": 2}
    updated = append_page_to_content(content, "new-id")
    assert updated["total_pages"] == 3
    assert content["pageCount"] == 3


@pytest.mark.parametrize(
    "deleted_value,expected",
    [(1, 1), (0, 2), (True, 1)],
    ids=["value-1", "value-0-undo", "value-true"],
)
def test_tombstone_value_semantics(deleted_value, expected):
    """Register value truthiness decides visibility (0 = undo restores)."""
    entry = _entry("d1")
    entry["deleted"] = {"timestamp": "3:1", "value": deleted_value}
    assert len(_visible_cpages_entries([_entry("p1"), entry])) == expected
