"""MCP SDK v2 protocol-era compatibility tests."""

import json
from unittest.mock import AsyncMock, Mock, patch

import pytest
from mcp import Client
from mcp.client import ClientRequestContext
from mcp.types import (
    ElicitRequestParams,
    ElicitResult,
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
    document.tags = []
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
            modern_image = next(
                tool for tool in modern_tools.tools if tool.name == "remarkable_image"
            )
            legacy_image = next(
                tool for tool in legacy_tools.tools if tool.name == "remarkable_image"
            )
            modern_merge_schema = modern_image.input_schema["properties"]["render_merged"]
            assert modern_merge_schema == legacy_image.input_schema["properties"]["render_merged"]
            assert modern_merge_schema["default"] is None

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
