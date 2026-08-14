"""MCP SDK v2 protocol-era compatibility tests."""

import json
from unittest.mock import AsyncMock, Mock, patch

import pytest
from mcp import Client
from mcp.client import ClientRequestContext
from mcp.types import (
    CreateMessageRequestParams,
    CreateMessageResult,
    ElicitRequestParams,
    ElicitResult,
    TextContent,
)

from remarkable_mcp.app_canvas import CANVAS_RESOURCE_URI
from remarkable_mcp.server import mcp


def _document(name: str = "Test Document") -> Mock:
    document = Mock()
    document.VissibleName = name
    document.ID = "doc-1"
    document.Parent = ""
    document.ModifiedClient = None
    document.is_folder = False
    return document


def _without_background_loader():
    return (
        patch("remarkable_mcp.resources.start_background_loader", return_value=None),
        patch(
            "remarkable_mcp.resources.stop_background_loader",
            new_callable=AsyncMock,
        ),
    )


@pytest.mark.asyncio
async def test_one_server_serves_modern_and_legacy_catalogs():
    """The same server exposes equivalent surfaces in both protocol eras."""
    api_client = Mock()
    api_client.get_meta_items.return_value = []

    loader_start, loader_stop = _without_background_loader()
    with (
        loader_start,
        loader_stop,
        patch("remarkable_mcp.tools.get_rmapi", return_value=api_client),
    ):
        async with (
            Client(mcp) as modern,
            Client(mcp, mode="legacy") as legacy,
        ):
            assert modern.protocol_version == "2026-07-28"
            assert legacy.protocol_version == "2025-11-25"

            modern_tools = await modern.list_tools()
            legacy_tools = await legacy.list_tools()
            assert {tool.name for tool in modern_tools.tools} == {
                tool.name for tool in legacy_tools.tools
            }

            modern_prompts = await modern.list_prompts()
            legacy_prompts = await legacy.list_prompts()
            assert {prompt.name for prompt in modern_prompts.prompts} == {
                prompt.name for prompt in legacy_prompts.prompts
            }

            canvas = next(tool for tool in modern_tools.tools if tool.name == "remarkable_canvas")
            assert canvas.meta["ui"]["resourceUri"] == CANVAS_RESOURCE_URI

            resource = await modern.read_resource(CANVAS_RESOURCE_URI)
            assert resource.contents[0].mime_type == "text/html;profile=mcp-app"
            assert "<!doctype html>" in resource.contents[0].text

            modern_status, legacy_status = (
                await modern.call_tool("remarkable_status", {}),
                await legacy.call_tool("remarkable_status", {}),
            )
            assert json.loads(modern_status.content[0].text)["transport"] == "cloud"
            assert json.loads(legacy_status.content[0].text)["transport"] == "cloud"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_version"),
    [("auto", "2026-07-28"), ("legacy", "2025-11-25")],
)
async def test_sampling_ocr_uses_era_portable_resolver(
    monkeypatch, mode: str, expected_version: str
):
    """Sampling OCR works through MRTR and the legacy server back-channel."""
    from remarkable_mcp import tools

    with tools._sampling_page_cache_lock:
        tools._sampling_page_cache.clear()
    monkeypatch.setenv("REMARKABLE_OCR_BACKEND", "sampling")
    document = _document()
    api_client = Mock()
    api_client.get_meta_items.return_value = [document]
    api_client.download.return_value = b"fixture"
    sample_calls = 0

    async def sample(
        context: ClientRequestContext, params: CreateMessageRequestParams
    ) -> CreateMessageResult:
        nonlocal sample_calls
        sample_calls += 1
        assert params.messages[0].content.type == "image"
        return CreateMessageResult(
            role="assistant",
            model="fixture-vision",
            content=TextContent(type="text", text="handwritten fixture"),
        )

    loader_start, loader_stop = _without_background_loader()
    with (
        loader_start,
        loader_stop,
        patch("remarkable_mcp.tools.get_rmapi", return_value=api_client),
        patch("remarkable_mcp.tools.get_document_page_count", return_value=1),
        patch(
            "remarkable_mcp.tools.render_page_from_document_zip",
            return_value=b"\x89PNG\r\nfixture",
        ) as render_page,
    ):
        async with Client(mcp, mode=mode, sampling_callback=sample) as client:
            result = await client.call_tool(
                "remarkable_image",
                {
                    "document": "Test Document",
                    "include_ocr": True,
                    "compatibility": True,
                },
            )
            assert client.protocol_version == expected_version

    payload = json.loads(result.content[0].text)
    assert payload["ocr_backend"] == "sampling"
    assert payload["ocr_text"] == "handwritten fixture"
    assert sample_calls == 1
    assert api_client.download.call_count == 1
    assert render_page.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_read_sampling_reuses_prepared_page(monkeypatch, mode: str):
    """Read sampling downloads and renders once across every protocol round."""
    from remarkable_mcp import tools

    with tools._sampling_page_cache_lock:
        tools._sampling_page_cache.clear()
    monkeypatch.setenv("REMARKABLE_OCR_BACKEND", "sampling")
    document = _document()
    api_client = Mock()
    api_client.get_meta_items.return_value = [document]
    api_client.download.return_value = b"fixture"

    async def sample(
        context: ClientRequestContext, params: CreateMessageRequestParams
    ) -> CreateMessageResult:
        return CreateMessageResult(
            role="assistant",
            model="fixture-vision",
            content=TextContent(type="text", text="read fixture"),
        )

    loader_start, loader_stop = _without_background_loader()
    with (
        loader_start,
        loader_stop,
        patch("remarkable_mcp.tools.get_rmapi", return_value=api_client),
        patch("remarkable_mcp.tools.get_file_type", return_value="notebook"),
        patch("remarkable_mcp.tools.get_cached_page_ocr", return_value=None),
        patch("remarkable_mcp.tools.cache_page_ocr"),
        patch("remarkable_mcp.tools.get_document_page_count", return_value=1),
        patch(
            "remarkable_mcp.tools.render_page_from_document_zip",
            return_value=b"\x89PNG\r\nfixture",
        ) as render_page,
    ):
        async with Client(mcp, mode=mode, sampling_callback=sample) as client:
            result = await client.call_tool(
                "remarkable_read",
                {"document": "Test Document", "include_ocr": True},
            )

    payload = json.loads(result.content[0].text)
    assert payload["content"] == "read fixture"
    assert payload["ocr_backend"] == "sampling"
    assert api_client.download.call_count == 1
    assert render_page.call_count == 1


@pytest.mark.asyncio
async def test_read_sampling_without_capability_skips_sample_render(monkeypatch):
    """A client without sampling goes directly to the local OCR fallback."""
    from remarkable_mcp import tools

    with tools._sampling_page_cache_lock:
        tools._sampling_page_cache.clear()
    monkeypatch.setenv("REMARKABLE_OCR_BACKEND", "sampling")
    document = _document()
    api_client = Mock()
    api_client.get_meta_items.return_value = [document]
    api_client.download.return_value = b"fixture"
    extracted = {
        "typed_text": [],
        "highlights": [],
        "handwritten_text": ["local fallback"],
        "annotated_pages": [],
        "pages": 1,
        "ocr_backend": "tesseract",
    }

    loader_start, loader_stop = _without_background_loader()
    with (
        loader_start,
        loader_stop,
        patch("remarkable_mcp.tools.get_rmapi", return_value=api_client),
        patch("remarkable_mcp.tools.get_file_type", return_value="notebook"),
        patch(
            "remarkable_mcp.tools.extract_text_from_document_zip",
            return_value=extracted,
        ),
        patch("remarkable_mcp.tools.render_page_from_document_zip") as render_page,
    ):
        async with Client(mcp) as client:
            result = await client.call_tool(
                "remarkable_read",
                {"document": "Test Document", "include_ocr": True},
            )

    payload = json.loads(result.content[0].text)
    assert payload["content"] == "local fallback"
    assert payload["ocr_backend"] == "tesseract"
    assert api_client.download.call_count == 1
    render_page.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_version"),
    [("auto", "2026-07-28"), ("legacy", "2025-11-25")],
)
@pytest.mark.parametrize("permanent", [False, True])
async def test_delete_confirmation_works_in_both_protocol_eras(
    monkeypatch, mode: str, expected_version: str, permanent: bool
):
    """A confirmed delete uses MRTR for modern clients and push for legacy."""
    for variable in (
        "REMARKABLE_SKIP_CONFIRM",
        "REMARKABLE_READ_ONLY",
        "REMARKABLE_USE_LOCAL_DIR",
        "REMARKABLE_LOCAL_DIR",
        "REMARKABLE_USE_SSH",
        "REMARKABLE_USE_USB_WEB",
    ):
        monkeypatch.delenv(variable, raising=False)

    document = _document("Old Notes")
    document.is_folder = False
    api_client = Mock(spec=["get_meta_items", "delete"])
    api_client.get_meta_items.return_value = [document]
    confirmation_calls = 0

    async def confirm(context: ClientRequestContext, params: ElicitRequestParams) -> ElicitResult:
        nonlocal confirmation_calls
        confirmation_calls += 1
        assert "Old Notes" in params.message
        if permanent:
            assert "irreversible" in params.message.lower()
            assert "cannot be recovered" in params.message.lower()
            assert "trash" in params.message.lower()
        else:
            assert "moves it to the trash" in params.message.lower()
            assert "irreversible" not in params.message.lower()
        return ElicitResult(action="accept", content={"confirm": True})

    loader_start, loader_stop = _without_background_loader()
    with (
        loader_start,
        loader_stop,
        patch("remarkable_mcp.write_tools.get_rmapi", return_value=api_client),
    ):
        async with Client(mcp, mode=mode, elicitation_callback=confirm) as client:
            result = await client.call_tool(
                "remarkable_delete",
                {"document": "Old Notes", "permanent": permanent},
            )
            assert client.protocol_version == expected_version

    payload = json.loads(result.content[0].text)
    assert confirmation_calls == 1
    if permanent:
        assert payload["_error"]["type"] == "permanent_delete_unsupported"
        api_client.delete.assert_not_called()
    else:
        assert payload["deleted"] is True
        api_client.delete.assert_called_once_with("doc-1")
