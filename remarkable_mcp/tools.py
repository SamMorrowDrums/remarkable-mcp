"""
MCP Tools for reMarkable tablet access.

All tools are read-only and idempotent - they only retrieve data from the
reMarkable Cloud and do not modify any documents.
"""

import base64
import os
import re
import tempfile
from pathlib import Path
from typing import Literal, Optional

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
from remarkable_mcp.extract import (
    extract_text_from_document_zip,
    extract_text_from_epub,
    extract_text_from_pdf,
    find_similar_documents,
    get_background_color,
    get_document_page_count,
    render_page_from_document_zip,
    render_page_from_document_zip_svg,
)
from remarkable_mcp.responses import make_error, make_response
from remarkable_mcp.sampling import (
    get_ocr_backend,
    ocr_pages_via_sampling,
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
        pass

    return None


@mcp.tool(annotations=READ_ANNOTATIONS)
async def remarkable_read(
    document: str,
    content_type: Literal["text", "raw", "annotations"] = "text",
    page: int = 1,
    grep: Optional[str] = None,
    include_ocr: bool = False,
    ctx: Context = None,
) -> str:
    """
    <usecase>Read and extract text content from a reMarkable document.</usecase>
    <instructions>
    Extracts content from a document with pagination to preserve context window.

    Content types:
    - "text" (default): Full extracted text (PDF/EPUB content + annotations)
    - "raw": Original PDF/EPUB text only (no annotations). SSH mode only.
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
        collection = client.get_meta_items()
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
        file_type = get_file_type(client, target_doc)

        # Collect content based on content_type
        text_parts = []
        raw_available = False

        # Get raw PDF/EPUB content if requested or for "text" mode
        if content_type in ("text", "raw") and file_type in ("pdf", "epub"):
            raw_data = download_raw_file(client, target_doc, file_type)
            if raw_data:
                raw_available = True
                with tempfile.NamedTemporaryFile(suffix=f".{file_type}", delete=False) as tmp:
                    tmp.write(raw_data)
                    tmp_path = Path(tmp.name)
                try:
                    if file_type == "pdf":
                        raw_text = extract_text_from_pdf(tmp_path)
                    else:
                        raw_text = extract_text_from_epub(tmp_path)
                    if raw_text:
                        text_parts.append(raw_text)
                finally:
                    tmp_path.unlink(missing_ok=True)
            elif content_type == "raw":
                # Raw requested but not available (likely cloud mode)
                return make_error(
                    error_type="raw_not_available",
                    message="Raw file download only available in SSH mode",
                    suggestion=(
                        "Use content_type='text' for extracted content, "
                        "or switch to SSH mode for raw file access. "
                        "See: https://remarkable.guide/guide/access/ssh.html"
                    ),
                )

        # Get annotations/typed text (for "text" or "annotations" mode)
        notebook_pages = []  # List of page content for notebook pagination
        ocr_backend_used = None  # Track which OCR backend was used

        if content_type in ("text", "annotations"):
            raw_doc = client.download(target_doc)
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                tmp.write(raw_doc)
                tmp_path = Path(tmp.name)

            # For notebooks (no PDF/EPUB), use page-based pagination
            is_notebook = file_type not in ("pdf", "epub")

            # Try sampling OCR for notebooks if requested and available
            sampling_ocr_used = False
            if is_notebook and include_ocr and ctx and should_use_sampling_ocr(ctx):
                try:
                    # Render all pages to PNG for sampling OCR
                    total_pages = get_document_page_count(tmp_path)
                    if total_pages > 0:
                        png_pages = []
                        for pg_num in range(1, total_pages + 1):
                            png_data = render_page_from_document_zip(tmp_path, pg_num)
                            if png_data:
                                png_pages.append(png_data)
                            else:
                                png_pages.append(b"")  # Placeholder for failed pages

                        if png_pages:
                            # Use sampling OCR for all pages
                            ocr_results = await ocr_pages_via_sampling(ctx, png_pages)
                            if ocr_results:
                                notebook_pages = ocr_results
                                sampling_ocr_used = True
                                ocr_backend_used = "sampling"
                except Exception:
                    # Fall back to sync OCR on any error
                    pass

            # Use sync extraction if sampling wasn't used
            if not sampling_ocr_used:
                try:
                    content = extract_text_from_document_zip(
                        tmp_path, include_ocr=include_ocr, doc_id=target_doc.ID
                    )
                finally:
                    tmp_path.unlink(missing_ok=True)

                if is_notebook and content["handwritten_text"]:
                    # Each item in handwritten_text is one notebook page
                    notebook_pages = content["handwritten_text"]
                    if include_ocr and notebook_pages:
                        # Determine which backend was used for non-sampling OCR
                        backend = get_ocr_backend()
                        if backend == "google" or (
                            backend == "auto" and os.environ.get("GOOGLE_VISION_API_KEY")
                        ):
                            ocr_backend_used = "google"
                        else:
                            ocr_backend_used = "tesseract"
            else:
                # Clean up temp file if we used sampling
                tmp_path.unlink(missing_ok=True)
                # Create empty content dict for fallback paths
                content = {
                    "typed_text": [],
                    "highlights": [],
                    "handwritten_text": notebook_pages,
                }

            if is_notebook and notebook_pages:
                pass  # notebook_pages already set
            else:
                # Add annotations section
                annotation_parts = []
                if content["typed_text"]:
                    annotation_parts.extend(content["typed_text"])
                if content["highlights"]:
                    annotation_parts.append("\n--- Highlights ---")
                    annotation_parts.extend(content["highlights"])
                if content["handwritten_text"]:
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

            ocr_result = remarkable_read(
                document=document,
                content_type=content_type,
                page=page,
                grep=grep,
                include_ocr=True,  # Enable OCR automatically
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
            hint_parts.append("Raw content requires SSH mode.")

        return make_response(result, " ".join(hint_parts))

    except Exception as e:
        return make_error(
            error_type="read_failed",
            message=str(e),
            suggestion="Check remarkable_status() to verify your connection.",
        )


@mcp.tool(annotations=BROWSE_ANNOTATIONS)
def remarkable_browse(path: str = "/", query: Optional[str] = None) -> str:
    """
    <usecase>Browse your reMarkable library or search for documents.</usecase>
    <instructions>
    Two modes:
    1. Browse mode (default): List contents of a folder
       - Use path="/" for root folder
       - Use path="/FolderName" to navigate into folders
    2. Search mode: Find documents by name
       - Set query="search term" to search across all documents

    Results include document names, types, and modification dates.

    Note: If REMARKABLE_ROOT_PATH is configured, only documents within that
    folder are accessible. Paths are relative to the root path.
    </instructions>
    <parameters>
    - path: Folder path to browse (default: "/" for root)
    - query: Search term to find documents by name (optional, triggers search mode)
    </parameters>
    <examples>
    - remarkable_browse()  # List root folder
    - remarkable_browse("/Work")  # List Work folder
    - remarkable_browse(query="meeting")  # Search for "meeting"
    </examples>
    """
    try:
        client = get_rmapi()
        collection = client.get_meta_items()
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
                if query_lower in item.VissibleName.lower():
                    matches.append(
                        {
                            "name": item.VissibleName,
                            "path": _apply_root_filter(item_path),
                            "type": "folder" if item.is_folder else "document",
                            "modified": (
                                item.ModifiedClient if hasattr(item, "ModifiedClient") else None
                            ),
                        }
                    )

            matches.sort(key=lambda x: x["name"])

            result = {"mode": "search", "query": query, "count": len(matches), "results": matches}

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
                hint = (
                    f"No results for '{query}'. "
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
            if item.is_folder:
                folders.append({"name": item.VissibleName, "id": item.ID})
            else:
                documents.append(
                    {
                        "name": item.VissibleName,
                        "id": item.ID,
                        "modified": (
                            item.ModifiedClient if hasattr(item, "ModifiedClient") else None
                        ),
                    }
                )

        result = {"mode": "browse", "path": path, "folders": folders, "documents": documents}

        # Build helpful hint
        hint_parts = [f"Found {len(folders)} folder(s) and {len(documents)} document(s)."]

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
def remarkable_recent(limit: int = 10, include_preview: bool = False) -> str:
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
        collection = client.get_meta_items()
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

        documents.sort(
            key=lambda x: (
                x.ModifiedClient if hasattr(x, "ModifiedClient") and x.ModifiedClient else ""
            ),
            reverse=True,
        )

        results = []
        for doc in documents[:limit]:
            doc_path = get_item_path(doc, items_by_id)
            doc_info = {
                "name": doc.VissibleName,
                "path": _apply_root_filter(doc_path),
                "modified": (doc.ModifiedClient if hasattr(doc, "ModifiedClient") else None),
            }

            if include_preview:
                # Download and extract preview (skip notebooks - they need slow OCR)
                file_type = get_file_type(client, doc)
                if file_type == "notebook":
                    # Notebooks need OCR for preview, skip for performance
                    doc_info["preview_skipped"] = "notebook (use remarkable_read with include_ocr)"
                else:
                    # PDFs and EPUBs have extractable text - fast to preview
                    try:
                        raw_doc = client.download(doc)
                        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                            tmp.write(raw_doc.content)
                            tmp_path = Path(tmp.name)

                        try:
                            content = extract_text_from_document_zip(
                                tmp_path, include_ocr=False, doc_id=doc.ID
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
def remarkable_search(
    query: str,
    grep: Optional[str] = None,
    limit: int = 5,
    include_ocr: bool = False,
) -> str:
    """
    <usecase>Search across multiple documents and return matching content.</usecase>
    <instructions>
    Searches document names for the query, then optionally searches content with grep.
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
    </parameters>
    <examples>
    - remarkable_search("meeting")  # Find docs with "meeting" in name
    - remarkable_search("journal", grep="project")  # Find "project" in journals
    - remarkable_search("notes", include_ocr=True)  # Search with OCR enabled
    </examples>
    """
    import json

    try:
        # Enforce limits
        limit = min(max(1, limit), 5)

        # First, find matching documents
        browse_result = remarkable_browse(query=query)
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

            try:
                read_result = remarkable_read(
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

        # Build hint
        docs_with_content = [d for d in search_results if "content" in d]
        if grep:
            matches = sum(d.get("grep_matches", 0) for d in docs_with_content)
            hint = f"Found {len(docs_with_content)} document(s) with {matches} grep match(es)."
        else:
            hint = f"Found {len(docs_with_content)} document(s) matching '{query}'."

        if docs_with_content:
            hint += f" To read more: remarkable_read('{docs_with_content[0]['path']}')."

        return make_response(result, hint)

    except Exception as e:
        return make_error(
            error_type="search_failed",
            message=str(e),
            suggestion="Check remarkable_status() to verify your connection.",
        )


@mcp.tool(annotations=STATUS_ANNOTATIONS)
def remarkable_status() -> str:
    """
    <usecase>Check connection status and authentication with reMarkable Cloud.</usecase>
    <instructions>
    Returns authentication status and diagnostic information.
    Use this to verify your connection or troubleshoot issues.
    </instructions>
    <examples>
    - remarkable_status()
    </examples>
    """
    import os

    from remarkable_mcp.api import REMARKABLE_USE_SSH

    # Determine transport mode
    if REMARKABLE_USE_SSH:
        from remarkable_mcp.ssh import (
            DEFAULT_SSH_HOST,
            DEFAULT_SSH_PORT,
            DEFAULT_SSH_USER,
        )

        transport = "ssh"
        ssh_host = os.environ.get("REMARKABLE_SSH_HOST", DEFAULT_SSH_HOST)
        ssh_user = os.environ.get("REMARKABLE_SSH_USER", DEFAULT_SSH_USER)
        ssh_port = int(os.environ.get("REMARKABLE_SSH_PORT", str(DEFAULT_SSH_PORT)))
        connection_info = f"SSH to {ssh_user}@{ssh_host}:{ssh_port}"
    else:
        transport = "cloud"
        connection_info = "environment variable" if REMARKABLE_TOKEN else "file (~/.rmapi)"

    try:
        client = get_rmapi()
        collection = client.get_meta_items()
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

        result = {
            "authenticated": True,
            "transport": transport,
            "connection": connection_info,
            "status": "connected",
            "document_count": doc_count,
        }

        # Add root path info if configured
        if root != "/":
            result["root_path"] = root

        hint_parts = [f"Connected successfully via {transport}. Found {doc_count} documents."]
        if root != "/":
            hint_parts.append(f"Filtered to root: {root}")
        hint_parts.append(
            "Use remarkable_browse() to see your files, "
            "or remarkable_recent() for recent documents."
        )

        return make_response(result, " ".join(hint_parts))

    except Exception as e:
        error_msg = str(e)

        result = {
            "authenticated": False,
            "transport": transport,
            "connection": connection_info,
            "error": error_msg,
        }

        if REMARKABLE_USE_SSH:
            hint = (
                "SSH connection failed. Make sure:\n"
                "1) Developer mode is enabled on your tablet\n"
                "2) Your reMarkable is connected via USB\n"
                "3) You can run: ssh root@10.11.99.1\n\n"
                "See: https://remarkable.guide/guide/access/ssh.html\n\n"
                "Or use cloud mode instead (remove --ssh flag)."
            )
        else:
            hint = (
                "To authenticate: "
                "1) Go to https://my.remarkable.com/device/browser/connect "
                "2) Get a one-time code "
                "3) Run: uv run python server.py --register YOUR_CODE "
                "4) Add REMARKABLE_TOKEN to your MCP config.\n\n"
                "Or use SSH mode (faster, recommended): uvx remarkable-mcp --ssh\n"
                "SSH setup guide: https://remarkable.guide/guide/access/ssh.html"
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
    ctx: Context = None,
):
    """
    <usecase>Get an image of a specific page from a reMarkable document.</usecase>
    <instructions>
    Renders a notebook or document page as an image (PNG or SVG). This is useful for:
    - Viewing hand-drawn diagrams, sketches, or UI mockups
    - Getting visual context that text extraction might miss
    - Implementing designs based on hand-drawn wireframes
    - SVG format for scalable vector graphics that can be edited

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
    the annotations layer is rendered (not the underlying PDF content).
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
    </parameters>
    <examples>
    - remarkable_image("UI Mockup")  # Get first page as embedded PNG resource
    - remarkable_image("Meeting Notes", page=2)  # Get second page
    - remarkable_image("/Work/Designs/Wireframe", background="#FFFFFF")  # White background
    - remarkable_image("Sketch", background="#00000000")  # Transparent background
    - remarkable_image("Diagram", output_format="svg")  # Get as embedded SVG resource
    - remarkable_image("Notes", compatibility=True)  # Return resource URI for retry
    - remarkable_image("Notes", include_ocr=True)  # Get image with OCR text extraction
    </examples>
    """
    try:
        # Resolve background color: use provided value or get from env/default
        if background is None:
            background = get_background_color()

        client = get_rmapi()
        collection = client.get_meta_items()
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
        raw_doc = client.download(target_doc)
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
            total_pages = get_document_page_count(tmp_path)

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
            if format_lower == "svg":
                svg_content = render_page_from_document_zip_svg(
                    tmp_path, page, background_color=background
                )

                if svg_content is None:
                    return make_error(
                        error_type="render_failed",
                        message="Failed to render page to SVG.",
                        suggestion="Make sure 'rmc' is installed. Try: uv add rmc",
                    )

                resource_uri = f"remarkablesvg:///{uri_path}.page-{page}.svg"

                if compatibility:
                    # Return SVG content in JSON for clients without embedded resource support
                    hint = (
                        f"Page {page}/{total_pages} as SVG. "
                        f"Use compatibility=False for embedded resource format."
                    )
                    return make_response(
                        {
                            "svg": svg_content,
                            "mime_type": "image/svg+xml",
                            "page": page,
                            "total_pages": total_pages,
                            "resource_uri": resource_uri,
                        },
                        hint,
                    )
                else:
                    # Return SVG as embedded TextResourceContents with info hint
                    text_resource = TextResourceContents(
                        uri=resource_uri,
                        mimeType="image/svg+xml",
                        text=svg_content,
                    )
                    embedded = EmbeddedResource(type="resource", resource=text_resource)
                    info = TextContent(
                        type="text",
                        text=f"Page {page}/{total_pages} of '{target_doc.VissibleName}' as SVG. "
                        f"Resource URI: {resource_uri}",
                    )
                    return [info, embedded]
            else:
                # PNG format
                png_data = render_page_from_document_zip(
                    tmp_path, page, background_color=background
                )

                if png_data is None:
                    return make_error(
                        error_type="render_failed",
                        message="Failed to render page to image.",
                        suggestion=(
                            "Make sure 'rmc' and 'cairosvg' are installed. Try: uv add rmc cairosvg"
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
                            if backend == "google" or (
                                backend == "auto" and os.environ.get("GOOGLE_VISION_API_KEY")
                            ):
                                ocr_text = _ocr_png_google_vision(ocr_tmp_path)
                                ocr_backend_used = "google"
                            else:
                                ocr_text = _ocr_png_tesseract(ocr_tmp_path)
                                ocr_backend_used = "tesseract"
                        finally:
                            ocr_tmp_path.unlink(missing_ok=True)

                resource_uri = f"remarkableimg:///{uri_path}.page-{page}.png"
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

                    response_data = {
                        "data_uri": data_uri,
                        "image_base64": png_base64,
                        "mime_type": "image/png",
                        "page": page,
                        "total_pages": total_pages,
                        "resource_uri": resource_uri,
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
