"""Pure exporter and temporary export-resource tests."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import fitz
import pytest
from PIL import Image

from remarkable_mcp.export_resources import ExportResourceStore
from remarkable_mcp.exporters import (
    ExportMetadata,
    RenderedPage,
    render_archive_pages,
    write_markdown_export,
    write_native_pdf_export,
    write_rendered_pdf_export,
)


def _metadata(*, source_type: str = "notebook", pages: int = 3) -> ExportMetadata:
    return ExportMetadata(
        document_id="11111111-2222-3333-4444-555555555555",
        title="Project Notes",
        path="/Work/Project Notes",
        source_type=source_type,
        page_count=pages,
        modified=datetime(2026, 8, 14, 12, 30, tzinfo=timezone.utc),
        tags=("work", "important"),
    )


def _png(color: str, size: tuple[int, int] = (200, 300)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, "PNG")
    return output.getvalue()


def _source_pdf() -> bytes:
    document = fitz.open()
    document.set_metadata({"author": "Original Author", "subject": "Original subject"})
    for text in ("first source page", "second source page"):
        page = document.new_page(width=200, height=300)
        page.insert_text((30, 40), text)
    data = document.tobytes()
    document.close()
    return data


def _archive(path: Path, content: dict, *, include_pdf: bool = False) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("doc.content", json.dumps(content))
        if include_pdf:
            archive.writestr("doc.pdf", _source_pdf())
    return path


class TestPdfExporters:
    def test_rendered_pdf_preserves_order_identity_and_failed_page(self, tmp_path):
        destination = tmp_path / "export.pdf"
        result = write_rendered_pdf_export(
            destination,
            _metadata(),
            [
                RenderedPage(page=1, image=_png("red")),
                RenderedPage(page=2, image=None, error="synthetic render failure"),
                RenderedPage(page=3, image=_png("blue")),
            ],
        )

        assert result.status == "partial"
        assert result.failed_pages == (2,)
        assert "synthetic render failure" in "\n".join(result.warnings)

        with fitz.open(destination) as document:
            assert len(document) == 3
            assert document.metadata["title"] == "Project Notes"
            assert _metadata().document_id in document.metadata["subject"]
            assert (
                f"remarkable-document-id:{_metadata().document_id}" in document.metadata["keywords"]
            )
            placeholder = document[1].get_text()
            assert "Physical page 2 could not be" in placeholder
            assert "synthetic render failure" in placeholder

            first = document[0].get_pixmap(matrix=fitz.Matrix(0.25, 0.25), alpha=False)
            third = document[2].get_pixmap(matrix=fitz.Matrix(0.25, 0.25), alpha=False)
            assert first.pixel(5, 5)[0] > first.pixel(5, 5)[2]
            assert third.pixel(5, 5)[2] > third.pixel(5, 5)[0]

    def test_native_pdf_preserves_pages_and_existing_metadata(self, tmp_path):
        destination = tmp_path / "native.pdf"
        result = write_native_pdf_export(
            _source_pdf(),
            destination,
            _metadata(source_type="pdf", pages=2),
        )

        assert result.status == "complete"
        assert result.pages == 2
        with fitz.open(destination) as document:
            assert [page.get_text().strip() for page in document] == [
                "first source page",
                "second source page",
            ]
            assert document.metadata["author"] == "Original Author"
            assert "Original subject" in document.metadata["subject"]
            assert _metadata().document_id in document.metadata["subject"]

    def test_rendered_pdf_rejects_non_sequential_pages(self, tmp_path):
        with pytest.raises(ValueError, match="expected 2"):
            write_rendered_pdf_export(
                tmp_path / "bad.pdf",
                _metadata(pages=2),
                [
                    RenderedPage(page=1, image=_png("white")),
                    RenderedPage(page=3, image=_png("white")),
                ],
            )

    def test_archive_pages_extract_once_and_preserve_physical_order(self, tmp_path):
        archive_path = _archive(
            tmp_path / "notebook.zip",
            {"fileType": "notebook", "pages": ["p1", "p2", "p3"]},
        )
        extracted_roots = []

        def render(root, page, background_color):
            assert (root / "doc.content").is_file()
            extracted_roots.append(root)
            if page == 2:
                raise ValueError("corrupt page")
            return _png(("red", "green", "blue")[page - 1]), (1404.0, 1872.0)

        with patch(
            "remarkable_mcp.exporters.render_page_full_page_from_extracted_document",
            side_effect=render,
        ):
            pages = list(render_archive_pages(archive_path, _metadata(), pdf_mode="merged"))

        assert [page.page for page in pages] == [1, 2, 3]
        assert pages[1].image is None
        assert "corrupt page" in pages[1].error
        assert pages[2].image is not None
        assert len({str(root) for root in extracted_roots}) == 1

    def test_pdf_archive_distinguishes_user_added_page_from_partial_failure(self, tmp_path):
        archive_path = _archive(
            tmp_path / "pdf.zip",
            {
                "fileType": "pdf",
                "cPages": {
                    "pages": [
                        {"id": "p1", "redir": {"value": 0}},
                        {"id": "p2"},
                        {"id": "p3", "redir": {"value": 1}},
                    ]
                },
            },
            include_pdf=True,
        )
        outcomes = [
            (_png("white"), None),
            (_png("white"), "Page has no PDF underlay (user-added page); annotation render."),
            (
                _png("white"),
                "Annotation overlay failed to render; returned PDF page without annotations.",
            ),
        ]

        with patch(
            "remarkable_mcp.exporters.render_merged_page_from_extracted_document",
            side_effect=outcomes,
        ):
            pages = list(
                render_archive_pages(
                    archive_path,
                    _metadata(source_type="pdf"),
                    pdf_mode="merged",
                )
            )

        assert pages[0].partial is False
        assert pages[1].partial is False
        assert pages[1].warning is None
        assert pages[2].partial is True


class TestMarkdownExporter:
    def test_markdown_uses_fixed_sections_and_verbatim_content(self, tmp_path):
        destination = tmp_path / "notes.md"
        extraction = {
            "typed_text": ["# Looks like a heading", "- looks like a list"],
            "highlights": ["Highlighted `phrase`"],
            "annotated_pages": [
                {
                    "page": 3,
                    "page_id": "p3",
                    "has_handwriting": True,
                    "highlights": [],
                },
                {
                    "page": 1,
                    "page_id": "p1",
                    "has_handwriting": False,
                    "highlights": ["Highlighted `phrase`"],
                },
            ],
            "handwritten_text": ["OCR result"],
            "ocr_backend": "tesseract",
            "pages": 3,
        }

        result = write_markdown_export(
            destination,
            _metadata(source_type="pdf"),
            source_text="## Source-like heading",
            extraction=extraction,
            include_ocr=True,
            warnings=["Source annotations were incomplete."],
        )
        text = destination.read_text()

        assert result.status == "partial"
        assert "remarkable_document_id: " in text
        assert _metadata().document_id in text
        assert 'export_status: "partial"' in text
        assert text.index("## Source text") < text.index("## Typed text")
        assert text.index("## Typed text") < text.index("## Annotations")
        assert text.index("## Annotations") < text.index("## Highlights")
        assert text.index("## Highlights") < text.index("## OCR")
        assert text.index("Physical page 1") < text.index("Physical page 3")
        assert "```text\n## Source-like heading\n```" in text
        assert "```text\n# Looks like a heading\n```" in text
        assert "Sparse OCR results" in text
        assert "### OCR excerpt 1" in text
        assert "## Export notes" in text

    def test_markdown_does_not_run_or_invent_ocr_when_not_requested(self, tmp_path):
        destination = tmp_path / "notes.md"
        result = write_markdown_export(
            destination,
            _metadata(pages=1),
            source_text=None,
            extraction={"pages": 1},
            include_ocr=False,
        )

        assert result.status == "complete"
        assert "_OCR was not requested for this export._" in destination.read_text()


class TestExportResourceStore:
    def test_expiry_and_safe_filename(self, tmp_path):
        elapsed = [0.0]
        base = datetime(2026, 8, 14, tzinfo=timezone.utc)
        store = ExportResourceStore(
            ttl_seconds=10,
            max_entries=2,
            root=tmp_path,
            monotonic_clock=lambda: elapsed[0],
            utcnow=lambda: base + timedelta(seconds=elapsed[0]),
        )

        published = store.publish(
            filename="../../Unsafe?.pdf",
            output_format="pdf",
            writer=lambda path: path.write_bytes(b"%PDF-export"),
        )
        assert published.resource.filename == "Unsafe_.pdf"
        assert store.read_bytes(published.resource.export_id, "pdf") == b"%PDF-export"

        elapsed[0] = 11
        with pytest.raises(FileNotFoundError, match="expired"):
            store.read_bytes(published.resource.export_id, "pdf")
        assert not any(tmp_path.rglob("Unsafe_.pdf"))

    def test_lru_eviction_updates_on_read(self, tmp_path):
        store = ExportResourceStore(ttl_seconds=60, max_entries=2, root=tmp_path)

        def publish(name):
            return store.publish(
                filename=name,
                output_format="markdown",
                writer=lambda path: path.write_text(name),
            )

        first = publish("first.md")
        second = publish("second.md")
        assert store.read_text(first.resource.export_id, "markdown") == "first.md"
        third = publish("third.md")

        with pytest.raises(FileNotFoundError):
            store.read_text(second.resource.export_id, "markdown")
        assert store.read_text(first.resource.export_id, "markdown") == "first.md"
        assert store.read_text(third.resource.export_id, "markdown") == "third.md"

    def test_failed_publish_and_cleanup_remove_managed_files(self, tmp_path):
        store = ExportResourceStore(ttl_seconds=60, max_entries=2, root=tmp_path)

        def fail(path):
            path.write_text("partial")
            raise RuntimeError("writer failed")

        with pytest.raises(RuntimeError, match="writer failed"):
            store.publish(filename="broken.md", output_format="markdown", writer=fail)
        assert list(tmp_path.rglob("*")) == []

        published = store.publish(
            filename="ready.md",
            output_format="markdown",
            writer=lambda path: path.write_text("ready"),
        )
        assert store.read_text(published.resource.export_id, "markdown") == "ready"
        store.cleanup()
        assert list(tmp_path.rglob("*")) == []
