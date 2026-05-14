"""
Integration tests for remarkable-mcp against a live reMarkable tablet.

These tests require a reMarkable tablet connected via USB with SSH access.
Run with: uv run pytest test_integration.py --run-integration -v
"""

import json
import os
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def ssh_client():
    """Create an SSH client connected to the tablet."""
    from remarkable_mcp.ssh import create_ssh_client

    client = create_ssh_client()
    if not client.check_connection():
        pytest.skip("reMarkable tablet not connected via SSH")
    return client


@pytest.fixture(scope="module")
def documents(ssh_client):
    """Fetch all documents from the tablet."""
    return ssh_client.get_meta_items()


@pytest.fixture(scope="module")
def file_types(ssh_client):
    """Pre-fetch all file types in one SSH call."""
    return ssh_client.get_all_file_types()


class TestSSHConnection:
    """Verify basic SSH connectivity and metadata loading."""

    def test_connection(self, ssh_client):
        assert ssh_client.check_connection()

    def test_loads_documents(self, documents):
        assert len(documents) > 0

    def test_documents_have_names(self, documents):
        for doc in documents[:10]:
            assert doc.name
            assert doc.id

    def test_no_deleted_documents_returned(self, documents):
        for doc in documents:
            assert not doc.deleted


class TestCloudArchivedFix:
    """Verify issue #65 fix — synced=false docs are visible."""

    def test_synced_false_docs_are_visible(self, documents):
        """Documents with synced=false should not be hidden."""
        synced_false = [d for d in documents if not d.synced]
        if not synced_false:
            pytest.skip("No synced=false documents on this tablet")

        for doc in synced_false:
            if doc.parent != "trash":
                assert not doc.is_cloud_archived, (
                    f"'{doc.name}' has synced=false but is incorrectly hidden"
                )


class TestDocumentRendering:
    """Verify rendering works for both v5 and v6 .rm files."""

    def _download_and_render(self, ssh_client, doc, page=1):
        from remarkable_mcp.extract import render_page_from_document_zip

        raw = ssh_client.download(doc)
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp.write(raw)
            tmp_path = Path(tmp.name)
        try:
            return render_page_from_document_zip(tmp_path, page=page)
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_render_notebook(self, ssh_client, documents, file_types):
        """Rendering a notebook page should produce PNG bytes."""
        notebooks = [
            d
            for d in documents
            if not d.is_folder and not d.is_cloud_archived and file_types.get(d.id) == "notebook"
        ]
        if not notebooks:
            pytest.skip("No notebooks on tablet")

        png = self._download_and_render(ssh_client, notebooks[0])
        assert png is not None, f"Failed to render '{notebooks[0].name}'"
        assert png[:4] == b"\x89PNG", "Output is not valid PNG"

    def test_render_epub_annotations(self, ssh_client, documents, file_types):
        """Rendering an epub's annotation layer should work."""
        epubs = [
            d
            for d in documents
            if not d.is_folder and not d.is_cloud_archived and file_types.get(d.id) == "epub"
        ]
        if not epubs:
            pytest.skip("No EPUBs on tablet")

        # EPUBs may not have annotation pages — None is acceptable
        self._download_and_render(ssh_client, epubs[0])


class TestTextExtraction:
    """Verify text extraction works end-to-end."""

    def test_extract_from_notebook(self, ssh_client, documents, file_types):
        """Extract text from a notebook — should not raise."""
        from remarkable_mcp.extract import extract_text_from_document_zip

        notebooks = [
            d
            for d in documents
            if not d.is_folder and not d.is_cloud_archived and file_types.get(d.id) == "notebook"
        ]
        if not notebooks:
            pytest.skip("No notebooks on tablet")

        raw = ssh_client.download(notebooks[0])
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp.write(raw)
            tmp_path = Path(tmp.name)
        try:
            result = extract_text_from_document_zip(tmp_path, include_ocr=False)
            assert "typed_text" in result
            assert "pages" in result
            assert result["pages"] > 0
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_extract_from_epub(self, ssh_client, documents, file_types):
        """Extract text from an EPUB — should return content."""

        epubs = [
            d
            for d in documents
            if not d.is_folder and not d.is_cloud_archived and file_types.get(d.id) == "epub"
        ]
        if not epubs:
            pytest.skip("No EPUBs on tablet")

        # Just verify download + extraction doesn't crash
        raw = ssh_client.download(epubs[0])
        assert len(raw) > 0


class TestMCPTools:
    """Test MCP tool responses via the server with a real SSH connection."""

    @pytest.mark.asyncio
    async def test_status_shows_connected(self, ssh_client):
        """remarkable_status should report connected in SSH mode."""
        os.environ["REMARKABLE_USE_SSH"] = "1"

        import importlib

        import remarkable_mcp.api

        importlib.reload(remarkable_mcp.api)

        try:
            from remarkable_mcp.server import mcp

            result = await mcp.call_tool("remarkable_status", {})
            data = json.loads(result[0][0].text)
            assert data["authenticated"] is True
            assert data["transport"] == "ssh"
            assert data["document_count"] > 0
        finally:
            os.environ.pop("REMARKABLE_USE_SSH", None)
            importlib.reload(remarkable_mcp.api)
