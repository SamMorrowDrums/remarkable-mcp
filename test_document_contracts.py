"""Regression tests for document lookup, page counts, and PDF rendering defaults."""

import asyncio
import io
import json
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from mcp import types

from remarkable_mcp.server import mcp


def _document(name: str, doc_id: str, parent: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        VissibleName=name,
        ID=doc_id,
        Parent=parent,
        ModifiedClient=None,
        is_folder=False,
        tags=[],
    )


def _extraction(
    *,
    pages: int,
    typed_text: list[str] | None = None,
    handwritten_text: list[str] | None = None,
) -> dict:
    return {
        "typed_text": typed_text or [],
        "highlights": [],
        "handwritten_text": handwritten_text,
        "pages": pages,
        "page_ids": [f"page-{i}" for i in range(1, pages + 1)],
        "annotated_pages": [],
        "ocr_backend": "tesseract" if handwritten_text else None,
        "tags": [],
    }


def _pdf_bytes() -> bytes:
    import fitz

    pdf = fitz.open()
    pdf.new_page(width=445, height=594)
    pdf_bytes = pdf.tobytes()
    pdf.close()
    return pdf_bytes


def _pdf_archive(*, annotated: bool, user_added: bool) -> bytes:
    from remarkable_mcp import notebooks

    pdf_bytes = _pdf_bytes()

    page = {"id": "page-1"}
    if not user_added:
        page["redir"] = {"value": 0}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "doc.content",
            json.dumps({"fileType": "pdf", "cPages": {"pages": [page]}}),
        )
        archive.writestr("doc.pdf", pdf_bytes)
        if annotated:
            archive.writestr("page-1.rm", notebooks.page_rm_bytes("PDF annotation"))
    return output.getvalue()


async def _call_tool(name: str, arguments: dict) -> types.CallToolResult:
    return await mcp.call_tool(name, arguments)


def _response_json(result: types.CallToolResult) -> dict:
    return json.loads(result.content[0].text)


@pytest.fixture(autouse=True)
def _clear_root_path(monkeypatch):
    monkeypatch.delenv("REMARKABLE_ROOT_PATH", raising=False)


class TestArchivedMetadataShapes:
    """The shared archive predicate supports every transport's metadata object."""

    def test_cloud_ssh_local_dir_and_usb_shapes(self):
        from remarkable_mcp.local_dir import Document as LocalDocument
        from remarkable_mcp.ssh import Document as SSHDocument
        from remarkable_mcp.sync import Document as CloudDocument
        from remarkable_mcp.tools import _is_cloud_archived
        from remarkable_mcp.usb_web import Document as USBDocument

        cloud_trash = CloudDocument("cloud", "", "Cloud", "DocumentType", parent="trash")
        cloud_live = CloudDocument("cloud-live", "", "Cloud live", "DocumentType")
        ssh_trash = SSHDocument("ssh", "", "SSH", "DocumentType", parent="trash")
        local_trash = LocalDocument("local", "", "Local", "DocumentType", parent="trash")
        local_unsynced = LocalDocument(
            "local-live",
            "",
            "Local live",
            "DocumentType",
            synced=False,
        )
        usb_parent_named_trash = USBDocument(
            "usb",
            "",
            "USB",
            "DocumentType",
            parent="trash",
        )

        assert _is_cloud_archived(cloud_trash) is True
        assert _is_cloud_archived(cloud_live) is False
        assert _is_cloud_archived(ssh_trash) is True
        assert _is_cloud_archived(local_trash) is True
        assert _is_cloud_archived(local_unsynced) is False
        assert _is_cloud_archived(usb_parent_named_trash) is False


class TestLiveDocumentLookup:
    def test_live_namesake_wins_and_trash_only_is_hidden(self):
        from remarkable_mcp.api import get_items_by_id
        from remarkable_mcp.tools import _find_target_document

        trashed = _document("Shared name", "trashed", parent="trash")
        live = _document("Shared name", "live")
        collection = [trashed, live]

        assert _find_target_document(collection, get_items_by_id(collection), "Shared name") is live
        assert _find_target_document([trashed], get_items_by_id([trashed]), "Shared name") is None

    @pytest.mark.asyncio
    async def test_read_and_image_resolve_the_live_namesake(self):
        import remarkable_mcp.tools as tools

        trashed = _document("Shared name", "trashed", parent="trash")
        live = _document("Shared name", "live")
        client = Mock()
        client.get_meta_items.return_value = [trashed, live]
        client.download.return_value = b"document archive"

        with (
            patch.object(tools, "get_rmapi", return_value=client),
            patch.object(tools, "get_file_type", return_value="notebook"),
            patch.object(tools, "get_cached_ocr_result", return_value=None),
            patch.object(
                tools,
                "extract_text_from_document_zip",
                return_value=_extraction(pages=1, typed_text=["live content"]),
            ),
        ):
            read_result = await _call_tool(
                "remarkable_read",
                {"document": "Shared name", "include_ocr": True},
            )

        assert _response_json(read_result)["content"] == "live content"
        client.download.assert_called_once_with(live)

        client.reset_mock()
        client.get_meta_items.return_value = [trashed, live]
        client.download.return_value = b"document archive"
        with (
            patch.object(tools, "get_rmapi", return_value=client),
            patch.object(tools, "get_document_page_count", return_value=1),
            patch.object(tools, "get_document_file_type", return_value="notebook"),
            patch.object(tools, "render_page_from_document_zip", return_value=b"png"),
        ):
            image_result = await _call_tool(
                "remarkable_image",
                {
                    "document": "Shared name",
                    "render_merged": False,
                    "compatibility": True,
                },
            )

        assert "_error" not in _response_json(image_result)
        client.download.assert_called_once_with(live)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tool_name", "arguments"),
        [
            ("remarkable_read", {"document": "Meting Notes"}),
            ("remarkable_image", {"document": "Meting Notes"}),
        ],
    )
    async def test_trashed_documents_are_excluded_from_suggestions(self, tool_name, arguments):
        import remarkable_mcp.tools as tools

        trashed = _document("Meeting Notes Trash", "trashed", parent="trash")
        live = _document("Meeting Notes Live", "live")
        client = Mock()
        client.get_meta_items.return_value = [trashed, live]

        with (
            patch.object(tools, "get_rmapi", return_value=client),
            patch.object(
                tools,
                "find_similar_documents",
                return_value=["Meeting Notes Live"],
            ) as find_similar,
        ):
            result = await _call_tool(tool_name, arguments)

        candidates = find_similar.call_args.args[1]
        assert candidates == [live]
        assert _response_json(result)["_error"]["did_you_mean"] == ["Meeting Notes Live"]


class TestReadPageCounts:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("label", "typed_text", "handwritten_text", "expected_content_pages", "expected_more"),
        [
            ("typed", ["typed notes"], None, 1, False),
            ("handwritten", [], ["ink one", "ink two"], 2, True),
            ("mixed", ["typed notes"], ["ink"], 1, False),
            ("blank", [], None, 1, False),
        ],
    )
    async def test_notebook_total_pages_are_physical(
        self,
        label,
        typed_text,
        handwritten_text,
        expected_content_pages,
        expected_more,
    ):
        import remarkable_mcp.tools as tools

        document = _document(f"{label} notebook", f"{label}-id")
        client = Mock()
        client.get_meta_items.return_value = [document]
        client.download.return_value = b"document archive"
        extraction = _extraction(
            pages=7,
            typed_text=typed_text,
            handwritten_text=handwritten_text,
        )

        with (
            patch.object(tools, "get_rmapi", return_value=client),
            patch.object(tools, "get_file_type", return_value="notebook"),
            patch.object(tools, "get_cached_ocr_result", return_value=None),
            patch.object(tools, "extract_text_from_document_zip", return_value=extraction),
        ):
            result = await _call_tool(
                "remarkable_read",
                {"document": document.VissibleName, "include_ocr": True},
            )

        data = _response_json(result)
        assert data["total_pages"] == 7
        assert data["content_pages"] == expected_content_pages
        assert data["more"] is expected_more
        assert data.get("next_page") == (2 if expected_more else None)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("file_type", ["pdf", "epub"])
    async def test_pdf_and_epub_keep_text_pagination_separate(self, file_type):
        import remarkable_mcp.tools as tools

        document = _document(f"Long {file_type}", f"{file_type}-id")
        client = Mock()
        client.get_meta_items.return_value = [document]
        client.download.return_value = b"document archive"
        long_text = "x" * (tools.DEFAULT_PAGE_SIZE + 1)

        with (
            patch.object(tools, "get_rmapi", return_value=client),
            patch.object(tools, "get_file_type", return_value=file_type),
            patch.object(tools, "download_raw_file", return_value=b"source"),
            patch.object(tools, "extract_text_from_pdf", return_value=long_text),
            patch.object(tools, "extract_text_from_epub", return_value=long_text),
            patch.object(
                tools,
                "extract_text_from_document_zip",
                return_value=_extraction(pages=5),
            ),
        ):
            result = await _call_tool("remarkable_read", {"document": document.VissibleName})

        data = _response_json(result)
        assert data["total_pages"] == 5
        assert data["content_pages"] == 2
        assert data["more"] is True
        assert data["next_page"] == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("file_type", ["pdf", "epub"])
    async def test_raw_reads_still_report_physical_pages(self, file_type):
        import remarkable_mcp.tools as tools

        document = _document(f"Raw {file_type}", f"raw-{file_type}")
        client = Mock()
        client.get_meta_items.return_value = [document]
        client.download.return_value = b"document archive"

        with (
            patch.object(tools, "get_rmapi", return_value=client),
            patch.object(tools, "get_file_type", return_value=file_type),
            patch.object(tools, "download_raw_file", return_value=b"source"),
            patch.object(tools, "extract_text_from_pdf", return_value="source text"),
            patch.object(tools, "extract_text_from_epub", return_value="source text"),
            patch.object(tools, "get_document_page_count", return_value=6),
        ):
            result = await _call_tool(
                "remarkable_read",
                {"document": document.VissibleName, "content_type": "raw"},
            )

        data = _response_json(result)
        assert data["total_pages"] == 6
        assert data["content_pages"] == 1
        assert data["more"] is False

    @pytest.mark.asyncio
    async def test_raw_pdf_counts_usb_native_pdf_fallback(self):
        import remarkable_mcp.tools as tools

        document = _document("USB PDF", "usb-pdf")
        client = Mock()
        client.get_meta_items.return_value = [document]
        client.download.return_value = _pdf_bytes()

        with (
            patch.object(tools, "get_rmapi", return_value=client),
            patch.object(tools, "get_file_type", return_value="pdf"),
            patch.object(tools, "download_raw_file", return_value=_pdf_bytes()),
            patch.object(tools, "extract_text_from_pdf", return_value="source text"),
        ):
            result = await _call_tool(
                "remarkable_read",
                {"document": document.VissibleName, "content_type": "raw"},
            )

        data = _response_json(result)
        assert "_error" not in data
        assert data["total_pages"] == 1
        assert data["content_pages"] == 1


class TestPdfRenderingDefault:
    @pytest.mark.asyncio
    async def test_schema_exposes_nullable_auto_default(self):
        tools = await mcp.list_tools()
        image_tool = next(tool for tool in tools if tool.name == "remarkable_image")
        schema = image_tool.input_schema["properties"]["render_merged"]

        assert schema["default"] is None
        assert {"type": "boolean"} in schema["anyOf"]
        assert {"type": "null"} in schema["anyOf"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("file_type", "render_merged", "expected_merged"),
        [
            ("pdf", None, True),
            ("notebook", None, False),
            ("pdf", False, False),
        ],
    )
    async def test_auto_mode_only_merges_pdf_backed_png(
        self,
        file_type,
        render_merged,
        expected_merged,
    ):
        import remarkable_mcp.tools as tools

        document = _document("Visual", "visual")
        client = Mock()
        client.get_meta_items.return_value = [document]
        client.download.return_value = b"document archive"
        arguments = {"document": "Visual", "compatibility": True}
        if render_merged is not None:
            arguments["render_merged"] = render_merged

        with (
            patch.object(tools, "get_rmapi", return_value=client),
            patch.object(tools, "get_document_page_count", return_value=1),
            patch.object(tools, "get_document_file_type", return_value=file_type),
            patch.object(
                tools,
                "render_merged_page_from_document_zip",
                return_value=(b"merged png", None),
            ) as merged_renderer,
            patch.object(
                tools, "render_page_from_document_zip", return_value=b"strokes"
            ) as strokes,
        ):
            result = await _call_tool("remarkable_image", arguments)

        data = _response_json(result)
        assert data["merged"] is expected_merged
        if expected_merged:
            merged_renderer.assert_called_once()
            strokes.assert_not_called()
        else:
            merged_renderer.assert_not_called()
            strokes.assert_called_once()

    @pytest.mark.asyncio
    async def test_auto_mode_preserves_user_added_pdf_page_annotation_fallback(self):
        import remarkable_mcp.tools as tools

        document = _document("PDF with added page", "pdf-added")
        client = Mock()
        client.get_meta_items.return_value = [document]
        client.download.return_value = b"document archive"
        note = "Page has no PDF underlay (user-added page); annotation-only render."

        with (
            patch.object(tools, "get_rmapi", return_value=client),
            patch.object(tools, "get_document_page_count", return_value=2),
            patch.object(tools, "get_document_file_type", return_value="pdf"),
            patch.object(
                tools,
                "render_merged_page_from_document_zip",
                return_value=(b"annotation png", note),
            ),
        ):
            result = await _call_tool(
                "remarkable_image",
                {"document": document.VissibleName, "page": 2, "compatibility": True},
            )

        data = _response_json(result)
        assert data["merged"] is False
        assert "user-added page" in data["_hint"]

    @pytest.mark.asyncio
    async def test_explicit_annotation_only_skips_pdf_underlay_fallback(self):
        import remarkable_mcp.tools as tools

        document = _document("Annotation only", "annotation-only")
        client = Mock()
        client.get_meta_items.return_value = [document]
        client.download.return_value = b"document archive"

        with (
            patch.object(tools, "get_rmapi", return_value=client),
            patch.object(tools, "get_document_page_count", return_value=1),
            patch.object(tools, "get_document_file_type", return_value="pdf"),
            patch.object(tools, "render_page_from_document_zip", return_value=None),
            patch.object(
                tools,
                "render_mapped_pdf_page_from_document_zip",
                return_value=(b"pdf underlay", True),
            ) as pdf_fallback,
            patch.object(
                tools,
                "render_page_full_page_from_document_zip",
                return_value=(b"blank annotation page", (1404.0, 1872.0)),
            ) as blank_page,
        ):
            result = await _call_tool(
                "remarkable_image",
                {
                    "document": document.VissibleName,
                    "render_merged": False,
                    "compatibility": True,
                },
            )

        data = _response_json(result)
        assert "_error" not in data
        assert data["merged"] is False
        assert data["render_source"] == "strokes"
        pdf_fallback.assert_not_called()
        blank_page.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("annotated", "user_added", "expected_merged"),
        [
            (False, False, True),
            (True, False, True),
            (False, True, False),
            (True, True, False),
        ],
    )
    async def test_auto_mode_renders_imported_and_user_added_pdf_pages(
        self,
        annotated,
        user_added,
        expected_merged,
    ):
        import remarkable_mcp.tools as tools

        document = _document("PDF variants", "pdf-variants")
        client = Mock()
        client.get_meta_items.return_value = [document]
        client.download.return_value = _pdf_archive(
            annotated=annotated,
            user_added=user_added,
        )

        with patch.object(tools, "get_rmapi", return_value=client):
            result = await _call_tool(
                "remarkable_image",
                {"document": document.VissibleName, "compatibility": True},
            )

        data = _response_json(result)
        assert "_error" not in data
        assert data["image_base64"]
        assert data["merged"] is expected_merged

    @pytest.mark.asyncio
    async def test_svg_auto_mode_remains_annotation_only_without_warning(self):
        import remarkable_mcp.tools as tools

        document = _document("PDF SVG", "pdf-svg")
        client = Mock()
        client.get_meta_items.return_value = [document]
        client.download.return_value = b"document archive"

        with (
            patch.object(tools, "get_rmapi", return_value=client),
            patch.object(tools, "get_document_page_count", return_value=1),
            patch.object(tools, "get_document_file_type", return_value="pdf"),
            patch.object(
                tools,
                "render_page_from_document_zip_svg",
                return_value="<svg></svg>",
            ),
        ):
            result = await _call_tool(
                "remarkable_image",
                {
                    "document": document.VissibleName,
                    "output_format": "svg",
                    "compatibility": True,
                },
            )

        data = _response_json(result)
        assert data["merged"] is False
        assert "render_merged is only supported" not in data["_hint"]


class TestCanvasArchivedLookup:
    @pytest.mark.asyncio
    async def test_canvas_resolves_live_namesake(self, monkeypatch):
        from PIL import Image

        import remarkable_mcp.api as api
        import remarkable_mcp.extract as extract
        from remarkable_mcp.app_canvas import _render_canvas_page

        image = io.BytesIO()
        Image.new("RGB", (12, 16), "white").save(image, "PNG")
        trashed = _document("Canvas notes", "trash", parent="trash")
        live = _document("Canvas notes", "live")
        client = Mock()
        client.get_meta_items.return_value = [trashed, live]
        client.download.return_value = b"document archive"

        monkeypatch.setattr(api, "get_rmapi", lambda: client)
        monkeypatch.setattr(api, "get_items_by_id", lambda items: {item.ID: item for item in items})
        monkeypatch.setattr(api, "get_item_path", lambda doc, items: f"/{doc.VissibleName}")
        monkeypatch.setattr(api, "get_active_transport", lambda: "cloud")
        monkeypatch.setattr(api, "download_raw_file", lambda client, doc, ext: None)
        monkeypatch.setattr(extract, "get_background_color", lambda: "#FBFBFB")
        monkeypatch.setattr(extract, "get_document_page_count", lambda path: 1)
        monkeypatch.setattr(extract, "get_document_file_type", lambda path: "notebook")
        monkeypatch.setattr(
            extract,
            "render_page_full_page_from_document_zip",
            lambda path, page, **kwargs: (image.getvalue(), (820.0, 1458.0)),
        )

        result = await _render_canvas_page("Canvas notes", 1, None)

        assert isinstance(result, types.CallToolResult)
        assert result.structured_content["document_name"] == "Canvas notes"
        client.download.assert_called_once_with(live)

    @pytest.mark.asyncio
    async def test_canvas_excludes_trash_from_suggestions(self, monkeypatch):
        import remarkable_mcp.api as api
        import remarkable_mcp.extract as extract
        from remarkable_mcp.app_canvas import _render_canvas_page

        trashed = _document("Meeting notes trash", "trash", parent="trash")
        live = _document("Meeting notes live", "live")
        client = Mock()
        client.get_meta_items.return_value = [trashed, live]
        candidates = []

        monkeypatch.setattr(api, "get_rmapi", lambda: client)
        monkeypatch.setattr(api, "get_items_by_id", lambda items: {item.ID: item for item in items})
        monkeypatch.setattr(extract, "get_background_color", lambda: "#FBFBFB")

        def find_similar(query, documents):
            candidates.extend(documents)
            return ["Meeting notes live"]

        monkeypatch.setattr(extract, "find_similar_documents", find_similar)

        result = await _render_canvas_page("Meting notes", 1, None)

        assert isinstance(result, str)
        assert candidates == [live]
        assert json.loads(result)["_error"]["did_you_mean"] == ["Meeting notes live"]


class _ResourceRecorder:
    def __init__(self):
        self.uris: list[str] = []

    def resource(self, uri, **kwargs):
        self.uris.append(uri)

        def register(function):
            return function

        return register


@pytest.fixture
def resource_registry(monkeypatch):
    import remarkable_mcp.resources as resources

    recorder = _ResourceRecorder()
    monkeypatch.setattr(resources, "mcp", recorder)
    monkeypatch.setattr(resources, "_registered_docs", set())
    monkeypatch.setattr(resources, "_registered_raw", set())
    monkeypatch.setattr(resources, "_registered_img", set())
    monkeypatch.setattr(resources, "_registered_uris", set())
    monkeypatch.setattr(resources, "_img_uri_to_doc", {})
    return resources, recorder


class TestArchivedResourceRegistration:
    def test_synchronous_registration_skips_trash(self, monkeypatch, resource_registry):
        import remarkable_mcp.api as api

        resources, recorder = resource_registry
        trashed = _document("Duplicate", "trash", parent="trash")
        live = _document("Duplicate", "live")
        client = Mock()
        client.get_meta_items.return_value = [trashed, live]
        client.get_all_file_types.return_value = {"trash": "notebook", "live": "notebook"}
        monkeypatch.setattr(api, "get_rmapi", lambda: client)

        assert resources.load_all_documents_sync() == 1
        assert resources._registered_docs == {"live"}
        assert all("_1" not in uri for uri in recorder.uris)

    @pytest.mark.asyncio
    async def test_background_registration_skips_trash(self, monkeypatch, resource_registry):
        import remarkable_mcp.api as api

        resources, recorder = resource_registry
        trashed = _document("Archived notes", "trash", parent="trash")
        live = _document("Live notes", "live")
        client = Mock()
        client.get_meta_items.return_value = [trashed, live]
        monkeypatch.setattr(api, "get_rmapi", lambda: client)

        await resources._load_documents_background(asyncio.Event())

        assert resources._registered_docs == {"live"}
        assert all(
            "Archived%20notes" not in uri and "Archived notes" not in uri for uri in recorder.uris
        )
