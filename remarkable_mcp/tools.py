"""
MCP Tools for reMarkable tablet access.

All tools in this module are read-only and idempotent - they only retrieve
data from your reMarkable (via whichever transport is active: cloud, SSH, or
USB web) and do not modify any documents.
"""

import base64
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Literal, Optional

from mcp.server.fastmcp import Context
from mcp.types import (
    BlobResourceContents,
    EmbeddedResource,
    TextContent,
    TextResourceContents,
    ToolAnnotations,
)

from remarkable_mcp.api import (
    REMARKABLE_TOKEN,
    download_raw_file,
    get_file_type,
    get_item_path,
    get_items_by_id,
    get_items_by_parent,
    get_rmapi,
)
from remarkable_mcp.concurrency import run_blocking
from remarkable_mcp.extract import (
    cache_page_ocr,
    extract_text_from_document_zip,
    extract_text_from_epub,
    extract_text_from_pdf,
    find_similar_documents,
    get_background_color,
    get_cached_ocr_result,
    get_cached_page_ocr,
    get_document_page_count,
    render_merged_page_from_document_zip,
    render_page_from_document_zip,
    render_page_from_document_zip_svg,
    render_page_full_page_from_document_zip,
    render_tablet_pdf_page_to_png,
)
from remarkable_mcp.responses import make_error, make_response
from remarkable_mcp.sampling import (
    get_ocr_backend,
    ocr_via_sampling,
    should_use_sampling_ocr,
)
from remarkable_mcp.server import mcp


def _get_root_path() -> str:
    """Get the configured root path filter, or '/' for full access.

    Handles: empty string, '/', '/Work', '/Work/', 'Work' -> normalized path
    """
    root = os.environ.get("REMARKABLE_ROOT_PATH", "").strip()
    # Empty or "/" means full access
    if not root or root == "/":
        return "/"
    # Normalize: ensure starts with / and no trailing slash
    if not root.startswith("/"):
        root = "/" + root
    if root.endswith("/"):
        root = root.rstrip("/")
    return root


def _is_within_root(path: str, root: str) -> bool:
    """Check if a path is within the configured root (case-insensitive)."""
    if root == "/":
        return True
    # Path must equal root or be a child of root (case-insensitive)
    path_lower = path.lower()
    root_lower = root.lower()
    return path_lower == root_lower or path_lower.startswith(root_lower + "/")


def _apply_root_filter(path: str) -> str:
    """Apply root filter to a path for display/API purposes.

    If root is '/Work', then '/Work/Project' becomes '/Project' in output.
    Case-insensitive matching, preserves original case in output.
    """
    root = _get_root_path()
    if root == "/":
        return path
    path_lower = path.lower()
    root_lower = root.lower()
    if path_lower == root_lower:
        return "/"
    if path_lower.startswith(root_lower + "/"):
        return path[len(root) :]
    return path


def _resolve_root_path(path: str) -> str:
    """Resolve a user-provided path to the actual path on device.

    If root is '/Work', then '/Project' becomes '/Work/Project'.
    """
    root = _get_root_path()
    if root == "/":
        return path
    if path == "/":
        return root
    # Prepend root to the path
    return root + path


# Base annotations for read-only operations
_BASE_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,  # Private cloud account, not open world
}

# Unique annotations for each tool with descriptive titles
READ_ANNOTATIONS = ToolAnnotations(
    title="Read reMarkable Document",
    **_BASE_ANNOTATIONS,
)

BROWSE_ANNOTATIONS = ToolAnnotations(
    title="Browse reMarkable Library",
    **_BASE_ANNOTATIONS,
)

SEARCH_ANNOTATIONS = ToolAnnotations(
    title="Search reMarkable Documents",
    **_BASE_ANNOTATIONS,
)

REVIEW_ANNOTATIONS = ToolAnnotations(
    title="Review/export reMarkable Documents",
    **_BASE_ANNOTATIONS,
)

RECENT_ANNOTATIONS = ToolAnnotations(
    title="Get Recent reMarkable Documents",
    **_BASE_ANNOTATIONS,
)

STATUS_ANNOTATIONS = ToolAnnotations(
    title="Check reMarkable Connection",
    **_BASE_ANNOTATIONS,
)

IMAGE_ANNOTATIONS = ToolAnnotations(
    title="Get reMarkable Page Image",
    **_BASE_ANNOTATIONS,
)

# Default page size for pagination (characters) - used for PDFs/EPUBs
DEFAULT_PAGE_SIZE = 8000


def _is_cloud_archived(item) -> bool:
    """Check if an item is cloud-archived (not available on device)."""
    # SSH mode: check is_cloud_archived property
    if hasattr(item, "is_cloud_archived"):
        return item.is_cloud_archived
    # Cloud mode: check parent == "trash"
    parent = item.Parent if hasattr(item, "Parent") else getattr(item, "parent", "")
    return parent == "trash"


def _modified_sort_key(item) -> float:
    """Return a sortable timestamp for an item's modified date.

    Normalizes ``ModifiedClient`` to a float epoch timestamp so documents with a
    missing/null modified date (e.g. freshly-created notebooks) sort to the
    bottom instead of crashing the comparator.

    A plain sentinel ``datetime`` cannot be used here: USB ``ModifiedClient``
    values are timezone-aware (parsed via ``fromisoformat``) while cloud and SSH
    values are naive (parsed via ``fromtimestamp``), and Python refuses to
    compare naive and aware datetimes. Reducing everything to ``.timestamp()``
    sidesteps both the str/datetime and the naive/aware comparison errors.
    """
    modified = getattr(item, "ModifiedClient", None)
    if isinstance(modified, datetime):
        try:
            return modified.timestamp()
        except (OverflowError, OSError, ValueError):
            return 0.0
    return 0.0


def _ocr_png_tesseract(png_path: Path) -> Optional[str]:
    """
    OCR a PNG file using Tesseract.

    Args:
        png_path: Path to the PNG file

    Returns:
        Extracted text, or None if OCR failed
    """
    try:
        import pytesseract
        from PIL import Image as PILImage
        from PIL import ImageFilter, ImageOps

        img = PILImage.open(png_path)

        # Convert to grayscale
        img = img.convert("L")

        # Increase contrast
        img = ImageOps.autocontrast(img, cutoff=2)

        # Slight sharpening
        img = img.filter(ImageFilter.SHARPEN)

        # Run OCR with settings optimized for sparse handwriting
        custom_config = r"--psm 11 --oem 3"
        text = pytesseract.image_to_string(img, config=custom_config)

        return text.strip() if text.strip() else None

    except ImportError:
        return None
    except Exception:
        return None


def _ocr_png_google_vision(png_path: Path) -> Optional[str]:
    """
    OCR a PNG file using Google Cloud Vision API.

    Args:
        png_path: Path to the PNG file

    Returns:
        Extracted text, or None if OCR failed
    """
    import base64

    import requests

    api_key = os.environ.get("GOOGLE_VISION_API_KEY")
    if not api_key:
        return None

    try:
        with open(png_path, "rb") as f:
            image_content = base64.b64encode(f.read()).decode("utf-8")

        url = f"https://vision.googleapis.com/v1/images:annotate?key={api_key}"
        payload = {
            "requests": [
                {
                    "image": {"content": image_content},
                    "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                }
            ]
        }

        response = requests.post(url, json=payload, timeout=60)
        if response.status_code == 200:
            data = response.json()
            if "responses" in data and data["responses"]:
                resp = data["responses"][0]
                if "fullTextAnnotation" in resp:
                    text = resp["fullTextAnnotation"]["text"]
                    return text.strip() if text.strip() else None

    except Exception:
        # Silently fail - OCR is best-effort and caller will handle None
        pass

    return None


@mcp.tool(annotations=READ_ANNOTATIONS)
async def remarkable_read(
    document: str,
    content_type: Literal["text", "raw", "annotations"] = "text",
    page: int = 1,
    grep: Optional[str] = None,
    include_ocr: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """
    <usecase>Read and extract text content from a reMarkable document.</usecase>
    <instructions>
    Extracts content from a document with pagination to preserve context window.

    Content types:
    - "text" (default): Full extracted text (PDF/EPUB content + annotations)
    - "raw": Original PDF/EPUB text only (no annotations). Works in every
      transport, as long as the source file is present (very large PDFs/EPUBs
      may not be synced to the cloud).
    - "annotations": Only annotations, highlights, and handwritten notes

    Use pagination to read large documents without overwhelming context:
    - Start with page=1 (default)
    - Check "more" field - if true, there's more content
    - Use "next_page" value to get the next page

    Use grep to search for specific content on the current page.

    When REMARKABLE_OCR_BACKEND=sampling is set and the client supports sampling,
    OCR will use the client's LLM for handwriting recognition (no API keys needed).
    </instructions>
    <parameters>
    - document: Document name or path (use remarkable_browse to find documents)
    - content_type: "text" (full), "raw" (PDF/EPUB only), "annotations" (notes only)
    - page: Page number (default: 1). For notebooks, this is the notebook page.
    - grep: Optional regex pattern to filter content (searches current page)
    - include_ocr: Enable handwriting OCR for annotations (default: False)
    </parameters>
    <examples>
    - remarkable_read("Meeting Notes")  # Get first page of text
    - remarkable_read("Book.pdf", content_type="raw")  # Get raw PDF text
    - remarkable_read("Notes", content_type="annotations")  # Only annotations
    - remarkable_read("Report", page=2)  # Get second page
    - remarkable_read("Manual", grep="installation")  # Search for keyword
    </examples>
    """
    try:
        client = get_rmapi()
        collection = await run_blocking(client.get_meta_items)
        items_by_id = get_items_by_id(collection)

        # Validate parameters
        page = max(1, page)
        # Internal page size for PDF/EPUB character-based pagination
        page_size = DEFAULT_PAGE_SIZE

        root = _get_root_path()
        # Resolve user-provided path to actual device path
        actual_document = _resolve_root_path(document) if document.startswith("/") else document

        # Find the document by name or path (case-insensitive, not folders)
        documents = [item for item in collection if not item.is_folder]
        target_doc = None
        document_lower = actual_document.lower().strip("/")

        for doc in documents:
            doc_path = get_item_path(doc, items_by_id)
            # Filter by root path
            if not _is_within_root(doc_path, root):
                continue
            # Match by name (case-insensitive)
            if doc.VissibleName.lower() == document_lower:
                target_doc = doc
                break
            # Also try matching by full path (case-insensitive)
            if doc_path.lower().strip("/") == document_lower:
                target_doc = doc
                break

        if not target_doc:
            # Find similar documents for suggestion (only within root)
            filtered_docs = [
                doc for doc in documents if _is_within_root(get_item_path(doc, items_by_id), root)
            ]
            similar = find_similar_documents(document, filtered_docs)
            search_term = document.split()[0] if document else "notes"
            return make_error(
                error_type="document_not_found",
                message=f"Document not found: '{document}'",
                suggestion=(
                    f"Try remarkable_browse(query='{search_term}') to search, "
                    "or remarkable_browse('/') to list all files."
                ),
                did_you_mean=similar if similar else None,
            )

        doc_path = get_item_path(target_doc, items_by_id)
        file_type = await run_blocking(get_file_type, client, target_doc)

        # Collect content based on content_type
        text_parts = []
        raw_available = False

        # Get raw PDF/EPUB content if requested or for "text" mode
        if content_type in ("text", "raw") and file_type in ("pdf", "epub"):
            raw_data = await run_blocking(download_raw_file, client, target_doc, file_type)
            if raw_data:
                raw_available = True
                with tempfile.NamedTemporaryFile(suffix=f".{file_type}", delete=False) as tmp:
                    tmp.write(raw_data)
                    tmp_path = Path(tmp.name)
                try:
                    if file_type == "pdf":
                        raw_text = await run_blocking(extract_text_from_pdf, tmp_path)
                    else:
                        raw_text = await run_blocking(extract_text_from_epub, tmp_path)
                    if raw_text:
                        text_parts.append(raw_text)
                finally:
                    tmp_path.unlink(missing_ok=True)
            elif content_type == "raw":
                # The document has no source PDF/EPUB blob (e.g. a pure notebook).
                return make_error(
                    error_type="raw_not_available",
                    message=f"No raw {file_type.upper()} source file found for this document",
                    suggestion=(
                        "Use content_type='text' for extracted content, "
                        "or content_type='annotations' for handwritten notes."
                    ),
                )

        # Get annotations/typed text (for "text" or "annotations" mode)
        notebook_pages = []  # List of page content for notebook pagination
        ocr_backend_used = None  # Track which OCR backend was used
        content = None  # Will hold extraction result
        total_notebook_pages = 0  # Track total pages for sampling mode

        if content_type in ("text", "annotations"):
            # For notebooks (no PDF/EPUB), use page-based pagination
            is_notebook = file_type not in ("pdf", "epub")

            # Determine if we should use sampling OCR
            use_sampling = is_notebook and include_ocr and ctx and should_use_sampling_ocr(ctx)

            # For sampling OCR: use per-page caching and only OCR requested page
            if use_sampling:
                # Check per-page cache first
                cached_text = await run_blocking(
                    get_cached_page_ocr, target_doc.ID, page, "sampling"
                )
                if cached_text is not None:
                    # We have cached OCR for this page
                    # Still need to get total page count
                    raw_doc = await run_blocking(client.download, target_doc)
                    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                        tmp.write(raw_doc)
                        tmp_path = Path(tmp.name)
                    try:
                        total_notebook_pages = await run_blocking(get_document_page_count, tmp_path)
                    finally:
                        tmp_path.unlink(missing_ok=True)

                    # Build notebook_pages list with just the cached page
                    notebook_pages = [""] * total_notebook_pages
                    notebook_pages[page - 1] = cached_text
                    ocr_backend_used = "sampling"
                else:
                    # No cache - render and OCR just the requested page
                    raw_doc = await run_blocking(client.download, target_doc)
                    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                        tmp.write(raw_doc)
                        tmp_path = Path(tmp.name)

                    try:
                        total_notebook_pages = await run_blocking(get_document_page_count, tmp_path)

                        if page > total_notebook_pages:
                            return make_error(
                                error_type="page_out_of_range",
                                message=f"Page {page} does not exist. "
                                f"Document has {total_notebook_pages} notebook page(s).",
                                suggestion=f"Use page=1 to {total_notebook_pages} "
                                "to read different pages.",
                            )

                        # Render just the requested page
                        png_data = await run_blocking(render_page_from_document_zip, tmp_path, page)
                        if png_data:
                            # OCR the single page
                            ocr_text = await ocr_via_sampling(ctx, png_data)
                            if ocr_text:
                                # Cache the result
                                await run_blocking(
                                    cache_page_ocr,
                                    target_doc.ID,
                                    page,
                                    "sampling",
                                    ocr_text,
                                )
                                # Build notebook_pages list
                                notebook_pages = [""] * total_notebook_pages
                                notebook_pages[page - 1] = ocr_text
                                ocr_backend_used = "sampling"
                    finally:
                        tmp_path.unlink(missing_ok=True)

            # For non-sampling: check full document cache or extract all
            if not use_sampling and is_notebook and include_ocr:
                cached = await run_blocking(
                    get_cached_ocr_result,
                    target_doc.ID,
                    include_ocr=True,
                    ocr_backend=None,
                )
                if cached and cached.get("handwritten_text"):
                    notebook_pages = cached["handwritten_text"]
                    ocr_backend_used = cached.get("ocr_backend")
                    content = cached

            # If not cached (non-sampling), perform extraction
            if not notebook_pages and is_notebook:
                raw_doc = await run_blocking(client.download, target_doc)
                with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                    tmp.write(raw_doc)
                    tmp_path = Path(tmp.name)

                try:
                    content = await run_blocking(
                        extract_text_from_document_zip,
                        tmp_path,
                        include_ocr=include_ocr,
                        doc_id=target_doc.ID,
                    )
                    if content.get("handwritten_text"):
                        notebook_pages = content["handwritten_text"]
                        ocr_backend_used = content.get("ocr_backend")
                finally:
                    tmp_path.unlink(missing_ok=True)

            # For non-notebooks or when no OCR pages, build annotation sections
            if not (is_notebook and notebook_pages):
                if content is None:
                    # Need to extract if we haven't already
                    raw_doc = await run_blocking(client.download, target_doc)
                    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                        tmp.write(raw_doc)
                        tmp_path = Path(tmp.name)
                    try:
                        content = await run_blocking(
                            extract_text_from_document_zip,
                            tmp_path,
                            include_ocr=include_ocr,
                            doc_id=target_doc.ID,
                        )
                    finally:
                        tmp_path.unlink(missing_ok=True)

                # Add annotations section
                annotation_parts = []
                if content.get("typed_text"):
                    annotation_parts.extend(content["typed_text"])
                if content.get("highlights"):
                    annotation_parts.append("\n--- Highlights ---")
                    annotation_parts.extend(content["highlights"])
                if content.get("handwritten_text"):
                    annotation_parts.append("\n--- Handwritten (OCR) ---")
                    annotation_parts.extend(content["handwritten_text"])

                if annotation_parts:
                    if text_parts and content_type == "text":
                        text_parts.append("\n\n=== Annotations ===\n")
                    text_parts.extend(annotation_parts)

        # For notebooks with OCR: use page-based pagination
        if notebook_pages:
            total_pages = len(notebook_pages)

            if page > total_pages:
                return make_error(
                    error_type="page_out_of_range",
                    message=f"Page {page} does not exist. "
                    f"Document has {total_pages} notebook page(s).",
                    suggestion=f"Use page=1 to {total_pages} to read different pages.",
                )

            page_content = notebook_pages[page - 1]
            has_more = page < total_pages

            # Apply grep filter if specified
            grep_matches = 0
            if grep:
                try:
                    pattern = re.compile(grep, re.IGNORECASE | re.MULTILINE)
                    if not pattern.search(page_content):
                        # No match on this page, search all pages
                        matching_pages = []
                        for i, pg in enumerate(notebook_pages, 1):
                            if pattern.search(pg):
                                matching_pages.append(i)
                        if matching_pages:
                            return make_error(
                                error_type="no_match_on_page",
                                message=f"No match for '{grep}' on page {page}.",
                                suggestion=f"Matches found on page(s): {matching_pages}. "
                                f"Try remarkable_read('{document}', "
                                f"page={matching_pages[0]}, grep='{grep}').",
                            )
                        else:
                            return make_error(
                                error_type="no_grep_matches",
                                message=f"No matches for '{grep}' in document.",
                                suggestion="Try a different search term.",
                            )
                    grep_matches = len(pattern.findall(page_content))
                except re.error as e:
                    return make_error(
                        error_type="invalid_grep",
                        message=f"Invalid regex pattern: {e}",
                        suggestion="Use a valid regex pattern or simple text string.",
                    )

            result = {
                "document": target_doc.VissibleName,
                "path": _apply_root_filter(doc_path),
                "file_type": "notebook",
                "content_type": content_type,
                "content": page_content,
                "page": page,
                "total_pages": total_pages,
                "page_type": "notebook",
                "total_chars": len(page_content),
                "more": has_more,
                "modified": (
                    target_doc.ModifiedClient if hasattr(target_doc, "ModifiedClient") else None
                ),
            }

            # Add OCR backend info if OCR was used
            if include_ocr and ocr_backend_used:
                result["ocr_backend"] = ocr_backend_used

            if grep:
                result["grep"] = grep
                result["grep_matches"] = grep_matches

            hint_parts = [f"Notebook page {page}/{total_pages}."]
            if has_more:
                doc_name = target_doc.VissibleName
                hint_parts.append(f"Next: remarkable_read('{doc_name}', page={page + 1}).")
            else:
                hint_parts.append("(last page)")
            if grep_matches:
                hint_parts.insert(0, f"Found {grep_matches} match(es) for '{grep}'.")

            return make_response(result, " ".join(hint_parts))

        # Combine all content
        full_text = "\n\n".join(text_parts) if text_parts else ""
        total_chars = len(full_text)

        # Apply grep filter if specified
        grep_matches = 0
        if grep and full_text:
            try:
                pattern = re.compile(grep, re.IGNORECASE | re.MULTILINE)
                # Find all matches and include context
                matches = []
                for match in pattern.finditer(full_text):
                    start = max(0, match.start() - 100)
                    end = min(len(full_text), match.end() + 100)
                    context = full_text[start:end]
                    # Add ellipsis if truncated
                    if start > 0:
                        context = "..." + context
                    if end < len(full_text):
                        context = context + "..."
                    matches.append(context)
                    grep_matches += 1

                if matches:
                    full_text = "\n\n---\n\n".join(matches)
                    total_chars = len(full_text)
                else:
                    full_text = ""
                    total_chars = 0
            except re.error as e:
                return make_error(
                    error_type="invalid_grep",
                    message=f"Invalid regex pattern: {e}",
                    suggestion="Use a valid regex pattern or simple text string.",
                )

        # Apply pagination
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size

        # Handle empty content case - auto-retry with OCR if not already enabled
        if total_chars == 0 and not include_ocr and file_type not in ("pdf", "epub"):
            # Auto-retry with OCR for notebooks
            import json

            ocr_result = await remarkable_read(
                document=document,
                content_type=content_type,
                page=page,
                grep=grep,
                include_ocr=True,  # Enable OCR automatically
                ctx=ctx,  # Pass context for sampling OCR
            )
            result_data = json.loads(ocr_result)
            if "_error" not in result_data:
                result_data["_ocr_auto_enabled"] = True
                result_data["_hint"] = (
                    "OCR auto-enabled (notebook had no typed text). " + result_data.get("_hint", "")
                )
            return json.dumps(result_data, indent=2)

        if total_chars == 0:
            if page > 1:
                return make_error(
                    error_type="page_out_of_range",
                    message=f"Page {page} does not exist. Document has 1 page(s).",
                    suggestion="Use page=1 to start from the beginning.",
                )
            # Return empty result for page 1
            result = {
                "document": target_doc.VissibleName,
                "path": _apply_root_filter(doc_path),
                "file_type": file_type or "notebook",
                "content_type": content_type,
                "content": "",
                "page": 1,
                "total_pages": 1,
                "total_chars": 0,
                "more": False,
                "modified": (
                    target_doc.ModifiedClient if hasattr(target_doc, "ModifiedClient") else None
                ),
            }
            hint = (
                f"Document '{target_doc.VissibleName}' has no extractable text content. "
                "This may be a handwritten notebook - try include_ocr=True for OCR extraction."
            )
            return make_response(result, hint)

        if start_idx >= total_chars:
            # Page out of range
            total_pages = max(1, (total_chars + page_size - 1) // page_size)
            return make_error(
                error_type="page_out_of_range",
                message=f"Page {page} does not exist. Document has {total_pages} page(s).",
                suggestion="Use page=1 to start from the beginning.",
            )

        page_content = full_text[start_idx:end_idx]
        has_more = end_idx < total_chars
        total_pages = max(1, (total_chars + page_size - 1) // page_size)

        result = {
            "document": target_doc.VissibleName,
            "path": _apply_root_filter(doc_path),
            "file_type": file_type or "notebook",
            "content_type": content_type,
            "content": page_content,
            "page": page,
            "total_pages": total_pages,
            "total_chars": total_chars,
            "more": has_more,
            "modified": (
                target_doc.ModifiedClient if hasattr(target_doc, "ModifiedClient") else None
            ),
        }

        # Add tags if present (from listing metadata, or from extraction in USB mode)
        tags = getattr(target_doc, "tags", None) or (content.get("tags") if content else None)
        if tags:
            result["tags"] = tags

        if has_more:
            result["next_page"] = page + 1

        if grep:
            result["grep"] = grep
            result["grep_matches"] = grep_matches

        # Build contextual hint
        hint_parts = []

        if grep:
            if grep_matches > 0:
                hint_parts.append(f"Found {grep_matches} match(es) for '{grep}'.")
            else:
                hint_parts.append(f"No matches for '{grep}' on this page.")
                if has_more:
                    hint_parts.append("Try searching other pages.")

        if has_more:
            hint_parts.append(
                f"Page {page}/{total_pages}. Next: remarkable_read('{document}', page={page + 1})"
            )
        else:
            hint_parts.append(f"Page {page}/{total_pages} (complete).")

        if content_type == "text" and not raw_available and file_type in ("pdf", "epub"):
            hint_parts.append(
                "No source PDF/EPUB blob was found for this document, so only "
                "annotations were extracted. Very large files may not be synced "
                "to the cloud; try SSH/USB mode with the device connected."
            )

        return make_response(result, " ".join(hint_parts))

    except Exception as e:
        return make_error(
            error_type="read_failed",
            message=str(e),
            suggestion="Check remarkable_status() to verify your connection.",
        )


@mcp.tool(annotations=BROWSE_ANNOTATIONS)
async def remarkable_browse(
    path: str = "/", query: Optional[str] = None, tags: Optional[List[str]] = None
) -> str:
    """
    <usecase>Browse your reMarkable library or search for documents.</usecase>
    <instructions>
    Three modes:
    1. Browse mode (default): List contents of a folder
       - Use path="/" for root folder
       - Use path="/FolderName" to navigate into folders
    2. Search mode: Find documents by name
       - Set query="search term" to search across all documents
    3. Filter by tags: Find documents with specific tags
       - Set tags=["tag1", "tag2"] to filter by tags
       - Works in both browse and search modes

    Results include document names, types, modification dates, and tags.

    Note: If REMARKABLE_ROOT_PATH is configured, only documents within that
    folder are accessible. Paths are relative to the root path.
    </instructions>
    <parameters>
    - path: Folder path to browse (default: "/" for root)
    - query: Search term to find documents by name (optional, triggers search mode)
    - tags: List of tags to filter documents (optional, case-insensitive)
    </parameters>
    <examples>
    - remarkable_browse()  # List root folder
    - remarkable_browse("/Work")  # List Work folder
    - remarkable_browse(query="meeting")  # Search for "meeting"
    - remarkable_browse(tags=["important"])  # Show documents with "important" tag
    - remarkable_browse(query="project", tags=["work"])  # Search with tag filter
    </examples>
    """
    try:
        client = get_rmapi()
        collection = await run_blocking(client.get_meta_items)
        items_by_id = get_items_by_id(collection)
        items_by_parent = get_items_by_parent(collection)

        root = _get_root_path()
        # Resolve user path to actual device path
        actual_path = _resolve_root_path(path)

        # Search mode
        if query:
            query_lower = query.lower()
            matches = []

            for item in collection:
                # Skip cloud-archived items
                if _is_cloud_archived(item):
                    continue
                item_path = get_item_path(item, items_by_id)
                # Filter by root path
                if not _is_within_root(item_path, root):
                    continue
                # Filter by tags if provided
                if tags:
                    item_tags_lower = [
                        t.lower() for t in (item.tags if hasattr(item, "tags") else [])
                    ]
                    if not any(tag.lower() in item_tags_lower for tag in tags):
                        continue
                if query_lower in item.VissibleName.lower():
                    match_info = {
                        "name": item.VissibleName,
                        "path": _apply_root_filter(item_path),
                        "type": "folder" if item.is_folder else "document",
                        "modified": (
                            item.ModifiedClient if hasattr(item, "ModifiedClient") else None
                        ),
                    }
                    # Add tags if present
                    if hasattr(item, "tags") and item.tags:
                        match_info["tags"] = item.tags
                    matches.append(match_info)

            matches.sort(key=lambda x: x["name"])

            result = {"mode": "search", "query": query, "count": len(matches), "results": matches}
            if tags:
                result["filter_tags"] = tags

            if matches:
                first_doc = next((m for m in matches if m["type"] == "document"), None)
                if first_doc:
                    hint = (
                        f"Found {len(matches)} results. "
                        f"To read a document: remarkable_read('{first_doc['name']}')."
                    )
                else:
                    hint = (
                        f"Found {len(matches)} folders. "
                        f"To browse one: remarkable_browse('{matches[0]['path']}')."
                    )
            else:
                filter_desc = f" with tags {tags}" if tags else ""
                hint = (
                    f"No results for '{query}'{filter_desc}. "
                    "Try remarkable_browse('/') to see all files, "
                    "or use a different search term."
                )

            return make_response(result, hint)

        # Browse mode - use actual_path (with root applied)
        if actual_path == "/" or actual_path == "":
            target_parent = ""
        else:
            # Navigate to the folder (case-insensitive)
            path_parts = [p for p in actual_path.strip("/").split("/") if p]
            current_parent = ""

            for i, part in enumerate(path_parts):
                part_lower = part.lower()
                found = False
                found_document = None

                for item in items_by_parent.get(current_parent, []):
                    if item.VissibleName.lower() == part_lower:
                        if item.is_folder:
                            current_parent = item.ID
                            found = True
                            break
                        else:
                            # Found a document with this name
                            found_document = item

                if not found:
                    # Check if it's a document (only valid as the last path part)
                    if found_document and i == len(path_parts) - 1:
                        # Auto-redirect: return first page of the document
                        doc_path = get_item_path(found_document, items_by_id)
                        # Check if within root before redirecting
                        if not _is_within_root(doc_path, root):
                            return make_error(
                                error_type="access_denied",
                                message=(
                                    f"Document '{found_document.VissibleName}' "
                                    "is outside the configured root path."
                                ),
                                suggestion="Check REMARKABLE_ROOT_PATH configuration.",
                            )
                        # Call remarkable_read internally and add redirect note
                        read_result = remarkable_read(_apply_root_filter(doc_path), page=1)
                        import json

                        result_data = json.loads(read_result)
                        if "_error" not in result_data:
                            result_data["_redirected_from"] = f"browse:{path}"
                            result_data["_hint"] = (
                                f"Auto-redirected from browse to read. "
                                f"{result_data.get('_hint', '')}"
                            )
                        return json.dumps(result_data, indent=2)

                    # Folder not found - suggest alternatives
                    available_folders = [
                        item.VissibleName
                        for item in items_by_parent.get(current_parent, [])
                        if item.is_folder
                    ]
                    available_docs = [
                        item.VissibleName
                        for item in items_by_parent.get(current_parent, [])
                        if not item.is_folder
                    ]
                    suggestion = "Use remarkable_browse('/') to see root folder contents."
                    if available_docs:
                        # Check if user might be looking for a document
                        for doc_name in available_docs:
                            if doc_name.lower() == part_lower:
                                suggestion = (
                                    f"'{doc_name}' is a document. "
                                    f"Use remarkable_read('{doc_name}') to read it."
                                )
                                break
                    return make_error(
                        error_type="folder_not_found",
                        message=f"Folder not found: '{part}'",
                        suggestion=suggestion,
                        did_you_mean=(available_folders[:5] if available_folders else None),
                    )

            target_parent = current_parent

        items = items_by_parent.get(target_parent, [])

        folders = []
        documents = []

        for item in sorted(items, key=lambda x: x.VissibleName.lower()):
            # Skip cloud-archived items
            if _is_cloud_archived(item):
                continue
            # Filter by tags if provided
            if tags and not item.is_folder:
                item_tags_lower = [t.lower() for t in (item.tags if hasattr(item, "tags") else [])]
                if not any(tag.lower() in item_tags_lower for tag in tags):
                    continue
            if item.is_folder:
                folders.append({"name": item.VissibleName, "id": item.ID})
            else:
                doc_info = {
                    "name": item.VissibleName,
                    "id": item.ID,
                    "modified": (item.ModifiedClient if hasattr(item, "ModifiedClient") else None),
                }
                # Add tags if present
                if hasattr(item, "tags") and item.tags:
                    doc_info["tags"] = item.tags
                documents.append(doc_info)

        result = {"mode": "browse", "path": path, "folders": folders, "documents": documents}
        if tags:
            result["filter_tags"] = tags

        # Build helpful hint
        tag_desc = f" with tags {tags}" if tags else ""
        hint_parts = [f"Found {len(folders)} folder(s) and {len(documents)} document(s){tag_desc}."]

        if documents:
            hint_parts.append(f"To read a document: remarkable_read('{documents[0]['name']}').")
        if folders:
            folder_path = f"{path.rstrip('/')}/{folders[0]['name']}"
            hint_parts.append(f"To enter a folder: remarkable_browse('{folder_path}').")
        if not folders and not documents:
            hint_parts.append("This folder is empty.")

        return make_response(result, " ".join(hint_parts))

    except Exception as e:
        return make_error(
            error_type="browse_failed",
            message=str(e),
            suggestion="Check remarkable_status() to verify your connection.",
        )


@mcp.tool(annotations=RECENT_ANNOTATIONS)
async def remarkable_recent(limit: int = 10, include_preview: bool = False) -> str:
    """
    <usecase>Get your most recently modified documents.</usecase>
    <instructions>
    Returns documents sorted by modification date (newest first).
    Optionally includes a text preview of each document's content.

    Use this to quickly find what you were working on recently.

    Note: If REMARKABLE_ROOT_PATH is configured, only documents within that
    folder are included.
    </instructions>
    <parameters>
    - limit: Maximum documents to return (default: 10, max: 50 without preview, 10 with preview)
    - include_preview: Include first ~200 chars of text content (default: False)
    </parameters>
    <examples>
    - remarkable_recent()  # Last 10 documents
    - remarkable_recent(limit=5, include_preview=True)  # With content preview
    </examples>
    """
    try:
        client = get_rmapi()
        collection = await run_blocking(client.get_meta_items)
        items_by_id = get_items_by_id(collection)

        # Clamp limit - lower max when previews enabled (expensive operation)
        max_limit = 10 if include_preview else 50
        limit = min(max(1, limit), max_limit)

        root = _get_root_path()

        # Get documents sorted by modified date (excluding archived, filtered by root)
        documents = []
        for item in collection:
            if item.is_folder or _is_cloud_archived(item):
                continue
            item_path = get_item_path(item, items_by_id)
            if not _is_within_root(item_path, root):
                continue
            documents.append(item)

        documents.sort(key=_modified_sort_key, reverse=True)

        results = []
        for doc in documents[:limit]:
            doc_path = get_item_path(doc, items_by_id)
            doc_info = {
                "name": doc.VissibleName,
                "path": _apply_root_filter(doc_path),
                "modified": (doc.ModifiedClient if hasattr(doc, "ModifiedClient") else None),
            }
            # Add tags if present
            if hasattr(doc, "tags") and doc.tags:
                doc_info["tags"] = doc.tags

            if include_preview:
                # Download and extract preview (skip notebooks - they need slow OCR)
                file_type = await run_blocking(get_file_type, client, doc)
                if file_type == "notebook":
                    # Notebooks need OCR for preview, skip for performance
                    doc_info["preview_skipped"] = "notebook (use remarkable_read with include_ocr)"
                else:
                    # PDFs and EPUBs have extractable text - fast to preview
                    try:
                        raw_doc = await run_blocking(client.download, doc)
                        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                            tmp.write(raw_doc)
                            tmp_path = Path(tmp.name)

                        try:
                            content = await run_blocking(
                                extract_text_from_document_zip,
                                tmp_path,
                                include_ocr=False,
                                doc_id=doc.ID,
                            )
                            preview_text = "\n".join(content["typed_text"])[:200]
                            if preview_text:
                                if len(preview_text) == 200:
                                    doc_info["preview"] = preview_text + "..."
                                else:
                                    doc_info["preview"] = preview_text
                            # No preview key if empty - cleaner response
                        finally:
                            tmp_path.unlink(missing_ok=True)
                    except Exception:
                        pass  # No preview key on error - cleaner response

            results.append(doc_info)

        result = {"count": len(results), "documents": results}

        if results:
            next_limit = min(limit * 2, 50)
            hint = (
                f"Showing {len(results)} recent documents. "
                f"To read one: remarkable_read('{results[0]['name']}'). "
                f"To see more: remarkable_recent(limit={next_limit})."
            )
        else:
            hint = "No documents found. Use remarkable_browse('/') to check your library."

        return make_response(result, hint)

    except Exception as e:
        return make_error(
            error_type="recent_failed",
            message=str(e),
            suggestion="Check remarkable_status() to verify your connection.",
        )


@mcp.tool(annotations=SEARCH_ANNOTATIONS)
async def remarkable_search(
    query: str,
    grep: Optional[str] = None,
    limit: int = 5,
    include_ocr: bool = False,
    tags: Optional[List[str]] = None,
) -> str:
    """
    <usecase>Search across multiple documents and return matching content.</usecase>
    <instructions>
    Searches document names for the query, then optionally searches content with grep.
    Can filter by tags to narrow results.
    Returns summaries from multiple documents in a single call.

    This is efficient for finding information across your library without
    making many individual tool calls.

    Limits:
    - Max 5 documents per search (to keep response size manageable)
    - Returns first page (~8000 chars) of each matching document
    - Use grep to filter to relevant sections
    </instructions>
    <parameters>
    - query: Search term for document names
    - grep: Optional pattern to search within document content
    - limit: Max documents to return (default: 5, max: 5)
    - include_ocr: Enable OCR for handwritten content (default: False)
    - tags: List of tags to filter documents (optional, case-insensitive)
    </parameters>
    <examples>
    - remarkable_search("meeting")  # Find docs with "meeting" in name
    - remarkable_search("journal", grep="project")  # Find "project" in journals
    - remarkable_search("notes", include_ocr=True)  # Search with OCR enabled
    - remarkable_search("project", tags=["work"])  # Find work-tagged projects
    </examples>
    """
    import json

    try:
        # Enforce limits
        limit = min(max(1, limit), 5)

        # First, find matching documents
        browse_result = await remarkable_browse(query=query, tags=tags)
        browse_data = json.loads(browse_result)

        if "_error" in browse_data:
            return browse_result

        results = browse_data.get("results", [])
        documents = [r for r in results if r.get("type") == "document"][:limit]

        if not documents:
            return make_error(
                error_type="no_documents_found",
                message=f"No documents found matching '{query}'.",
                suggestion="Try a different search term or use remarkable_browse('/') to list all.",
            )

        # Read each document
        search_results = []
        for doc in documents:
            doc_result = {
                "name": doc["name"],
                "path": doc["path"],
                "modified": doc.get("modified"),
            }
            # Include tags if present
            if "tags" in doc:
                doc_result["tags"] = doc["tags"]

            try:
                read_result = await remarkable_read(
                    document=doc["path"],
                    page=1,
                    grep=grep,
                    include_ocr=include_ocr,
                )
                read_data = json.loads(read_result)

                if "_error" not in read_data:
                    doc_result["content"] = read_data.get("content", "")[:2000]  # Limit per doc
                    doc_result["total_pages"] = read_data.get("total_pages", 1)
                    if grep:
                        doc_result["grep_matches"] = read_data.get("grep_matches", 0)
                    if len(read_data.get("content", "")) > 2000:
                        doc_result["truncated"] = True
                else:
                    doc_result["error"] = read_data["_error"]["message"]
            except Exception as e:
                doc_result["error"] = str(e)

            search_results.append(doc_result)

        result = {
            "query": query,
            "grep": grep,
            "count": len(search_results),
            "documents": search_results,
        }
        if tags:
            result["filter_tags"] = tags

        # Build hint
        docs_with_content = [d for d in search_results if "content" in d]
        tag_desc = f" with tags {tags}" if tags else ""
        if grep:
            matches = sum(d.get("grep_matches", 0) for d in docs_with_content)
            hint = (
                f"Found {len(docs_with_content)} document(s){tag_desc} "
                f"with {matches} grep match(es)."
            )
        else:
            hint = f"Found {len(docs_with_content)} document(s) matching '{query}'{tag_desc}."

        if docs_with_content:
            hint += f" To read more: remarkable_read('{docs_with_content[0]['path']}')."

        return make_response(result, hint)

    except Exception as e:
        return make_error(
            error_type="search_failed",
            message=str(e),
            suggestion="Check remarkable_status() to verify your connection.",
        )


def _review_timestamp(value) -> Optional[float]:
    """Return a comparable timestamp for ISO strings or datetime values."""
    if not value:
        return None
    if isinstance(value, datetime):
        try:
            return value.timestamp()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized).timestamp()
        except ValueError:
            return None
    return None


def _review_metadata_from_read(data: dict, fallback_path: str) -> dict:
    """Convert a remarkable_read page response into review/export metadata."""
    doc = {
        "name": data.get("document") or fallback_path.strip("/").split("/")[-1] or fallback_path,
        "path": data.get("path") or fallback_path,
        "file_type": data.get("file_type"),
        "modified": data.get("modified"),
        "content": data.get("content", ""),
    }
    if "tags" in data:
        doc["tags"] = data["tags"]
    if "ocr_backend" in data:
        doc["ocr_backend"] = data["ocr_backend"]
    return doc


async def _review_read_full_document(path: str, include_ocr: bool) -> dict:
    """Read every available page from a document through remarkable_read."""
    page = 1
    pages = []
    doc = None
    while page <= 100:
        read_result = await remarkable_read(document=path, page=page, include_ocr=include_ocr)
        data = json.loads(read_result)
        if "_error" in data:
            return {"path": path, "error": data["_error"].get("message", "read failed")}
        if doc is None:
            doc = _review_metadata_from_read(data, path)
        content = data.get("content", "")
        if content:
            pages.append(content)
        if not data.get("more"):
            break
        page = int(data.get("next_page") or page + 1)
    if doc is None:
        doc = {"name": path.strip("/").split("/")[-1] or path, "path": path, "content": ""}
    doc["content"] = "\n\n".join(pages)
    return doc


def _review_scope(document, folder, tags, since) -> dict:
    """Build a stable scope object for output."""
    if document:
        return {"document": document}
    scope = {"folder": folder or "/"}
    if tags:
        scope["tags"] = tags
    if since:
        scope["since"] = since
    return scope


def _review_markdown(result: dict) -> str:
    """Render review/export data as deterministic Markdown."""
    lines = ["# reMarkable review export", ""]
    scope = result["scope"]
    if "document" in scope:
        lines.append(f"Scope: document `{scope['document']}`")
    else:
        lines.append(f"Scope: folder `{scope.get('folder', '/')}`")
        if scope.get("tags"):
            lines.append("Tags: " + ", ".join(f"`{tag}`" for tag in scope["tags"]))
        if scope.get("since"):
            lines.append(f"Since: `{scope['since']}`")
    lines.append(f"Documents: {result['count']}")
    for doc in result["documents"]:
        lines.extend(["", f"## {doc.get('name') or doc.get('path')}"])
        if doc.get("path"):
            lines.append(f"Path: `{doc['path']}`")
        if doc.get("modified"):
            lines.append(f"Modified: `{doc['modified']}`")
        if doc.get("tags"):
            lines.append("Tags: " + ", ".join(f"`{tag}`" for tag in doc["tags"]))
        if doc.get("error"):
            lines.append(f"Error: {doc['error']}")
            continue
        lines.extend(["", doc.get("content", "")])
    return "\n".join(lines).rstrip() + "\n"


def _review_text(result: dict) -> str:
    """Render review/export data as plain text."""
    chunks = ["reMarkable review export", f"documents: {result['count']}"]
    for doc in result["documents"]:
        chunks.append(f"\n{doc.get('name') or doc.get('path')}\n{doc.get('path', '')}")
        if doc.get("modified"):
            chunks.append(f"modified: {doc['modified']}")
        if doc.get("tags"):
            chunks.append("tags: " + ", ".join(doc["tags"]))
        if doc.get("error"):
            chunks.append("error: " + doc["error"])
        else:
            chunks.append(doc.get("content", ""))
    return "\n".join(chunks).rstrip() + "\n"


@mcp.tool(annotations=REVIEW_ANNOTATIONS)
async def remarkable_review(
    document: Optional[str] = None,
    folder: Optional[str] = None,
    tags: Optional[List[str]] = None,
    since: Optional[str] = None,
    include_ocr: bool = False,
    output_format: str = "json",
) -> str:
    """
    <usecase>Export deterministic review content from one document or folder.</usecase>
    <instructions>
    Read-only helper for review workflows. It does not summarize, classify,
    mutate documents, call an LLM, or integrate with external note/task systems.
    It gathers document metadata plus extracted text/OCR from existing read tools
    and returns it as JSON, Markdown, or plain text.
    </instructions>
    <parameters>
    - document: Optional document name/path/id to export.
    - folder: Optional folder to export when document is omitted (default "/").
    - tags: Optional tags to filter folder exports.
    - since: Optional ISO timestamp/date; folder exports skip older documents.
    - include_ocr: Enable OCR where remarkable_read supports it.
    - output_format: "json", "markdown", or "text".
    </parameters>
    <examples>
    - remarkable_review(document="/Daily/Daily Notes", output_format="markdown")
    - remarkable_review(folder="/Daily", tags=["review"], output_format="json")
    </examples>
    """
    output_format = (output_format or "json").lower()
    if output_format not in {"json", "markdown", "text"}:
        return make_error(
            error_type="invalid_output_format",
            message=f"Unsupported output_format: '{output_format}'.",
            suggestion='Use output_format="json", "markdown", or "text".',
        )

    since_ts = _review_timestamp(since) if since else None
    if since and since_ts is None:
        return make_error(
            error_type="invalid_since",
            message=f"Invalid since value: '{since}'.",
            suggestion='Use an ISO date or timestamp, e.g. "2026-06-24".',
        )

    if document:
        docs = [await _review_read_full_document(document, include_ocr)]
    else:
        browse_result = await remarkable_browse(path=folder or "/", tags=tags)
        browse_data = json.loads(browse_result)
        if "_error" in browse_data:
            return browse_result
        candidates = browse_data.get("documents") or browse_data.get("results") or []
        docs = []
        for candidate in candidates:
            if candidate.get("type") != "document":
                continue
            modified_ts = _review_timestamp(candidate.get("modified"))
            if since_ts is not None and (modified_ts is None or modified_ts < since_ts):
                continue
            path = candidate.get("path") or candidate.get("name")
            if not path:
                continue
            doc = await _review_read_full_document(path, include_ocr)
            if "tags" not in doc and candidate.get("tags"):
                doc["tags"] = candidate["tags"]
            if not doc.get("modified") and candidate.get("modified"):
                doc["modified"] = candidate["modified"]
            docs.append(doc)

    result = {
        "output_format": output_format,
        "scope": _review_scope(document, folder, tags, since),
        "count": len(docs),
        "documents": docs,
    }
    if output_format == "markdown":
        return _review_markdown(result)
    if output_format == "text":
        return _review_text(result)
    return make_response(result, f"Exported {len(docs)} document(s) for review.")


@mcp.tool(annotations=STATUS_ANNOTATIONS)
async def remarkable_status() -> str:
    """
    <usecase>Check connection status, active transport, and write capabilities.</usecase>
    <instructions>
    Returns authentication status, the active transport (cloud, ssh, or usb-web),
    the document count, and a capability matrix describing what each transport can
    do. Use this to verify your connection, choose a transport, or troubleshoot.

    Capability notes:
    - Cloud (default): full read/render/upload/mkdir/move/rename/delete — no device
      needed, works from anywhere your token is valid.
    - SSH: full capabilities over a local/USB connection to the tablet.
    - USB web: read, render, and upload (to root) only — the tablet's USB web
      interface firmware exposes no folder/move/rename/delete endpoints. For full
      write parity over a USB cable, use SSH mode pointed at the USB IP.
    Write tools (upload/mkdir/move/rename/delete) are enabled by default; run
    with --read-only (or REMARKABLE_READ_ONLY=1) to expose a read-only server.
    </instructions>
    <examples>
    - remarkable_status()
    </examples>
    """
    import os

    from remarkable_mcp.api import (
        REMARKABLE_USE_SSH,
        REMARKABLE_USE_USB_WEB,
        get_active_transport,
    )
    from remarkable_mcp.write_tools import write_enabled

    # Determine the *selected* transport from configuration (pre-fallback).
    if REMARKABLE_USE_USB_WEB:
        from remarkable_mcp.usb_web import DEFAULT_USB_HOST

        selected_transport = "usb-web"
        usb_host = os.environ.get("REMARKABLE_USB_HOST", DEFAULT_USB_HOST)
        connection_info = f"USB web interface at {usb_host}"
    elif REMARKABLE_USE_SSH:
        from remarkable_mcp.ssh import (
            DEFAULT_SSH_HOST,
            DEFAULT_SSH_PORT,
            DEFAULT_SSH_USER,
        )

        selected_transport = "ssh"
        ssh_host = os.environ.get("REMARKABLE_SSH_HOST", DEFAULT_SSH_HOST)
        ssh_user = os.environ.get("REMARKABLE_SSH_USER", DEFAULT_SSH_USER)
        ssh_port = int(os.environ.get("REMARKABLE_SSH_PORT", str(DEFAULT_SSH_PORT)))
        connection_info = f"SSH to {ssh_user}@{ssh_host}:{ssh_port}"
    else:
        selected_transport = "cloud"
        connection_info = "environment variable" if REMARKABLE_TOKEN else "file (~/.rmapi)"

    # What each transport is capable of (independent of read-only mode).
    # read/render are always available; the booleans below are the write surface.
    capability_matrix = {
        "cloud": {
            "read": True,
            "render": True,
            "upload": True,
            "mkdir": True,
            "move": True,
            "rename": True,
            "delete": True,
        },
        "ssh": {
            "read": True,
            "render": True,
            "upload": True,
            "mkdir": True,
            "move": True,
            "rename": True,
            "delete": True,
        },
        "usb-web": {
            "read": True,
            "render": True,
            "upload": True,
            "mkdir": False,
            "move": False,
            "rename": False,
            "delete": False,
        },
    }
    writes_on = write_enabled()
    transport = selected_transport

    try:
        client = get_rmapi()
        # Reflect any startup fallback to cloud (device unreachable + token set).
        transport = get_active_transport()
        fell_back = transport != selected_transport
        if fell_back:
            connection_info = (
                f"cloud (fell back from {selected_transport}: device unreachable, "
                "cloud token configured)"
            )

        collection = await run_blocking(client.get_meta_items)
        items_by_id = get_items_by_id(collection)

        root = _get_root_path()

        # Count documents (not folders, filtered by root)
        doc_count = 0
        for item in collection:
            if item.is_folder:
                continue
            item_path = get_item_path(item, items_by_id)
            if _is_within_root(item_path, root):
                doc_count += 1

        # Effective capabilities for the active transport: write ops only count
        # when not in read-only mode.
        transport_caps = capability_matrix[transport]
        effective_caps = {
            cap: (supported if cap in ("read", "render") else supported and writes_on)
            for cap, supported in transport_caps.items()
        }

        result = {
            "authenticated": True,
            "transport": transport,
            "connection": connection_info,
            "status": "connected",
            "document_count": doc_count,
            "write_enabled": writes_on,
            "capabilities": effective_caps,
            "capabilities_by_transport": capability_matrix,
        }
        if fell_back:
            result["fell_back_to_cloud"] = True

        # Add root path info if configured
        if root != "/":
            result["root_path"] = root

        hint_parts = [f"Connected successfully via {transport}. Found {doc_count} documents."]
        if fell_back:
            hint_parts.append(
                f"Note: {selected_transport} was selected but not reachable, so it "
                "fell back to cloud (a cloud token is configured). Set "
                "REMARKABLE_DISABLE_CLOUD_FALLBACK=1 to disable this."
            )
        if root != "/":
            hint_parts.append(f"Filtered to root: {root}")
        if writes_on:
            if transport == "usb-web":
                hint_parts.append(
                    "Write is enabled, but the USB web interface only supports upload "
                    "(to root). For mkdir/move/rename/delete over USB, use SSH mode."
                )
            else:
                hint_parts.append(
                    "Write is enabled: upload, mkdir, move, rename, and delete are available."
                )
        else:
            hint_parts.append(
                "Read-only mode is active (--read-only / REMARKABLE_READ_ONLY=1). "
                "Restart without it to enable write tools."
            )
        hint_parts.append(
            "Use remarkable_browse() to see your files, "
            "or remarkable_recent() for recent documents."
        )

        return make_response(result, " ".join(hint_parts))

    except Exception as e:
        error_msg = str(e)
        # Reflect fallback in the reported transport when one occurred.
        transport = get_active_transport()

        result = {
            "authenticated": False,
            "transport": transport,
            "connection": connection_info,
            "error": error_msg,
            "write_enabled": writes_on,
            "capabilities_by_transport": capability_matrix,
        }

        if transport == "ssh":
            hint = (
                "SSH connection failed. Make sure:\n"
                "1) Developer mode / SSH is enabled on your tablet\n"
                "2) Your reMarkable is connected (USB or same network)\n"
                "3) You can run: ssh root@10.11.99.1\n\n"
                "See: https://remarkable.guide/guide/access/ssh.html\n\n"
                "Or use cloud mode instead (remove --ssh flag) — no device needed."
            )
        elif transport == "usb-web":
            hint = (
                "USB web interface not reachable. Make sure:\n"
                "1) Your reMarkable is connected via USB\n"
                "2) USB web interface is enabled (Settings → Storage)\n"
                "3) The device is on and unlocked"
            )
        else:
            hint = (
                "Cloud authentication failed. To connect:\n"
                "1) Go to https://my.remarkable.com/device/browser/connect\n"
                "2) Get a one-time code\n"
                "3) Run: uvx remarkable-mcp --register YOUR_CODE\n"
                "Cloud mode works from anywhere — no device required. SSH/USB modes "
                "are also available for local access (add --ssh or --usb)."
            )

        return make_response(result, hint)


@mcp.tool(annotations=IMAGE_ANNOTATIONS)
async def remarkable_image(
    document: str,
    page: int = 1,
    background: Optional[str] = None,
    output_format: str = "png",
    compatibility: bool = False,
    include_ocr: bool = False,
    render_merged: bool = False,
    ctx: Optional[Context] = None,
):
    """
    <usecase>Get an image of a specific page from a reMarkable document.</usecase>
    <instructions>
    Renders a notebook or document page as an image (PNG or SVG). This is useful for:
    - Viewing hand-drawn diagrams, sketches, or UI mockups
    - Getting visual context that text extraction might miss
    - Implementing designs based on hand-drawn wireframes
    - SVG format for scalable vector graphics that can be edited

    ## Merged PDF + Annotation Rendering

    Set render_merged=True to composite the PDF page with the annotation layer into
    a single image. This is ideal for annotated PDFs where the annotation-only render
    is hard to interpret without the printed page context. Only works with PNG format
    and documents that have a PDF underlay.

    ## Response Formats

    By default, images are returned as embedded resources (EmbeddedResource) which
    include the full image data inline:
    - PNG: Returned as BlobResourceContents with base64-encoded data
    - SVG: Returned as TextResourceContents with SVG markup

    If your client doesn't support embedded resources in tool responses, set
    compatibility=True to receive a JSON response with just the resource URI.
    The client can then fetch the resource separately.

    Optionally, enable include_ocr=True to extract text from the image using OCR.
    When REMARKABLE_OCR_BACKEND=sampling is set and the client supports sampling,
    the client's own LLM will be used for OCR (no API keys needed).

    Note: This works best with notebooks and handwritten content. For PDFs/EPUBs,
    the annotations layer is rendered (not the underlying PDF content) unless
    render_merged=True is set.
    </instructions>
    <parameters>
    - document: Document name or path (use remarkable_browse to find documents)
    - page: Page number (default: 1, 1-indexed)
    - background: Background color as hex code. Supports RGB (#RRGGBB) or RGBA (#RRGGBBAA).
      Default is "#FBFBFB" (reMarkable paper color), or set REMARKABLE_BACKGROUND_COLOR
      env var to override. Use "#00000000" for transparent.
    - output_format: Output format - "png" (default) or "svg" for vector graphics
    - compatibility: If True, return resource URI in JSON instead of embedded resource.
      Use this if your client doesn't support embedded resources in tool responses.
    - include_ocr: Enable OCR text extraction from the image (default: False).
      When REMARKABLE_OCR_BACKEND=sampling, uses the client's LLM via MCP sampling.
    - render_merged: Composite PDF page + annotation layer into one image (default: False).
      Only works with PNG format and documents that have a PDF underlay.
    </parameters>
    <examples>
    - remarkable_image("UI Mockup")  # Get first page as embedded PNG resource
    - remarkable_image("Meeting Notes", page=2)  # Get second page
    - remarkable_image("/Work/Designs/Wireframe", background="#FFFFFF")  # White background
    - remarkable_image("Sketch", background="#00000000")  # Transparent background
    - remarkable_image("Diagram", output_format="svg")  # Get as embedded SVG resource
    - remarkable_image("Notes", compatibility=True)  # Return resource URI for retry
    - remarkable_image("Notes", include_ocr=True)  # Get image with OCR text extraction
    - remarkable_image("Annotated PDF", render_merged=True)  # PDF + annotations composited
    </examples>
    """
    try:
        # Resolve background color: use provided value or get from env/default
        if background is None:
            background = await run_blocking(get_background_color)

        client = get_rmapi()
        collection = await run_blocking(client.get_meta_items)
        items_by_id = get_items_by_id(collection)

        root = _get_root_path()
        # Resolve user-provided path to actual device path
        actual_document = _resolve_root_path(document) if document.startswith("/") else document

        # Find the document by name or path (case-insensitive, not folders)
        documents = [item for item in collection if not item.is_folder]
        target_doc = None
        document_lower = actual_document.lower().strip("/")

        for doc in documents:
            doc_path = get_item_path(doc, items_by_id)
            # Filter by root path
            if not _is_within_root(doc_path, root):
                continue
            # Match by name (case-insensitive)
            if doc.VissibleName.lower() == document_lower:
                target_doc = doc
                break
            # Also try matching by full path (case-insensitive)
            if doc_path.lower().strip("/") == document_lower:
                target_doc = doc
                break

        if not target_doc:
            # Find similar documents for suggestion (only within root)
            filtered_docs = [
                doc for doc in documents if _is_within_root(get_item_path(doc, items_by_id), root)
            ]
            similar = find_similar_documents(document, filtered_docs)
            search_term = document.split()[0] if document else "notes"
            return make_error(
                error_type="document_not_found",
                message=f"Document not found: '{document}'",
                suggestion=(
                    f"Try remarkable_browse(query='{search_term}') to search, "
                    "or remarkable_browse('/') to list all files."
                ),
                did_you_mean=similar if similar else None,
            )

        # Download the document
        raw_doc = await run_blocking(client.download, target_doc)
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp.write(raw_doc)
            tmp_path = Path(tmp.name)

        try:
            # Validate format parameter
            format_lower = output_format.lower()
            if format_lower not in ("png", "svg"):
                return make_error(
                    error_type="invalid_format",
                    message=f"Invalid format: '{output_format}'. Supported formats: png, svg",
                    suggestion="Use output_format='png' for raster or 'svg' for vectors.",
                )

            # Get total page count
            total_pages = await run_blocking(get_document_page_count, tmp_path)

            if total_pages == 0:
                return make_error(
                    error_type="no_pages",
                    message=f"Document '{target_doc.VissibleName}' has no renderable pages.",
                    suggestion=(
                        "This may be a PDF/EPUB without annotations. "
                        "Use remarkable_read() to extract text content instead."
                    ),
                )

            if page < 1 or page > total_pages:
                return make_error(
                    error_type="page_out_of_range",
                    message=f"Page {page} does not exist. Document has {total_pages} page(s).",
                    suggestion=f"Use page=1 to {total_pages} to view different pages.",
                )

            # Build resource URI for this page
            doc_path = _apply_root_filter(get_item_path(target_doc, items_by_id))
            uri_path = doc_path.lstrip("/")

            # Render the page based on format
            merged_note = None
            is_merged = False

            if format_lower == "svg":
                if render_merged:
                    merged_note = (
                        "render_merged is only supported with PNG format; "
                        "returning annotation-only SVG."
                    )

                svg_content = await run_blocking(
                    render_page_from_document_zip_svg,
                    tmp_path,
                    page,
                    background_color=background,
                )

                if svg_content is None:
                    return make_error(
                        error_type="render_failed",
                        message="Failed to render page to SVG.",
                        suggestion=(
                            "SVG output requires local stroke parsing, so the "
                            "page may be empty or in a newer format. Try "
                            "output_format='png', which falls back to the "
                            "tablet's native PDF export in USB/SSH mode, or "
                            "remarkable_read() to extract text instead."
                        ),
                    )

                resource_uri = f"remarkablesvg:///{uri_path}.page-{page}.svg"

                if compatibility:
                    # Return SVG content in JSON for clients without embedded resource support
                    hint = (
                        f"Page {page}/{total_pages} as SVG. "
                        f"Use compatibility=False for embedded resource format."
                    )
                    if merged_note:
                        hint = f"{merged_note} {hint}"
                    response_data = {
                        "svg": svg_content,
                        "mime_type": "image/svg+xml",
                        "page": page,
                        "total_pages": total_pages,
                        "resource_uri": resource_uri,
                        "merged": False,
                    }
                    return make_response(response_data, hint)
                else:
                    # Return SVG as embedded TextResourceContents with info hint
                    text_resource = TextResourceContents(
                        uri=resource_uri,
                        mimeType="image/svg+xml",
                        text=svg_content,
                    )
                    embedded = EmbeddedResource(type="resource", resource=text_resource)
                    info_text = (
                        f"Page {page}/{total_pages} of '{target_doc.VissibleName}' as SVG. "
                        f"Resource URI: {resource_uri}"
                    )
                    if merged_note:
                        info_text = f"{merged_note}\n{info_text}"
                    info = TextContent(type="text", text=info_text)
                    return [info, embedded]
            else:
                # PNG format
                if render_merged:
                    png_data, merged_note = await run_blocking(
                        render_merged_page_from_document_zip,
                        tmp_path,
                        page,
                        background_color=background,
                    )
                    is_merged = png_data is not None and merged_note is None
                else:
                    png_data = await run_blocking(
                        render_page_from_document_zip,
                        tmp_path,
                        page,
                        background_color=background,
                    )

                # Portable fallback: the local stroke renderer can fail to
                # produce an image for empty pages, newer .rm block formats, or
                # when the client machine lacks a working cairo/libcairo for
                # cairosvg. In that case we rasterize the requested page from the
                # document's source PDF with PyMuPDF (which needs no system
                # cairo). USB/SSH expose the tablet's natively-rendered (merged)
                # PDF; cloud exposes the original source PDF — either covers the
                # failure cases above. Notebooks have no PDF, so download_raw_file
                # returns None and this safely no-ops. Credit: ljdutel (#95).
                rendered_via_pdf = False
                if png_data is None:
                    pdf_bytes = await run_blocking(download_raw_file, client, target_doc, "pdf")
                    if pdf_bytes:
                        png_data = await run_blocking(
                            render_tablet_pdf_page_to_png, pdf_bytes, page
                        )
                        rendered_via_pdf = png_data is not None

                # Blank-page fallback: the stroke renderer returns None for a
                # notebook page with no drawable strokes (e.g. a freshly-created
                # or blank page). The interactive canvas already renders such
                # pages full-bleed at their own paper size; do the same here so a
                # blank page yields a blank image instead of render_failed,
                # keeping remarkable_image consistent with remarkable_canvas.
                if png_data is None:
                    full = await run_blocking(
                        render_page_full_page_from_document_zip,
                        tmp_path,
                        page,
                        background_color=background,
                    )
                    if full is not None:
                        png_data = full[0]

                if png_data is None:
                    return make_error(
                        error_type="render_failed",
                        message="Failed to render page to image.",
                        suggestion=(
                            "The page may be empty, or local rendering "
                            "dependencies (cairo/libcairo for cairosvg) may be "
                            "missing. For PDF-backed documents the source PDF is "
                            "used automatically as a fallback. You can also try "
                            "remarkable_read() to extract text instead."
                        ),
                    )

                # Handle OCR if requested - extract text from the image
                ocr_text = None
                ocr_backend_used = None
                if include_ocr:
                    # Try sampling-based OCR if configured and available
                    # This sends the image to the client's LLM to extract text
                    if ctx and should_use_sampling_ocr(ctx):
                        ocr_text = await ocr_via_sampling(ctx, png_data)
                        if ocr_text:
                            ocr_backend_used = "sampling"

                    # Fall back to traditional OCR if sampling failed or not available
                    if ocr_text is None:
                        # Need to temporarily save PNG to file for tesseract/google
                        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as ocr_tmp:
                            ocr_tmp.write(png_data)
                            ocr_tmp_path = Path(ocr_tmp.name)
                        try:
                            backend = get_ocr_backend()
                            # When backend is "sampling" but sampling failed, fall through to
                            # Google (if API key available) or Tesseract as per documented behavior
                            if backend in ("sampling", "google") or (
                                backend == "auto" and os.environ.get("GOOGLE_VISION_API_KEY")
                            ):
                                ocr_text = await run_blocking(_ocr_png_google_vision, ocr_tmp_path)
                                if ocr_text:
                                    ocr_backend_used = "google"
                            # Fall through to Tesseract if Google not available or returned None
                            if ocr_text is None:
                                ocr_text = await run_blocking(_ocr_png_tesseract, ocr_tmp_path)
                                if ocr_text:
                                    ocr_backend_used = "tesseract"
                        finally:
                            ocr_tmp_path.unlink(missing_ok=True)

                uri_suffix = ".merged.png" if is_merged else ".png"
                resource_uri = f"remarkableimg:///{uri_path}.page-{page}{uri_suffix}"
                png_base64 = base64.b64encode(png_data).decode("utf-8")

                # Build OCR info for response if OCR was requested
                ocr_info = {}
                if include_ocr:
                    ocr_info["ocr_text"] = ocr_text
                    ocr_info["ocr_backend"] = ocr_backend_used
                    if ocr_text is None:
                        ocr_info["ocr_message"] = "No text detected in image"

                if compatibility:
                    # Return base64 PNG in JSON for clients without embedded resource support
                    # Include data URI format for direct use in HTML <img> tags
                    data_uri = f"data:image/png;base64,{png_base64}"
                    hint = (
                        f"Page {page}/{total_pages} as base64-encoded PNG. "
                        f"Use 'data_uri' directly in HTML img src. "
                        f"Use compatibility=False for embedded resource format."
                    )
                    if include_ocr and ocr_text:
                        hint = (
                            f"Page {page}/{total_pages} with OCR text "
                            f"(backend: {ocr_backend_used})."
                        )
                    elif include_ocr:
                        hint = f"Page {page}/{total_pages}. No text detected via OCR."
                    if merged_note:
                        hint = f"{merged_note} {hint}"
                    elif is_merged:
                        hint = f"Rendered with PDF + annotation compositing. {hint}"
                    if rendered_via_pdf:
                        hint = (
                            "Rendered via the tablet's native PDF export "
                            "(local stroke render unavailable). " + hint
                        )

                    response_data = {
                        "data_uri": data_uri,
                        "image_base64": png_base64,
                        "mime_type": "image/png",
                        "page": page,
                        "total_pages": total_pages,
                        "resource_uri": resource_uri,
                        "merged": is_merged,
                        "render_source": "tablet_pdf" if rendered_via_pdf else "strokes",
                        **ocr_info,
                    }
                    return make_response(response_data, hint)
                else:
                    # Return PNG as embedded BlobResourceContents with info hint
                    blob_resource = BlobResourceContents(
                        uri=resource_uri,
                        mimeType="image/png",
                        blob=png_base64,
                    )
                    embedded = EmbeddedResource(type="resource", resource=blob_resource)

                    info_text = f"Page {page}/{total_pages} of '{target_doc.VissibleName}' as PNG. "
                    info_text += f"Resource URI: {resource_uri}"
                    if is_merged:
                        info_text += "\nRendered with PDF + annotation compositing."
                    if rendered_via_pdf:
                        info_text += (
                            "\nRendered via the tablet's native PDF export "
                            "(local stroke render unavailable)."
                        )
                    if merged_note:
                        info_text += f"\nNote: {merged_note}"
                    if include_ocr and ocr_text:
                        info_text += f"\n\nOCR Text (via {ocr_backend_used}):\n{ocr_text}"
                    elif include_ocr:
                        info_text += "\n\nOCR: No text detected in image."

                    info = TextContent(type="text", text=info_text)
                    return [info, embedded]

        finally:
            tmp_path.unlink(missing_ok=True)

    except Exception as e:
        return make_error(
            error_type="image_failed",
            message=str(e),
            suggestion="Check remarkable_status() to verify your connection.",
        )
