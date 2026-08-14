"""Reusable PDF and Markdown exporters built on the existing render/extract paths."""

from __future__ import annotations

import io
import json
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, Literal, Optional

import fitz
from PIL import Image

from remarkable_mcp.extract import (
    _resolve_pdf_page_index,
    render_merged_page_from_extracted_document,
    render_page_full_page_from_extracted_document,
)

PdfMode = Literal["merged", "annotations"]
ExportStatus = Literal["complete", "partial"]


@dataclass(frozen=True)
class ExportMetadata:
    """Stable source metadata carried into every exported format."""

    document_id: str
    title: str
    path: str
    source_type: str
    page_count: Optional[int]
    modified: datetime | str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RenderedPage:
    """One physical page in export order."""

    page: int
    image: bytes | None
    warning: str | None = None
    error: str | None = None
    partial: bool = False


@dataclass(frozen=True)
class ExportBuildResult:
    """Outcome metadata returned alongside a generated file."""

    status: ExportStatus
    pages: int
    failed_pages: tuple[int, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    ocr_backend: str | None = None


def _unique_strings(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = str(value).strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return tuple(result)


def _modified_text(modified: datetime | str | None) -> str | None:
    if isinstance(modified, datetime):
        return modified.isoformat()
    if modified is None:
        return None
    value = str(modified).strip()
    return value or None


def _pdf_metadata(existing: dict, metadata: ExportMetadata) -> dict[str, str]:
    result = {key: str(value or "") for key, value in existing.items()}
    identity = (
        f"reMarkable document ID: {metadata.document_id}; "
        f"path: {metadata.path}; source type: {metadata.source_type}"
    )
    previous_subject = result.get("subject", "").strip()
    result["subject"] = f"{previous_subject}\n{identity}".strip()
    result["title"] = metadata.title

    keywords = [part.strip() for part in result.get("keywords", "").split(",") if part.strip()]
    keywords.extend(f"remarkable-document-id:{metadata.document_id}" for _ in range(1))
    keywords.extend(metadata.tags)
    result["keywords"] = ", ".join(_unique_strings(keywords))
    result["producer"] = "remarkable-mcp (PyMuPDF)"
    if not result.get("creator"):
        result["creator"] = "remarkable-mcp"
    return result


def write_native_pdf_export(
    pdf_bytes: bytes,
    destination: Path,
    metadata: ExportMetadata,
) -> ExportBuildResult:
    """Preserve an existing complete PDF while adding stable source metadata."""
    if not pdf_bytes.lstrip().startswith(b"%PDF"):
        raise ValueError("Native PDF export did not contain PDF data")

    with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
        page_count = len(document)
        if page_count == 0:
            raise ValueError("Native PDF export has no pages")
        document.set_metadata(_pdf_metadata(document.metadata, metadata))
        document.save(str(destination), garbage=3, deflate=True)

    return ExportBuildResult(status="complete", pages=page_count)


def _page_size(image_bytes: bytes) -> tuple[float, float]:
    with Image.open(io.BytesIO(image_bytes)) as image:
        width, height = image.size
    if width <= 0 or height <= 0:
        raise ValueError("Rendered page has invalid dimensions")
    # Existing merged rendering uses two pixels per PDF point. Applying the same
    # scale keeps source-PDF exports at their original physical dimensions.
    return max(1.0, width / 2), max(1.0, height / 2)


def write_rendered_pdf_export(
    destination: Path,
    metadata: ExportMetadata,
    pages: Iterable[RenderedPage],
) -> ExportBuildResult:
    """Assemble ordered rendered pages, retaining failed ordinals as placeholders."""
    output = fitz.open()
    failed_pages: list[int] = []
    warnings: list[str] = []
    expected_page = 1
    previous_size = (445.0, 594.0)
    partial = False

    try:
        for rendered in pages:
            if rendered.page != expected_page:
                raise ValueError(
                    f"Rendered pages must be sequential: expected {expected_page}, "
                    f"received {rendered.page}"
                )
            expected_page += 1

            if rendered.warning:
                warnings.append(f"Page {rendered.page}: {rendered.warning}")
            partial = partial or rendered.partial

            image_bytes = rendered.image
            if image_bytes is not None:
                page_count_before = len(output)
                try:
                    previous_size = _page_size(image_bytes)
                    page = output.new_page(width=previous_size[0], height=previous_size[1])
                    page.insert_image(page.rect, stream=image_bytes)
                    continue
                except Exception as exc:
                    while len(output) > page_count_before:
                        output.delete_page(page_count_before)
                    rendered = RenderedPage(
                        page=rendered.page,
                        image=None,
                        error=f"Rendered image could not be added to PDF: {exc}",
                        partial=True,
                    )

            partial = True
            failed_pages.append(rendered.page)
            reason = rendered.error or "Page could not be rendered"
            warnings.append(f"Page {rendered.page}: {reason}")
            page = output.new_page(width=previous_size[0], height=previous_size[1])
            message = (
                f"reMarkable export placeholder\n\n"
                f"Physical page {rendered.page} could not be rendered.\n\n{reason}"
            )
            margin = max(6.0, min(36.0, previous_size[0] * 0.08, previous_size[1] * 0.08))
            font_size = max(6.0, min(12.0, previous_size[0] / 30))
            remaining = page.insert_textbox(
                fitz.Rect(
                    margin,
                    margin,
                    previous_size[0] - margin,
                    previous_size[1] - margin,
                ),
                message,
                fontsize=font_size,
                color=(0.65, 0.0, 0.0),
            )
            if remaining < 0:
                page.insert_text(
                    (margin, margin + font_size),
                    message,
                    fontsize=max(5.0, font_size / 2),
                    color=(0.65, 0.0, 0.0),
                )

        page_count = len(output)
        if page_count == 0:
            raise ValueError("Document has no physical pages to export")
        if metadata.page_count is not None and page_count != metadata.page_count:
            raise ValueError(
                f"Export produced {page_count} pages but metadata declares "
                f"{metadata.page_count} physical pages"
            )

        output.set_metadata(_pdf_metadata({}, metadata))
        output.save(str(destination), garbage=3, deflate=True)
    finally:
        output.close()

    unique_warnings = _unique_strings(warnings)
    return ExportBuildResult(
        status="partial" if partial or failed_pages else "complete",
        pages=page_count,
        failed_pages=tuple(failed_pages),
        warnings=unique_warnings,
    )


def render_archive_pages(
    archive_path: Path,
    metadata: ExportMetadata,
    *,
    pdf_mode: PdfMode = "merged",
    background_color: str | None = None,
) -> Iterator[RenderedPage]:
    """Render every physical page from one archive extraction in device order."""
    if metadata.page_count is None or metadata.page_count <= 0:
        raise ValueError("Physical page count is unavailable")

    with tempfile.TemporaryDirectory(prefix="remarkable-export-archive-") as tmpdir:
        extracted = Path(tmpdir)
        with zipfile.ZipFile(archive_path, "r") as archive:
            archive.extractall(extracted)

        has_source_pdf = any(extracted.glob("**/*.pdf"))
        for page_number in range(1, metadata.page_count + 1):
            try:
                if pdf_mode == "merged" and metadata.source_type == "pdf" and has_source_pdf:
                    has_mapped_underlay = (
                        _resolve_pdf_page_index(extracted, page_number) is not None
                    )
                    image, note = render_merged_page_from_extracted_document(
                        extracted,
                        page=page_number,
                        background_color=background_color,
                    )
                    partial = image is None or bool(note and has_mapped_underlay)
                    yield RenderedPage(
                        page=page_number,
                        image=image,
                        warning=note if partial else None,
                        error=None if image is not None else note,
                        partial=partial,
                    )
                    continue

                full_page = render_page_full_page_from_extracted_document(
                    extracted,
                    page=page_number,
                    background_color=background_color,
                )
                warning = None
                partial = False
                if pdf_mode == "merged" and metadata.source_type in ("pdf", "epub"):
                    warning = (
                        f"{metadata.source_type.upper()} source underlay is unavailable; "
                        "exported the full-page annotation layer."
                    )
                    partial = True
                yield RenderedPage(
                    page=page_number,
                    image=full_page[0] if full_page is not None else None,
                    warning=warning,
                    error=None if full_page is not None else "Full-page annotation render failed",
                    partial=partial,
                )
            except Exception as exc:
                yield RenderedPage(
                    page=page_number,
                    image=None,
                    error=f"Page render raised {type(exc).__name__}: {exc}",
                    partial=True,
                )


def write_archive_pdf_export(
    archive_path: Path,
    destination: Path,
    metadata: ExportMetadata,
    *,
    pdf_mode: PdfMode = "merged",
    background_color: str | None = None,
) -> ExportBuildResult:
    """Render and assemble a PDF from a reMarkable document archive."""
    return write_rendered_pdf_export(
        destination,
        metadata,
        render_archive_pages(
            archive_path,
            metadata,
            pdf_mode=pdf_mode,
            background_color=background_color,
        ),
    )


def _yaml_value(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _verbatim_block(text: str) -> list[str]:
    longest = max((len(match) for match in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return [f"{fence}text", text.rstrip(), fence]


def _heading_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip() or "Untitled reMarkable document"


def write_markdown_export(
    destination: Path,
    metadata: ExportMetadata,
    *,
    source_text: str | None,
    extraction: dict | None,
    include_ocr: bool,
    warnings: Iterable[str] = (),
) -> ExportBuildResult:
    """Write an honest, sectioned Markdown representation of extracted content."""
    content = extraction or {}
    export_warnings = _unique_strings(warnings)
    status: ExportStatus = "partial" if export_warnings else "complete"
    modified = _modified_text(metadata.modified)

    lines = [
        "---",
        f"title: {_yaml_value(metadata.title)}",
        f"remarkable_document_id: {_yaml_value(metadata.document_id)}",
        f"remarkable_path: {_yaml_value(metadata.path)}",
        f"source_type: {_yaml_value(metadata.source_type)}",
        f"physical_pages: {_yaml_value(metadata.page_count)}",
        f"modified: {_yaml_value(modified)}",
        f"export_status: {_yaml_value(status)}",
    ]
    if metadata.tags:
        lines.append("tags:")
        lines.extend(f"  - {_yaml_value(tag)}" for tag in metadata.tags)
    else:
        lines.append("tags: []")
    lines.extend(["---", "", f"# {_heading_text(metadata.title)}", ""])

    lines.extend(["## Source text", ""])
    if source_text is None:
        if metadata.source_type == "notebook":
            lines.append("_Not applicable to a native notebook._")
        else:
            lines.append("_Source text was not available from this transport._")
    elif source_text.strip():
        lines.extend(_verbatim_block(source_text))
    else:
        lines.append("_No source text was extracted._")
    lines.append("")

    lines.extend(["## Typed text", ""])
    typed_text = [str(value) for value in content.get("typed_text") or [] if str(value).strip()]
    if typed_text:
        for index, value in enumerate(typed_text, 1):
            if len(typed_text) > 1:
                lines.extend([f"### Typed excerpt {index}", ""])
            lines.extend(_verbatim_block(value))
            lines.append("")
    else:
        lines.extend(["_No typed text was extracted._", ""])

    lines.extend(["## Annotations", ""])
    annotated_pages = sorted(
        content.get("annotated_pages") or [],
        key=lambda entry: int(entry.get("page") or 0),
    )
    if annotated_pages:
        for entry in annotated_pages:
            marks: list[str] = []
            if entry.get("has_handwriting"):
                marks.append("handwriting")
            highlight_count = len(entry.get("highlights") or [])
            if highlight_count:
                marks.append(f"{highlight_count} highlight" + ("s" if highlight_count != 1 else ""))
            page_id = entry.get("page_id")
            identity = f"; page ID `{page_id}`" if page_id else ""
            lines.append(
                f"- Physical page {entry.get('page')}{identity}: "
                + (", ".join(marks) or "annotated")
            )
    else:
        lines.append("_No annotated pages were identified._")
    lines.append("")

    lines.extend(["## Highlights", ""])
    highlights = [str(value) for value in content.get("highlights") or [] if str(value).strip()]
    if highlights:
        for index, value in enumerate(highlights, 1):
            lines.extend([f"### Highlight {index}", ""])
            lines.extend(_verbatim_block(value))
            lines.append("")
    else:
        lines.extend(["_No highlighted text was extracted._", ""])

    lines.extend(["## OCR", ""])
    ocr_backend = content.get("ocr_backend")
    if include_ocr:
        if ocr_backend:
            lines.extend([f"Backend: `{ocr_backend}`", ""])
        handwritten = [
            str(value) for value in content.get("handwritten_text") or [] if str(value).strip()
        ]
        if handwritten:
            lines.extend(
                [
                    "_OCR excerpts remain in extraction order. Sparse OCR results are "
                    "not assigned to physical pages when the extractor cannot prove "
                    "that mapping._",
                    "",
                ]
            )
            for index, value in enumerate(handwritten, 1):
                lines.extend([f"### OCR excerpt {index}", ""])
                lines.extend(_verbatim_block(value))
                lines.append("")
        else:
            lines.extend(["_No OCR text was extracted._", ""])
    else:
        lines.extend(["_OCR was not requested for this export._", ""])

    if export_warnings:
        lines.extend(["## Export notes", ""])
        lines.extend(f"- {warning}" for warning in export_warnings)
        lines.append("")

    destination.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    pages = metadata.page_count or int(content.get("pages") or 0)
    return ExportBuildResult(
        status=status,
        pages=pages,
        warnings=export_warnings,
        ocr_backend=str(ocr_backend) if ocr_backend else None,
    )
