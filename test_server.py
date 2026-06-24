#!/usr/bin/env python3
"""
Tests for reMarkable MCP Server

Tests the 4 intent-based tools using FastMCP's testing capabilities.
"""

import json
import os
import sys
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
            "remarkable_review",
            "remarkable_status",
            "remarkable_image",
            "remarkable_canvas",
        ]

        for tool_name in expected_tools:
            assert tool_name in tool_names, f"Tool {tool_name} not found"

    @pytest.mark.asyncio
    async def test_tools_count(self):
        """Cloud default: read tools + canvas + writes + cloud-safe authoring.

        In cloud mode ``remarkable_author`` is exposed for ``create_document`` and
        ``daily_notebook``; add_page/draw remain SSH-only.
        """
        tools = await mcp.list_tools()
        assert len(tools) == 14, f"Expected 14 tools, got {len(tools)}"

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

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.get_rmapi")
    async def test_status_reports_cloud_after_fallback(self, mock_get_rmapi, monkeypatch):
        """When USB/SSH falls back to cloud, status reports the effective transport."""
        import remarkable_mcp.api as api

        monkeypatch.setattr(api, "REMARKABLE_USE_USB_WEB", True)
        monkeypatch.setattr(api, "REMARKABLE_USE_SSH", False)
        # Simulate that get_rmapi() already resolved to a cloud fallback.
        monkeypatch.setattr(api, "get_active_transport", lambda: "cloud")

        mock_client = Mock()
        mock_client.get_meta_items.return_value = []
        mock_get_rmapi.return_value = mock_client

        result = await mcp.call_tool("remarkable_status", {})
        data = json.loads(result[0][0].text)

        assert data["authenticated"] is True
        assert data["transport"] == "cloud"
        assert data["fell_back_to_cloud"] is True
        # Effective capabilities reflect cloud (full write surface when enabled).
        assert data["capabilities"]["read"] is True
        assert "usb-web" in data["_hint"]  # mentions what it fell back from
        assert "fell back" in data["_hint"].lower()


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
# Test remarkable_review Tool
# =============================================================================


class TestRemarkableReview:
    """Test remarkable_review export helper."""

    @pytest.mark.asyncio
    async def test_review_single_document_json_export(self):
        """Review exports one document as deterministic JSON without summarizing."""
        read_payload = make_response(
            {
                "document": "Daily Notes",
                "path": "/Daily/Daily Notes",
                "file_type": "notebook",
                "content": "todo: review notes",
                "page": 1,
                "total_pages": 1,
                "more": False,
                "modified": "2026-06-24T10:00:00Z",
                "tags": ["review"],
            },
            "ok",
        )

        with patch(
            "remarkable_mcp.tools.remarkable_read", new=AsyncMock(return_value=read_payload)
        ):
            result = await mcp.call_tool(
                "remarkable_review",
                {"document": "/Daily/Daily Notes", "output_format": "json"},
            )

        data = json.loads(result[0][0].text)
        assert data["output_format"] == "json"
        assert data["scope"] == {"document": "/Daily/Daily Notes"}
        assert data["count"] == 1
        assert data["documents"][0]["path"] == "/Daily/Daily Notes"
        assert data["documents"][0]["content"] == "todo: review notes"
        assert data["documents"][0]["tags"] == ["review"]
        assert "summary" not in data["documents"][0]

    @pytest.mark.asyncio
    async def test_review_single_document_markdown_export(self):
        """Review can render deterministic Markdown for a document."""
        read_payload = make_response(
            {
                "document": "Daily Notes",
                "path": "/Daily/Daily Notes",
                "file_type": "notebook",
                "content": "archive this",
                "page": 1,
                "total_pages": 1,
                "more": False,
                "modified": None,
            },
            "ok",
        )

        with patch(
            "remarkable_mcp.tools.remarkable_read", new=AsyncMock(return_value=read_payload)
        ):
            result = await mcp.call_tool(
                "remarkable_review",
                {"document": "/Daily/Daily Notes", "output_format": "markdown"},
            )

        text = result[0][0].text
        assert text.startswith("# reMarkable review export")
        assert "## Daily Notes" in text
        assert "Path: `/Daily/Daily Notes`" in text
        assert "archive this" in text

    @pytest.mark.asyncio
    async def test_review_folder_filters_tags_and_since(self):
        """Folder review exports matching documents using browse metadata."""
        browse_payload = make_response(
            {
                "mode": "browse",
                "path": "/Daily",
                "documents": [
                    {
                        "name": "Keep",
                        "path": "/Daily/Keep",
                        "type": "document",
                        "modified": "2026-06-24T10:00:00Z",
                        "tags": ["review"],
                    },
                    {
                        "name": "Old",
                        "path": "/Daily/Old",
                        "type": "document",
                        "modified": "2026-06-20T10:00:00Z",
                        "tags": ["review"],
                    },
                ],
            },
            "ok",
        )
        read_payload = make_response(
            {
                "document": "Keep",
                "path": "/Daily/Keep",
                "file_type": "pdf",
                "content": "review me",
                "page": 1,
                "total_pages": 1,
                "more": False,
                "modified": "2026-06-24T10:00:00Z",
                "tags": ["review"],
            },
            "ok",
        )

        with (
            patch(
                "remarkable_mcp.tools.remarkable_browse", new=AsyncMock(return_value=browse_payload)
            ),
            patch(
                "remarkable_mcp.tools.remarkable_read", new=AsyncMock(return_value=read_payload)
            ) as read,
        ):
            result = await mcp.call_tool(
                "remarkable_review",
                {
                    "folder": "/Daily",
                    "tags": ["review"],
                    "since": "2026-06-23T00:00:00Z",
                    "output_format": "json",
                },
            )

        data = json.loads(result[0][0].text)
        assert data["scope"] == {
            "folder": "/Daily",
            "tags": ["review"],
            "since": "2026-06-23T00:00:00Z",
        }
        assert data["count"] == 1
        assert data["documents"][0]["path"] == "/Daily/Keep"
        read.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_review_rejects_invalid_output_format(self):
        """Review rejects unknown output formats with a structured error."""
        result = await mcp.call_tool(
            "remarkable_review",
            {"document": "Daily Notes", "output_format": "html"},
        )
        data = json.loads(result[0][0].text)
        assert data["_error"]["type"] == "invalid_output_format"


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
    @patch("remarkable_mcp.tools.render_page_full_page_from_document_zip")
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
        mock_full,
        mock_document,
    ):
        """With no local render, no tablet PDF, and no blank-page render, report render_failed."""
        mock_client = Mock()
        mock_get_rmapi.return_value = mock_client
        mock_document.is_folder = False
        mock_client.get_meta_items.return_value = [mock_document]
        mock_client.download.return_value = b"fake zip"
        mock_page_count.return_value = 1
        mock_render.return_value = None
        mock_download_raw.return_value = None  # cloud: no native export
        mock_full.return_value = None  # full-page blank render also unavailable

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

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.render_page_full_page_from_document_zip")
    @patch("remarkable_mcp.tools.download_raw_file")
    @patch("remarkable_mcp.tools.render_page_from_document_zip")
    @patch("remarkable_mcp.tools.get_document_page_count")
    @patch("remarkable_mcp.tools.get_rmapi")
    async def test_image_falls_back_to_blank_full_page(
        self,
        mock_get_rmapi,
        mock_page_count,
        mock_render,
        mock_download_raw,
        mock_full,
        mock_document,
    ):
        """A strokeless notebook page renders as a blank page (matches the canvas).

        When the stroke renderer returns None and there is no PDF underlay (a
        notebook), remarkable_image falls back to the full-page renderer used by
        remarkable_canvas so a blank/freshly-created page yields a blank image
        instead of render_failed.
        """
        mock_client = Mock()
        mock_get_rmapi.return_value = mock_client
        mock_document.is_folder = False
        mock_client.get_meta_items.return_value = [mock_document]
        mock_client.download.return_value = b"fake zip"
        mock_page_count.return_value = 1
        mock_render.return_value = None  # no strokes -> stroke renderer None
        mock_download_raw.return_value = None  # notebook -> no PDF underlay
        mock_full.return_value = (b"\x89PNG-blank-page", (1404.0, 1872.0))

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
        assert "_error" not in data
        assert data.get("image_base64")
        mock_full.assert_called_once()


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

        assert len(tools) == 14

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


def _make_v6_rm_bytes() -> bytes:
    """Build a minimal current-firmware (v6) .rm file with a single line.

    Uses rmscene's own writer, which both produces a deterministic fixture and
    exercises the rmscene>=0.8.0 read/write round-trip our renderer relies on
    (the version that parses the current scene format — see #95 / PR #97).
    """
    import io
    import uuid

    from rmscene import scene_items as si
    from rmscene.crdt_sequence import CrdtSequenceItem
    from rmscene.scene_stream import (
        AuthorIdsBlock,
        MigrationInfoBlock,
        PageInfoBlock,
        SceneLineItemBlock,
        SceneTreeBlock,
        TreeNodeBlock,
        write_blocks,
    )
    from rmscene.tagged_block_common import CrdtId

    points = [
        si.Point(x=float(x), y=0.0, speed=0, direction=0, width=2, pressure=0) for x in (-50, 0, 50)
    ]
    line = si.Line(
        color=si.PenColor.BLACK,
        tool=si.Pen.BALLPOINT_1,
        points=points,
        thickness_scale=1.0,
        starting_length=0.0,
    )
    node_id = CrdtId(0, 11)
    blocks = [
        AuthorIdsBlock(author_uuids={1: uuid.uuid4()}),
        MigrationInfoBlock(migration_id=CrdtId(0, 1), is_device=True),
        PageInfoBlock(loads_count=1, merges_count=0, text_chars_count=0, text_lines_count=0),
        SceneTreeBlock(
            tree_id=node_id,
            node_id=CrdtId(0, 0),
            is_update=True,
            parent_id=CrdtId(0, 0),
        ),
        TreeNodeBlock(group=si.Group(node_id=node_id)),
        SceneLineItemBlock(
            parent_id=node_id,
            item=CrdtSequenceItem(
                item_id=CrdtId(0, 12),
                left_id=CrdtId(0, 0),
                right_id=CrdtId(0, 0),
                deleted_length=0,
                value=line,
            ),
        ),
    ]
    buf = io.BytesIO()
    write_blocks(buf, blocks)
    return buf.getvalue()


class TestRmToSvgRendering:
    """Regression tests for .rm -> SVG rendering after dropping rmc (PR #97).

    rmc transitively pinned rmscene<0.7.0, which cannot parse current-firmware
    .rm scene blocks (#95). _rm_to_svg now renders via the in-repo rmscene v6/v5
    renderers directly, with no rmc subprocess.
    """

    def test_rmc_dependency_removed(self):
        """The rmc subprocess helper is gone; rmscene is the modern (>=0.8) parser."""
        from importlib.metadata import version

        from packaging.version import Version

        from remarkable_mcp import extract

        assert not hasattr(extract, "_rmc_executable")
        assert Version(version("rmscene")) >= Version("0.8.0")

    def test_rm_to_svg_renders_v6_current_firmware(self):
        """_rm_to_svg renders a current-firmware (v6) file via rmscene (the #95 fix)."""
        from remarkable_mcp.extract import _rm_to_svg

        try:
            data = _make_v6_rm_bytes()
        except Exception as exc:  # pragma: no cover - guards rmscene API drift
            pytest.skip(f"could not synthesize a v6 fixture with rmscene: {exc}")

        assert data[:33] == b"reMarkable .lines file, version=6"

        with tempfile.NamedTemporaryFile(suffix=".rm", delete=False) as rm_tmp:
            rm_tmp.write(data)
            rm_path = Path(rm_tmp.name)
        with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as svg_tmp:
            svg_path = Path(svg_tmp.name)

        try:
            result = _rm_to_svg(rm_path, svg_path)
            assert result is True
            svg_content = svg_path.read_text()
            assert "<svg" in svg_content
            assert "<path" in svg_content
        finally:
            rm_path.unlink(missing_ok=True)
            svg_path.unlink(missing_ok=True)

    def test_rm_to_svg_renders_v5(self):
        """_rm_to_svg renders a v5 file via the built-in renderer (no rmc needed)."""
        import struct

        from remarkable_mcp.extract import _rm_to_svg

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

    def test_parse_retry_after_seconds(self):
        """Numeric (delay-seconds) Retry-After is parsed as seconds."""
        from remarkable_mcp.sync import _parse_retry_after

        resp = Mock()
        resp.headers = {"Retry-After": "7"}
        assert _parse_retry_after(resp) == 7.0

    def test_parse_retry_after_http_date_future_capped(self):
        """HTTP-date Retry-After in the future is honoured and capped at MAX."""
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime

        from remarkable_mcp.sync import MAX_RETRY_DELAY, _parse_retry_after

        future = datetime.now(timezone.utc) + timedelta(hours=1)
        resp = Mock()
        resp.headers = {"Retry-After": format_datetime(future)}
        # An hour out is well beyond MAX_RETRY_DELAY, so it clamps to the cap.
        assert _parse_retry_after(resp) == MAX_RETRY_DELAY

    def test_parse_retry_after_http_date_past_returns_none(self):
        """An HTTP-date already in the past yields None (fall back to backoff)."""
        from remarkable_mcp.sync import _parse_retry_after

        resp = Mock()
        resp.headers = {"Retry-After": "Wed, 21 Oct 2020 07:28:00 GMT"}
        assert _parse_retry_after(resp) is None

    def test_parse_retry_after_invalid_and_missing_return_none(self):
        """Garbage or missing Retry-After yields None."""
        from remarkable_mcp.sync import _parse_retry_after

        garbage = Mock()
        garbage.headers = {"Retry-After": "soon-ish"}
        assert _parse_retry_after(garbage) is None

        missing = Mock()
        missing.headers = {}
        assert _parse_retry_after(missing) is None

    @patch("remarkable_mcp.sync.time.sleep")
    @patch("remarkable_mcp.sync._issue_request")
    def test_retry_after_http_date_honoured_through_wrapper(self, mock_request, mock_sleep):
        """A 429 with an HTTP-date Retry-After drives the backoff sleep."""
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime

        from remarkable_mcp.sync import MAX_RETRY_DELAY, _http_request_with_retry

        future = datetime.now(timezone.utc) + timedelta(hours=1)
        rate_limited = Mock()
        rate_limited.status_code = 429
        rate_limited.headers = {"Retry-After": format_datetime(future)}

        ok_response = Mock()
        ok_response.status_code = 200

        mock_request.side_effect = [rate_limited, ok_response]

        result = _http_request_with_retry("GET", "https://example.com/api")
        assert result.status_code == 200
        mock_sleep.assert_called_once_with(MAX_RETRY_DELAY)


# =============================================================================
# Test Write Tools
# =============================================================================


class TestWriteTools:
    """Test write tools default-on behavior, the read-only gate, and safety checks."""

    def test_write_enabled_default_on(self):
        """write_enabled() is True by default (write is the default mode)."""
        from remarkable_mcp.write_tools import read_only_enabled, write_enabled

        old = os.environ.pop("REMARKABLE_READ_ONLY", None)
        try:
            assert write_enabled() is True
            assert read_only_enabled() is False
        finally:
            if old is not None:
                os.environ["REMARKABLE_READ_ONLY"] = old

    def test_read_only_env_var_disables_write(self):
        """REMARKABLE_READ_ONLY disables write tools; falsy/unset leaves them on."""
        from remarkable_mcp.write_tools import read_only_enabled, write_enabled

        old = os.environ.get("REMARKABLE_READ_ONLY")
        try:
            for truthy in ("1", "true", "yes"):
                os.environ["REMARKABLE_READ_ONLY"] = truthy
                assert write_enabled() is False, truthy
                assert read_only_enabled() is True, truthy

            for falsy in ("0", "", "no"):
                os.environ["REMARKABLE_READ_ONLY"] = falsy
                assert write_enabled() is True, falsy
                assert read_only_enabled() is False, falsy
        finally:
            if old is not None:
                os.environ["REMARKABLE_READ_ONLY"] = old
            else:
                os.environ.pop("REMARKABLE_READ_ONLY", None)

    def test_legacy_write_env_var_is_noop(self):
        """The legacy REMARKABLE_ENABLE_WRITE var no longer affects write_enabled()."""
        from remarkable_mcp.write_tools import write_enabled

        old_enable = os.environ.get("REMARKABLE_ENABLE_WRITE")
        old_ro = os.environ.pop("REMARKABLE_READ_ONLY", None)
        try:
            # Even explicitly "disabling" via the legacy var keeps write on.
            os.environ["REMARKABLE_ENABLE_WRITE"] = "0"
            assert write_enabled() is True
            # Read-only still wins regardless of the legacy var.
            os.environ["REMARKABLE_ENABLE_WRITE"] = "1"
            os.environ["REMARKABLE_READ_ONLY"] = "1"
            assert write_enabled() is False
        finally:
            if old_enable is not None:
                os.environ["REMARKABLE_ENABLE_WRITE"] = old_enable
            else:
                os.environ.pop("REMARKABLE_ENABLE_WRITE", None)
            os.environ.pop("REMARKABLE_READ_ONLY", None)
            if old_ro is not None:
                os.environ["REMARKABLE_READ_ONLY"] = old_ro

    def test_read_only_blocks_write_transport(self):
        """In read-only mode, _require_write_transport returns an educational error."""
        import json

        from remarkable_mcp.write_tools import _require_write_transport

        old = os.environ.get("REMARKABLE_READ_ONLY")
        try:
            os.environ["REMARKABLE_READ_ONLY"] = "1"
            err = _require_write_transport()
            assert err is not None
            payload = json.loads(err)
            assert payload["_error"]["type"] == "write_disabled"
            assert "--read-only" in payload["_error"]["suggestion"]
        finally:
            if old is not None:
                os.environ["REMARKABLE_READ_ONLY"] = old
            else:
                os.environ.pop("REMARKABLE_READ_ONLY", None)

    @pytest.mark.asyncio
    async def test_write_tools_registered_by_default(self):
        """Write tools ARE registered by default (write-on); read-only would skip them.

        Cloud mode exposes ``remarkable_author`` for create_document only; SSH
        exposes the full authoring surface. See
        ``test_author_registered_in_cloud_and_ssh_only`` for transport gating.
        """
        tools = await mcp.list_tools()
        tool_names = [tool.name for tool in tools]

        write_tool_names = [
            "remarkable_upload",
            "remarkable_mkdir",
            "remarkable_move",
            "remarkable_rename",
            "remarkable_author",
            "remarkable_delete",
        ]

        for tool_name in write_tool_names:
            assert tool_name in tool_names, (
                f"Write tool {tool_name} should be registered by default"
            )

    @pytest.mark.asyncio
    async def test_write_tools_registered_when_enabled(self):
        """Write tools register in SSH mode (default-on, re-registered explicitly here)."""
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
                "remarkable_author",
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
                patch.dict(
                    os.environ,
                    {"REMARKABLE_USE_SSH": "1", "REMARKABLE_SKIP_CONFIRM": "1"},
                ),
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
                "remarkable_author",
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
                "remarkable_author",
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
        env.pop("REMARKABLE_READ_ONLY", None)  # write is enabled by default
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

    @pytest.mark.asyncio
    async def test_author_registered_in_cloud_and_ssh_only(self):
        """remarkable_author is present in cloud/SSH, hidden in USB web.

        Cloud mode supports create_document only; SSH supports the full
        create_document/add_page/draw authoring surface.
        """
        from remarkable_mcp.write_tools import register_write_tools

        # SSH mode -> author present.
        with patch.dict(os.environ, {"REMARKABLE_USE_SSH": "1"}):
            register_write_tools()
            try:
                names = {t.name for t in await mcp.list_tools()}
                assert "remarkable_author" in names
            finally:
                mcp._tool_manager._tools.pop("remarkable_author", None)

        # Cloud mode -> author present for create_document.
        env = {k: v for k, v in os.environ.items() if k != "REMARKABLE_USE_SSH"}
        env.pop("REMARKABLE_USE_USB_WEB", None)
        with patch.dict(os.environ, env, clear=True):
            register_write_tools()
            try:
                names = {t.name for t in await mcp.list_tools()}
                assert "remarkable_author" in names
            finally:
                for name in [
                    "remarkable_author",
                    "remarkable_upload",
                    "remarkable_mkdir",
                    "remarkable_move",
                    "remarkable_rename",
                    "remarkable_delete",
                ]:
                    mcp._tool_manager._tools.pop(name, None)

        # USB web mode -> author hidden.
        env = {k: v for k, v in os.environ.items() if k != "REMARKABLE_USE_SSH"}
        env["REMARKABLE_USE_USB_WEB"] = "1"
        with patch.dict(os.environ, env, clear=True):
            register_write_tools()
            try:
                names = {t.name for t in await mcp.list_tools()}
                assert "remarkable_author" not in names
            finally:
                mcp._tool_manager._tools.pop("remarkable_author", None)


class TestCloudWriteDispatch:
    """Cloud-mode write tools dispatch to the RemarkableClient methods."""

    def _cloud_env(self):
        env = {k: v for k, v in os.environ.items() if k != "REMARKABLE_USE_SSH"}
        env.pop("REMARKABLE_USE_USB_WEB", None)
        env.pop("REMARKABLE_READ_ONLY", None)  # write is enabled by default
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
            "remarkable_author",
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
    async def test_cloud_author_create_document_dispatch(self):
        """Cloud remarkable_author supports create_document via the cloud client."""
        from remarkable_mcp.write_tools import register_write_tools

        with patch.dict(os.environ, self._cloud_env(), clear=True):
            register_write_tools()
            try:
                folder = self._make_item("folder-1", "Inbox", is_folder=True)
                mock_doc = Mock()
                mock_doc.id = "new-native-doc"
                client = Mock(spec=["get_meta_items", "create_notebook"])
                client.get_meta_items.return_value = [folder]
                client.create_notebook.return_value = mock_doc
                with patch("remarkable_mcp.write_tools.get_rmapi", return_value=client):
                    result = await mcp.call_tool(
                        "remarkable_author",
                        {
                            "method": "create_document",
                            "name": "Cloud Sketches",
                            "text": "Agenda\nFollow-ups",
                            "folder": "/Inbox",
                        },
                    )
                data = json.loads(result[0][0].text)
                assert data["created"] is True
                assert data["transport"] == "cloud"
                assert data["document_id"] == "new-native-doc"
                assert data["folder"] == "/Inbox"
                assert data["has_text"] is True
                client.create_notebook.assert_called_once_with(
                    "Cloud Sketches", "folder-1", "Agenda\nFollow-ups"
                )
            finally:
                self._cleanup()

    @pytest.mark.asyncio
    async def test_cloud_author_daily_notebook_creates_missing_notebook(self):
        """Cloud daily_notebook creates a dated native notebook when absent."""
        from remarkable_mcp.write_tools import register_write_tools

        with patch.dict(os.environ, self._cloud_env(), clear=True):
            register_write_tools()
            try:
                folder = self._make_item("folder-1", "Inbox", is_folder=True)
                mock_doc = Mock()
                mock_doc.id = "daily-doc-id"
                client = Mock(spec=["get_meta_items", "create_notebook"])
                client.get_meta_items.return_value = [folder]
                client.create_notebook.return_value = mock_doc
                with patch("remarkable_mcp.write_tools.get_rmapi", return_value=client):
                    result = await mcp.call_tool(
                        "remarkable_author",
                        {
                            "method": "daily_notebook",
                            "date": "2026-06-24",
                            "folder": "/Inbox",
                            "name_format": "Daily Notes {date}",
                            "text": "Starter notes",
                        },
                    )
                data = json.loads(result[0][0].text)
                assert data["created"] is True
                assert data["found"] is False
                assert data["document"] == "Daily Notes 2026-06-24"
                assert data["document_id"] == "daily-doc-id"
                assert data["path"] == "/Inbox/Daily Notes 2026-06-24"
                assert data["date"] == "2026-06-24"
                assert data["folder"] == "/Inbox"
                assert data["transport"] == "cloud"
                client.create_notebook.assert_called_once_with(
                    "Daily Notes 2026-06-24", "folder-1", "Starter notes"
                )
            finally:
                self._cleanup()

    @pytest.mark.asyncio
    async def test_cloud_author_daily_notebook_returns_existing_notebook(self):
        """Cloud daily_notebook is idempotent when the notebook already exists."""
        from remarkable_mcp.write_tools import register_write_tools

        with patch.dict(os.environ, self._cloud_env(), clear=True):
            register_write_tools()
            try:
                folder = self._make_item("folder-1", "Inbox", is_folder=True)
                existing = self._make_item(
                    "daily-doc-id", "Daily Notes 2026-06-24", parent="folder-1"
                )
                client = Mock(spec=["get_meta_items", "create_notebook"])
                client.get_meta_items.return_value = [folder, existing]
                with patch("remarkable_mcp.write_tools.get_rmapi", return_value=client):
                    result = await mcp.call_tool(
                        "remarkable_author",
                        {
                            "method": "daily_notebook",
                            "date": "2026-06-24",
                            "folder": "/Inbox",
                        },
                    )
                data = json.loads(result[0][0].text)
                assert data["created"] is False
                assert data["found"] is True
                assert data["document"] == "Daily Notes 2026-06-24"
                assert data["document_id"] == "daily-doc-id"
                assert data["path"] == "/Inbox/Daily Notes 2026-06-24"
                assert data["transport"] == "cloud"
                client.create_notebook.assert_not_called()
            finally:
                self._cleanup()

    @pytest.mark.asyncio
    async def test_cloud_author_daily_notebook_rejects_invalid_date(self):
        """daily_notebook requires an ISO YYYY-MM-DD date when provided."""
        from remarkable_mcp.write_tools import register_write_tools

        with patch.dict(os.environ, self._cloud_env(), clear=True):
            register_write_tools()
            try:
                client = Mock(spec=["get_meta_items", "create_notebook"])
                client.get_meta_items.return_value = []
                with patch("remarkable_mcp.write_tools.get_rmapi", return_value=client):
                    result = await mcp.call_tool(
                        "remarkable_author",
                        {"method": "daily_notebook", "date": "24-06-2026"},
                    )
                data = json.loads(result[0][0].text)
                assert data["_error"]["type"] == "invalid_date"
                client.create_notebook.assert_not_called()
            finally:
                self._cleanup()

    @pytest.mark.asyncio
    async def test_cloud_author_daily_notebook_rejects_missing_folder(self):
        """daily_notebook returns a structured error for missing folders."""
        from remarkable_mcp.write_tools import register_write_tools

        with patch.dict(os.environ, self._cloud_env(), clear=True):
            register_write_tools()
            try:
                folder = self._make_item("folder-1", "Inbox", is_folder=True)
                client = Mock(spec=["get_meta_items", "create_notebook"])
                client.get_meta_items.return_value = [folder]
                with patch("remarkable_mcp.write_tools.get_rmapi", return_value=client):
                    result = await mcp.call_tool(
                        "remarkable_author",
                        {
                            "method": "daily_notebook",
                            "date": "2026-06-24",
                            "folder": "/Missing",
                        },
                    )
                data = json.loads(result[0][0].text)
                assert data["_error"]["type"] == "folder_not_found"
                client.create_notebook.assert_not_called()
            finally:
                self._cleanup()

    @pytest.mark.asyncio
    async def test_cloud_author_rejects_add_page_and_draw(self):
        """Cloud authoring intentionally exposes create_document only."""
        from remarkable_mcp.write_tools import register_write_tools

        with patch.dict(os.environ, self._cloud_env(), clear=True):
            register_write_tools()
            try:
                for method in ("add_page", "draw"):
                    result = await mcp.call_tool(
                        "remarkable_author", {"method": method, "document": "Notebook"}
                    )
                    data = json.loads(result[0][0].text)
                    assert data["_error"]["type"] == "unsupported_in_cloud"
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

        env = {**self._cloud_env(), "REMARKABLE_SKIP_CONFIRM": "1"}
        with patch.dict(os.environ, env, clear=True):
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
    async def test_cloud_delete_refused_without_elicitation(self):
        """Default-on writes: a client that can't confirm must NOT delete silently."""
        from remarkable_mcp.write_tools import register_write_tools

        # No REMARKABLE_SKIP_CONFIRM, and the client does not support elicitation.
        env = {k: v for k, v in self._cloud_env().items() if k != "REMARKABLE_SKIP_CONFIRM"}
        with patch.dict(os.environ, env, clear=True):
            register_write_tools()
            try:
                target = self._make_item("doc-1", "Old Notes")
                client = Mock(spec=["get_meta_items", "delete"])
                client.get_meta_items.return_value = [target]
                with (
                    patch("remarkable_mcp.write_tools.get_rmapi", return_value=client),
                    patch(
                        "remarkable_mcp.write_tools.client_supports_elicitation",
                        return_value=False,
                    ),
                ):
                    result = await mcp.call_tool("remarkable_delete", {"document": "Old Notes"})
                data = json.loads(result[0][0].text)
                assert data["_error"]["type"] == "confirmation_unavailable"
                client.delete.assert_not_called()
            finally:
                self._cleanup()

    @pytest.mark.asyncio
    async def test_cloud_delete_skip_confirm_bypasses_elicitation(self):
        """REMARKABLE_SKIP_CONFIRM=1 allows deletes without a prompt (automation)."""
        from remarkable_mcp.write_tools import register_write_tools

        env = {**self._cloud_env(), "REMARKABLE_SKIP_CONFIRM": "1"}
        with patch.dict(os.environ, env, clear=True):
            register_write_tools()
            try:
                target = self._make_item("doc-1", "Old Notes")
                client = Mock(spec=["get_meta_items", "delete"])
                client.get_meta_items.return_value = [target]
                with (
                    patch("remarkable_mcp.write_tools.get_rmapi", return_value=client),
                    patch(
                        "remarkable_mcp.write_tools.client_supports_elicitation",
                        return_value=False,
                    ),
                ):
                    result = await mcp.call_tool("remarkable_delete", {"document": "Old Notes"})
                data = json.loads(result[0][0].text)
                assert data["deleted"] is True
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


class TestCloudNativeNotebookCreate:
    """Cloud client can create a native notebook document."""

    def test_create_notebook_uploads_native_notebook_blobs(self, monkeypatch):
        from remarkable_mcp.sync import Document, RemarkableClient

        client = RemarkableClient(user_token="user-token")
        uploaded = []

        def fake_upload(content, filename):
            uploaded.append((filename, content))
            return {
                "hash": f"hash-{len(uploaded)}",
                "type": "0",
                "id": filename,
                "subfiles": 0,
                "size": len(content),
            }

        monkeypatch.setattr(client, "_upload_file_blob", fake_upload)
        monkeypatch.setattr(
            client,
            "_upload_doc_index",
            lambda doc_id, files: {
                "hash": "doc-index-hash",
                "type": "0",
                "id": doc_id,
                "subfiles": len(files),
                "size": sum(int(f["size"]) for f in files),
            },
        )
        synced_entries = []

        def fake_sync_root(mutate):
            synced_entries.extend(mutate([]))

        monkeypatch.setattr(client, "_sync_root", fake_sync_root)
        ids = iter(
            [
                "00000000-0000-4000-8000-000000000001",
                "00000000-0000-4000-8000-000000000002",
                "00000000-0000-4000-8000-000000000003",
            ]
        )
        monkeypatch.setattr("remarkable_mcp.notebooks.new_uuid", ids.__next__)

        doc = client.create_notebook("Cloud Sketches", parent_id="folder-id", text="Agenda")

        assert isinstance(doc, Document)
        assert doc.id == "00000000-0000-4000-8000-000000000001"
        assert doc.name == "Cloud Sketches"
        assert doc.parent == "folder-id"
        filenames = [name for name, _content in uploaded]
        assert filenames == [
            "00000000-0000-4000-8000-000000000001/00000000-0000-4000-8000-000000000002.rm",
            "00000000-0000-4000-8000-000000000001.content",
            "00000000-0000-4000-8000-000000000001.metadata",
        ]
        content_data = json.loads(
            dict(uploaded)["00000000-0000-4000-8000-000000000001.content"].decode("utf-8")
        )
        assert content_data["fileType"] == "notebook"
        assert content_data["pageCount"] == 1
        assert content_data["cPages"]["pages"][0]["id"] == "00000000-0000-4000-8000-000000000002"
        metadata = json.loads(
            dict(uploaded)["00000000-0000-4000-8000-000000000001.metadata"].decode("utf-8")
        )
        assert metadata["visibleName"] == "Cloud Sketches"
        assert metadata["parent"] == "folder-id"
        assert metadata["type"] == "DocumentType"
        assert synced_entries == [
            {
                "hash": "doc-index-hash",
                "type": "0",
                "id": "00000000-0000-4000-8000-000000000001",
                "subfiles": 3,
                "size": sum(len(content) for _name, content in uploaded),
            }
        ]


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


class TestCloudStartupFallback:
    """USB/SSH selected but unreachable should fall back to cloud when a token exists."""

    @staticmethod
    def _device_client(reachable):
        client = Mock(name="device")
        client.check_connection = Mock(return_value=reachable)
        return client

    def _setup_cloud(self, monkeypatch, tmp_path, token):
        """Redirect HOME, set the cloud token, and stub the cloud client loader."""
        import remarkable_mcp.api as api

        monkeypatch.setattr(api.Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(api, "REMARKABLE_TOKEN", token)
        cloud = Mock(name="cloud")
        monkeypatch.setattr("remarkable_mcp.sync.load_client_from_token", lambda token_json: cloud)
        return api, cloud

    def test_usb_unreachable_falls_back_to_cloud(self, monkeypatch, tmp_path):
        api, cloud = self._setup_cloud(monkeypatch, tmp_path, '{"devicetoken": "d"}')
        monkeypatch.setattr(api, "REMARKABLE_USE_USB_WEB", True)
        monkeypatch.setattr(api, "REMARKABLE_USE_SSH", False)
        monkeypatch.setattr(api, "REMARKABLE_DISABLE_CLOUD_FALLBACK", False)

        device = self._device_client(reachable=False)
        creations = []

        def factory():
            creations.append(1)
            return device

        monkeypatch.setattr("remarkable_mcp.usb_web.create_usb_web_client", factory)

        api.reset_client_cache()
        try:
            result = api.get_rmapi()
            assert result is cloud
            assert api.get_active_transport() == "cloud"
            # Resolution is cached: the device is probed once, later calls return
            # the cloud client directly without re-probing.
            assert api.get_rmapi() is cloud
            assert len(creations) == 1
            device.check_connection.assert_called_once()
        finally:
            api.reset_client_cache()

    def test_ssh_unreachable_falls_back_to_cloud_via_file_token(self, monkeypatch, tmp_path):
        # Token available via ~/.rmapi file rather than the env var.
        api, cloud = self._setup_cloud(monkeypatch, tmp_path, None)
        (tmp_path / ".rmapi").write_text('{"devicetoken": "d"}')
        monkeypatch.setattr(api, "REMARKABLE_USE_SSH", True)
        monkeypatch.setattr(api, "REMARKABLE_USE_USB_WEB", False)
        monkeypatch.setattr(api, "REMARKABLE_DISABLE_CLOUD_FALLBACK", False)

        device = self._device_client(reachable=False)
        monkeypatch.setattr("remarkable_mcp.ssh.create_ssh_client", lambda: device)

        api.reset_client_cache()
        try:
            assert api.get_rmapi() is cloud
            assert api.get_active_transport() == "cloud"
        finally:
            api.reset_client_cache()

    def test_device_reachable_no_fallback(self, monkeypatch, tmp_path):
        api, cloud = self._setup_cloud(monkeypatch, tmp_path, '{"devicetoken": "d"}')
        monkeypatch.setattr(api, "REMARKABLE_USE_USB_WEB", True)
        monkeypatch.setattr(api, "REMARKABLE_USE_SSH", False)
        monkeypatch.setattr(api, "REMARKABLE_DISABLE_CLOUD_FALLBACK", False)

        device = self._device_client(reachable=True)
        monkeypatch.setattr("remarkable_mcp.usb_web.create_usb_web_client", lambda: device)

        api.reset_client_cache()
        try:
            assert api.get_rmapi() is device
            assert api.get_active_transport() == "usb-web"
        finally:
            api.reset_client_cache()

    def test_no_token_no_fallback_returns_device(self, monkeypatch, tmp_path):
        # No env token and an empty HOME => no cloud token => cannot fall back.
        api, cloud = self._setup_cloud(monkeypatch, tmp_path, None)
        monkeypatch.setattr(api, "REMARKABLE_USE_USB_WEB", True)
        monkeypatch.setattr(api, "REMARKABLE_USE_SSH", False)
        monkeypatch.setattr(api, "REMARKABLE_DISABLE_CLOUD_FALLBACK", False)

        device = self._device_client(reachable=False)
        monkeypatch.setattr("remarkable_mcp.usb_web.create_usb_web_client", lambda: device)

        api.reset_client_cache()
        try:
            # Returns the unreachable device client so its own errors surface;
            # the unreachable client is NOT cached, so a later call can succeed
            # once the device is connected.
            assert api.get_rmapi() is device
            assert api.get_active_transport() == "usb-web"
        finally:
            api.reset_client_cache()

    def test_fallback_disabled_returns_device(self, monkeypatch, tmp_path):
        api, cloud = self._setup_cloud(monkeypatch, tmp_path, '{"devicetoken": "d"}')
        monkeypatch.setattr(api, "REMARKABLE_USE_SSH", True)
        monkeypatch.setattr(api, "REMARKABLE_USE_USB_WEB", False)
        monkeypatch.setattr(api, "REMARKABLE_DISABLE_CLOUD_FALLBACK", True)

        device = self._device_client(reachable=False)
        monkeypatch.setattr("remarkable_mcp.ssh.create_ssh_client", lambda: device)

        api.reset_client_cache()
        try:
            assert api.get_rmapi() is device
            assert api.get_active_transport() == "ssh"
        finally:
            api.reset_client_cache()


class TestSSHKeyAuth:
    """SSH command construction for key-based / password / agent-free auth."""

    def _capture(self, monkeypatch, *, text):
        """Patch subprocess.run in ssh module and capture the argv it receives."""
        import remarkable_mcp.ssh as ssh_mod

        captured = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return Mock(returncode=0, stdout=("ok" if text else b"ok"), stderr="")

        monkeypatch.setattr(ssh_mod.subprocess, "run", fake_run)
        return captured

    def test_explicit_key_pins_identity_and_ignores_agent(self, monkeypatch):
        from remarkable_mcp.ssh import SSHClient

        captured = self._capture(monkeypatch, text=True)
        client = SSHClient(key_path="~/.ssh/id_ed25519")
        client._ssh_command("echo ok")

        argv = captured["args"]
        expected_key = os.path.expanduser("~/.ssh/id_ed25519")
        assert "sshpass" not in argv
        assert "-o" in argv and "BatchMode=yes" in argv
        assert "-i" in argv
        assert expected_key in argv
        assert "IdentitiesOnly=yes" in argv
        # Identity must be supplied before the destination/command.
        assert argv.index(expected_key) < argv.index("echo ok")

    def test_keyless_default_has_batchmode_but_no_identity(self, monkeypatch):
        from remarkable_mcp.ssh import SSHClient

        captured = self._capture(monkeypatch, text=True)
        client = SSHClient()
        client._ssh_command("echo ok")

        argv = captured["args"]
        assert "BatchMode=yes" in argv
        assert "-i" not in argv
        assert "IdentitiesOnly=yes" not in argv
        assert "sshpass" not in argv

    def test_password_uses_sshpass_without_batchmode(self, monkeypatch):
        from remarkable_mcp.ssh import SSHClient

        captured = self._capture(monkeypatch, text=True)
        client = SSHClient(password="secret")
        client._ssh_command("echo ok")

        argv = captured["args"]
        assert argv[:3] == ["sshpass", "-p", "secret"]
        # Password auth must not force BatchMode (it would block the password).
        assert "BatchMode=yes" not in argv
        assert "IdentitiesOnly=yes" not in argv

    def test_scp_download_also_pins_identity(self, monkeypatch):
        from remarkable_mcp.ssh import SSHClient

        captured = self._capture(monkeypatch, text=False)
        client = SSHClient(key_path="/keys/rm_ed25519")
        client._scp_download("/home/root/file.pdf")

        argv = captured["args"]
        assert "-i" in argv
        assert "/keys/rm_ed25519" in argv
        assert "IdentitiesOnly=yes" in argv
        assert "BatchMode=yes" in argv

    def test_create_ssh_client_reads_key_env(self, monkeypatch):
        from remarkable_mcp.ssh import create_ssh_client

        monkeypatch.setenv("REMARKABLE_SSH_KEY", "~/.ssh/rm_key")
        monkeypatch.delenv("REMARKABLE_SSH_PASSWORD", raising=False)
        client = create_ssh_client()
        assert client.key_path == os.path.expanduser("~/.ssh/rm_key")

    def test_key_path_defaults_to_none(self):
        from remarkable_mcp.ssh import SSHClient

        assert SSHClient().key_path is None


# =============================================================================
# MCP Apps interactive canvas
# =============================================================================


def _ctx_with_extensions(extensions):
    """Build a mock Context whose client advertises the given extensions."""
    from mcp.types import ClientCapabilities

    caps = ClientCapabilities.model_validate(
        {"extensions": extensions} if extensions is not None else {}
    )
    ctx = Mock()
    ctx.session = Mock()
    ctx.session.client_params = Mock()
    ctx.session.client_params.capabilities = caps
    return ctx


class TestClientSupportsApps:
    """Test MCP Apps UI capability negotiation."""

    def test_supports_when_app_mime_advertised(self):
        from remarkable_mcp.capabilities import APP_UI_EXTENSION_ID, client_supports_apps

        ctx = _ctx_with_extensions(
            {APP_UI_EXTENSION_ID: {"mimeTypes": ["text/html;profile=mcp-app"]}}
        )
        assert client_supports_apps(ctx) is True

    def test_supports_when_extension_has_no_mimetypes(self):
        from remarkable_mcp.capabilities import APP_UI_EXTENSION_ID, client_supports_apps

        ctx = _ctx_with_extensions({APP_UI_EXTENSION_ID: {}})
        assert client_supports_apps(ctx) is True

    def test_not_supported_without_extension(self):
        from remarkable_mcp.capabilities import client_supports_apps

        ctx = _ctx_with_extensions({})
        assert client_supports_apps(ctx) is False

    def test_not_supported_with_wrong_mimetype(self):
        from remarkable_mcp.capabilities import APP_UI_EXTENSION_ID, client_supports_apps

        ctx = _ctx_with_extensions({APP_UI_EXTENSION_ID: {"mimeTypes": ["application/json"]}})
        assert client_supports_apps(ctx) is False

    def test_not_supported_when_no_capabilities(self):
        from remarkable_mcp.capabilities import client_supports_apps

        ctx = Mock()
        ctx.session = None
        assert client_supports_apps(ctx) is False


class TestCanvasResource:
    """Test the canvas app HTML resource."""

    def test_canvas_html_is_self_contained_bridge(self):
        from remarkable_mcp.app_canvas import _CANVAS_HTML

        # Self-contained HTML with the MCP Apps postMessage bridge wiring.
        assert "<!doctype html>" in _CANVAS_HTML.lower()
        assert "ui/initialize" in _CANVAS_HTML
        assert "ui/notifications/tool-result" in _CANVAS_HTML
        assert "tools/call" in _CANVAS_HTML
        assert "remarkable_canvas" in _CANVAS_HTML
        assert "png_data_uri" in _CANVAS_HTML

    def test_canvas_has_add_page_flow(self):
        from remarkable_mcp.app_canvas import _CANVAS_HTML

        # The +Page control queues a local blank page and Save materializes it
        # via remarkable_author(method="add_page") before drawing cached strokes.
        assert 'id="addpage"' in _CANVAS_HTML
        assert "pendingPages" in _CANVAS_HTML
        assert '"add_page"' in _CANVAS_HTML
        assert "renderPending" in _CANVAS_HTML
        # +Page is gated to native notebooks (PDFs/EPUBs have fixed pages).
        assert 'state.fileType === "notebook"' in _CANVAS_HTML

    def test_canvas_footer_distinguishes_transport_from_read_only(self):
        from remarkable_mcp.app_canvas import _CANVAS_HTML

        # When write is enabled but the transport isn't SSH, the footer mirrors
        # the draw tool's SSH-only error instead of the generic read-only string.
        assert "writeMode" in _CANVAS_HTML
        assert 'state.transport !== "ssh"' in _CANVAS_HTML
        assert "connect via SSH" in _CANVAS_HTML
        assert "Read-only viewer" in _CANVAS_HTML

    def test_canvas_bridge_is_spec_compliant(self):
        from remarkable_mcp.app_canvas import _CANVAS_HTML

        # ui/initialize must carry a protocol version + client info per spec.
        assert "protocolVersion" in _CANVAS_HTML
        assert "2026-01-26" in _CANVAS_HTML
        assert "clientInfo" in _CANVAS_HTML
        # Input/result asymmetry: tool-input params are wrapped as {arguments},
        # tool-result params are a bare CallToolResult.
        assert "ui/notifications/tool-input" in _CANVAS_HTML
        assert ".arguments" in _CANVAS_HTML

    def test_canvas_resource_uses_app_mime(self):
        from remarkable_mcp.app_canvas import CANVAS_RESOURCE_URI
        from remarkable_mcp.capabilities import APP_UI_MIME

        assert CANVAS_RESOURCE_URI == "ui://remarkable/canvas"
        assert APP_UI_MIME == "text/html;profile=mcp-app"


class TestFullPageRender:
    """Full-page (uncropped) render used by the interactive canvas.

    The canvas renders the WHOLE page (viewBox from the page's own
    SceneInfo.paper_size) and addresses pages by cPages index, so the drawing
    overlay's normalized coordinates map onto the same stroke space the write
    tool uses, and blank pages still render.
    """

    def test_svg_full_page_viewbox_is_centered(self):
        from remarkable_mcp.extract import _svg_full_page

        svg = _svg_full_page([], 820.0, 1458.0)
        # Center-origin X, top-origin Y: viewBox "-W/2 0 W H".
        assert 'viewBox="-410 0 820 1458"' in svg
        assert "<svg" in svg

    def test_full_page_png_returns_paper_size_and_matching_aspect(self):
        import io as _io

        from PIL import Image

        from remarkable_mcp.extract import render_rm_file_full_page_png

        try:
            data = _make_v6_rm_bytes()
        except Exception as exc:  # pragma: no cover - guards rmscene API drift
            pytest.skip(f"could not synthesize a v6 fixture: {exc}")

        with tempfile.NamedTemporaryFile(suffix=".rm", delete=False) as rm_tmp:
            rm_tmp.write(data)
            rm_path = Path(rm_tmp.name)
        try:
            result = render_rm_file_full_page_png(rm_path, background_color="#FFFFFF")
            assert result is not None
            png, (w, h) = result
            # The fixture has no SceneInfo -> fall back to the standard page extent.
            assert (round(w), round(h)) == (1404, 1872)
            im = Image.open(_io.BytesIO(png))
            # PNG aspect must match the page aspect so [0,1] maps linearly.
            assert abs(im.size[0] / im.size[1] - w / h) < 0.01
        finally:
            rm_path.unlink(missing_ok=True)

    def test_full_page_render_is_cpages_indexed_and_renders_blank_pages(self):
        import io as _io
        import json as _json
        import zipfile as _zip

        from PIL import Image

        from remarkable_mcp.extract import render_page_full_page_from_document_zip

        try:
            rm_bytes = _make_v6_rm_bytes()
        except Exception as exc:  # pragma: no cover - guards rmscene API drift
            pytest.skip(f"could not synthesize a v6 fixture: {exc}")

        # Two pages in cPages, but only page 1 ("p1") has a .rm layer on disk.
        content = {"cPages": {"pages": [{"id": "p1"}, {"id": "p2"}]}}
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as ztmp:
            zpath = Path(ztmp.name)
        try:
            with _zip.ZipFile(zpath, "w") as zf:
                zf.writestr("doc.content", _json.dumps(content))
                zf.writestr("p1.rm", rm_bytes)  # p2.rm intentionally absent

            # Page 1 has a drawable layer -> rendered from its .rm.
            r1 = render_page_full_page_from_document_zip(zpath, 1, background_color="#FFFFFF")
            assert r1 is not None
            png1, size1 = r1
            assert Image.open(_io.BytesIO(png1)).size[0] > 0

            # Page 2 exists in cPages but has no .rm -> a BLANK full page is
            # rendered (not None). If pages were addressed by filesystem .rm
            # order this would be out of range (only one .rm file exists).
            r2 = render_page_full_page_from_document_zip(zpath, 2, background_color="#FFFFFF")
            assert r2 is not None
            _png2, size2 = r2
            assert size2 == size1  # blank page sized to the document's paper size

            # Page 3 is not in cPages -> out of range.
            assert render_page_full_page_from_document_zip(zpath, 3) is None
        finally:
            zpath.unlink(missing_ok=True)

    def test_typed_text_emits_svg_text_elements(self):
        from remarkable_mcp import notebooks as nb
        from remarkable_mcp.extract import _v6_blocks, _v6_text_svg_elements

        with tempfile.NamedTemporaryFile(suffix=".rm", delete=False) as rm_tmp:
            rm_tmp.write(nb.page_rm_bytes("Hello world\nSecond line"))
            rm_path = Path(rm_tmp.name)
        try:
            elements = _v6_text_svg_elements(_v6_blocks(rm_path))
            assert len(elements) == 2
            joined = "".join(elements)
            assert "<text " in joined
            assert "Hello world" in joined
            assert "Second line" in joined
        finally:
            rm_path.unlink(missing_ok=True)

    def test_blank_page_has_no_typed_text(self):
        from remarkable_mcp import notebooks as nb
        from remarkable_mcp.extract import _v6_blocks, _v6_text_svg_elements

        with tempfile.NamedTemporaryFile(suffix=".rm", delete=False) as rm_tmp:
            rm_tmp.write(nb.blank_page_rm_bytes())
            rm_path = Path(rm_tmp.name)
        try:
            # An empty RootTextBlock (carried by synthesized blank pages) yields
            # no <text> nodes, so the page renders blank.
            assert _v6_text_svg_elements(_v6_blocks(rm_path)) == []
        finally:
            rm_path.unlink(missing_ok=True)

    def test_typed_text_rasterizes_to_visible_pixels(self):
        import io as _io

        from PIL import Image

        from remarkable_mcp import notebooks as nb
        from remarkable_mcp.extract import render_rm_file_full_page_png

        with tempfile.NamedTemporaryFile(suffix=".rm", delete=False) as rm_tmp:
            rm_tmp.write(nb.page_rm_bytes("Hello world"))
            rm_path = Path(rm_tmp.name)
        try:
            result = render_rm_file_full_page_png(rm_path, background_color="#FFFFFF")
            assert result is not None
            png, _ = result
            im = Image.open(_io.BytesIO(png)).convert("L")
            # Typed text must rasterize to dark pixels (not a blank white page).
            assert sum(im.histogram()[:128]) > 0
        finally:
            rm_path.unlink(missing_ok=True)

    def test_typed_text_in_cropped_read_only_render(self):
        """remarkable_image's content-cropped render also draws typed text."""
        from remarkable_mcp import notebooks as nb
        from remarkable_mcp.extract import _render_rm_v6_to_svg

        with tempfile.NamedTemporaryFile(suffix=".rm", delete=False) as rm_tmp:
            # A text-only page has no strokes; the cropped render must still
            # produce output (it previously returned None with no ink).
            rm_tmp.write(nb.page_rm_bytes("Hello world\nSecond line"))
            rm_path = Path(rm_tmp.name)
        try:
            svg = _render_rm_v6_to_svg(rm_path)
            assert svg is not None
            assert "<text " in svg
            assert "Hello world" in svg
        finally:
            rm_path.unlink(missing_ok=True)

    def test_typed_text_png_read_only_path_has_dark_pixels(self):
        import io as _io

        from PIL import Image

        from remarkable_mcp import notebooks as nb
        from remarkable_mcp.extract import render_rm_file_to_png

        with tempfile.NamedTemporaryFile(suffix=".rm", delete=False) as rm_tmp:
            rm_tmp.write(nb.page_rm_bytes("Hello world"))
            rm_path = Path(rm_tmp.name)
        try:
            png = render_rm_file_to_png(rm_path, background_color="#FFFFFF")
            assert png is not None
            im = Image.open(_io.BytesIO(png)).convert("L")
            assert sum(im.histogram()[:128]) > 0
        finally:
            rm_path.unlink(missing_ok=True)

    def test_wrap_text_helper(self):
        from remarkable_mcp.extract import _wrap_text

        # No wrapping when the text fits or no width/advance is known.
        assert _wrap_text("short line", 936, 15) == ["short line"]
        assert _wrap_text("anything at all", 0, 15) == ["anything at all"]
        # A long line wraps into multiple pieces, each within the width budget.
        long = " ".join(["word"] * 60)  # 60 words -> well past a 936-unit box
        lines = _wrap_text(long, 936, 15)
        assert len(lines) > 1
        for line in lines:
            assert len(line) * 15 <= 936
        # Round-trips the words (wrapping only changes whitespace).
        assert " ".join(lines).split() == long.split()
        # A single over-long word is kept on its own line rather than split.
        assert _wrap_text("supercalifragilistic", 50, 15) == ["supercalifragilistic"]

    def test_long_paragraph_wraps_into_multiple_lines(self):
        from remarkable_mcp import notebooks as nb
        from remarkable_mcp.extract import _v6_blocks, _v6_text_svg_elements

        long_para = (
            "The smallest frog is under 8mm long; the largest, the Goliath "
            "frog, can reach 32cm and is genuinely enormous for an amphibian."
        )
        with tempfile.NamedTemporaryFile(suffix=".rm", delete=False) as rm_tmp:
            rm_tmp.write(nb.page_rm_bytes(long_para))
            rm_path = Path(rm_tmp.name)
        try:
            elements = _v6_text_svg_elements(_v6_blocks(rm_path))
            # One paragraph wider than the text box must emit more than one line.
            assert len(elements) > 1
        finally:
            rm_path.unlink(missing_ok=True)

    def test_typed_text_baseline_matches_device_offset(self):
        """First line sits where the device draws it (calibrated, not the old
        rmc offset that rendered text ~50 units too high)."""
        import re

        from remarkable_mcp import notebooks as nb
        from remarkable_mcp.extract import _v6_blocks, _v6_text_svg_elements

        with tempfile.NamedTemporaryFile(suffix=".rm", delete=False) as rm_tmp:
            rm_tmp.write(nb.page_rm_bytes("Frog Facts"))
            rm_path = Path(rm_tmp.name)
        try:
            elements = _v6_text_svg_elements(_v6_blocks(rm_path))
            y = float(re.search(r'y="([-\d.]+)"', elements[0]).group(1))
            # pos_y(234) + TOP(-39) + line_height(70) == 265 for a 1404x1872 page;
            # comfortably below the old value of 216.
            assert 255 <= y <= 275
        finally:
            rm_path.unlink(missing_ok=True)

    def test_typed_text_metrics_scale_with_page_height(self):
        """Layout metrics scale linearly with the page's normalized height so
        typed text stays proportional on non-1872 geometries (e.g. reMarkable
        Move/classic). run_smoke only asserts render PASS/FAIL, not pixel
        layout, so this is the guard against a scaling regression."""
        import re
        from unittest.mock import patch

        from remarkable_mcp import extract
        from remarkable_mcp import notebooks as nb
        from remarkable_mcp.extract import _v6_blocks, _v6_text_svg_elements

        with tempfile.NamedTemporaryFile(suffix=".rm", delete=False) as rm_tmp:
            # Two paragraphs so the line-to-line gap isolates the scaled
            # line_height independent of the (unscaled) text-box position.
            rm_tmp.write(nb.page_rm_bytes("First line\nSecond line"))
            rm_path = Path(rm_tmp.name)
        try:
            blocks = _v6_blocks(rm_path)
            width, _ = extract._v6_paper_size(blocks)

            def metrics_at(page_h):
                with patch.object(extract, "_v6_paper_size", return_value=(width, page_h)):
                    els = _v6_text_svg_elements(blocks)
                y0 = float(re.search(r'y="([-\d.]+)"', els[0]).group(1))
                y1 = float(re.search(r'y="([-\d.]+)"', els[1]).group(1))
                size = float(re.search(r'font-size="([-\d.]+)"', els[0]).group(1))
                return y1 - y0, size

            # Reference 1872-tall page: full-size metrics.
            gap_ref, size_ref = metrics_at(extract._TEXT_REF_PAGE_HEIGHT)
            assert gap_ref == pytest.approx(extract._TEXT_DEFAULT_LINE_HEIGHT, abs=0.5)
            assert size_ref == pytest.approx(extract._TEXT_DEFAULT_FONT_SIZE, abs=0.5)

            # Half / double height -> metrics scale linearly.
            gap_half, size_half = metrics_at(extract._TEXT_REF_PAGE_HEIGHT / 2)
            assert gap_half == pytest.approx(gap_ref / 2, abs=0.5)
            assert size_half == pytest.approx(size_ref / 2, abs=0.5)

            gap_dbl, size_dbl = metrics_at(extract._TEXT_REF_PAGE_HEIGHT * 2)
            assert gap_dbl == pytest.approx(gap_ref * 2, abs=0.5)
            assert size_dbl == pytest.approx(size_ref * 2, abs=0.5)
        finally:
            rm_path.unlink(missing_ok=True)


class TestRenderCanvasPage:
    """Test the read-only canvas page renderer."""

    def _png_bytes(self):
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (12, 16), "white").save(buf, "PNG")
        return buf.getvalue()

    def _patch_common(
        self, monkeypatch, *, page_count=3, png=None, doc_name="Notes", file_type="notebook"
    ):
        import remarkable_mcp.api as api
        import remarkable_mcp.extract as extract
        import remarkable_mcp.tools as tools

        doc = Mock(VissibleName=doc_name, is_folder=False)
        client = Mock()
        client.get_meta_items.return_value = [doc]
        client.download.return_value = b"PK\x03\x04zip"

        monkeypatch.setattr(api, "get_rmapi", lambda: client)
        monkeypatch.setattr(api, "get_items_by_id", lambda c: {})
        monkeypatch.setattr(api, "get_item_path", lambda d, i: "/" + d.VissibleName)
        monkeypatch.setattr(api, "get_active_transport", lambda: "cloud")
        monkeypatch.setattr(api, "download_raw_file", lambda c, d, ext: None)
        monkeypatch.setattr(extract, "get_background_color", lambda: "#FBFBFB")
        monkeypatch.setattr(extract, "get_document_page_count", lambda p: page_count)
        monkeypatch.setattr(extract, "get_document_file_type", lambda p: file_type)
        monkeypatch.setattr(
            extract,
            "render_page_full_page_from_document_zip",
            lambda p, pg, **k: ((png, (820.0, 1458.0)) if png is not None else None),
        )
        monkeypatch.setattr(extract, "find_similar_documents", lambda q, docs: [])
        monkeypatch.setattr(tools, "_get_root_path", lambda: "/")
        monkeypatch.setattr(tools, "_is_within_root", lambda path, root: True)
        monkeypatch.setattr(tools, "_resolve_root_path", lambda p: p)
        monkeypatch.setattr(tools, "_apply_root_filter", lambda p: p)
        return doc

    @pytest.mark.asyncio
    async def test_render_returns_structured_content(self, monkeypatch):
        from mcp import types

        from remarkable_mcp.app_canvas import _render_canvas_page

        self._patch_common(monkeypatch, page_count=3, png=self._png_bytes())
        result = await _render_canvas_page("Notes", 2, None)

        assert isinstance(result, types.CallToolResult)
        sc = result.structuredContent
        assert sc["page"] == 2
        assert sc["total_pages"] == 3
        assert sc["document_name"] == "Notes"
        assert sc["png_data_uri"].startswith("data:image/png;base64,")
        assert sc["writable"] is False
        assert sc["page_width_px"] == 12
        assert sc["page_height_px"] == 16
        assert sc["paper_size"] == [820, 1458]
        assert sc["file_type"] == "notebook"
        assert sc["transport"] == "cloud"
        assert sc["write_mode"] is True
        # Embedded PNG is included for non-app clients.
        assert any(isinstance(c, types.EmbeddedResource) for c in result.content)

    @pytest.mark.asyncio
    async def test_render_document_not_found(self, monkeypatch):
        from remarkable_mcp.app_canvas import _render_canvas_page

        self._patch_common(monkeypatch, page_count=3, png=self._png_bytes())
        result = await _render_canvas_page("Nonexistent", 1, None)

        assert isinstance(result, str)
        data = json.loads(result)
        assert data["_error"]["type"] == "document_not_found"

    @pytest.mark.asyncio
    async def test_render_page_out_of_range(self, monkeypatch):
        from remarkable_mcp.app_canvas import _render_canvas_page

        self._patch_common(monkeypatch, page_count=2, png=self._png_bytes())
        result = await _render_canvas_page("Notes", 99, None)

        assert isinstance(result, str)
        data = json.loads(result)
        assert data["_error"]["type"] == "page_out_of_range"

    @pytest.mark.asyncio
    async def test_render_failed_when_no_png(self, monkeypatch):
        from remarkable_mcp.app_canvas import _render_canvas_page

        # render returns None and there's no PDF fallback -> render_failed
        self._patch_common(monkeypatch, page_count=2, png=None)
        result = await _render_canvas_page("Notes", 1, None)

        assert isinstance(result, str)
        data = json.loads(result)
        assert data["_error"]["type"] == "render_failed"

    @pytest.mark.asyncio
    async def test_render_wraps_transport_errors(self, monkeypatch):
        import remarkable_mcp.api as api
        from remarkable_mcp.app_canvas import _render_canvas_page

        def _boom():
            raise RuntimeError("network down")

        monkeypatch.setattr(api, "get_rmapi", _boom)
        result = await _render_canvas_page("Notes", 1, None)

        assert isinstance(result, str)
        data = json.loads(result)
        assert data["_error"]["type"] == "canvas_failed"


class TestRegisterAppTools:
    """Test that registering the app adds the canvas tool + resource."""

    @pytest.mark.asyncio
    async def test_register_app_tools_adds_canvas(self, monkeypatch):
        from mcp.server.fastmcp import FastMCP

        import remarkable_mcp.app_canvas as app_canvas
        import remarkable_mcp.server as server_mod

        # register_app_tools imports mcp from server at call time, so redirect
        # that global to a throwaway server to avoid mutating the real one.
        local = FastMCP("test-app")
        monkeypatch.setattr(server_mod, "mcp", local)

        app_canvas.register_app_tools()
        tools = await local.list_tools()
        names = [t.name for t in tools]
        assert "remarkable_canvas" in names
        canvas = next(t for t in tools if t.name == "remarkable_canvas")
        assert canvas.meta["ui"]["resourceUri"] == "ui://remarkable/canvas"
        # No output schema so we can return either CallToolResult or an error string.
        assert canvas.outputSchema is None
        resources = await local.list_resources()
        assert any(str(r.uri) == "ui://remarkable/canvas" for r in resources)

    def test_app_canvas_importable_standalone(self):
        """Importing app_canvas before server must not hit a circular import."""
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-c", "import remarkable_mcp.app_canvas"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr


class TestCanvasRegisteredByDefault:
    """The canvas app is always registered (no feature flag) and degrades gracefully."""

    @pytest.mark.asyncio
    async def test_canvas_tool_present_on_default_server(self):
        tools = await mcp.list_tools()
        names = [t.name for t in tools]
        assert "remarkable_canvas" in names

    @pytest.mark.asyncio
    async def test_canvas_resource_present_on_default_server(self):
        resources = await mcp.list_resources()
        assert any(str(r.uri) == "ui://remarkable/canvas" for r in resources)

    @pytest.mark.asyncio
    @patch("remarkable_mcp.tools.get_rmapi")
    async def test_status_no_longer_reports_app_enabled(self, mock_get_rmapi):
        # The app has no on/off gate anymore, so status should not carry the key.
        mock_client = Mock()
        mock_get_rmapi.return_value = mock_client
        mock_client.get_meta_items.return_value = []

        result = await mcp.call_tool("remarkable_status", {})
        data = json.loads(result[0][0].text)
        assert "app_enabled" not in data


# =============================================================================
# Test CLI flag wiring (--write / --read-only)
# =============================================================================


class TestCLIFlags:
    """CLI flag wiring for the write/read-only gate."""

    def test_write_and_read_only_mutually_exclusive(self):
        """Passing both --write and --read-only is an argparse error (exit 2)."""
        from remarkable_mcp.cli import main

        with patch.object(sys, "argv", ["remarkable-mcp", "--write", "--read-only"]):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 2

    def test_read_only_flag_sets_env(self):
        """--read-only sets REMARKABLE_READ_ONLY=1 before starting the server."""
        from remarkable_mcp import cli

        old = os.environ.pop("REMARKABLE_READ_ONLY", None)
        try:
            with (
                patch.object(sys, "argv", ["remarkable-mcp", "--read-only"]),
                patch("remarkable_mcp.server.run") as mock_run,
            ):
                cli.main()
            mock_run.assert_called_once()
            assert os.environ.get("REMARKABLE_READ_ONLY") == "1"
        finally:
            os.environ.pop("REMARKABLE_READ_ONLY", None)
            if old is not None:
                os.environ["REMARKABLE_READ_ONLY"] = old

    def test_write_flag_is_accepted_noop(self):
        """--write is accepted but does not enable read-only (write stays on)."""
        from remarkable_mcp import cli

        old = os.environ.pop("REMARKABLE_READ_ONLY", None)
        try:
            with (
                patch.object(sys, "argv", ["remarkable-mcp", "--write"]),
                patch("remarkable_mcp.server.run") as mock_run,
            ):
                cli.main()
            mock_run.assert_called_once()
            assert "REMARKABLE_READ_ONLY" not in os.environ
        finally:
            if old is not None:
                os.environ["REMARKABLE_READ_ONLY"] = old


class TestCanvasWrite:
    """Test the remarkable_author tool (method="draw" stroke write-back, SSH-first)."""

    @pytest.fixture(autouse=True)
    def _register_author(self):
        """Expose remarkable_author for these tests.

        The tool is SSH-only and therefore hidden in the default (cloud) import
        mode, so register it explicitly under SSH, then remove it afterward to
        restore the default surface. Individual tests still patch
        REMARKABLE_USE_SSH at call time so the impl runs in SSH mode.
        """
        from remarkable_mcp.write_tools import register_write_tools

        with patch.dict(os.environ, {"REMARKABLE_USE_SSH": "1"}):
            register_write_tools()
        yield
        mcp._tool_manager._tools.pop("remarkable_author", None)

    def _mock_doc(self):
        doc = Mock()
        doc.VissibleName = "Sketchbook"
        doc.ID = "doc-abc"
        doc.Parent = ""
        doc.is_folder = False
        doc.is_cloud_archived = False
        return doc

    @pytest.mark.asyncio
    async def test_empty_strokes_rejected(self):
        with patch.dict(os.environ, {"REMARKABLE_USE_SSH": "1"}):
            result = await mcp.call_tool(
                "remarkable_author",
                {"method": "draw", "document": "Sketchbook", "page": 1, "strokes": []},
            )
            data = json.loads(result[0][0].text)
            assert data["_error"]["type"] == "no_strokes"

    @pytest.mark.asyncio
    async def test_unknown_method_is_educational(self):
        with patch.dict(os.environ, {"REMARKABLE_USE_SSH": "1"}):
            result = await mcp.call_tool("remarkable_author", {"method": "frobnicate"})
            data = json.loads(result[0][0].text)
            assert data["_error"]["type"] == "unknown_method"
            assert "draw" in data["_error"]["did_you_mean"]

    @pytest.mark.asyncio
    async def test_missing_layer_autocreates_overlay(self):
        """A page with no .rm overlay gets a blank drawable layer created automatically."""
        import remarkable_mcp.strokes as strokes_mod
        import remarkable_mcp.write_tools as wt

        doc = self._mock_doc()
        content = json.dumps({"cPages": {"pages": [{"id": "page-1"}]}, "fileType": "pdf"})

        def fake_scp(path):
            if path.endswith(".content"):
                return content.encode()
            return None  # page .rm absent

        client = Mock(spec=["get_meta_items", "_scp_download", "_ssh_command"])
        client.get_meta_items.return_value = [doc]
        client._scp_download.side_effect = fake_scp

        writes = {}

        with (
            patch.dict(os.environ, {"REMARKABLE_USE_SSH": "1"}),
            patch.object(wt, "get_rmapi", lambda: client),
            patch.object(wt, "_write_remote_bytes", lambda ssh, p, d: writes.__setitem__(p, d)),
            patch.object(wt, "_restart_xochitl") as mock_restart,
            patch.object(
                strokes_mod,
                "page_geometry",
                lambda b: {
                    "paper_width": 1404,
                    "paper_height": 1872,
                    "has_scene_info": False,
                    "has_layer": True,
                },
            ),
            patch.object(
                strokes_mod, "append_strokes", lambda original, strokes: original + b"NEW"
            ),
        ):
            result = await mcp.call_tool(
                "remarkable_author",
                {
                    "method": "draw",
                    "document": "Sketchbook",
                    "page": 1,
                    "strokes": [{"points": [[0.1, 0.2], [0.8, 0.2]], "tool": "fineliner"}],
                },
            )
            data = json.loads(result[0][0].text)
            assert data["written"] is True
            assert data["created_overlay"] is True
            rm = f"{wt.XOCHITL_PATH}/doc-abc/page-1.rm"
            # The overlay was written; nothing pre-existed, so no .bak.
            assert rm in writes
            assert (rm + ".bak") not in writes
            mock_restart.assert_called_once()

    @pytest.mark.asyncio
    async def test_happy_path_appends_and_backs_up(self):
        """Strokes are appended, the pristine original is backed up once, xochitl restarts."""
        import remarkable_mcp.strokes as strokes_mod
        import remarkable_mcp.write_tools as wt

        doc = self._mock_doc()
        content = json.dumps(
            {"cPages": {"pages": [{"id": "page-1"}, {"id": "page-2"}]}, "fileType": "notebook"}
        )

        def fake_scp(path):
            if path.endswith(".content"):
                return content.encode()
            if path.endswith("/page-1.rm"):
                return b"ORIGINAL_RM_BYTES"
            return None

        client = Mock(spec=["get_meta_items", "_scp_download", "_ssh_command"])
        client.get_meta_items.return_value = [doc]
        client._scp_download.side_effect = fake_scp
        client._ssh_command.return_value = "no"  # .bak does not exist yet

        writes = {}

        def fake_write(ssh, remote_path, data):
            writes[remote_path] = data

        with (
            patch.dict(os.environ, {"REMARKABLE_USE_SSH": "1"}),
            patch.object(wt, "get_rmapi", lambda: client),
            patch.object(wt, "_write_remote_bytes", fake_write),
            patch.object(wt, "_restart_xochitl") as mock_restart,
            patch.object(
                strokes_mod,
                "page_geometry",
                lambda b: {
                    "paper_width": 1404,
                    "paper_height": 1872,
                    "has_scene_info": True,
                    "has_layer": True,
                },
            ),
            patch.object(
                strokes_mod, "append_strokes", lambda original, strokes: original + b"NEW"
            ),
        ):
            result = await mcp.call_tool(
                "remarkable_author",
                {
                    "method": "draw",
                    "document": "Sketchbook",
                    "page": 1,
                    "strokes": [
                        {
                            "points": [[0.1, 0.2], [0.8, 0.2]],
                            "tool": "highlighter",
                            "color": "yellow",
                        }
                    ],
                    "ui_submitted": True,
                },
            )
            data = json.loads(result[0][0].text)
            assert data["written"] is True
            assert data["page"] == 1
            assert data["total_pages"] == 2
            assert data["strokes_added"] == 1
            assert data["paper_size"] == [1404, 1872]
            assert data["ui_submitted"] is True
            assert data["created_overlay"] is False
            mock_restart.assert_called_once()
            # Pristine original backed up, then the appended bytes written.
            bak = f"{wt.XOCHITL_PATH}/doc-abc/page-1.rm.bak"
            rm = f"{wt.XOCHITL_PATH}/doc-abc/page-1.rm"
            assert writes[bak] == b"ORIGINAL_RM_BYTES"
            assert writes[rm] == b"ORIGINAL_RM_BYTESNEW"

    @pytest.mark.asyncio
    async def test_epub_warns_about_reflow(self):
        """Annotating a reflowable EPUB surfaces a drift caveat in the response."""
        import remarkable_mcp.strokes as strokes_mod
        import remarkable_mcp.write_tools as wt

        doc = self._mock_doc()
        content = json.dumps({"cPages": {"pages": [{"id": "page-1"}]}, "fileType": "epub"})

        def fake_scp(path):
            if path.endswith(".content"):
                return content.encode()
            if path.endswith("/page-1.rm"):
                return b"ORIGINAL_RM_BYTES"
            return None

        client = Mock(spec=["get_meta_items", "_scp_download", "_ssh_command"])
        client.get_meta_items.return_value = [doc]
        client._scp_download.side_effect = fake_scp
        client._ssh_command.return_value = "yes"  # .bak already exists

        with (
            patch.dict(os.environ, {"REMARKABLE_USE_SSH": "1"}),
            patch.object(wt, "get_rmapi", lambda: client),
            patch.object(wt, "_write_remote_bytes", lambda *a: None),
            patch.object(wt, "_restart_xochitl"),
            patch.object(
                strokes_mod,
                "page_geometry",
                lambda b: {
                    "paper_width": 1404,
                    "paper_height": 1872,
                    "has_scene_info": True,
                    "has_layer": True,
                },
            ),
            patch.object(
                strokes_mod, "append_strokes", lambda original, strokes: original + b"NEW"
            ),
        ):
            result = await mcp.call_tool(
                "remarkable_author",
                {
                    "method": "draw",
                    "document": "Sketchbook",
                    "page": 1,
                    "strokes": [{"points": [[0.1, 0.2], [0.8, 0.2]], "tool": "highlighter"}],
                },
            )
            data = json.loads(result[0][0].text)
            assert data["written"] is True
            assert "caveat" in data and "EPUB" in data["caveat"]

    @pytest.mark.asyncio
    async def test_add_page_appends_blank_page(self):
        """method="add_page" uploads a blank .rm and grows the notebook's .content."""
        import remarkable_mcp.write_tools as wt

        doc = self._mock_doc()
        content = json.dumps(
            {
                "cPages": {
                    "pages": [{"id": "p1", "idx": {"timestamp": "1:2", "value": "ba"}}],
                    "uuids": [{"first": "4ceffb02-db05-55dc-bf8b-c2aeb505a8e6", "second": 1}],
                },
                "fileType": "notebook",
                "pageCount": 1,
            }
        )

        def fake_scp(path):
            if path.endswith(".content"):
                return content.encode()
            return None

        client = Mock(spec=["get_meta_items", "_scp_download", "_ssh_command"])
        client.get_meta_items.return_value = [doc]
        client._scp_download.side_effect = fake_scp

        writes = {}
        contents = {}

        with (
            patch.dict(os.environ, {"REMARKABLE_USE_SSH": "1"}),
            patch.object(wt, "get_rmapi", lambda: client),
            patch.object(wt, "_write_remote_bytes", lambda ssh, p, d: writes.__setitem__(p, d)),
            patch.object(wt, "_write_content_file", lambda ssh, u, d: contents.__setitem__(u, d)),
            patch.object(wt, "_restart_xochitl") as mock_restart,
            patch.object(wt, "_invalidate_client_cache", lambda c: None),
        ):
            result = await mcp.call_tool(
                "remarkable_author", {"method": "add_page", "document": "Sketchbook"}
            )
            data = json.loads(result[0][0].text)
            assert data["added"] is True
            assert data["page_added"] == 2
            assert data["total_pages"] == 2
            mock_restart.assert_called_once()
            assert any(p.endswith(".rm") for p in writes)
            assert contents["doc-abc"]["pageCount"] == 2
            pages = contents["doc-abc"]["cPages"]["pages"]
            assert len(pages) == 2
            assert pages[1]["idx"]["value"] == "bb"

    @pytest.mark.asyncio
    async def test_add_page_rejects_non_notebook(self):
        """Adding a page to a PDF (flat pages list) returns an educational error."""
        import remarkable_mcp.write_tools as wt

        doc = self._mock_doc()
        content = json.dumps({"pages": ["p1", "p2"], "fileType": "pdf"})

        def fake_scp(path):
            if path.endswith(".content"):
                return content.encode()
            return None

        client = Mock(spec=["get_meta_items", "_scp_download", "_ssh_command"])
        client.get_meta_items.return_value = [doc]
        client._scp_download.side_effect = fake_scp

        with (
            patch.dict(os.environ, {"REMARKABLE_USE_SSH": "1"}),
            patch.object(wt, "get_rmapi", lambda: client),
        ):
            result = await mcp.call_tool(
                "remarkable_author", {"method": "add_page", "document": "Sketchbook"}
            )
            data = json.loads(result[0][0].text)
            assert data["_error"]["type"] == "not_a_notebook"

    @pytest.mark.asyncio
    async def test_create_document_blank(self):
        """method="create_document" scaffolds a notebook (.rm + .content + .metadata)."""
        import remarkable_mcp.write_tools as wt

        client = Mock(spec=["get_meta_items", "_scp_download", "_ssh_command"])
        client.get_meta_items.return_value = []

        writes = {}
        contents = {}
        metas = {}

        with (
            patch.dict(os.environ, {"REMARKABLE_USE_SSH": "1"}),
            patch.object(wt, "get_rmapi", lambda: client),
            patch.object(wt, "_write_remote_bytes", lambda ssh, p, d: writes.__setitem__(p, d)),
            patch.object(wt, "_write_content_file", lambda ssh, u, d: contents.__setitem__(u, d)),
            patch.object(wt, "_write_metadata", lambda ssh, u, d: metas.__setitem__(u, d)),
            patch.object(wt, "_restart_xochitl") as mock_restart,
            patch.object(wt, "_invalidate_client_cache", lambda c: None),
        ):
            result = await mcp.call_tool(
                "remarkable_author", {"method": "create_document", "name": "My notes"}
            )
            data = json.loads(result[0][0].text)
            assert data["created"] is True
            assert data["document"] == "My notes"
            assert data["total_pages"] == 1
            assert data["has_text"] is False
            uid = data["document_id"]
            assert uid in contents and uid in metas
            assert contents[uid]["fileType"] == "notebook"
            assert contents[uid]["pageCount"] == 1
            assert metas[uid]["visibleName"] == "My notes"
            assert metas[uid]["type"] == "DocumentType"
            assert any(p.endswith(".rm") for p in writes)
            mock_restart.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_document_with_text_seeds_first_page(self):
        """Seeding text sets has_text; the typed text renders in the canvas preview."""
        import remarkable_mcp.write_tools as wt

        client = Mock(spec=["get_meta_items", "_scp_download", "_ssh_command"])
        client.get_meta_items.return_value = []

        with (
            patch.dict(os.environ, {"REMARKABLE_USE_SSH": "1"}),
            patch.object(wt, "get_rmapi", lambda: client),
            patch.object(wt, "_write_remote_bytes", lambda ssh, p, d: None),
            patch.object(wt, "_write_content_file", lambda ssh, u, d: None),
            patch.object(wt, "_write_metadata", lambda ssh, u, d: None),
            patch.object(wt, "_restart_xochitl"),
            patch.object(wt, "_invalidate_client_cache", lambda c: None),
        ):
            result = await mcp.call_tool(
                "remarkable_author",
                {"method": "create_document", "name": "My notes", "text": "Agenda"},
            )
            data = json.loads(result[0][0].text)
            assert data["has_text"] is True

    @pytest.mark.asyncio
    async def test_create_document_requires_name(self):
        with patch.dict(os.environ, {"REMARKABLE_USE_SSH": "1"}):
            result = await mcp.call_tool("remarkable_author", {"method": "create_document"})
            data = json.loads(result[0][0].text)
            assert data["_error"]["type"] == "missing_parameter"


class TestNotebookBuilders:
    """Pure-logic tests for remarkable_mcp.notebooks (no transport)."""

    def test_blank_page_is_drawable(self):
        import io

        from remarkable_mcp import notebooks as nb
        from remarkable_mcp import strokes as s

        raw = nb.blank_page_rm_bytes()
        blocks = list(s.read_blocks(io.BytesIO(raw)))
        assert s.find_target_layer(blocks) is not None

    def test_text_page_is_drawable_and_appendable(self):
        import io

        from remarkable_mcp import notebooks as nb
        from remarkable_mcp import strokes as s

        raw = nb.page_rm_bytes("Hello\nWorld")
        blocks = list(s.read_blocks(io.BytesIO(raw)))
        assert s.find_target_layer(blocks) is not None
        out = s.append_strokes(
            raw, [{"points": [[0.1, 0.1], [0.5, 0.5]], "tool": "fineliner", "color": "black"}]
        )
        assert out.startswith(raw)

    def test_next_page_idx_sequence(self):
        from remarkable_mcp import notebooks as nb

        assert nb.next_page_idx([]) == "ba"
        vals = []
        for _ in range(5):
            vals.append(nb.next_page_idx(vals))
        assert vals == ["ba", "bb", "bc", "bd", "be"]
        assert nb.next_page_idx(["bz"]) == "bza"

    def test_new_notebook_content_shape(self):
        from remarkable_mcp import notebooks as nb

        au = nb.new_uuid()
        pid = nb.new_uuid()
        c = nb.new_notebook_content([pid], au)
        assert c["fileType"] == "notebook"
        assert c["formatVersion"] == 2
        assert c["pageCount"] == 1
        assert c["cPages"]["pages"][0]["id"] == pid
        assert c["cPages"]["pages"][0]["idx"]["value"] == "ba"
        assert c["cPages"]["uuids"][0]["first"] == au
        # serializable
        json.dumps(c)

    def test_content_author_uuid_matches_rm(self):
        import io

        from rmscene.scene_stream import AuthorIdsBlock

        from remarkable_mcp import notebooks as nb
        from remarkable_mcp import strokes as s

        au = nb.new_uuid()
        raw = nb.blank_page_rm_bytes(author_uuid=au)
        blocks = list(s.read_blocks(io.BytesIO(raw)))
        author_block = next(b for b in blocks if isinstance(b, AuthorIdsBlock))
        c = nb.new_notebook_content([nb.new_uuid()], au)
        assert c["cPages"]["uuids"][0]["first"] == str(author_block.author_uuids[1])

    def test_append_page_to_content_grows(self):
        from remarkable_mcp import notebooks as nb

        au = nb.new_uuid()
        c = nb.new_notebook_content([nb.new_uuid()], au)
        res = nb.append_page_to_content(c, nb.new_uuid())
        assert res["page_index"] == 2
        assert res["total_pages"] == 2
        assert c["pageCount"] == 2
        assert c["cPages"]["pages"][1]["idx"]["value"] == "bb"

    def test_append_page_rejects_non_notebook(self):
        from remarkable_mcp import notebooks as nb

        with pytest.raises(ValueError):
            nb.append_page_to_content({"pages": ["a", "b"], "fileType": "pdf"}, "x")

    def test_metadata_shape(self):
        from remarkable_mcp import notebooks as nb

        m = nb.new_document_metadata("Hello", parent="folder-uuid")
        assert m["visibleName"] == "Hello"
        assert m["type"] == "DocumentType"
        assert m["parent"] == "folder-uuid"
        assert m["deleted"] is False
        assert m["metadatamodified"] is True
        json.dumps(m)


class TestGetDocumentFileType:
    """Pure-logic tests for extract.get_document_file_type (zip .content reader)."""

    def _zip_with_content(self, content):
        import json as _json
        import tempfile
        import zipfile as _zip
        from pathlib import Path

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as ztmp:
            zpath = Path(ztmp.name)
        with _zip.ZipFile(zpath, "w") as zf:
            if content is not None:
                zf.writestr("doc.content", _json.dumps(content))
        return zpath

    def test_reads_notebook_file_type(self):
        from remarkable_mcp.extract import get_document_file_type

        zpath = self._zip_with_content({"fileType": "notebook", "formatVersion": 2})
        try:
            assert get_document_file_type(zpath) == "notebook"
        finally:
            zpath.unlink(missing_ok=True)

    def test_reads_pdf_file_type(self):
        from remarkable_mcp.extract import get_document_file_type

        zpath = self._zip_with_content({"fileType": "pdf"})
        try:
            assert get_document_file_type(zpath) == "pdf"
        finally:
            zpath.unlink(missing_ok=True)

    def test_missing_content_returns_empty(self):
        from remarkable_mcp.extract import get_document_file_type

        zpath = self._zip_with_content(None)  # no .content entry
        try:
            assert get_document_file_type(zpath) == ""
        finally:
            zpath.unlink(missing_ok=True)

    def test_bad_zip_returns_empty(self):
        import tempfile
        from pathlib import Path

        from remarkable_mcp.extract import get_document_file_type

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as ztmp:
            ztmp.write(b"not a zip")
            zpath = Path(ztmp.name)
        try:
            assert get_document_file_type(zpath) == ""
        finally:
            zpath.unlink(missing_ok=True)
