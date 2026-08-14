"""Single-document export tool contracts across representative transports."""

from __future__ import annotations

import io
import json
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

import fitz
import pytest
from mcp import types

from remarkable_mcp.export_resources import export_store
from remarkable_mcp.server import mcp


def _document(name: str, doc_id: str = "doc-1"):
    return SimpleNamespace(
        VissibleName=name,
        ID=doc_id,
        Parent="",
        ModifiedClient=None,
        is_folder=False,
        tags=["exported"],
    )


def _pdf_bytes(pages: int = 2) -> bytes:
    document = fitz.open()
    for number in range(1, pages + 1):
        page = document.new_page(width=445, height=594)
        page.insert_text((40, 50), f"source page {number}")
    data = document.tobytes()
    document.close()
    return data


def _notebook_archive(doc_id: str = "doc-1", text: str = "Typed export content") -> bytes:
    from remarkable_mcp import notebooks

    page_id = "page-1"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            f"{doc_id}.content",
            json.dumps({"fileType": "notebook", "cPages": {"pages": [{"id": page_id}]}}),
        )
        archive.writestr(f"{page_id}.rm", notebooks.page_rm_bytes(text))
    return output.getvalue()


def _annotated_pdf_archive(doc_id: str = "doc-1") -> bytes:
    from remarkable_mcp import notebooks

    page_id = "page-1"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            f"{doc_id}.content",
            json.dumps(
                {
                    "fileType": "pdf",
                    "cPages": {
                        "pages": [{"id": page_id, "redir": {"value": 0}}],
                    },
                }
            ),
        )
        archive.writestr(f"{doc_id}.pdf", _pdf_bytes(1))
        archive.writestr(f"{page_id}.rm", notebooks.page_rm_bytes("PDF annotation"))
    return output.getvalue()


def _json_result(result: types.CallToolResult) -> dict:
    return json.loads(result.content[0].text)


def _resource_link(result: types.CallToolResult) -> types.ResourceLink:
    return next(content for content in result.content if isinstance(content, types.ResourceLink))


@pytest.fixture(autouse=True)
def _clean_export_store():
    export_store.cleanup()
    yield
    export_store.cleanup()


class TestExportToolContract:
    @pytest.mark.asyncio
    async def test_tool_metadata_describes_temporary_local_write(self):
        tools = await mcp.list_tools()
        tool = next(item for item in tools if item.name == "remarkable_export")

        assert tool.annotations.read_only_hint is False
        assert tool.annotations.destructive_hint is False
        assert tool.input_schema["properties"]["output_format"]["default"] == "pdf"
        assert tool.input_schema["properties"]["pdf_mode"]["default"] == "merged"
        assert tool.input_schema["properties"]["include_ocr"]["default"] is False
        assert "temporary directory" in tool.description
        assert "without modifying the tablet" in tool.description

    @pytest.mark.asyncio
    async def test_native_pdf_returns_small_resource_link_with_stable_identity(self):
        import remarkable_mcp.tools as tool_module

        document = _document("Version 1.2 plan", "stable-pdf-id")
        client = Mock()
        client.get_meta_items.return_value = [document]
        client.download.return_value = _pdf_bytes()

        with (
            patch.object(tool_module, "get_rmapi", return_value=client),
            patch.object(tool_module, "get_file_type", return_value="pdf"),
        ):
            result = await mcp.call_tool(
                "remarkable_export",
                {"document": document.VissibleName, "output_format": "pdf"},
            )

        data = _json_result(result)
        assert "_error" not in data, data
        link = _resource_link(result)
        assert data["document_id"] == "stable-pdf-id"
        assert data["filename"] == "Version 1.2 plan.pdf"
        assert data["status"] == "complete"
        assert data["temporary_local_file"] is True
        assert data["resource_uri"] == link.uri
        assert not any(isinstance(content, types.EmbeddedResource) for content in result.content)
        assert "base64" not in result.content[0].text.lower()

        resource = await mcp.read_resource(link.uri)
        assert resource[0].mime_type == "application/pdf"
        with fitz.open(stream=resource[0].content, filetype="pdf") as exported:
            assert len(exported) == 2
            assert "stable-pdf-id" in exported.metadata["subject"]
            assert [page.get_text().strip() for page in exported] == [
                "source page 1",
                "source page 2",
            ]

    @pytest.mark.asyncio
    async def test_flattened_pdf_rejects_annotation_only_export(self):
        import remarkable_mcp.tools as tool_module

        document = _document("Flattened PDF")
        client = Mock()
        client.get_meta_items.return_value = [document]
        client.download.return_value = _pdf_bytes()

        with (
            patch.object(tool_module, "get_rmapi", return_value=client),
            patch.object(tool_module, "get_file_type", return_value="pdf"),
        ):
            result = await mcp.call_tool(
                "remarkable_export",
                {
                    "document": document.VissibleName,
                    "output_format": "pdf",
                    "pdf_mode": "annotations",
                },
            )

        assert _json_result(result)["_error"]["type"] == "annotation_only_not_available"
        assert not any(isinstance(content, types.ResourceLink) for content in result.content)

    @pytest.mark.asyncio
    async def test_pdf_archive_merges_complete_page_by_default(self):
        import remarkable_mcp.tools as tool_module

        document = _document("Annotated PDF", "annotated-id")
        client = Mock()
        client.get_meta_items.return_value = [document]
        client.download.return_value = _annotated_pdf_archive(document.ID)

        with (
            patch.object(tool_module, "get_rmapi", return_value=client),
            patch.object(tool_module, "get_file_type", return_value="pdf"),
        ):
            result = await mcp.call_tool(
                "remarkable_export",
                {"document": document.VissibleName, "output_format": "pdf"},
            )

        data = _json_result(result)
        assert data["status"] == "complete"
        assert data["pages"] == 1
        assert data["pdf_mode"] == "merged"
        resource = await mcp.read_resource(_resource_link(result).uri)
        with fitz.open(stream=resource[0].content, filetype="pdf") as exported:
            assert len(exported) == 1
            assert exported[0].rect.width > 0
            assert exported[0].rect.height > 0

    @pytest.mark.asyncio
    async def test_partial_page_diagnostics_are_returned_with_resource(self):
        import remarkable_mcp.tools as tool_module
        from remarkable_mcp.exporters import ExportBuildResult

        document = _document("Partial Notebook", "partial-id")
        client = Mock()
        client.get_meta_items.return_value = [document]
        client.download.return_value = _notebook_archive(document.ID)

        def partial_export(_archive, destination, _metadata, **_kwargs):
            destination.write_bytes(_pdf_bytes(1))
            return ExportBuildResult(
                status="partial",
                pages=1,
                failed_pages=(1,),
                warnings=("Page 1: synthetic failure",),
            )

        with (
            patch.object(tool_module, "get_rmapi", return_value=client),
            patch.object(tool_module, "get_file_type", return_value="notebook"),
            patch.object(tool_module, "download_raw_file", return_value=None),
            patch.object(tool_module, "write_archive_pdf_export", side_effect=partial_export),
        ):
            result = await mcp.call_tool(
                "remarkable_export",
                {"document": document.VissibleName, "output_format": "pdf"},
            )

        data = _json_result(result)
        assert data["status"] == "partial"
        assert data["failed_pages"] == [1]
        assert data["warnings"] == ["Page 1: synthetic failure"]
        assert "Partial export" in data["_hint"]
        assert _resource_link(result)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "arguments",
        [
            {"output_format": "markdown", "pdf_mode": "annotations"},
            {"output_format": "pdf", "include_ocr": True},
        ],
    )
    async def test_incompatible_options_are_not_silently_ignored(self, arguments):
        result = await mcp.call_tool(
            "remarkable_export",
            {"document": "Anything", **arguments},
        )
        assert _json_result(result)["_error"]["type"] == "invalid_export_options"

    @pytest.mark.asyncio
    async def test_document_not_found_uses_standard_educational_error(self):
        import remarkable_mcp.tools as tool_module

        client = Mock()
        client.get_meta_items.return_value = []
        with patch.object(tool_module, "get_rmapi", return_value=client):
            result = await mcp.call_tool("remarkable_export", {"document": "Missing"})

        data = _json_result(result)
        assert data["_error"]["type"] == "document_not_found"
        assert "remarkable_browse" in data["_error"]["suggestion"]


class TestExportTransportFixtures:
    @pytest.mark.asyncio
    async def test_local_dir_fixture_exports_markdown(self, tmp_path):
        from remarkable_mcp.local_dir import LocalDirClient

        doc_id = "local-doc"
        page_id = "local-page"
        (tmp_path / f"{doc_id}.metadata").write_text(
            json.dumps(
                {
                    "visibleName": "Local Notes",
                    "type": "DocumentType",
                    "parent": "",
                    "tags": ["offline"],
                }
            )
        )
        (tmp_path / f"{doc_id}.content").write_text(
            json.dumps(
                {
                    "fileType": "notebook",
                    "cPages": {"pages": [{"id": page_id}]},
                }
            )
        )
        page_dir = tmp_path / doc_id
        page_dir.mkdir()
        from remarkable_mcp import notebooks

        (page_dir / f"{page_id}.rm").write_bytes(notebooks.page_rm_bytes("Local typed text"))
        client = LocalDirClient(tmp_path)

        with patch("remarkable_mcp.tools.get_rmapi", return_value=client):
            result = await mcp.call_tool(
                "remarkable_export",
                {"document": "Local Notes", "output_format": "markdown"},
            )

        data = _json_result(result)
        link = _resource_link(result)
        assert data["source_type"] == "notebook"
        assert data["document_id"] == doc_id
        resource = await mcp.read_resource(link.uri)
        assert resource[0].mime_type == "text/markdown; charset=utf-8"
        assert "Local typed text" in resource[0].content
        assert f'remarkable_document_id: "{doc_id}"' in resource[0].content

    @pytest.mark.asyncio
    async def test_content_addressed_cloud_fixture_exports_markdown(self):
        from remarkable_mcp.sync import Document, RemarkableClient

        doc_id = "cloud-doc"
        page_id = "page-1"
        archive = _notebook_archive(doc_id, "Cloud typed text")
        with zipfile.ZipFile(io.BytesIO(archive)) as source:
            content_bytes = source.read(f"{doc_id}.content")
            page_bytes = source.read(f"{page_id}.rm")

        document = Document(
            id=doc_id,
            hash="document-index",
            name="Cloud Notes",
            doc_type="DocumentType",
            files=[
                {"id": f"{doc_id}.content", "hash": "content-hash", "size": len(content_bytes)},
                {"id": f"{page_id}.rm", "hash": "page-hash", "size": len(page_bytes)},
            ],
        )
        index = (
            "3\n"
            f"content-hash:0:{doc_id}.content:0:{len(content_bytes)}\n"
            f"page-hash:0:{page_id}.rm:0:{len(page_bytes)}\n"
        ).encode()
        blobs = {
            "document-index": index,
            "content-hash": content_bytes,
            "page-hash": page_bytes,
        }
        client = RemarkableClient(user_token="fixture")
        client._documents = [document]
        client._documents_by_id = {doc_id: document}
        client.get_meta_items = Mock(return_value=[document])
        client._get_file = Mock(side_effect=lambda blob_hash, _name: blobs[blob_hash])

        with patch("remarkable_mcp.tools.get_rmapi", return_value=client):
            result = await mcp.call_tool(
                "remarkable_export",
                {"document": "Cloud Notes", "output_format": "markdown"},
            )

        data = _json_result(result)
        assert "_error" not in data, data
        link = _resource_link(result)
        assert data["document_id"] == doc_id
        assert data["representation"] == "document_archive"
        resource = await mcp.read_resource(link.uri)
        assert "Cloud typed text" in resource[0].content
