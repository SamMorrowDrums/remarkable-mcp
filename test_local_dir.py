#!/usr/bin/env python3
"""
Tests for the local-directory transport (remarkable_mcp.local_dir).

Covers client behaviour against a fake xochitl-style directory, directory
resolution, transport selection in api.get_rmapi(), and the read-only
guarantee in write_tools.
"""

import io
import json
import zipfile

import pytest

from remarkable_mcp import api
from remarkable_mcp.local_dir import (
    LocalDirClient,
    check_local_dir_available,
    create_local_dir_client,
    find_default_local_dir,
)

# =============================================================================
# Fixtures
# =============================================================================

NOTEBOOK_ID = "11111111-1111-1111-1111-111111111111"
PDF_ID = "22222222-2222-2222-2222-222222222222"
FOLDER_ID = "33333333-3333-3333-3333-333333333333"
DELETED_ID = "44444444-4444-4444-4444-444444444444"
PAGE_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

RM_V6_HEADER = b"reMarkable .lines file, version=6          " + b"\x00" * 16


@pytest.fixture
def xochitl_dir(tmp_path):
    """Build a minimal fake desktop-app data directory."""
    d = tmp_path / "desktop"
    d.mkdir()

    # A folder
    (d / f"{FOLDER_ID}.metadata").write_text(
        json.dumps(
            {
                "visibleName": "Work",
                "type": "CollectionType",
                "parent": "",
                "lastModified": "1700000000000",
            }
        )
    )

    # A handwritten notebook inside the folder
    (d / f"{NOTEBOOK_ID}.metadata").write_text(
        json.dumps(
            {
                "visibleName": "Meeting Notes",
                "type": "DocumentType",
                "parent": FOLDER_ID,
                "pinned": True,
                "lastModified": "1700000001000",
                "tags": ["important"],
            }
        )
    )
    (d / f"{NOTEBOOK_ID}.content").write_text(
        json.dumps(
            {
                "fileType": "notebook",
                "cPages": {"pages": [{"id": PAGE_ID}]},
            }
        )
    )
    nb_dir = d / NOTEBOOK_ID
    nb_dir.mkdir()
    (nb_dir / f"{PAGE_ID}.rm").write_bytes(RM_V6_HEADER)

    # A PDF-backed document at root
    (d / f"{PDF_ID}.metadata").write_text(
        json.dumps(
            {
                "visibleName": "Paper",
                "type": "DocumentType",
                "parent": "",
                "lastModified": "1700000002000",
            }
        )
    )
    (d / f"{PDF_ID}.content").write_text(json.dumps({"fileType": "pdf"}))
    (d / f"{PDF_ID}.pdf").write_bytes(b"%PDF-1.4 fake pdf bytes")

    # A deleted document (must be skipped)
    (d / f"{DELETED_ID}.metadata").write_text(
        json.dumps(
            {
                "visibleName": "Gone",
                "type": "DocumentType",
                "parent": "",
                "deleted": True,
            }
        )
    )

    return d


@pytest.fixture
def client(xochitl_dir):
    return LocalDirClient(xochitl_dir)


# =============================================================================
# Client behaviour
# =============================================================================


class TestLocalDirClient:
    def test_check_connection(self, client):
        assert client.check_connection() is True

    def test_check_connection_missing_dir(self, tmp_path):
        assert LocalDirClient(tmp_path / "nope").check_connection() is False

    def test_check_connection_empty_dir(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert LocalDirClient(empty).check_connection() is False

    def test_get_meta_items(self, client):
        items = client.get_meta_items()
        # Deleted document skipped
        assert {d.id for d in items} == {NOTEBOOK_ID, PDF_ID, FOLDER_ID}

        notebook = next(d for d in items if d.id == NOTEBOOK_ID)
        assert notebook.name == "Meeting Notes"
        assert notebook.parent == FOLDER_ID
        assert notebook.pinned is True
        assert notebook.tags == ["important"]
        assert notebook.last_modified is not None
        assert notebook.is_folder is False

        folder = next(d for d in items if d.id == FOLDER_ID)
        assert folder.is_folder is True

    def test_get_meta_items_limit(self, client):
        assert len(client.get_meta_items(limit=2)) == 2

    def test_get_meta_items_cached(self, client):
        first = client.get_meta_items()
        assert client.get_meta_items() is first

    def test_get_doc(self, client):
        assert client.get_doc(NOTEBOOK_ID).name == "Meeting Notes"
        assert client.get_doc("missing") is None

    def test_compat_properties(self, client):
        doc = client.get_doc(NOTEBOOK_ID)
        # Cloud-client naming compatibility used across tools.py
        assert doc.VissibleName == "Meeting Notes"
        assert doc.ID == NOTEBOOK_ID
        assert doc.Parent == FOLDER_ID

    def test_download_notebook_zip(self, client):
        doc = client.get_doc(NOTEBOOK_ID)
        blob = client.download(doc)
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = set(zf.namelist())
            assert f"{PAGE_ID}.rm" in names
            assert f"{NOTEBOOK_ID}.content" in names
            assert zf.read(f"{PAGE_ID}.rm").startswith(b"reMarkable .lines file")

    def test_download_pdf_zip_includes_source(self, client):
        doc = client.get_doc(PDF_ID)
        blob = client.download(doc)
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = set(zf.namelist())
            assert f"{PDF_ID}.content" in names
            assert f"{PDF_ID}.pdf" in names

    def test_download_raw_file(self, client):
        doc = client.get_doc(PDF_ID)
        assert client.download_raw_file(doc, "pdf").startswith(b"%PDF")
        assert client.download_raw_file(doc, "epub") is None

    def test_get_file_type(self, client):
        assert client.get_file_type(client.get_doc(PDF_ID)) == "pdf"
        assert client.get_file_type(client.get_doc(NOTEBOOK_ID)) == "notebook"

    def test_get_all_file_types(self, client):
        types = client.get_all_file_types()
        assert types[PDF_ID] == "pdf"
        assert types[NOTEBOOK_ID] == "notebook"


# =============================================================================
# Directory resolution
# =============================================================================


class TestDirectoryResolution:
    def test_explicit_arg(self, xochitl_dir):
        client = create_local_dir_client(str(xochitl_dir))
        assert client.base_dir == xochitl_dir

    def test_env_path(self, xochitl_dir, monkeypatch):
        monkeypatch.setenv("REMARKABLE_LOCAL_DIR", str(xochitl_dir))
        client = create_local_dir_client()
        assert client.base_dir == xochitl_dir

    def test_no_dir_found_raises(self, monkeypatch, tmp_path):
        monkeypatch.delenv("REMARKABLE_LOCAL_DIR", raising=False)
        monkeypatch.setattr(
            "remarkable_mcp.local_dir.DEFAULT_DIR_CANDIDATES",
            (str(tmp_path / "missing"),),
        )
        with pytest.raises(RuntimeError, match="REMARKABLE_LOCAL_DIR"):
            create_local_dir_client()

    def test_find_default_local_dir(self, xochitl_dir, monkeypatch):
        monkeypatch.setattr(
            "remarkable_mcp.local_dir.DEFAULT_DIR_CANDIDATES",
            (str(xochitl_dir),),
        )
        assert find_default_local_dir() == xochitl_dir

    def test_check_local_dir_available(self, xochitl_dir, tmp_path):
        assert check_local_dir_available(str(xochitl_dir)) is True
        assert check_local_dir_available(str(tmp_path / "missing")) is False


# =============================================================================
# Transport selection (api.get_rmapi)
# =============================================================================


class TestTransportSelection:
    @pytest.fixture(autouse=True)
    def reset_cache(self):
        api.reset_client_cache()
        yield
        api.reset_client_cache()

    def test_get_rmapi_local_dir(self, xochitl_dir, monkeypatch):
        monkeypatch.setenv("REMARKABLE_LOCAL_DIR", str(xochitl_dir))
        monkeypatch.setattr(api, "REMARKABLE_USE_LOCAL_DIR", True)
        client = api.get_rmapi()
        assert isinstance(client, LocalDirClient)
        assert api.get_active_transport() == "local-dir"

    def test_local_dir_takes_priority_over_ssh(self, xochitl_dir, monkeypatch):
        monkeypatch.setenv("REMARKABLE_LOCAL_DIR", str(xochitl_dir))
        monkeypatch.setattr(api, "REMARKABLE_USE_LOCAL_DIR", True)
        monkeypatch.setattr(api, "REMARKABLE_USE_SSH", True)
        client = api.get_rmapi()
        assert isinstance(client, LocalDirClient)

    def test_missing_dir_falls_back_to_cloud(self, monkeypatch, tmp_path):
        monkeypatch.delenv("REMARKABLE_LOCAL_DIR", raising=False)
        monkeypatch.setattr(api, "REMARKABLE_USE_LOCAL_DIR", True)
        monkeypatch.setattr(
            "remarkable_mcp.local_dir.DEFAULT_DIR_CANDIDATES",
            (str(tmp_path / "missing"),),
        )
        monkeypatch.setattr(api, "_is_cloud_token_available", lambda: True)
        sentinel = object()
        monkeypatch.setattr(api, "_get_cloud_client", lambda: sentinel)
        client = api.get_rmapi()
        assert client is sentinel
        assert api.get_active_transport() == "cloud"

    def test_missing_dir_no_fallback_raises(self, monkeypatch, tmp_path):
        monkeypatch.delenv("REMARKABLE_LOCAL_DIR", raising=False)
        monkeypatch.setattr(api, "REMARKABLE_USE_LOCAL_DIR", True)
        monkeypatch.setattr(
            "remarkable_mcp.local_dir.DEFAULT_DIR_CANDIDATES",
            (str(tmp_path / "missing"),),
        )
        monkeypatch.setattr(api, "_is_cloud_token_available", lambda: False)
        with pytest.raises(RuntimeError):
            api.get_rmapi()


# =============================================================================
# Read-only guarantee
# =============================================================================


class TestReadOnlyGuarantee:
    def test_write_disabled_in_local_dir_mode(self, monkeypatch):
        from remarkable_mcp import write_tools

        monkeypatch.setenv("REMARKABLE_USE_LOCAL_DIR", "1")
        monkeypatch.delenv("REMARKABLE_READ_ONLY", raising=False)
        assert write_tools.write_enabled() is False

    def test_local_dir_not_cloud_mode(self, monkeypatch):
        from remarkable_mcp import write_tools

        monkeypatch.setenv("REMARKABLE_USE_LOCAL_DIR", "1")
        assert write_tools._is_cloud_mode() is False
        assert write_tools._is_local_dir_mode() is True

    def test_write_enabled_without_local_dir(self, monkeypatch):
        from remarkable_mcp import write_tools

        monkeypatch.delenv("REMARKABLE_USE_LOCAL_DIR", raising=False)
        monkeypatch.delenv("REMARKABLE_LOCAL_DIR", raising=False)
        monkeypatch.delenv("REMARKABLE_READ_ONLY", raising=False)
        assert write_tools.write_enabled() is True
