#!/usr/bin/env python3
"""
Tests for reMarkable MCP Server

Tests the 4 intent-based tools using FastMCP's testing capabilities.
"""

import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
import requests

from remarkable_mcp.api import (
    get_item_path,
    get_items_by_id,
    register_and_get_token,
)
from remarkable_mcp.extract import (
    extract_text_from_document_zip,
    extract_text_from_rm_file,
    find_similar_documents,
)
from remarkable_mcp.responses import (
    make_error,
    make_response,
)
from remarkable_mcp.server import mcp

# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def mock_document():
    """Create a mock Document object."""
    doc = Mock()
    doc.VissibleName = "Test Document"
    doc.ID = "doc-123"
    doc.Parent = ""
    doc.ModifiedClient = "2024-01-15T10:30:00Z"
    return doc


@pytest.fixture
def mock_folder():
    """Create a mock Folder object."""
    folder = Mock()
    folder.VissibleName = "Test Folder"
    folder.ID = "folder-456"
    folder.Parent = ""
    return folder


@pytest.fixture
def mock_collection(mock_document, mock_folder):
    """Create a mock collection of items."""
    return [mock_document, mock_folder]


@pytest.fixture
def sample_zip_file():
    """Create a sample reMarkable document zip for testing."""
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        with zipfile.ZipFile(tmp.name, "w") as zf:
            # Add a sample text file
            zf.writestr("sample.txt", "This is sample text content")
            # Add a sample content json
            zf.writestr("metadata.content", '{"text": "Content metadata text"}')
        yield Path(tmp.name)
    Path(tmp.name).unlink(missing_ok=True)


# =============================================================================
# Test MCP Server Initialization
# =============================================================================


class TestMCPServerInitialization:
    """Test MCP server initialization and basic functionality."""

    def test_server_name(self):
        """Test that server has correct name."""
        assert mcp.name == "remarkable"

    @pytest.mark.asyncio
    async def test_tools_registered(self):
        """Test that all expected tools are registered."""
        tools = await mcp.list_tools()
        tool_names = [tool.name for tool in tools]

        expected_tools = [
            "remarkable_read",
            "remarkable_browse",
            "remarkable_recent",
            "remarkable_search",
            "remarkable_status",
            "remarkable_image",
        ]

        for tool_name in expected_tools:
            assert tool_name in tool_names, f"Tool {tool_name} not found"

    @pytest.mark.asyncio
    async def test_tools_count(self):
        """Test that we have exactly 6 intent-based tools."""
        tools = await mcp.list_tools()
        assert len(tools) == 6, f"Expected 6 tools, got {len(tools)}"

    @pytest.mark.asyncio
    async def test_tool_schemas(self):
        """Test that tools have proper schemas."""
        tools = await mcp.list_tools()

        for tool in tools:
            assert tool.name, "Tool should have a name"
            assert tool.description, "Tool should have a description"
            assert hasattr(tool, "inputSchema"), "Tool should have inputSchema"

    @pytest.mark.asyncio
    async def test_all_tools_have_xml_docstrings(self):
        """Test that all tools have XML-structured documentation."""
        tools = await mcp.list_tools()

        for tool in tools:
            # Check for XML tags in description
            desc = tool.description
            assert "<usecase>" in desc, f"Tool {tool.name} missing <usecase> tag"


# =============================================================================
# Test Helper Functions
# =============================================================================


class TestHelperFunctions:
    """Test helper functions."""

    def test_make_response(self):
        """Test response creation with hint."""
        data = {"key": "value"}
        result = make_response(data, "This is a hint")
        parsed = json.loads(result)

        assert parsed["key"] == "value"
        assert parsed["_hint"] == "This is a hint"

    def test_make_error(self):
        """Test error creation with suggestions."""
        result = make_error(
            error_type="test_error",
            message="Something went wrong",
            suggestion="Try this instead",
            did_you_mean=["option1", "option2"],
        )
        parsed = json.loads(result)

        assert parsed["_error"]["type"] == "test_error"
        assert parsed["_error"]["message"] == "Something went wrong"
        assert parsed["_error"]["suggestion"] == "Try this instead"
        assert parsed["_error"]["did_you_mean"] == ["option1", "option2"]

    def test_make_error_without_did_you_mean(self):
        """Test error creation without did_you_mean."""
        result = make_error(
            error_type="test_error", message="Error message", suggestion="Suggestion"
        )
        parsed = json.loads(result)

        assert "did_you_mean" not in parsed["_error"]

    def test_find_similar_documents(self):
        """Test fuzzy document matching."""
        docs = [
            Mock(VissibleName="Meeting Notes"),
            Mock(VissibleName="Project Plan"),
            Mock(VissibleName="Notes Daily"),
        ]

        # Exact partial match
        results = find_similar_documents("Notes", docs)
        assert "Meeting Notes" in results or "Notes Daily" in results

        # Fuzzy match
        results = find_similar_documents("Meating", docs, limit=3)
        assert len(results) <= 3

    def test_get_items_by_id(self, mock_collection):
        """Test building ID lookup dict."""
        items_by_id = get_items_by_id(mock_collection)

        assert "doc-123" in items_by_id
        assert "folder-456" in items_by_id

    def test_get_item_path(self, mock_document, mock_collection):
        """Test getting full item path."""
        items_by_id = get_items_by_id(mock_collection)
        path = get_item_path(mock_document, items_by_id)

        assert path == "/Test Document"

    def test_get_item_path_nested(self, mock_folder):
        """Test getting path for nested item."""
        # Create nested structure
        child_doc = Mock()
        child_doc.VissibleName = "Child Doc"
        child_doc.ID = "child-789"
        child_doc.Parent = mock_folder.ID

        items_by_id = {mock_folder.ID: mock_folder, child_doc.ID: child_doc}

        path = get_item_path(child_doc, items_by_id)
        assert path == "/Test Folder/Child Doc"


# =============================================================================
# Test Text Extraction
# =============================================================================


class TestTextExtraction:
    """Test text extraction functions."""

    def test_extract_text_from_document_zip(self, sample_zip_file):
        """Test extracting text from a zip file."""
        result = extract_text_from_document_zip(sample_zip_file)

        assert "typed_text" in result
        assert "highlights" in result
        assert "handwritten_text" in result
        assert "pages" in result

        # Should have extracted text from txt file
        assert any("sample text" in text.lower() for text in result["typed_text"])

    def test_extract_text_from_rm_file_no_rmscene(self):
        """Test graceful fallback when rmscene not available."""
        # Create a dummy file
        with tempfile.NamedTemporaryFile(suffix=".rm", delete=False) as tmp:
            tmp.write(b"dummy data")
            tmp_path = Path(tmp.name)

        try:
            # This should return empty list if rmscene fails
            result = extract_text_from_rm_file(tmp_path)
            assert isinstance(result, list)
        finally:
            tmp_path.unlink(missing_ok=True)


# =============================================================================
# Test remarkable_status Tool
# =============================================================================


class TestRemarkableStatus:
    """Test remarkable_status tool."""

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.get_rmapi")
    async def test_status_authenticated(self, mock_get_rmapi):
        """Test status when authenticated."""
        mock_client = Mock()
        mock_get_rmapi.return_value = mock_client
        mock_client.get_meta_items.return_value = []

        result = await mcp.call_tool("remarkable_status", {})
        data = json.loads(result[0][0].text)

        assert data["authenticated"] is True
        assert "transport" in data
        assert "connection" in data
        assert data["status"] == "connected"
        assert "_hint" in data

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.get_rmapi")
    async def test_status_capability_matrix(self, mock_get_rmapi):
        """Status exposes per-transport and effective capability matrices."""
        mock_client = Mock()
        mock_get_rmapi.return_value = mock_client
        mock_client.get_meta_items.return_value = []

        result = await mcp.call_tool("remarkable_status", {})
        data = json.loads(result[0][0].text)

        assert "write_enabled" in data
        assert "capabilities" in data
        assert "capabilities_by_transport" in data

        matrix = data["capabilities_by_transport"]
        # All three transports are described.
        assert set(matrix) == {"cloud", "ssh", "usb-web"}
        # Read/render are universal.
        for caps in matrix.values():
            assert caps["read"] is True
            assert caps["render"] is True
        # Cloud and SSH have full write surface; USB web is upload-only.
        for mode in ("cloud", "ssh"):
            assert all(matrix[mode][op] for op in ("upload", "mkdir", "move", "rename", "delete"))
        assert matrix["usb-web"]["upload"] is True
        assert not any(matrix["usb-web"][op] for op in ("mkdir", "move", "rename", "delete"))

        # Effective capabilities for the active transport always cover read/render.
        assert data["capabilities"]["read"] is True
        assert data["capabilities"]["render"] is True

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.get_rmapi")
    async def test_status_not_authenticated(self, mock_get_rmapi):
        """Test status when not authenticated."""
        mock_get_rmapi.side_effect = RuntimeError("Failed to authenticate")

        result = await mcp.call_tool("remarkable_status", {})
        data = json.loads(result[0][0].text)

        assert data["authenticated"] is False
        assert "error" in data
        assert "_hint" in data
        # Hint should include registration instructions or SSH mode
        assert "register" in data["_hint"].lower() or "ssh" in data["_hint"].lower()


# =============================================================================
# Test remarkable_browse Tool
# =============================================================================


class TestRemarkableBrowse:
    """Test remarkable_browse tool."""

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.get_rmapi")
    async def test_browse_root(self, mock_get_rmapi):
        """Test browsing root folder."""
        mock_client = Mock()
        mock_get_rmapi.return_value = mock_client
        mock_client.get_meta_items.return_value = []

        result = await mcp.call_tool("remarkable_browse", {"path": "/"})
        data = json.loads(result[0][0].text)

        assert data["mode"] == "browse"
        assert data["path"] == "/"
        assert "_hint" in data

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.get_rmapi")
    async def test_browse_search_mode(self, mock_get_rmapi):
        """Test search mode."""
        mock_client = Mock()
        mock_get_rmapi.return_value = mock_client

        # Create mock items that have VissibleName
        mock_doc = Mock()
        mock_doc.VissibleName = "Test Document"
        mock_doc.ID = "doc-123"
        mock_doc.Parent = ""
        mock_doc.ModifiedClient = "2024-01-15"

        mock_client.get_meta_items.return_value = [mock_doc]

        result = await mcp.call_tool("remarkable_browse", {"query": "Test"})
        data = json.loads(result[0][0].text)

        assert data["mode"] == "search"
        assert data["query"] == "Test"
        assert "results" in data
        assert "_hint" in data

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.get_rmapi")
    async def test_browse_error_handling(self, mock_get_rmapi):
        """Test error handling in browse."""
        mock_get_rmapi.side_effect = RuntimeError("Connection failed")

        result = await mcp.call_tool("remarkable_browse", {"path": "/"})
        data = json.loads(result[0][0].text)

        assert "_error" in data
        assert data["_error"]["type"] == "browse_failed"


# =============================================================================
# Test remarkable_recent Tool
# =============================================================================


class TestRemarkableRecent:
    """Test remarkable_recent tool."""

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.get_rmapi")
    async def test_recent_default_limit(self, mock_get_rmapi):
        """Test getting recent documents with default limit."""
        mock_client = Mock()
        mock_get_rmapi.return_value = mock_client
        mock_client.get_meta_items.return_value = []

        result = await mcp.call_tool("remarkable_recent", {})
        data = json.loads(result[0][0].text)

        assert "count" in data
        assert "documents" in data
        assert "_hint" in data

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.get_rmapi")
    async def test_recent_custom_limit(self, mock_get_rmapi):
        """Test getting recent documents with custom limit."""
        mock_client = Mock()
        mock_get_rmapi.return_value = mock_client
        mock_client.get_meta_items.return_value = []

        result = await mcp.call_tool("remarkable_recent", {"limit": 5})
        data = json.loads(result[0][0].text)

        assert "count" in data
        assert "documents" in data

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.get_rmapi")
    async def test_recent_limit_clamped(self, mock_get_rmapi):
        """Test that limit is clamped to valid range."""
        mock_client = Mock()
        mock_get_rmapi.return_value = mock_client
        mock_client.get_meta_items.return_value = []

        # Test with limit > 50
        result = await mcp.call_tool("remarkable_recent", {"limit": 100})
        # Should not raise an error
        data = json.loads(result[0][0].text)
        assert "count" in data

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.get_rmapi")
    async def test_recent_error_handling(self, mock_get_rmapi):
        """Test error handling in recent."""
        mock_get_rmapi.side_effect = RuntimeError("Connection failed")

        result = await mcp.call_tool("remarkable_recent", {})
        data = json.loads(result[0][0].text)

        assert "_error" in data
        assert data["_error"]["type"] == "recent_failed"

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.get_rmapi")
    async def test_recent_include_preview_does_not_crash(self, mock_get_rmapi):
        """Test that include_preview=True works without AttributeError on download result.

        This is a regression test for the bug where client.download() returns bytes
        but the code called raw_doc.content (treating it like a requests.Response).
        """
        import io

        mock_client = Mock()
        mock_get_rmapi.return_value = mock_client

        # Create a PDF document mock
        doc = Mock()
        doc.VissibleName = "My PDF"
        doc.ID = "pdf-123"
        doc.Parent = ""
        doc.ModifiedClient = "2024-01-15T10:30:00Z"
        doc.is_folder = False
        doc.tags = []

        mock_client.get_meta_items.return_value = [doc]

        # download() returns bytes (not a requests.Response)
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zf:
            zf.writestr("pdf-123.content", '{"fileType": "pdf"}')
        mock_client.download.return_value = zip_buffer.getvalue()

        # Simulate get_file_type returning "pdf"
        with patch("remarkable_mcp.tools.get_file_type", return_value="pdf"):
            result = await mcp.call_tool("remarkable_recent", {"include_preview": True})
        data = json.loads(result[0][0].text)

        # Should not crash with AttributeError; may return empty preview but no error
        assert "_error" not in data
        assert "documents" in data

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.get_rmapi")
    async def test_recent_handles_null_and_mixed_modified_dates(self, mock_get_rmapi):
        """Regression test for #96: remarkable_recent must not crash when documents
        have a null modified date, nor when modified dates mix tz-aware (USB) and
        tz-naive (cloud/SSH) datetimes.

        Previously the sort key returned "" (str) for missing dates and a datetime
        otherwise, raising TypeError ('<' not supported between str and datetime).
        A tz-aware sentinel would still crash cloud/SSH (naive vs aware compare).
        """
        from datetime import datetime, timezone

        class FakeDoc:
            def __init__(self, doc_id, name, modified):
                self.ID = doc_id
                self.VissibleName = name
                self.Parent = ""
                self.is_folder = False
                self.tags = []
                self.ModifiedClient = modified

        mock_client = Mock()
        mock_get_rmapi.return_value = mock_client
        # newest: tz-aware (USB style); middle: tz-naive (cloud style); oldest: None
        mock_client.get_meta_items.return_value = [
            FakeDoc("a", "Naive Middle", datetime(2024, 6, 1, 12, 0, 0)),
            FakeDoc("b", "Fresh Notebook", None),
            FakeDoc("c", "Aware Newest", datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)),
        ]

        result = await mcp.call_tool("remarkable_recent", {})
        data = json.loads(result[0][0].text)

        assert "_error" not in data
        names = [d["name"] for d in data["documents"]]
        assert names == ["Aware Newest", "Naive Middle", "Fresh Notebook"]


# =============================================================================
# Test remarkable_search Tool
# =============================================================================


class TestRemarkableSearch:
    """Test remarkable_search tool."""

    @pytest.fixture(autouse=True)
    def _clear_root_path(self, monkeypatch):
        # Ensure tests are independent of any ambient REMARKABLE_ROOT_PATH
        monkeypatch.delenv("REMARKABLE_ROOT_PATH", raising=False)

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.get_rmapi")
    async def test_search_awaits_remarkable_read(self, mock_get_rmapi):
        """Regression test: remarkable_search must await the async remarkable_read.

        Previously remarkable_search was a sync function calling the async
        remarkable_read without awaiting it, resulting in per-document errors:
            'the JSON object must be str, bytes or bytearray, not coroutine'
        because json.loads() was called on a coroutine object.
        """
        mock_client = Mock()
        mock_get_rmapi.return_value = mock_client

        doc = Mock()
        doc.VissibleName = "mcp notes"
        doc.ID = "doc-mcp-1"
        doc.Parent = ""
        doc.ModifiedClient = "2024-01-15T10:30:00Z"
        doc.is_folder = False
        doc.is_cloud_archived = False
        doc.tags = []

        mock_client.get_meta_items.return_value = [doc]

        # Minimal zip so remarkable_read can succeed end-to-end
        import io

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zf:
            zf.writestr("doc-mcp-1.content", '{"fileType": "notebook"}')
        mock_client.download.return_value = zip_buffer.getvalue()

        result = await mcp.call_tool("remarkable_search", {"query": "mcp", "limit": 2})
        data = json.loads(result[0][0].text)

        # Top-level call must succeed (not surface a coroutine TypeError)
        assert "_error" not in data, f"Unexpected top-level error: {data.get('_error')}"
        assert "documents" in data
        assert data["count"] >= 1

        # No per-document result should contain the coroutine error
        for doc_result in data["documents"]:
            err = doc_result.get("error", "")
            assert "coroutine" not in err, f"Per-document coroutine error leaked through: {err}"

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.get_rmapi")
    async def test_search_no_documents_found(self, mock_get_rmapi):
        """Test search returns clean error when no documents match."""
        mock_client = Mock()
        mock_get_rmapi.return_value = mock_client
        mock_client.get_meta_items.return_value = []

        result = await mcp.call_tool("remarkable_search", {"query": "nonexistent-xyz"})
        data = json.loads(result[0][0].text)

        assert "_error" in data
        assert data["_error"]["type"] == "no_documents_found"


# =============================================================================
# Test remarkable_read Tool
# =============================================================================


class TestRemarkableRead:
    """Test remarkable_read tool."""

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.get_rmapi")
    async def test_read_document_not_found(self, mock_get_rmapi):
        """Test reading a non-existent document."""
        mock_client = Mock()
        mock_get_rmapi.return_value = mock_client
        mock_client.get_meta_items.return_value = []

        result = await mcp.call_tool("remarkable_read", {"document": "NonExistent"})
        data = json.loads(result[0][0].text)

        assert "_error" in data
        assert data["_error"]["type"] == "document_not_found"
        assert "suggestion" in data["_error"]

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.get_rmapi")
    async def test_read_error_handling(self, mock_get_rmapi):
        """Test error handling in read."""
        mock_get_rmapi.side_effect = RuntimeError("Connection failed")

        result = await mcp.call_tool("remarkable_read", {"document": "Test"})
        data = json.loads(result[0][0].text)

        assert "_error" in data
        assert data["_error"]["type"] == "read_failed"

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.get_rmapi")
    async def test_read_provides_suggestions(self, mock_get_rmapi, mock_document):
        """Test that read provides 'did you mean' suggestions."""
        mock_client = Mock()
        mock_get_rmapi.return_value = mock_client
        mock_client.get_meta_items.return_value = [mock_document]

        # Search for something similar but not exact
        result = await mcp.call_tool("remarkable_read", {"document": "Test Doc"})
        data = json.loads(result[0][0].text)

        # Should get a not found error with suggestions
        assert "_error" in data
        assert data["_error"]["type"] == "document_not_found"

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.get_rmapi")
    async def test_read_notebook_empty_content_ocr_retry(self, mock_get_rmapi):
        """Test that remarkable_read correctly awaits the OCR auto-retry for empty notebooks.

        This is a regression test for the bug where the recursive call to
        remarkable_read() was missing 'await', causing a coroutine object to be
        passed to json.loads() with the error:
        'the JSON object must be str, bytes or bytearray, not coroutine'
        """
        mock_client = Mock()
        mock_get_rmapi.return_value = mock_client

        # Create a notebook document mock
        doc = Mock()
        doc.VissibleName = "Quick sheets"
        doc.ID = "notebook-123"
        doc.Parent = ""
        doc.ModifiedClient = "2024-01-15T10:30:00Z"
        doc.is_folder = False
        doc.tags = []

        mock_client.get_meta_items.return_value = [doc]

        # Create a minimal zip with no typed text (simulates a handwritten notebook)
        import io

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zf:
            # Add empty content file (no text field) to simulate notebook
            zf.writestr("notebook-123.content", '{"fileType": "notebook"}')
        zip_bytes = zip_buffer.getvalue()

        mock_client.download.return_value = zip_bytes

        # This should NOT raise "the JSON object must be str, bytes or bytearray, not coroutine"
        # Previously failed because remarkable_read() was called without 'await'
        result = await mcp.call_tool("remarkable_read", {"document": "Quick sheets"})
        data = json.loads(result[0][0].text)

        # Should return a valid response (not a coroutine error)
        assert (
            "_error" not in data
            or data["_error"]["type"] != "read_failed"
            or ("coroutine" not in data["_error"].get("message", ""))
        ), f"Got coroutine error: {data}"


# =============================================================================
# Test remarkable_image Tool
# =============================================================================


class TestRemarkableImage:
    """Test remarkable_image tool."""

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.get_rmapi")
    async def test_image_document_not_found(self, mock_get_rmapi):
        """Test getting image from non-existent document."""
        mock_client = Mock()
        mock_get_rmapi.return_value = mock_client
        mock_client.get_meta_items.return_value = []

        result = await mcp.call_tool("remarkable_image", {"document": "NonExistent"})
        data = json.loads(result[0].text)

        assert "_error" in data
        assert data["_error"]["type"] == "document_not_found"
        assert "suggestion" in data["_error"]

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.get_rmapi")
    async def test_image_error_handling(self, mock_get_rmapi):
        """Test error handling in image tool."""
        mock_get_rmapi.side_effect = RuntimeError("Connection failed")

        result = await mcp.call_tool("remarkable_image", {"document": "Test"})
        data = json.loads(result[0].text)

        assert "_error" in data
        assert data["_error"]["type"] == "image_failed"

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.get_rmapi")
    async def test_image_provides_suggestions(self, mock_get_rmapi, mock_document):
        """Test that image tool provides 'did you mean' suggestions."""
        mock_client = Mock()
        mock_get_rmapi.return_value = mock_client
        mock_client.get_meta_items.return_value = [mock_document]

        # Search for something similar but not exact
        result = await mcp.call_tool("remarkable_image", {"document": "Test Doc"})
        data = json.loads(result[0].text)

        # Should get a not found error with suggestions
        assert "_error" in data
        assert data["_error"]["type"] == "document_not_found"

    @pytest.mark.asyncio
    async def test_image_compatibility_parameter_in_schema(self):
        """Test that remarkable_image tool has the compatibility parameter in its schema."""
        tools = await mcp.list_tools()
        image_tool = next(t for t in tools if t.name == "remarkable_image")

        # Check that compatibility parameter exists in the input schema
        assert "compatibility" in image_tool.inputSchema.get("properties", {})
        compat_schema = image_tool.inputSchema["properties"]["compatibility"]
        assert compat_schema.get("type") == "boolean"
        assert compat_schema.get("default") is False


# =============================================================================
# Test Merged Rendering
# =============================================================================


class TestMergedRendering:
    """Test render_merged parameter for remarkable_image."""

    @pytest.mark.asyncio
    async def test_render_merged_parameter_in_schema(self):
        """Test that remarkable_image tool has the render_merged parameter in its schema."""
        tools = await mcp.list_tools()
        image_tool = next(t for t in tools if t.name == "remarkable_image")

        # Check that render_merged parameter exists in the input schema
        assert "render_merged" in image_tool.inputSchema.get("properties", {})
        merged_schema = image_tool.inputSchema["properties"]["render_merged"]
        assert merged_schema.get("type") == "boolean"
        assert merged_schema.get("default") is False

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.get_rmapi")
    @patch("remarkable_mcp.tools.render_merged_page_from_document_zip")
    @patch("remarkable_mcp.tools.get_document_page_count")
    async def test_render_merged_fallback_no_pdf(
        self,
        mock_page_count,
        mock_render_merged,
        mock_get_rmapi,
        mock_document,
    ):
        """Test render_merged falls back when document has no PDF underlay."""
        mock_client = Mock()
        mock_get_rmapi.return_value = mock_client
        mock_document.is_folder = False
        mock_client.get_meta_items.return_value = [mock_document]
        mock_client.download.return_value = b"fake zip"
        mock_page_count.return_value = 3
        # Simulate merged function returning annotation-only with a note (no PDF)
        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
        mock_render_merged.return_value = (
            fake_png,
            "No PDF underlay found; returned annotation-only render.",
        )

        with patch("tempfile.NamedTemporaryFile") as mock_tmpfile:
            mock_tmp = Mock()
            mock_tmp.__enter__ = Mock(return_value=mock_tmp)
            mock_tmp.__exit__ = Mock(return_value=False)
            mock_tmp.name = "/tmp/test.zip"
            mock_tmpfile.return_value = mock_tmp
            with patch("pathlib.Path.unlink"):
                result = await mcp.call_tool(
                    "remarkable_image",
                    {
                        "document": "Test Document",
                        "render_merged": True,
                        "compatibility": True,
                    },
                )

        data = json.loads(result[0].text)
        # Should fall back to annotation-only since no PDF underlay
        assert data.get("merged") is False

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.get_rmapi")
    async def test_render_merged_svg_ignored(self, mock_get_rmapi, mock_document):
        """Test that SVG format ignores render_merged gracefully."""
        mock_client = Mock()
        mock_get_rmapi.return_value = mock_client
        mock_document.is_folder = False
        mock_client.get_meta_items.return_value = [mock_document]
        mock_client.download.return_value = b"fake zip"

        with (
            patch("tempfile.NamedTemporaryFile") as mock_tmpfile,
            patch("remarkable_mcp.tools.get_document_page_count", return_value=2),
            patch(
                "remarkable_mcp.tools.render_page_from_document_zip_svg",
                return_value='<svg xmlns="http://www.w3.org/2000/svg"></svg>',
            ),
        ):
            mock_tmp = Mock()
            mock_tmp.__enter__ = Mock(return_value=mock_tmp)
            mock_tmp.__exit__ = Mock(return_value=False)
            mock_tmp.name = "/tmp/test.zip"
            mock_tmpfile.return_value = mock_tmp
            with patch("pathlib.Path.unlink"):
                result = await mcp.call_tool(
                    "remarkable_image",
                    {
                        "document": "Test Document",
                        "output_format": "svg",
                        "render_merged": True,
                        "compatibility": True,
                    },
                )

        data = json.loads(result[0].text)
        # SVG should return successfully but merged=False
        assert data.get("merged") is False
        assert "render_merged is only supported with PNG" in data.get("_hint", "")

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.get_rmapi")
    @patch("remarkable_mcp.tools.render_merged_page_from_document_zip")
    @patch("remarkable_mcp.tools.get_document_page_count")
    async def test_render_merged_success_path(
        self,
        mock_page_count,
        mock_render_merged,
        mock_get_rmapi,
        mock_document,
    ):
        """Test render_merged success path: returns merged PNG with .merged.png URI."""
        mock_client = Mock()
        mock_get_rmapi.return_value = mock_client
        mock_document.is_folder = False
        mock_client.get_meta_items.return_value = [mock_document]
        mock_client.download.return_value = b"fake zip"
        mock_page_count.return_value = 5
        # Simulate successful merged render (no fallback note)
        composited_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
        mock_render_merged.return_value = (composited_png, None)

        with patch("tempfile.NamedTemporaryFile") as mock_tmpfile:
            mock_tmp = Mock()
            mock_tmp.__enter__ = Mock(return_value=mock_tmp)
            mock_tmp.__exit__ = Mock(return_value=False)
            mock_tmp.name = "/tmp/test.zip"
            mock_tmpfile.return_value = mock_tmp
            with patch("pathlib.Path.unlink"):
                result = await mcp.call_tool(
                    "remarkable_image",
                    {
                        "document": "Test Document",
                        "render_merged": True,
                        "compatibility": True,
                    },
                )

        data = json.loads(result[0].text)
        assert data.get("merged") is True
        assert data.get("resource_uri", "").endswith(".merged.png")
        hint = data.get("_hint", "")
        assert "PDF" in hint and "annotation" in hint.lower()
        # The merged renderer should have been called once for this page
        assert mock_render_merged.called


def _make_synthetic_pdf(pages: int = 2) -> bytes:
    """Build a small multi-page PDF in memory for fallback tests."""
    import fitz

    doc = fitz.open()
    for _ in range(pages):
        # reMarkable-ish portrait page dimensions in points
        doc.new_page(width=445, height=594)
    data = doc.tobytes()
    doc.close()
    return data


class TestRenderTabletPdfFallback:
    """Tests for the portable tablet-PDF render fallback (issue #95/#102/#94)."""

    def test_render_tablet_pdf_page_to_png_returns_png(self):
        """A valid PDF page rasterizes to PNG bytes."""
        from remarkable_mcp.extract import render_tablet_pdf_page_to_png

        pdf = _make_synthetic_pdf(2)
        png = render_tablet_pdf_page_to_png(pdf, page=1)
        assert png is not None
        assert png.startswith(b"\x89PNG\r\n\x1a\n")

    def test_render_tablet_pdf_page_out_of_range(self):
        """Out-of-range page returns None rather than raising."""
        from remarkable_mcp.extract import render_tablet_pdf_page_to_png

        pdf = _make_synthetic_pdf(1)
        assert render_tablet_pdf_page_to_png(pdf, page=5) is None
        assert render_tablet_pdf_page_to_png(pdf, page=0) is None

    def test_render_tablet_pdf_invalid_bytes(self):
        """Invalid PDF bytes return None rather than raising."""
        from remarkable_mcp.extract import render_tablet_pdf_page_to_png

        assert render_tablet_pdf_page_to_png(b"not a pdf", page=1) is None

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.download_raw_file")
    @patch("remarkable_mcp.tools.render_page_from_document_zip")
    @patch("remarkable_mcp.tools.get_document_page_count")
    @patch("remarkable_mcp.tools.get_rmapi")
    async def test_image_falls_back_to_tablet_pdf(
        self,
        mock_get_rmapi,
        mock_page_count,
        mock_render,
        mock_download_raw,
        mock_document,
    ):
        """When local stroke render returns None, fall back to the tablet PDF."""
        mock_client = Mock()
        mock_get_rmapi.return_value = mock_client
        mock_document.is_folder = False
        mock_client.get_meta_items.return_value = [mock_document]
        mock_client.download.return_value = b"fake zip"
        mock_page_count.return_value = 2
        # Local stroke renderer fails (e.g. empty page or missing libcairo)
        mock_render.return_value = None
        # Tablet exposes a natively-rendered PDF
        mock_download_raw.return_value = _make_synthetic_pdf(2)

        with patch("tempfile.NamedTemporaryFile") as mock_tmpfile:
            mock_tmp = Mock()
            mock_tmp.__enter__ = Mock(return_value=mock_tmp)
            mock_tmp.__exit__ = Mock(return_value=False)
            mock_tmp.name = "/tmp/test.zip"
            mock_tmpfile.return_value = mock_tmp
            with patch("pathlib.Path.unlink"):
                result = await mcp.call_tool(
                    "remarkable_image",
                    {"document": "Test Document", "page": 2, "compatibility": True},
                )

        data = json.loads(result[0].text)
        assert "_error" not in data
        assert data.get("render_source") == "tablet_pdf"
        assert data.get("image_base64")
        assert "native PDF export" in data.get("_hint", "")
        mock_download_raw.assert_called_once()

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.download_raw_file")
    @patch("remarkable_mcp.tools.render_page_from_document_zip")
    @patch("remarkable_mcp.tools.get_document_page_count")
    @patch("remarkable_mcp.tools.get_rmapi")
    async def test_image_render_failed_when_no_fallback(
        self,
        mock_get_rmapi,
        mock_page_count,
        mock_render,
        mock_download_raw,
        mock_document,
    ):
        """With no local render and no tablet PDF (e.g. cloud), report render_failed."""
        mock_client = Mock()
        mock_get_rmapi.return_value = mock_client
        mock_document.is_folder = False
        mock_client.get_meta_items.return_value = [mock_document]
        mock_client.download.return_value = b"fake zip"
        mock_page_count.return_value = 1
        mock_render.return_value = None
        mock_download_raw.return_value = None  # cloud: no native export

        with patch("tempfile.NamedTemporaryFile") as mock_tmpfile:
            mock_tmp = Mock()
            mock_tmp.__enter__ = Mock(return_value=mock_tmp)
            mock_tmp.__exit__ = Mock(return_value=False)
            mock_tmp.name = "/tmp/test.zip"
            mock_tmpfile.return_value = mock_tmp
            with patch("pathlib.Path.unlink"):
                result = await mcp.call_tool(
                    "remarkable_image",
                    {"document": "Test Document", "page": 1, "compatibility": True},
                )

        data = json.loads(result[0].text)
        assert data["_error"]["type"] == "render_failed"
        # The misleading "v5 / rmc" message is gone; guidance is actionable
        suggestion = data["_error"]["suggestion"].lower()
        assert "v5" not in suggestion
        assert "cairo" in suggestion or "native pdf" in suggestion


# =============================================================================
# Test Registration
# =============================================================================


class TestRegistration:
    """Test registration functionality."""

    @patch("remarkable_mcp.sync.time.sleep")
    @patch("remarkable_mcp.sync._issue_request")
    @patch("pathlib.Path.write_text")
    def test_register_and_get_token(self, mock_write_text, mock_request, mock_sleep):
        """Test registration process."""
        # Mock successful API response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = "test_device_token_12345"
        mock_request.return_value = mock_response

        token = register_and_get_token("test_code")

        # Should return JSON with devicetoken
        import json

        token_data = json.loads(token)
        assert token_data["devicetoken"] == "test_device_token_12345"
        assert "usertoken" in token_data

        # Verify API was called
        mock_request.assert_called_once()
        call_args = mock_request.call_args
        assert "webapp-prod.cloud.remarkable.engineering" in call_args[0][1]

    @patch("remarkable_mcp.sync.time.sleep")
    @patch("remarkable_mcp.sync._issue_request")
    def test_register_invalid_code(self, mock_request, mock_sleep):
        """Test registration with invalid/expired code."""
        # Mock 400 response (invalid code)
        mock_response = Mock()
        mock_response.status_code = 400
        mock_response.text = ""
        mock_request.return_value = mock_response

        with pytest.raises(RuntimeError, match="Registration failed"):
            register_and_get_token("invalid_code")


# =============================================================================
# End-to-End Tests
# =============================================================================


class TestE2E:
    """End-to-end tests for MCP server."""

    def test_server_can_initialize(self):
        """Test that server can be initialized."""
        assert mcp is not None
        assert mcp.name == "remarkable"

    @pytest.mark.asyncio
    async def test_server_lists_all_tools(self):
        """Test that server can list all tools (e2e)."""
        tools = await mcp.list_tools()

        assert len(tools) == 6

        # Check each tool has required properties and starts with remarkable_
        for tool in tools:
            assert hasattr(tool, "name")
            assert hasattr(tool, "description")
            assert tool.name.startswith("remarkable_")

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.get_rmapi")
    async def test_e2e_call_tool_flow(self, mock_get_rmapi):
        """Test end-to-end flow of calling a tool."""
        mock_client = Mock()
        mock_get_rmapi.return_value = mock_client
        mock_client.get_meta_items.return_value = []

        # Call status tool
        result = await mcp.call_tool("remarkable_status", {})

        # Verify we get valid JSON back
        data = json.loads(result[0][0].text)
        assert "authenticated" in data
        assert "_hint" in data

    @pytest.mark.asyncio
    async def test_tool_parameters_schema(self):
        """Test that tool parameters have proper schemas."""
        tools = await mcp.list_tools()

        # Check specific tools exist
        browse_tool = next(t for t in tools if t.name == "remarkable_browse")
        assert browse_tool is not None

        read_tool = next(t for t in tools if t.name == "remarkable_read")
        assert read_tool is not None

        recent_tool = next(t for t in tools if t.name == "remarkable_recent")
        assert recent_tool is not None

        status_tool = next(t for t in tools if t.name == "remarkable_status")
        assert status_tool is not None

    @pytest.mark.asyncio
    async def test_all_tools_return_json_with_hint(self):
        """Test that all tools return JSON with _hint field."""
        with patch("remarkable_mcp.tools.get_rmapi") as mock_get_rmapi:
            mock_client = Mock()
            mock_get_rmapi.return_value = mock_client
            mock_client.get_meta_items.return_value = []

            # Test status
            result = await mcp.call_tool("remarkable_status", {})
            data = json.loads(result[0][0].text)
            assert "_hint" in data

            # Test browse
            result = await mcp.call_tool("remarkable_browse", {"path": "/"})
            data = json.loads(result[0][0].text)
            assert "_hint" in data or "_error" in data

            # Test recent
            result = await mcp.call_tool("remarkable_recent", {})
            data = json.loads(result[0][0].text)
            assert "_hint" in data or "_error" in data


# =============================================================================
# Test Response Consistency
# =============================================================================


class TestResponseConsistency:
    """Test that responses follow consistent patterns."""

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.get_rmapi")
    async def test_all_errors_have_required_fields(self, mock_get_rmapi):
        """Test that all error responses have required fields."""
        mock_get_rmapi.side_effect = RuntimeError("Test error")

        tools_to_test = [
            ("remarkable_status", {}),
            ("remarkable_browse", {"path": "/"}),
            ("remarkable_recent", {}),
            ("remarkable_read", {"document": "test"}),
        ]

        for tool_name, args in tools_to_test:
            result = await mcp.call_tool(tool_name, args)
            data = json.loads(result[0][0].text)

            # Either success with _hint or error with _error
            has_hint = "_hint" in data
            has_error = "_error" in data

            assert has_hint or has_error, f"Tool {tool_name} response missing _hint or _error"

            if has_error:
                assert "type" in data["_error"], f"Error in {tool_name} missing type"
                assert "message" in data["_error"], f"Error in {tool_name} missing message"
                assert "suggestion" in data["_error"], f"Error in {tool_name} missing suggestion"


# =============================================================================
# Test Capability Checking
# =============================================================================


class TestCapabilityChecking:
    """Test capability checking utilities."""

    def test_get_client_capabilities_without_context(self):
        """Test get_client_capabilities returns None without valid context."""
        from remarkable_mcp.capabilities import get_client_capabilities

        # Create mock context without session
        mock_ctx = Mock()
        mock_ctx.session = None

        result = get_client_capabilities(mock_ctx)
        assert result is None

    def test_get_client_capabilities_without_client_params(self):
        """Test get_client_capabilities returns None without client_params."""
        from remarkable_mcp.capabilities import get_client_capabilities

        mock_ctx = Mock()
        mock_ctx.session = Mock()
        mock_ctx.session.client_params = None

        result = get_client_capabilities(mock_ctx)
        assert result is None

    def test_get_client_capabilities_with_valid_context(self):
        """Test get_client_capabilities returns capabilities when available."""
        from mcp.types import ClientCapabilities, SamplingCapability

        from remarkable_mcp.capabilities import get_client_capabilities

        mock_caps = ClientCapabilities(sampling=SamplingCapability())

        mock_ctx = Mock()
        mock_ctx.session = Mock()
        mock_ctx.session.client_params = Mock()
        mock_ctx.session.client_params.capabilities = mock_caps

        result = get_client_capabilities(mock_ctx)
        assert result is not None
        assert result.sampling is not None

    def test_client_supports_sampling_true(self):
        """Test client_supports_sampling returns True when sampling available."""
        from mcp.types import ClientCapabilities, SamplingCapability

        from remarkable_mcp.capabilities import client_supports_sampling

        mock_caps = ClientCapabilities(sampling=SamplingCapability())

        mock_ctx = Mock()
        mock_ctx.session = Mock()
        mock_ctx.session.client_params = Mock()
        mock_ctx.session.client_params.capabilities = mock_caps

        result = client_supports_sampling(mock_ctx)
        assert result is True

    def test_client_supports_sampling_false(self):
        """Test client_supports_sampling returns False when sampling not available."""
        from mcp.types import ClientCapabilities

        from remarkable_mcp.capabilities import client_supports_sampling

        mock_caps = ClientCapabilities(sampling=None)

        mock_ctx = Mock()
        mock_ctx.session = Mock()
        mock_ctx.session.client_params = Mock()
        mock_ctx.session.client_params.capabilities = mock_caps

        result = client_supports_sampling(mock_ctx)
        assert result is False

    def test_client_supports_elicitation(self):
        """Test client_supports_elicitation."""
        from mcp.types import ClientCapabilities, ElicitationCapability

        from remarkable_mcp.capabilities import client_supports_elicitation

        # Test with elicitation enabled
        mock_caps = ClientCapabilities(elicitation=ElicitationCapability())

        mock_ctx = Mock()
        mock_ctx.session = Mock()
        mock_ctx.session.client_params = Mock()
        mock_ctx.session.client_params.capabilities = mock_caps

        assert client_supports_elicitation(mock_ctx) is True

        # Test with elicitation disabled
        mock_caps = ClientCapabilities(elicitation=None)
        mock_ctx.session.client_params.capabilities = mock_caps

        assert client_supports_elicitation(mock_ctx) is False

    def test_client_supports_roots(self):
        """Test client_supports_roots."""
        from mcp.types import ClientCapabilities, RootsCapability

        from remarkable_mcp.capabilities import client_supports_roots

        # Test with roots enabled
        mock_caps = ClientCapabilities(roots=RootsCapability())

        mock_ctx = Mock()
        mock_ctx.session = Mock()
        mock_ctx.session.client_params = Mock()
        mock_ctx.session.client_params.capabilities = mock_caps

        assert client_supports_roots(mock_ctx) is True

        # Test with roots disabled
        mock_caps = ClientCapabilities(roots=None)
        mock_ctx.session.client_params.capabilities = mock_caps

        assert client_supports_roots(mock_ctx) is False

    def test_client_supports_experimental(self):
        """Test client_supports_experimental."""
        from mcp.types import ClientCapabilities

        from remarkable_mcp.capabilities import client_supports_experimental

        # Test with experimental feature present
        mock_caps = ClientCapabilities(experimental={"my_feature": {}})

        mock_ctx = Mock()
        mock_ctx.session = Mock()
        mock_ctx.session.client_params = Mock()
        mock_ctx.session.client_params.capabilities = mock_caps

        assert client_supports_experimental(mock_ctx, "my_feature") is True
        assert client_supports_experimental(mock_ctx, "other_feature") is False

        # Test with no experimental features
        mock_caps = ClientCapabilities(experimental=None)
        mock_ctx.session.client_params.capabilities = mock_caps

        assert client_supports_experimental(mock_ctx, "my_feature") is False

    def test_get_client_info(self):
        """Test get_client_info."""
        from remarkable_mcp.capabilities import get_client_info

        mock_ctx = Mock()
        mock_ctx.session = Mock()
        mock_ctx.session.client_params = Mock()
        mock_ctx.session.client_params.clientInfo = Mock()
        mock_ctx.session.client_params.clientInfo.name = "Test Client"
        mock_ctx.session.client_params.clientInfo.version = "1.0.0"
        mock_ctx.session.client_params.protocolVersion = "2024-11-05"

        result = get_client_info(mock_ctx)
        assert result is not None
        assert result["name"] == "Test Client"
        assert result["version"] == "1.0.0"
        assert result["protocol_version"] == "2024-11-05"

    def test_get_client_info_without_client_info(self):
        """Test get_client_info when clientInfo is None."""
        from remarkable_mcp.capabilities import get_client_info

        mock_ctx = Mock()
        mock_ctx.session = Mock()
        mock_ctx.session.client_params = Mock()
        mock_ctx.session.client_params.clientInfo = None
        mock_ctx.session.client_params.protocolVersion = "2024-11-05"

        result = get_client_info(mock_ctx)
        assert result is not None
        assert result["name"] is None
        assert result["version"] is None
        assert result["protocol_version"] == "2024-11-05"

    def test_get_protocol_version(self):
        """Test get_protocol_version."""
        from remarkable_mcp.capabilities import get_protocol_version

        mock_ctx = Mock()
        mock_ctx.session = Mock()
        mock_ctx.session.client_params = Mock()
        mock_ctx.session.client_params.protocolVersion = "2024-11-05"

        result = get_protocol_version(mock_ctx)
        assert result == "2024-11-05"

    def test_get_protocol_version_without_context(self):
        """Test get_protocol_version returns None without valid context."""
        from remarkable_mcp.capabilities import get_protocol_version

        mock_ctx = Mock()
        mock_ctx.session = None

        result = get_protocol_version(mock_ctx)
        assert result is None

    def test_capability_imports_from_package(self):
        """Test that capability utilities can be imported from main package."""
        from remarkable_mcp import (
            client_supports_elicitation,
            client_supports_experimental,
            client_supports_roots,
            client_supports_sampling,
            get_client_capabilities,
            get_client_info,
            get_protocol_version,
        )

        # Verify all functions are callable
        assert callable(get_client_capabilities)
        assert callable(client_supports_sampling)
        assert callable(client_supports_elicitation)
        assert callable(client_supports_roots)
        assert callable(client_supports_experimental)
        assert callable(get_client_info)
        assert callable(get_protocol_version)


# =============================================================================
# Test Sampling OCR
# =============================================================================


class TestSamplingOCR:
    """Test sampling-based OCR functionality."""

    def test_get_ocr_backend_default(self):
        """Test default OCR backend is auto."""
        import os

        from remarkable_mcp.sampling import get_ocr_backend

        # Clear any env var
        env_backup = os.environ.get("REMARKABLE_OCR_BACKEND")
        if "REMARKABLE_OCR_BACKEND" in os.environ:
            del os.environ["REMARKABLE_OCR_BACKEND"]

        try:
            result = get_ocr_backend()
            assert result == "auto"
        finally:
            if env_backup is not None:
                os.environ["REMARKABLE_OCR_BACKEND"] = env_backup

    def test_get_ocr_backend_sampling(self):
        """Test OCR backend can be set to sampling."""
        import os

        from remarkable_mcp.sampling import get_ocr_backend

        env_backup = os.environ.get("REMARKABLE_OCR_BACKEND")
        os.environ["REMARKABLE_OCR_BACKEND"] = "sampling"

        try:
            result = get_ocr_backend()
            assert result == "sampling"
        finally:
            if env_backup is not None:
                os.environ["REMARKABLE_OCR_BACKEND"] = env_backup
            elif "REMARKABLE_OCR_BACKEND" in os.environ:
                del os.environ["REMARKABLE_OCR_BACKEND"]

    def test_should_use_sampling_ocr_false_when_not_configured(self):
        """Test should_use_sampling_ocr returns False when not configured."""
        import os

        from mcp.types import ClientCapabilities, SamplingCapability

        from remarkable_mcp.sampling import should_use_sampling_ocr

        env_backup = os.environ.get("REMARKABLE_OCR_BACKEND")
        if "REMARKABLE_OCR_BACKEND" in os.environ:
            del os.environ["REMARKABLE_OCR_BACKEND"]

        try:
            # Create mock context with sampling capability
            mock_caps = ClientCapabilities(sampling=SamplingCapability())
            mock_ctx = Mock()
            mock_ctx.session = Mock()
            mock_ctx.session.client_params = Mock()
            mock_ctx.session.client_params.capabilities = mock_caps

            # Should return False because backend is "auto", not "sampling"
            result = should_use_sampling_ocr(mock_ctx)
            assert result is False
        finally:
            if env_backup is not None:
                os.environ["REMARKABLE_OCR_BACKEND"] = env_backup

    def test_should_use_sampling_ocr_true_when_configured(self):
        """Test should_use_sampling_ocr returns True when configured and client supports it."""
        import os

        from mcp.types import ClientCapabilities, SamplingCapability

        from remarkable_mcp.sampling import should_use_sampling_ocr

        env_backup = os.environ.get("REMARKABLE_OCR_BACKEND")
        os.environ["REMARKABLE_OCR_BACKEND"] = "sampling"

        try:
            # Create mock context with sampling capability
            mock_caps = ClientCapabilities(sampling=SamplingCapability())
            mock_ctx = Mock()
            mock_ctx.session = Mock()
            mock_ctx.session.client_params = Mock()
            mock_ctx.session.client_params.capabilities = mock_caps

            result = should_use_sampling_ocr(mock_ctx)
            assert result is True
        finally:
            if env_backup is not None:
                os.environ["REMARKABLE_OCR_BACKEND"] = env_backup
            elif "REMARKABLE_OCR_BACKEND" in os.environ:
                del os.environ["REMARKABLE_OCR_BACKEND"]

    def test_should_use_sampling_ocr_false_when_client_doesnt_support(self):
        """Test should_use_sampling_ocr returns False when client doesn't support sampling."""
        import os

        from mcp.types import ClientCapabilities

        from remarkable_mcp.sampling import should_use_sampling_ocr

        env_backup = os.environ.get("REMARKABLE_OCR_BACKEND")
        os.environ["REMARKABLE_OCR_BACKEND"] = "sampling"

        try:
            # Create mock context WITHOUT sampling capability
            mock_caps = ClientCapabilities(sampling=None)
            mock_ctx = Mock()
            mock_ctx.session = Mock()
            mock_ctx.session.client_params = Mock()
            mock_ctx.session.client_params.capabilities = mock_caps

            result = should_use_sampling_ocr(mock_ctx)
            assert result is False
        finally:
            if env_backup is not None:
                os.environ["REMARKABLE_OCR_BACKEND"] = env_backup
            elif "REMARKABLE_OCR_BACKEND" in os.environ:
                del os.environ["REMARKABLE_OCR_BACKEND"]

    def test_ocr_system_prompt_structure(self):
        """Test the OCR system prompt is properly structured."""
        from remarkable_mcp.sampling import OCR_SYSTEM_PROMPT, OCR_USER_PROMPT

        # Check that system prompt contains key instructions
        assert "OCR" in OCR_SYSTEM_PROMPT
        assert "ONLY" in OCR_SYSTEM_PROMPT
        assert "[NO TEXT DETECTED]" in OCR_SYSTEM_PROMPT
        assert "reMarkable" in OCR_SYSTEM_PROMPT

        # Check user prompt is concise
        assert "text" in OCR_USER_PROMPT.lower()
        assert len(OCR_USER_PROMPT) < 200  # Should be short and focused

    @pytest.mark.asyncio
    async def test_ocr_via_sampling_returns_none_without_session(self):
        """Test ocr_via_sampling returns None when session is not available."""
        from remarkable_mcp.sampling import ocr_via_sampling

        mock_ctx = Mock()
        mock_ctx.session = None

        result = await ocr_via_sampling(mock_ctx, b"fake_png_data")
        assert result is None

    def test_sampling_imports_from_module(self):
        """Test that sampling utilities can be imported."""
        from remarkable_mcp.sampling import (
            OCR_SYSTEM_PROMPT,
            OCR_USER_PROMPT,
            get_ocr_backend,
            ocr_pages_via_sampling,
            ocr_via_sampling,
            should_use_sampling_ocr,
        )

        # Verify all functions/constants are accessible
        assert callable(ocr_via_sampling)
        assert callable(ocr_pages_via_sampling)
        assert callable(get_ocr_backend)
        assert callable(should_use_sampling_ocr)
        assert isinstance(OCR_SYSTEM_PROMPT, str)
        assert isinstance(OCR_USER_PROMPT, str)


# =============================================================================
# Test Tag Support
# =============================================================================


class TestTagSupport:
    """Test tag-related functionality."""

    @pytest.mark.asyncio
    async def test_document_has_tags_field(self):
        """Test that Document dataclass includes tags field."""
        from remarkable_mcp.sync import Document

        doc = Document(
            id="test-id",
            hash="test-hash",
            name="Test Doc",
            doc_type="DocumentType",
            tags=["work", "important"],
        )
        assert hasattr(doc, "tags")
        assert doc.tags == ["work", "important"]

    @pytest.mark.asyncio
    async def test_document_tags_default_empty(self):
        """Test that Document tags default to empty list."""
        from remarkable_mcp.sync import Document

        doc = Document(
            id="test-id",
            hash="test-hash",
            name="Test Doc",
            doc_type="DocumentType",
        )
        assert hasattr(doc, "tags")
        assert doc.tags == []

    @pytest.mark.asyncio
    async def test_browse_includes_tags(self):
        """Test that remarkable_browse includes tags in response."""
        mock_client = Mock()
        mock_doc = Mock()
        mock_doc.VissibleName = "Tagged Doc"
        mock_doc.ID = "doc-1"
        mock_doc.Parent = ""
        mock_doc.is_folder = False
        mock_doc.ModifiedClient = None
        mock_doc.tags = ["work", "project"]

        mock_client.get_meta_items.return_value = [mock_doc]

        with patch("remarkable_mcp.tools.get_rmapi", return_value=mock_client):
            with patch("remarkable_mcp.tools._is_cloud_archived", return_value=False):
                result = await mcp.call_tool("remarkable_browse", {"path": "/"})
                data = json.loads(result[0][0].text)

                assert data["mode"] == "browse"
                assert len(data["documents"]) == 1
                assert data["documents"][0]["name"] == "Tagged Doc"
                assert "tags" in data["documents"][0]
                assert data["documents"][0]["tags"] == ["work", "project"]

    @pytest.mark.asyncio
    async def test_browse_filter_by_tags(self):
        """Test that remarkable_browse can filter documents by tags."""
        mock_client = Mock()

        mock_doc1 = Mock()
        mock_doc1.VissibleName = "Work Doc"
        mock_doc1.ID = "doc-1"
        mock_doc1.Parent = ""
        mock_doc1.is_folder = False
        mock_doc1.ModifiedClient = None
        mock_doc1.tags = ["work"]

        mock_doc2 = Mock()
        mock_doc2.VissibleName = "Personal Doc"
        mock_doc2.ID = "doc-2"
        mock_doc2.Parent = ""
        mock_doc2.is_folder = False
        mock_doc2.ModifiedClient = None
        mock_doc2.tags = ["personal"]

        mock_client.get_meta_items.return_value = [mock_doc1, mock_doc2]

        with patch("remarkable_mcp.tools.get_rmapi", return_value=mock_client):
            with patch("remarkable_mcp.tools._is_cloud_archived", return_value=False):
                result = await mcp.call_tool("remarkable_browse", {"path": "/", "tags": ["work"]})
                data = json.loads(result[0][0].text)

                assert data["mode"] == "browse"
                assert len(data["documents"]) == 1
                assert data["documents"][0]["name"] == "Work Doc"
                assert "filter_tags" in data
                assert data["filter_tags"] == ["work"]

    @pytest.mark.asyncio
    async def test_browse_search_mode_includes_tags(self):
        """Test that remarkable_browse in search mode includes tags in results."""
        mock_client = Mock()
        mock_doc = Mock()
        mock_doc.VissibleName = "Meeting Notes"
        mock_doc.ID = "doc-1"
        mock_doc.Parent = ""
        mock_doc.is_folder = False
        mock_doc.ModifiedClient = None
        mock_doc.tags = ["meeting", "important"]

        mock_client.get_meta_items.return_value = [mock_doc]

        with patch("remarkable_mcp.tools.get_rmapi", return_value=mock_client):
            with patch("remarkable_mcp.tools._is_cloud_archived", return_value=False):
                result = await mcp.call_tool("remarkable_browse", {"query": "meeting"})
                data = json.loads(result[0][0].text)

                assert data["mode"] == "search"
                assert len(data["results"]) == 1
                assert "tags" in data["results"][0]
                assert data["results"][0]["tags"] == ["meeting", "important"]

    @pytest.mark.asyncio
    async def test_browse_search_mode_filter_by_tags(self):
        """Test that remarkable_browse in search mode can filter by tags."""
        mock_client = Mock()

        mock_doc1 = Mock()
        mock_doc1.VissibleName = "Work Meeting"
        mock_doc1.ID = "doc-1"
        mock_doc1.Parent = ""
        mock_doc1.is_folder = False
        mock_doc1.ModifiedClient = None
        mock_doc1.tags = ["work", "meeting"]

        mock_doc2 = Mock()
        mock_doc2.VissibleName = "Personal Meeting"
        mock_doc2.ID = "doc-2"
        mock_doc2.Parent = ""
        mock_doc2.is_folder = False
        mock_doc2.ModifiedClient = None
        mock_doc2.tags = ["personal", "meeting"]

        mock_client.get_meta_items.return_value = [mock_doc1, mock_doc2]

        with patch("remarkable_mcp.tools.get_rmapi", return_value=mock_client):
            with patch("remarkable_mcp.tools._is_cloud_archived", return_value=False):
                result = await mcp.call_tool(
                    "remarkable_browse", {"query": "meeting", "tags": ["work"]}
                )
                data = json.loads(result[0][0].text)

                assert data["mode"] == "search"
                assert len(data["results"]) == 1
                assert data["results"][0]["name"] == "Work Meeting"
                assert "filter_tags" in data
                assert data["filter_tags"] == ["work"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# =============================================================================
# Regression tests for fixed bugs
# =============================================================================


class TestIsCloudArchivedFix:
    """Regression tests for issue #65 — synced=false docs must not be hidden."""

    def test_synced_false_is_not_cloud_archived(self):
        """Documents with synced=false should be visible (not archived).

        The synced field means 'local changes pushed to cloud', NOT 'document
        is present on the device'. Chrome extension docs arrive with synced=false.
        """
        from remarkable_mcp.ssh import Document

        doc = Document(
            id="d1",
            hash="d1",
            name="Chrome Article",
            doc_type="DocumentType",
            parent="",
            synced=False,
        )
        assert doc.is_cloud_archived is False

    def test_trashed_doc_is_cloud_archived(self):
        """Documents in trash should still be hidden."""
        from remarkable_mcp.ssh import Document

        doc = Document(
            id="d2",
            hash="d2",
            name="Trashed",
            doc_type="DocumentType",
            parent="trash",
            synced=True,
        )
        assert doc.is_cloud_archived is True

    def test_normal_doc_is_not_cloud_archived(self):
        """Normal documents should be visible."""
        from remarkable_mcp.ssh import Document

        doc = Document(
            id="d3",
            hash="d3",
            name="Normal",
            doc_type="DocumentType",
            parent="",
            synced=True,
        )
        assert doc.is_cloud_archived is False

    def test_synced_false_in_trash_is_cloud_archived(self):
        """Documents that are both synced=false AND in trash should be hidden."""
        from remarkable_mcp.ssh import Document

        doc = Document(
            id="d4",
            hash="d4",
            name="Trashed Unsynced",
            doc_type="DocumentType",
            parent="trash",
            synced=False,
        )
        assert doc.is_cloud_archived is True


class TestRmcResolution:
    """Regression tests for rmc binary resolution (issues #52, #78, #80)."""

    def test_rmc_executable_returns_string(self):
        """_rmc_executable should always return a string path."""
        from remarkable_mcp.extract import _rmc_executable

        result = _rmc_executable()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_rmc_executable_finds_venv_binary(self):
        """_rmc_executable should find rmc in the venv's bin directory."""
        from remarkable_mcp.extract import _rmc_executable

        result = _rmc_executable()
        # Should find it either on PATH or in venv
        assert Path(result).stem == "rmc"

    def test_rmc_executable_falls_back_to_venv(self):
        """When rmc is not on PATH, should find it in the venv bin."""
        import sys

        from remarkable_mcp.extract import _rmc_executable

        venv_rmc = Path(sys.executable).parent / "rmc"
        if not venv_rmc.exists():
            pytest.skip("rmc not in venv bin")

        # Capture real which() before patching
        real_which = shutil.which

        # Patch so PATH lookup returns None, but venv-bin lookup works
        with patch("remarkable_mcp.extract.shutil.which") as mock_which:
            mock_which.side_effect = lambda name, path=None: (
                real_which(name, path=path) if path else None
            )
            result = _rmc_executable()
        assert Path(result).stem == "rmc"

    @patch("remarkable_mcp.extract.shutil.which", return_value=None)
    def test_rmc_executable_falls_back_to_bare(self, mock_which):
        """When rmc is nowhere, should return bare 'rmc' for clear error."""
        from remarkable_mcp.extract import _rmc_executable

        result = _rmc_executable()
        assert result == "rmc"

    @patch("remarkable_mcp.extract.subprocess.run")
    def test_rm_to_svg_v5_fallback(self, mock_run):
        """_rm_to_svg should use v5 fallback when rmc is not available."""
        import struct

        from remarkable_mcp.extract import _rm_to_svg

        # Simulate rmc not found
        mock_run.side_effect = FileNotFoundError("rmc not found")

        # Build minimal v5 .rm file with one stroke
        buf = bytearray()
        header = b"reMarkable .lines file, version=5          "
        buf.extend(header[:43])
        buf.extend(struct.pack("<I", 1))  # 1 layer
        buf.extend(struct.pack("<I", 1))  # 1 stroke
        pen, color, pad, base_width = 0, 0, 0, 2.0
        segments = [(100, 100, 0, 0, 2.0, 0.5), (200, 200, 0, 0, 2.0, 0.5)]
        buf.extend(struct.pack("<IIIIfI", pen, color, pad, 0, base_width, len(segments)))
        for x, y, speed, tilt, width, pressure in segments:
            buf.extend(struct.pack("<ffffff", x, y, speed, tilt, width, pressure))

        with tempfile.NamedTemporaryFile(suffix=".rm", delete=False) as rm_tmp:
            rm_tmp.write(bytes(buf))
            rm_path = Path(rm_tmp.name)
        with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as svg_tmp:
            svg_path = Path(svg_tmp.name)

        try:
            result = _rm_to_svg(rm_path, svg_path)
            assert result is True
            svg_content = svg_path.read_text()
            assert "<svg" in svg_content
            assert "M 100.0 100.0" in svg_content
        finally:
            rm_path.unlink(missing_ok=True)
            svg_path.unlink(missing_ok=True)

    def test_rm_to_svg_returns_false_for_garbage(self):
        """_rm_to_svg should return False for unrecognized file formats."""
        from remarkable_mcp.extract import _rm_to_svg

        with tempfile.NamedTemporaryFile(suffix=".rm", delete=False) as rm_tmp:
            rm_tmp.write(b"this is not a valid rm file at all")
            rm_path = Path(rm_tmp.name)
        with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as svg_tmp:
            svg_path = Path(svg_tmp.name)

        try:
            result = _rm_to_svg(rm_path, svg_path)
            assert result is False
        finally:
            rm_path.unlink(missing_ok=True)
            svg_path.unlink(missing_ok=True)


# =============================================================================
# Test USB Web Interface
# =============================================================================


class TestUSBWebInterface:
    """Test USB web interface client."""

    @patch("requests.request")
    def test_usb_web_check_connection(self, mock_request):
        """Test USB web interface connection check."""
        from remarkable_mcp.usb_web import USBWebClient

        # Mock successful response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = []
        mock_request.return_value = mock_response

        client = USBWebClient()
        assert client.check_connection() is True

        # Verify request was made
        mock_request.assert_called_once()

    @patch("requests.request")
    def test_usb_web_connection_error(self, mock_request):
        """Test USB web interface connection error."""
        from remarkable_mcp.usb_web import USBWebClient

        # Mock connection error
        mock_request.side_effect = Exception("Connection refused")

        client = USBWebClient()
        assert client.check_connection() is False

    @patch("requests.request")
    def test_usb_web_get_meta_items(self, mock_request):
        """Test fetching documents via USB web interface."""
        from remarkable_mcp.usb_web import USBWebClient

        # Mock successful response with documents
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"ID": "doc1", "VissibleName": "Test Doc", "Type": "DocumentType", "fileType": "pdf"},
            {"ID": "folder1", "VissibleName": "Test Folder", "Type": "CollectionType"},
        ]
        mock_request.return_value = mock_response

        client = USBWebClient()
        docs = client.get_meta_items()

        assert len(docs) >= 2
        assert any(d.name == "Test Doc" for d in docs)
        assert any(d.is_folder for d in docs)
        # fileType from API response is captured
        pdf_doc = next(d for d in docs if d.name == "Test Doc")
        assert pdf_doc.file_type == "pdf"
        assert client.get_file_type(pdf_doc) == "pdf"

    @patch("requests.request")
    def test_usb_web_download(self, mock_request):
        """Test downloading document via USB web interface."""
        from remarkable_mcp.usb_web import Document, USBWebClient

        # Mock successful download response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = b"fake zip content"
        mock_request.return_value = mock_response

        client = USBWebClient()
        doc = Document(id="doc1", hash="doc1", name="Test", doc_type="DocumentType")

        content = client.download(doc)
        assert content == b"fake zip content"

    @patch("remarkable_mcp.usb_web.create_usb_web_client")
    def test_get_rmapi_usb_web_mode(self, mock_create_client):
        """Test get_rmapi in USB web mode."""
        import os
        import sys

        # Set USB web mode before importing
        os.environ["REMARKABLE_USE_USB_WEB"] = "1"

        # Reload the module to pick up the new env var
        if "remarkable_mcp.api" in sys.modules:
            import importlib

            import remarkable_mcp.api

            importlib.reload(remarkable_mcp.api)
            from remarkable_mcp.api import get_rmapi
        else:
            from remarkable_mcp.api import get_rmapi

        # Mock USB web client
        mock_client = Mock()
        mock_create_client.return_value = mock_client

        try:
            client = get_rmapi()
            assert client == mock_client
            mock_create_client.assert_called_once()
        finally:
            # Clean up
            if "REMARKABLE_USE_USB_WEB" in os.environ:
                del os.environ["REMARKABLE_USE_USB_WEB"]
            # Reload to reset
            if "remarkable_mcp.api" in sys.modules:
                import importlib

                import remarkable_mcp.api

                importlib.reload(remarkable_mcp.api)

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.get_rmapi")
    async def test_status_usb_web_mode(self, mock_get_rmapi):
        """Test remarkable_status in USB web mode."""
        import os
        import sys

        # Set USB web mode before importing
        os.environ["REMARKABLE_USE_USB_WEB"] = "1"

        # Reload the modules to pick up the new env var
        if "remarkable_mcp.api" in sys.modules:
            import importlib

            import remarkable_mcp.api

            importlib.reload(remarkable_mcp.api)

        try:
            # Mock USB web client
            mock_client = Mock()
            mock_doc = Mock()
            mock_doc.is_folder = False
            mock_doc.VissibleName = "Test"
            mock_doc.ID = "doc1"
            mock_doc.Parent = ""
            mock_client.get_meta_items.return_value = [mock_doc]
            mock_get_rmapi.return_value = mock_client

            result = await mcp.call_tool("remarkable_status", {})
            data = json.loads(result[0][0].text)

            assert data["authenticated"] is True
            assert data["transport"] == "usb-web"
            assert "USB web interface" in data["connection"]
        finally:
            # Clean up
            if "REMARKABLE_USE_USB_WEB" in os.environ:
                del os.environ["REMARKABLE_USE_USB_WEB"]
            # Reload to reset
            if "remarkable_mcp.api" in sys.modules:
                import importlib

                import remarkable_mcp.api

                importlib.reload(remarkable_mcp.api)


# =============================================================================
# Test Retry Backoff
# =============================================================================


class TestCloudSyncFileHeaders:
    """Regression tests for reMarkable cloud rm-filename header validation."""

    def _response(self, *, text="", content=b"", json_data=None):
        response = Mock()
        response.status_code = 200
        response.text = text
        response.content = content
        response.headers = {}
        response.raise_for_status = Mock()
        if json_data is not None:
            response.json.return_value = json_data
        return response

    @patch("remarkable_mcp.sync._http_request_with_retry")
    def test_get_meta_items_sends_logical_rm_filename_headers(self, mock_request):
        """Cloud sync list requests must send the logical filename for each blob."""
        from remarkable_mcp.sync import FILES_URL, RemarkableClient

        root_hash = "root-hash"
        doc_id = "doc-123"
        doc_hash = "doc-hash"
        metadata_hash = "metadata-hash"

        mock_request.side_effect = [
            self._response(text='{"hash": "root-hash"}', json_data={"hash": root_hash}),
            self._response(content=f"3\n{doc_hash}:80000000:{doc_id}:1:123\n".encode("utf-8")),
            self._response(
                content=(f"3\n{metadata_hash}:0:{doc_id}.metadata:0:77\n").encode("utf-8")
            ),
            self._response(
                content=json.dumps(
                    {
                        "visibleName": "Header Test",
                        "type": "DocumentType",
                        "lastModified": "1710000000000",
                    }
                ).encode("utf-8")
            ),
        ]

        client = RemarkableClient(user_token="user-token")
        docs = client.get_meta_items()

        assert [doc.name for doc in docs] == ["Header Test"]
        file_headers = {
            call.args[1]: call.kwargs["headers"].get("rm-filename")
            for call in mock_request.call_args_list
            if call.args[1].startswith(FILES_URL)
        }
        assert file_headers == {
            f"{FILES_URL}/{root_hash}": "root.docSchema",
            f"{FILES_URL}/{doc_hash}": f"{doc_id}.docSchema",
            f"{FILES_URL}/{metadata_hash}": f"{doc_id}.metadata",
        }

    @patch("remarkable_mcp.sync._http_request_with_retry")
    def test_download_sends_entry_ids_as_rm_filename_headers(self, mock_request):
        """Cloud sync downloads must pass each blob's index id as rm-filename."""
        from remarkable_mcp.sync import FILES_URL, Document, RemarkableClient

        doc_id = "doc-123"
        doc_hash = "doc-hash"
        content_hash = "content-hash"
        page_hash = "page-hash"

        index_bytes = (
            f"3\n{content_hash}:0:{doc_id}.content:0:18\n{page_hash}:0:{doc_id}/page-1.rm:0:9\n"
        ).encode("utf-8")

        # Key responses by hash: download() fetches the content blobs in
        # parallel, so a positional side_effect list would be consumed in a
        # nondeterministic order.
        blob_by_hash = {
            doc_hash: index_bytes,
            content_hash: b'{"fileType": "notebook"}',
            page_hash: b"rm-bytes",
        }

        def fake_request(method, url, **kwargs):
            return self._response(content=blob_by_hash[url.rsplit("/", 1)[-1]])

        mock_request.side_effect = fake_request

        client = RemarkableClient(user_token="user-token")
        doc = Document(id=doc_id, hash=doc_hash, name="Header Test", doc_type="DocumentType")
        payload = client.download(doc)

        import io

        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            assert zf.read(f"{doc_id}.content") == b'{"fileType": "notebook"}'
            assert zf.read(f"{doc_id}/page-1.rm") == b"rm-bytes"
        file_headers = {
            call.args[1]: call.kwargs["headers"].get("rm-filename")
            for call in mock_request.call_args_list
            if call.args[1].startswith(FILES_URL)
        }
        assert file_headers == {
            f"{FILES_URL}/{doc_hash}": f"{doc_id}.docSchema",
            f"{FILES_URL}/{content_hash}": f"{doc_id}.content",
            f"{FILES_URL}/{page_hash}": f"{doc_id}/page-1.rm",
        }


class TestRetryBackoff:
    """Test retry with exponential backoff for cloud API requests."""

    @pytest.fixture(autouse=True)
    def _clean_retry_env(self, monkeypatch):
        """Ensure retry env vars are unset so tests use defaults."""
        monkeypatch.delenv("REMARKABLE_RETRY_ATTEMPTS", raising=False)
        monkeypatch.delenv("REMARKABLE_RETRY_DELAY", raising=False)

    @patch("remarkable_mcp.sync.time.sleep")
    @patch("remarkable_mcp.sync._issue_request")
    def test_retry_succeeds_after_transient_503(self, mock_request, mock_sleep):
        """Retry succeeds when a transient 503 clears on the second attempt."""
        from remarkable_mcp.sync import _http_request_with_retry

        fail_response = Mock()
        fail_response.status_code = 503
        fail_response.headers = {}

        ok_response = Mock()
        ok_response.status_code = 200

        mock_request.side_effect = [fail_response, ok_response]

        result = _http_request_with_retry("GET", "https://example.com/api")
        assert result.status_code == 200
        assert mock_request.call_count == 2
        mock_sleep.assert_called_once()

    @patch("remarkable_mcp.sync.time.sleep")
    @patch("remarkable_mcp.sync._issue_request")
    def test_retry_exhaustion_raises_last_exception(self, mock_request, mock_sleep):
        """After exhausting retries on connection errors, the last exception is raised."""
        from remarkable_mcp.sync import _http_request_with_retry

        mock_request.side_effect = requests.ConnectionError("refused")

        with pytest.raises(requests.ConnectionError, match="refused"):
            _http_request_with_retry("GET", "https://example.com/api")

        assert mock_request.call_count == 3  # DEFAULT_RETRY_ATTEMPTS

    @patch("remarkable_mcp.sync.time.sleep")
    @patch("remarkable_mcp.sync._issue_request")
    def test_no_retry_on_401(self, mock_request, mock_sleep):
        """401 is not retried - it is handled by the caller's token renewal."""
        from remarkable_mcp.sync import _http_request_with_retry

        response_401 = Mock()
        response_401.status_code = 401

        mock_request.return_value = response_401

        result = _http_request_with_retry("GET", "https://example.com/api")
        assert result.status_code == 401
        assert mock_request.call_count == 1
        mock_sleep.assert_not_called()

    @patch("remarkable_mcp.sync.time.sleep")
    @patch("remarkable_mcp.sync._issue_request")
    def test_no_retry_on_400(self, mock_request, mock_sleep):
        """400 is not retried - client errors are not transient."""
        from remarkable_mcp.sync import _http_request_with_retry

        response_400 = Mock()
        response_400.status_code = 400

        mock_request.return_value = response_400

        result = _http_request_with_retry("GET", "https://example.com/api")
        assert result.status_code == 400
        assert mock_request.call_count == 1
        mock_sleep.assert_not_called()

    @patch("remarkable_mcp.sync.time.sleep")
    @patch("remarkable_mcp.sync._issue_request")
    def test_retry_after_header_honoured(self, mock_request, mock_sleep):
        """Retry-After header value is used as the sleep duration."""
        from remarkable_mcp.sync import _http_request_with_retry

        rate_limited = Mock()
        rate_limited.status_code = 429
        rate_limited.headers = {"Retry-After": "5"}

        ok_response = Mock()
        ok_response.status_code = 200

        mock_request.side_effect = [rate_limited, ok_response]

        result = _http_request_with_retry("GET", "https://example.com/api")
        assert result.status_code == 200
        mock_sleep.assert_called_once_with(5.0)

    @patch("remarkable_mcp.sync.time.sleep")
    @patch("remarkable_mcp.sync._issue_request")
    def test_retry_after_header_capped_at_max(self, mock_request, mock_sleep):
        """Retry-After values above MAX_RETRY_DELAY are capped."""
        from remarkable_mcp.sync import MAX_RETRY_DELAY, _http_request_with_retry

        rate_limited = Mock()
        rate_limited.status_code = 429
        rate_limited.headers = {"Retry-After": "999"}

        ok_response = Mock()
        ok_response.status_code = 200

        mock_request.side_effect = [rate_limited, ok_response]

        result = _http_request_with_retry("GET", "https://example.com/api")
        assert result.status_code == 200
        mock_sleep.assert_called_once_with(MAX_RETRY_DELAY)

    def test_compute_sleep_within_bounds(self):
        """_compute_sleep always returns a value between 0 and MAX_RETRY_DELAY."""
        from remarkable_mcp.sync import MAX_RETRY_DELAY, _compute_sleep

        for attempt in range(10):
            for _ in range(50):
                val = _compute_sleep(2.0, attempt)
                assert 0 <= val <= MAX_RETRY_DELAY

    @patch("remarkable_mcp.sync.time.sleep")
    @patch("remarkable_mcp.sync._issue_request")
    def test_retry_exhaustion_returns_last_response(self, mock_request, mock_sleep):
        """When all retries return retryable status, the last response is returned."""
        from remarkable_mcp.sync import _http_request_with_retry

        response_503 = Mock()
        response_503.status_code = 503
        response_503.headers = {}

        mock_request.return_value = response_503

        result = _http_request_with_retry("GET", "https://example.com/api")
        assert result.status_code == 503
        assert mock_request.call_count == 3


# =============================================================================
# Test Write Tools
# =============================================================================


class TestWriteTools:
    """Test write tools opt-in behavior and safety checks."""

    def test_write_enabled_default_off(self):
        """Test that write_enabled() returns False by default."""
        from remarkable_mcp.write_tools import write_enabled

        # Ensure env var is not set
        old = os.environ.pop("REMARKABLE_ENABLE_WRITE", None)
        try:
            assert write_enabled() is False
        finally:
            if old is not None:
                os.environ["REMARKABLE_ENABLE_WRITE"] = old

    def test_write_enabled_with_env_var(self):
        """Test that write_enabled() returns True with env var."""
        from remarkable_mcp.write_tools import write_enabled

        old = os.environ.get("REMARKABLE_ENABLE_WRITE")
        try:
            os.environ["REMARKABLE_ENABLE_WRITE"] = "1"
            assert write_enabled() is True

            os.environ["REMARKABLE_ENABLE_WRITE"] = "true"
            assert write_enabled() is True

            os.environ["REMARKABLE_ENABLE_WRITE"] = "yes"
            assert write_enabled() is True

            os.environ["REMARKABLE_ENABLE_WRITE"] = "0"
            assert write_enabled() is False

            os.environ["REMARKABLE_ENABLE_WRITE"] = ""
            assert write_enabled() is False
        finally:
            if old is not None:
                os.environ["REMARKABLE_ENABLE_WRITE"] = old
            else:
                os.environ.pop("REMARKABLE_ENABLE_WRITE", None)

    @pytest.mark.asyncio
    async def test_write_tools_not_registered_by_default(self):
        """Test that write tools are NOT registered without --write flag."""
        tools = await mcp.list_tools()
        tool_names = [tool.name for tool in tools]

        write_tool_names = [
            "remarkable_upload",
            "remarkable_mkdir",
            "remarkable_move",
            "remarkable_rename",
            "remarkable_delete",
        ]

        for tool_name in write_tool_names:
            assert tool_name not in tool_names, (
                f"Write tool {tool_name} should not be registered without --write flag"
            )

    @pytest.mark.asyncio
    async def test_write_tools_registered_when_enabled(self):
        """Test that write tools ARE registered with REMARKABLE_ENABLE_WRITE=1 in SSH mode."""
        from remarkable_mcp.write_tools import register_write_tools

        with patch.dict(os.environ, {"REMARKABLE_USE_SSH": "1"}):
            register_write_tools()

        try:
            tools = await mcp.list_tools()
            tool_names = [tool.name for tool in tools]

            write_tool_names = [
                "remarkable_upload",
                "remarkable_mkdir",
                "remarkable_move",
                "remarkable_rename",
                "remarkable_delete",
            ]

            for tool_name in write_tool_names:
                assert tool_name in tool_names, f"Write tool {tool_name} should be registered"
        finally:
            # Clean up: remove registered tools to not affect other tests
            for name in [
                "remarkable_upload",
                "remarkable_mkdir",
                "remarkable_move",
                "remarkable_rename",
                "remarkable_delete",
            ]:
                mcp._tool_manager._tools.pop(name, None)

    @pytest.mark.asyncio
    async def test_delete_marks_document_deleted(self):
        """Test that delete marks the document's metadata as deleted."""
        from remarkable_mcp.write_tools import register_write_tools

        with patch.dict(os.environ, {"REMARKABLE_USE_SSH": "1"}):
            register_write_tools()

        try:
            mock_doc = Mock()
            mock_doc.VissibleName = "Test Doc"
            mock_doc.ID = "doc-123"
            mock_doc.Parent = ""
            mock_doc.is_folder = False
            mock_doc.is_cloud_archived = False

            with (
                patch("remarkable_mcp.write_tools.get_rmapi") as mock_get_rmapi,
                patch("remarkable_mcp.write_tools._write_metadata") as mock_write_meta,
                patch("remarkable_mcp.write_tools._restart_xochitl"),
                patch.dict(os.environ, {"REMARKABLE_USE_SSH": "1"}),
            ):
                import importlib

                import remarkable_mcp.api

                importlib.reload(remarkable_mcp.api)

                try:
                    mock_client = Mock(
                        spec=[
                            "get_meta_items",
                            "_scp_download",
                            "_ssh_command",
                            "_documents",
                            "_documents_by_id",
                            "host",
                            "user",
                            "port",
                            "password",
                        ]
                    )
                    mock_client.get_meta_items.return_value = [mock_doc]
                    mock_client._scp_download.return_value = (
                        b'{"visibleName": "Test Doc", "deleted": false}'
                    )
                    mock_get_rmapi.return_value = mock_client

                    result = await mcp.call_tool(
                        "remarkable_delete",
                        {
                            "document": "Test Doc",
                        },
                    )
                    data = json.loads(result[0][0].text)
                    assert data["deleted"] is True
                    assert data["name"] == "Test Doc"
                    # Verify metadata was written with deleted=True
                    mock_write_meta.assert_called_once()
                    written_metadata = mock_write_meta.call_args[0][2]
                    assert written_metadata["deleted"] is True
                finally:
                    if "REMARKABLE_USE_SSH" in os.environ:
                        del os.environ["REMARKABLE_USE_SSH"]
                    importlib.reload(remarkable_mcp.api)
        finally:
            for name in [
                "remarkable_upload",
                "remarkable_mkdir",
                "remarkable_move",
                "remarkable_rename",
                "remarkable_delete",
            ]:
                mcp._tool_manager._tools.pop(name, None)

    @pytest.mark.asyncio
    async def test_managed_write_tools_registered_in_cloud_mode(self):
        """Cloud mode now has full write parity: mkdir/move/rename/delete register."""
        from remarkable_mcp.write_tools import register_write_tools

        # Cloud mode: neither SSH nor USB web
        env = {k: v for k, v in os.environ.items() if k != "REMARKABLE_USE_SSH"}
        env.pop("REMARKABLE_USE_USB_WEB", None)
        with patch.dict(os.environ, env, clear=True):
            register_write_tools()
            try:
                tools = await mcp.list_tools()
                names = {t.name for t in tools}
                assert "remarkable_upload" in names
                assert "remarkable_mkdir" in names
                assert "remarkable_move" in names
                assert "remarkable_rename" in names
                assert "remarkable_delete" in names
            finally:
                for name in [
                    "remarkable_upload",
                    "remarkable_mkdir",
                    "remarkable_move",
                    "remarkable_rename",
                    "remarkable_delete",
                ]:
                    mcp._tool_manager._tools.pop(name, None)

    @pytest.mark.asyncio
    async def test_all_write_tools_have_xml_docstrings(self):
        """Test that all write tools have XML-structured documentation."""
        from remarkable_mcp.write_tools import register_write_tools

        with patch.dict(os.environ, {"REMARKABLE_USE_SSH": "1"}):
            register_write_tools()

        try:
            tools = await mcp.list_tools()
            write_tools = [
                t
                for t in tools
                if t.name
                in (
                    "remarkable_upload",
                    "remarkable_mkdir",
                    "remarkable_move",
                    "remarkable_rename",
                    "remarkable_delete",
                )
            ]

            for tool in write_tools:
                desc = tool.description
                assert "<usecase>" in desc, f"Write tool {tool.name} missing <usecase> tag"
        finally:
            for name in [
                "remarkable_upload",
                "remarkable_mkdir",
                "remarkable_move",
                "remarkable_rename",
                "remarkable_delete",
            ]:
                mcp._tool_manager._tools.pop(name, None)

    @pytest.mark.asyncio
    async def test_upload_dispatches_to_cloud(self):
        """Upload in cloud mode dispatches to the cloud client's upload_document."""
        import tempfile

        from remarkable_mcp.write_tools import register_write_tools

        env = {k: v for k, v in os.environ.items() if k != "REMARKABLE_USE_SSH"}
        env.pop("REMARKABLE_USE_USB_WEB", None)
        env["REMARKABLE_ENABLE_WRITE"] = "1"
        with patch.dict(os.environ, env, clear=True):
            register_write_tools()
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(b"%PDF-1.4 test")
                pdf_path = tmp.name
            try:
                mock_doc = Mock()
                mock_doc.id = "new-doc-id"
                mock_client = Mock(spec=["get_meta_items", "upload_document"])
                mock_client.get_meta_items.return_value = []
                mock_client.upload_document.return_value = mock_doc

                with patch("remarkable_mcp.write_tools.get_rmapi", return_value=mock_client):
                    result = await mcp.call_tool(
                        "remarkable_upload",
                        {"file_path": pdf_path, "document_name": "My Doc"},
                    )
                data = json.loads(result[0][0].text)
                assert data["uploaded"] is True
                assert data["transport"] == "cloud"
                assert data["uuid"] == "new-doc-id"
                mock_client.upload_document.assert_called_once()
                # content, name, ext, parent_id
                args = mock_client.upload_document.call_args[0]
                assert args[1] == "My Doc"
                assert args[2] == "pdf"
                assert args[3] == ""  # root
            finally:
                os.unlink(pdf_path)
                for name in [
                    "remarkable_upload",
                    "remarkable_mkdir",
                    "remarkable_move",
                    "remarkable_rename",
                    "remarkable_delete",
                ]:
                    mcp._tool_manager._tools.pop(name, None)

    @pytest.mark.asyncio
    async def test_mkdir_not_registered_in_usb_web_mode(self):
        """SSH-only write tools must not be exposed in USB web mode (upload-only)."""
        from remarkable_mcp.write_tools import register_write_tools

        env = {k: v for k, v in os.environ.items() if k != "REMARKABLE_USE_SSH"}
        env["REMARKABLE_USE_USB_WEB"] = "1"
        with patch.dict(os.environ, env, clear=True):
            register_write_tools()
            try:
                tools = await mcp.list_tools()
                names = {t.name for t in tools}
                assert "remarkable_upload" in names  # upload works on USB web
                assert "remarkable_mkdir" not in names
                assert "remarkable_move" not in names
                assert "remarkable_rename" not in names
                assert "remarkable_delete" not in names
            finally:
                for name in [
                    "remarkable_upload",
                    "remarkable_mkdir",
                    "remarkable_move",
                    "remarkable_rename",
                    "remarkable_delete",
                ]:
                    mcp._tool_manager._tools.pop(name, None)


class TestCloudWriteDispatch:
    """Cloud-mode write tools dispatch to the RemarkableClient methods."""

    def _cloud_env(self):
        env = {k: v for k, v in os.environ.items() if k != "REMARKABLE_USE_SSH"}
        env.pop("REMARKABLE_USE_USB_WEB", None)
        env["REMARKABLE_ENABLE_WRITE"] = "1"
        return env

    def _make_item(self, doc_id, name, parent="", is_folder=False):
        item = Mock()
        item.ID = doc_id
        item.VissibleName = name
        item.Parent = parent
        item.is_folder = is_folder
        return item

    def _cleanup(self):
        for name in [
            "remarkable_upload",
            "remarkable_mkdir",
            "remarkable_move",
            "remarkable_rename",
            "remarkable_delete",
        ]:
            mcp._tool_manager._tools.pop(name, None)

    @pytest.mark.asyncio
    async def test_cloud_mkdir_dispatch(self):
        from remarkable_mcp.write_tools import register_write_tools

        with patch.dict(os.environ, self._cloud_env(), clear=True):
            register_write_tools()
            try:
                new_folder = Mock()
                new_folder.id = "folder-xyz"
                client = Mock(spec=["get_meta_items", "create_folder"])
                client.get_meta_items.return_value = []
                client.create_folder.return_value = new_folder
                with patch("remarkable_mcp.write_tools.get_rmapi", return_value=client):
                    result = await mcp.call_tool("remarkable_mkdir", {"folder_name": "Projects"})
                data = json.loads(result[0][0].text)
                assert data["created"] is True
                assert data["transport"] == "cloud"
                assert data["uuid"] == "folder-xyz"
                client.create_folder.assert_called_once_with("Projects", "")
            finally:
                self._cleanup()

    @pytest.mark.asyncio
    async def test_cloud_rename_dispatch(self):
        from remarkable_mcp.write_tools import register_write_tools

        with patch.dict(os.environ, self._cloud_env(), clear=True):
            register_write_tools()
            try:
                target = self._make_item("doc-1", "Old Name")
                client = Mock(spec=["get_meta_items", "rename"])
                client.get_meta_items.return_value = [target]
                with patch("remarkable_mcp.write_tools.get_rmapi", return_value=client):
                    result = await mcp.call_tool(
                        "remarkable_rename",
                        {"document": "Old Name", "new_name": "New Name"},
                    )
                data = json.loads(result[0][0].text)
                assert data["renamed"] is True
                assert data["transport"] == "cloud"
                client.rename.assert_called_once_with("doc-1", "New Name")
            finally:
                self._cleanup()

    @pytest.mark.asyncio
    async def test_cloud_move_dispatch(self):
        from remarkable_mcp.write_tools import register_write_tools

        with patch.dict(os.environ, self._cloud_env(), clear=True):
            register_write_tools()
            try:
                target = self._make_item("doc-1", "Report")
                dest = self._make_item("fold-1", "Archive", is_folder=True)
                client = Mock(spec=["get_meta_items", "move"])
                client.get_meta_items.return_value = [target, dest]
                with patch("remarkable_mcp.write_tools.get_rmapi", return_value=client):
                    result = await mcp.call_tool(
                        "remarkable_move",
                        {"document": "Report", "dest_folder": "Archive"},
                    )
                data = json.loads(result[0][0].text)
                assert data["moved"] is True
                assert data["transport"] == "cloud"
                client.move.assert_called_once_with("doc-1", "fold-1")
            finally:
                self._cleanup()

    @pytest.mark.asyncio
    async def test_cloud_delete_dispatch(self):
        from remarkable_mcp.write_tools import register_write_tools

        with patch.dict(os.environ, self._cloud_env(), clear=True):
            register_write_tools()
            try:
                target = self._make_item("doc-1", "Old Notes")
                client = Mock(spec=["get_meta_items", "delete"])
                client.get_meta_items.return_value = [target]
                with patch("remarkable_mcp.write_tools.get_rmapi", return_value=client):
                    result = await mcp.call_tool("remarkable_delete", {"document": "Old Notes"})
                data = json.loads(result[0][0].text)
                assert data["deleted"] is True
                assert data["transport"] == "cloud"
                client.delete.assert_called_once_with("doc-1")
            finally:
                self._cleanup()

    @pytest.mark.asyncio
    async def test_cloud_delete_cancelled_by_elicitation(self):
        """When the user declines elicitation, delete is aborted and nothing changes."""
        from remarkable_mcp.write_tools import register_write_tools

        with patch.dict(os.environ, self._cloud_env(), clear=True):
            register_write_tools()
            try:
                client = Mock(spec=["get_meta_items", "delete"])
                decline = Mock()
                decline.action = "decline"
                decline.data = None
                with (
                    patch("remarkable_mcp.write_tools.get_rmapi", return_value=client),
                    patch(
                        "remarkable_mcp.write_tools.client_supports_elicitation",
                        return_value=True,
                    ),
                    patch(
                        "mcp.server.fastmcp.Context.elicit",
                        new=AsyncMock(return_value=decline),
                    ),
                ):
                    result = await mcp.call_tool("remarkable_delete", {"document": "Old Notes"})
                data = json.loads(result[0][0].text)
                assert data["deleted"] is False
                assert data["cancelled"] is True
                client.delete.assert_not_called()
            finally:
                self._cleanup()


class TestConcurrentToolDispatch:
    """Regression: tool calls must not serialize on the event loop.

    Before the asyncio.to_thread fix, async tool handlers ran their blocking
    work (subprocess/SSH/requests/etc.) directly on the asyncio loop, so a
    second concurrent call_tool request was forced to wait for the first one
    to finish. This test simulates blocking I/O with time.sleep inside the
    mocked client and asserts that two concurrent calls overlap.
    """

    @patch("remarkable_mcp.tools.get_rmapi")
    async def test_concurrent_browse_calls_overlap(self, mock_get_rmapi):
        import asyncio
        import time

        call_delay = 0.4

        def slow_get_meta_items():
            time.sleep(call_delay)
            return []

        mock_client = Mock()
        mock_client.get_meta_items.side_effect = slow_get_meta_items
        mock_get_rmapi.return_value = mock_client

        start = time.monotonic()
        results = await asyncio.gather(
            mcp.call_tool("remarkable_browse", {}),
            mcp.call_tool("remarkable_browse", {}),
        )
        elapsed = time.monotonic() - start

        assert len(results) == 2
        # Two concurrent calls should complete in well under 2x the per-call
        # delay; if they serialized we'd see ~2 * call_delay.
        assert elapsed < call_delay * 1.7, (
            f"Concurrent tool calls appear serialized: elapsed={elapsed:.2f}s "
            f"vs single-call delay={call_delay}s"
        )


class TestCloudBlobCache:
    """Tests for the content-addressed blob cache in the cloud client."""

    def _response(self, *, content=b"", status=200):
        response = Mock()
        response.status_code = status
        response.content = content
        response.headers = {}
        response.raise_for_status = Mock()
        return response

    @patch("remarkable_mcp.sync._http_request_with_retry")
    def test_get_file_caches_by_hash(self, mock_request):
        """A second fetch of the same hash is served from cache (no new request)."""
        from remarkable_mcp.sync import RemarkableClient

        mock_request.return_value = self._response(content=b"blob-bytes")
        client = RemarkableClient(user_token="user-token")

        first = client._get_file("hash-a", "a.docSchema")
        second = client._get_file("hash-a", "a.docSchema")

        assert first == b"blob-bytes"
        assert second == b"blob-bytes"
        assert mock_request.call_count == 1

    @patch("remarkable_mcp.sync._http_request_with_retry")
    def test_different_hash_is_refetched(self, mock_request):
        """A changed document yields a new hash, which is fetched fresh.

        This is what makes the cache invalidation-safe: blobs are keyed by their
        content hash, so a modified document always produces a different key.
        """
        from remarkable_mcp.sync import RemarkableClient

        mock_request.side_effect = [
            self._response(content=b"old-content"),
            self._response(content=b"new-content"),
        ]
        client = RemarkableClient(user_token="user-token")

        assert client._get_file("hash-old", "doc.content") == b"old-content"
        assert client._get_file("hash-new", "doc.content") == b"new-content"
        assert mock_request.call_count == 2

    @patch("remarkable_mcp.sync._http_request_with_retry")
    def test_cache_can_be_disabled(self, mock_request, monkeypatch):
        """REMARKABLE_DISABLE_CACHE forces every fetch to hit the network."""
        from remarkable_mcp.sync import RemarkableClient

        monkeypatch.setenv("REMARKABLE_DISABLE_CACHE", "1")
        mock_request.side_effect = [
            self._response(content=b"v1"),
            self._response(content=b"v1"),
        ]
        client = RemarkableClient(user_token="user-token")

        client._get_file("hash-a", "a.docSchema")
        client._get_file("hash-a", "a.docSchema")
        assert mock_request.call_count == 2

    @patch("remarkable_mcp.sync._http_request_with_retry")
    def test_large_blobs_are_not_cached(self, mock_request, monkeypatch):
        """Blobs above the size threshold are streamed through, not cached."""
        from remarkable_mcp.sync import RemarkableClient

        monkeypatch.setenv("REMARKABLE_CACHE_MAX_BLOB", "8")
        big = b"x" * 64
        mock_request.side_effect = [
            self._response(content=big),
            self._response(content=big),
        ]
        client = RemarkableClient(user_token="user-token")

        client._get_file("hash-big", "big.bin")
        client._get_file("hash-big", "big.bin")
        assert mock_request.call_count == 2


class TestSessionPooling:
    """Tests for the thread-local pooled HTTP session seam."""

    def test_get_session_is_thread_local_and_reused(self):
        """The same thread reuses one pooled session across calls."""
        from remarkable_mcp import sync

        sync._thread_local.__dict__.pop("session", None)
        try:
            session_a = sync._get_session()
            session_b = sync._get_session()
            assert session_a is session_b
            assert session_a.adapters  # adapters mounted
        finally:
            sync._thread_local.__dict__.pop("session", None)

    def test_issue_request_uses_pooled_session(self):
        """_issue_request dispatches through the thread-local session."""
        from remarkable_mcp import sync

        fake_session = Mock()
        fake_session.request.return_value = "resp"
        with patch("remarkable_mcp.sync._get_session", return_value=fake_session):
            result = sync._issue_request("GET", "https://example.com", timeout=5)
        assert result == "resp"
        fake_session.request.assert_called_once_with("GET", "https://example.com", timeout=5)


class TestParallelDownload:
    """Tests for parallelized, order-stable cloud document downloads."""

    def _response(self, *, content=b""):
        response = Mock()
        response.status_code = 200
        response.content = content
        response.headers = {}
        response.raise_for_status = Mock()
        return response

    @patch("remarkable_mcp.sync._http_request_with_retry")
    def test_download_preserves_blob_order(self, mock_request):
        """Parallel fetches still assemble the zip in original blob order."""
        import io

        from remarkable_mcp.sync import Document, RemarkableClient

        doc_id = "doc-1"
        index = "3\n" + "".join(f"hash-{i}:0:{doc_id}/page-{i}.rm:0:5\n" for i in range(10))

        # Key responses by hash (the real client fetches FILES_URL/<hash>), so
        # the test is deterministic regardless of the order parallel workers run.
        def fake_request(method, url, **kwargs):
            blob_hash = url.rsplit("/", 1)[-1]
            if blob_hash == "doc-hash":
                return self._response(content=index.encode("utf-8"))
            idx = int(blob_hash.split("-")[1])
            return self._response(content=f"page-{idx}".encode("utf-8"))

        mock_request.side_effect = fake_request

        client = RemarkableClient(user_token="user-token")
        doc = Document(id=doc_id, hash="doc-hash", name="N", doc_type="DocumentType")
        payload = client.download(doc)

        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            names = zf.namelist()
            assert names == [f"{doc_id}/page-{i}.rm" for i in range(10)]
            for i in range(10):
                assert zf.read(f"{doc_id}/page-{i}.rm") == f"page-{i}".encode("utf-8")


class TestBackgroundLoaderSingleFetch:
    """Regression: the background loader must fetch the library exactly once."""

    @pytest.mark.asyncio
    async def test_loader_fetches_once_without_limit(self, monkeypatch):
        import asyncio

        import remarkable_mcp.resources as resources

        fake_client = Mock()
        fake_client.get_meta_items.return_value = [
            Mock(is_folder=False),
            Mock(is_folder=False),
            Mock(is_folder=True),
        ]

        monkeypatch.setattr("remarkable_mcp.api.get_rmapi", lambda: fake_client)
        monkeypatch.setattr("remarkable_mcp.api.get_items_by_id", lambda items: {})

        registered = []
        monkeypatch.setattr(
            resources,
            "_register_document",
            lambda client, doc, items_by_id, file_types=None, root="/": registered.append(doc),
        )

        await resources._load_documents_background(asyncio.Event())

        # Exactly one unbounded fetch (no growing per-batch limit -> no O(n^2)).
        fake_client.get_meta_items.assert_called_once_with()
        # Only the two non-folder documents are registered.
        assert len(registered) == 2


class TestCloudClientCache:
    """The cloud client must be cached per process (one token renewal)."""

    def test_cloud_client_cached_and_resettable(self, monkeypatch, tmp_path):
        import remarkable_mcp.api as api

        # Force cloud mode and redirect HOME so we never touch the real ~/.rmapi.
        monkeypatch.setattr(api.Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(api, "REMARKABLE_USE_USB_WEB", False)
        monkeypatch.setattr(api, "REMARKABLE_USE_SSH", False)
        monkeypatch.setattr(api, "REMARKABLE_TOKEN", '{"devicetoken": "d", "usertoken": "u"}')

        created = []

        def fake_loader(token_json):
            client = Mock(name=f"client-{len(created)}")
            created.append(client)
            return client

        monkeypatch.setattr("remarkable_mcp.sync.load_client_from_token", fake_loader)

        api.reset_client_cache()
        first = api.get_rmapi()
        second = api.get_rmapi()
        assert first is second
        assert len(created) == 1  # built only once

        # Resetting forces a rebuild (e.g. after re-registering a token).
        api.reset_client_cache()
        third = api.get_rmapi()
        assert third is not first
        assert len(created) == 2
