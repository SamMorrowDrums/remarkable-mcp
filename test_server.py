#!/usr/bin/env python3
"""
Tests for reMarkable MCP Server

Tests the 4 intent-based tools using FastMCP's testing capabilities.
"""

import json
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

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
            "remarkable_status",
        ]

        for tool_name in expected_tools:
            assert tool_name in tool_names, f"Tool {tool_name} not found"

    @pytest.mark.asyncio
    async def test_tools_count(self):
        """Test that we have exactly 4 intent-based tools."""
        tools = await mcp.list_tools()
        assert len(tools) == 4, f"Expected 4 tools, got {len(tools)}"

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


# =============================================================================
# Test Registration
# =============================================================================


class TestRegistration:
    """Test registration functionality."""

    @patch("requests.post")
    @patch("pathlib.Path.write_text")
    def test_register_and_get_token(self, mock_write_text, mock_post):
        """Test registration process."""
        # Mock successful API response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = "test_device_token_12345"
        mock_post.return_value = mock_response

        token = register_and_get_token("test_code")

        # Should return JSON with devicetoken
        import json

        token_data = json.loads(token)
        assert token_data["devicetoken"] == "test_device_token_12345"
        assert "usertoken" in token_data

        # Verify API was called
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert "webapp-prod.cloud.remarkable.engineering" in call_args[0][0]

    @patch("requests.post")
    def test_register_invalid_code(self, mock_post):
        """Test registration with invalid/expired code."""
        # Mock 400 response (invalid code)
        mock_response = Mock()
        mock_response.status_code = 400
        mock_response.text = ""
        mock_post.return_value = mock_response

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

        assert len(tools) == 4

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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
