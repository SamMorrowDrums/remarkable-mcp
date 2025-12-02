"""
MCP Resources for reMarkable tablet access.

Provides:
- remarkable:///{path}.txt - extracted text from any document
- remarkableraw:///{path}.txt - raw PDF/EPUB text content (SSH mode only)
- remarkableimg:///{path}.page-{page}.png - page image for notebooks (PNG)
- remarkablesvg:///{path}.page-{page}.svg - page image for notebooks (SVG vector)

Resources are loaded at startup (SSH) or in background batches (cloud).
Respects REMARKABLE_ROOT_PATH environment variable for folder filtering.
"""

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional, Set

from mcp.types import Completion, ResourceTemplateReference

from remarkable_mcp.server import mcp

logger = logging.getLogger(__name__)


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


def _apply_root_filter(path: str, root: str) -> str:
    """Apply root filter to a path for display/URI purposes.

    If root is '/Work', then '/Work/Project' becomes '/Project' in output.
    Case-insensitive matching, preserves original case in output.
    """
    if root == "/":
        return path
    path_lower = path.lower()
    root_lower = root.lower()
    if path_lower == root_lower:
        return "/"
    if path_lower.startswith(root_lower + "/"):
        return path[len(root) :]
    return path


# Background loader state
_registered_docs: Set[str] = set()  # Track document IDs for text resources
_registered_raw: Set[str] = set()  # Track document IDs for raw resources
_registered_img: Set[str] = set()  # Track document IDs for image resources
_registered_uris: Set[str] = set()  # Track URIs for collision detection
_img_uri_to_doc: dict[str, tuple] = {}  # Map image URI template -> (client, doc) for page count


def _is_ssh_mode() -> bool:
    """Check if SSH transport is enabled (evaluated at runtime)."""
    return os.environ.get("REMARKABLE_USE_SSH", "").lower() in ("1", "true", "yes")


def _make_doc_resource(client, document):
    """Create a resource function for a document.

    Returns only user-supplied content: typed text, annotations, highlights,
    and OCR for handwritten content. Does NOT include original PDF/EPUB text.
    Use raw resources for original document text.

    Supports sampling OCR when REMARKABLE_OCR_BACKEND=sampling is configured
    and the client supports the sampling capability.
    """
    from mcp.server.fastmcp import Context

    from remarkable_mcp.extract import (
        extract_text_from_document_zip,
        render_page_from_document_zip,
    )

    async def doc_resource(ctx: Context = None) -> str:
        try:
            text_parts = []

            # Download notebook data for annotations/typed text/handwritten
            raw = client.download(document)
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                tmp.write(raw)
                tmp_path = Path(tmp.name)
            try:
                # First try without OCR (faster)
                content = extract_text_from_document_zip(tmp_path, include_ocr=False)

                if content["typed_text"]:
                    text_parts.extend(content["typed_text"])
                if content["highlights"]:
                    if text_parts:
                        text_parts.append("\n--- Highlights ---")
                    text_parts.extend(content["highlights"])

                # If no text found and document has pages, try OCR for handwritten
                if not text_parts and content["pages"] > 0:
                    # Check if we should use sampling OCR
                    ocr_text = None
                    if ctx is not None:
                        from remarkable_mcp.sampling import (
                            ocr_pages_via_sampling,
                            should_use_sampling_ocr,
                        )

                        if should_use_sampling_ocr(ctx):
                            # Render pages and use sampling OCR
                            from remarkable_mcp.extract import get_background_color

                            png_pages = []
                            for page_num in range(1, content["pages"] + 1):
                                png_data = render_page_from_document_zip(
                                    tmp_path,
                                    page_num,
                                    background_color=get_background_color(),
                                )
                                if png_data:
                                    png_pages.append(png_data)

                            if png_pages:
                                ocr_results = await ocr_pages_via_sampling(ctx, png_pages)
                                if ocr_results:
                                    ocr_text = [t for t in ocr_results if t]

                    # Fall back to standard OCR if sampling didn't work
                    if ocr_text:
                        text_parts.extend(ocr_text)
                    else:
                        content = extract_text_from_document_zip(tmp_path, include_ocr=True)
                        if content["handwritten_text"]:
                            text_parts.extend(content["handwritten_text"])

                return "\n\n".join(text_parts) if text_parts else "(No user content)"
            finally:
                tmp_path.unlink(missing_ok=True)
        except Exception as e:
            return f"Error: {e}"

    return doc_resource


def _make_raw_resource(client, document, file_type: str):
    """Create a resource function for raw PDF/EPUB text extraction."""
    from remarkable_mcp.api import download_raw_file
    from remarkable_mcp.extract import extract_text_from_epub, extract_text_from_pdf

    def raw_resource() -> str:
        try:
            if not _is_ssh_mode():
                return "Error: Raw file download only available in SSH mode"

            raw_data = download_raw_file(client, document, file_type)

            if not raw_data:
                return f"Raw {file_type.upper()} file not found"

            # Extract text from the raw file
            with tempfile.NamedTemporaryFile(suffix=f".{file_type}", delete=False) as tmp:
                tmp.write(raw_data)
                tmp_path = Path(tmp.name)

            try:
                if file_type == "pdf":
                    text = extract_text_from_pdf(tmp_path)
                elif file_type == "epub":
                    text = extract_text_from_epub(tmp_path)
                else:
                    text = f"Unsupported file type: {file_type}"

                return text if text else f"(No text content in {file_type.upper()} file)"
            finally:
                tmp_path.unlink(missing_ok=True)
        except Exception as e:
            return f"Error: {e}"

    return raw_resource


def _make_image_resource(client, document):
    """Create a resource function for page images from a notebook.

    Returns a function that takes a page number and returns PNG bytes.
    Uses the standard reMarkable background color for resources (configurable via env).
    """
    from remarkable_mcp.extract import get_background_color, render_page_from_document_zip

    def image_resource(page: str) -> bytes:
        try:
            page_num = int(page)
            if page_num < 1:
                raise ValueError("Page number must be >= 1")
        except ValueError as e:
            raise ValueError(f"Invalid page number: {page}") from e

        raw_doc = client.download(document)
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp.write(raw_doc)
            tmp_path = Path(tmp.name)

        try:
            # Use reMarkable standard background color for resources
            png_data = render_page_from_document_zip(
                tmp_path, page_num, background_color=get_background_color()
            )
            if png_data is None:
                raise RuntimeError(
                    f"Failed to render page {page_num}. "
                    "Make sure 'rmc' and 'cairosvg' are installed."
                )
            return png_data
        finally:
            tmp_path.unlink(missing_ok=True)

    return image_resource


def _make_svg_resource(client, document):
    """Create a resource function for SVG page images from a notebook.

    Returns a function that takes a page number and returns SVG content.
    Uses the standard reMarkable background color for resources (configurable via env).
    """
    from remarkable_mcp.extract import (
        get_background_color,
        render_page_from_document_zip_svg,
    )

    def svg_resource(page: str) -> str:
        try:
            page_num = int(page)
            if page_num < 1:
                raise ValueError("Page number must be >= 1")
        except ValueError as e:
            raise ValueError(f"Invalid page number: {page}") from e

        raw_doc = client.download(document)
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp.write(raw_doc)
            tmp_path = Path(tmp.name)

        try:
            # Use reMarkable standard background color for resources
            svg_content = render_page_from_document_zip_svg(
                tmp_path, page_num, background_color=get_background_color()
            )
            if svg_content is None:
                raise RuntimeError(
                    f"Failed to render page {page_num} to SVG. Make sure 'rmc' is installed."
                )
            return svg_content
        finally:
            tmp_path.unlink(missing_ok=True)

    return svg_resource


def _register_document(
    client, doc, items_by_id=None, file_types: dict = None, root: str = "/"
) -> bool:
    """Register a single document as resources.

    Registers:
    - Text resource for all documents
    - Raw resource for PDF/EPUB files (SSH mode only)
    - Image template resource for notebooks (not PDF/EPUB)

    Args:
        client: The reMarkable API client
        doc: Document metadata object
        items_by_id: Dict mapping IDs to items for path resolution
        file_types: Dict mapping doc IDs to file types (for raw resources)
        root: Root path filter (documents outside root are skipped)
    """
    global _registered_docs, _registered_raw, _registered_img, _registered_uris

    doc_id = doc.ID

    # Skip if already registered (by ID)
    if doc_id in _registered_docs:
        return False

    # Skip cloud-archived documents (not available on device)
    if hasattr(doc, "is_cloud_archived") and doc.is_cloud_archived:
        return False

    # Get the full path
    doc_name = doc.VissibleName
    if items_by_id:
        from remarkable_mcp.api import get_item_path

        full_path = get_item_path(doc, items_by_id)
    else:
        full_path = f"/{doc_name}"

    # Filter by root path
    if not _is_within_root(full_path, root):
        return False

    # Apply root filter for display paths (e.g., /Work/Project -> /Project)
    display_path = _apply_root_filter(full_path, root)

    # Use the filtered path for URIs
    uri_path = display_path.lstrip("/")

    # Register text resource (use /// for empty netloc)
    base_uri = f"remarkable:///{uri_path}.txt"
    counter = 1
    final_uri = base_uri
    display_name = f"{display_path}.txt"
    while final_uri in _registered_uris:
        final_uri = f"remarkable:///{uri_path}_{counter}.txt"
        display_name = f"{display_path} ({counter}).txt"
        counter += 1

    desc = f"Content from '{display_path}'"
    if doc.ModifiedClient:
        desc += f" (modified: {doc.ModifiedClient})"

    mcp.resource(final_uri, name=display_name, description=desc, mime_type="text/plain")(
        _make_doc_resource(client, doc)
    )

    _registered_docs.add(doc_id)
    _registered_uris.add(final_uri)

    # Get file type for this document
    file_type = None
    if file_types is not None:
        file_type = file_types.get(doc_id)
    if file_type is None:
        # Infer from document name
        name_lower = doc.VissibleName.lower()
        if name_lower.endswith(".pdf"):
            file_type = "pdf"
        elif name_lower.endswith(".epub"):
            file_type = "epub"
        else:
            file_type = "notebook"

    # Register raw resource for PDF/EPUB files (SSH mode only)
    if _is_ssh_mode() and file_type in ("pdf", "epub"):
        # Raw resources now return extracted text, use .txt extension
        raw_uri = f"remarkableraw:///{uri_path}.{file_type}.txt"
        raw_counter = 1
        final_raw_uri = raw_uri
        raw_display = f"{display_path} (raw {file_type.upper()}).txt"
        while final_raw_uri in _registered_uris:
            final_raw_uri = f"remarkableraw:///{uri_path}_{raw_counter}.{file_type}.txt"
            raw_display = f"{display_path} (raw {file_type.upper()}) ({raw_counter}).txt"
            raw_counter += 1

        raw_desc = f"Raw {file_type.upper()} text content: '{display_path}'"
        if doc.ModifiedClient:
            raw_desc += f" (modified: {doc.ModifiedClient})"

        mcp.resource(
            final_raw_uri,
            name=raw_display,
            description=raw_desc,
            mime_type="text/plain",
        )(_make_raw_resource(client, doc, file_type))

        _registered_raw.add(doc_id)
        _registered_uris.add(final_raw_uri)

    # Register image template resources for notebooks only (not PDF/EPUB)
    if file_type == "notebook":
        # PNG resource template with {page} parameter
        img_uri = f"remarkableimg:///{uri_path}.page-{{page}}.png"
        img_counter = 1
        final_img_uri = img_uri
        img_display = f"{display_path} (page image)"
        while final_img_uri in _registered_uris:
            final_img_uri = f"remarkableimg:///{uri_path}_{img_counter}.page-{{page}}.png"
            img_display = f"{display_path} ({img_counter}) (page image)"
            img_counter += 1

        img_desc = f"PNG image of page from notebook '{display_path}'"
        if doc.ModifiedClient:
            img_desc += f" (modified: {doc.ModifiedClient})"

        mcp.resource(
            final_img_uri,
            name=img_display,
            description=img_desc,
            mime_type="image/png",
        )(_make_image_resource(client, doc))

        _registered_img.add(doc_id)
        _registered_uris.add(final_img_uri)

        # Store mapping for completion handler to look up page counts
        _img_uri_to_doc[final_img_uri] = (client, doc)

        # SVG resource template with {page} parameter
        svg_uri = f"remarkablesvg:///{uri_path}.page-{{page}}.svg"
        svg_counter = 1
        final_svg_uri = svg_uri
        svg_display = f"{display_path} (SVG)"
        while final_svg_uri in _registered_uris:
            final_svg_uri = f"remarkablesvg:///{uri_path}_{svg_counter}.page-{{page}}.svg"
            svg_display = f"{display_path} ({svg_counter}) (SVG)"
            svg_counter += 1

        svg_desc = f"SVG vector image of page from notebook '{display_path}'"
        if doc.ModifiedClient:
            svg_desc += f" (modified: {doc.ModifiedClient})"

        mcp.resource(
            final_svg_uri,
            name=svg_display,
            description=svg_desc,
            mime_type="image/svg+xml",
        )(_make_svg_resource(client, doc))

        _registered_uris.add(final_svg_uri)

        # Store mapping for SVG completions too
        _img_uri_to_doc[final_svg_uri] = (client, doc)

    return True


def load_all_documents_sync() -> int:
    """
    Load and register all documents synchronously.
    Used for SSH mode where loading is fast.
    Returns the number of documents registered.

    Respects REMARKABLE_ROOT_PATH environment variable.
    """
    global _registered_docs, _registered_raw, _registered_img

    from remarkable_mcp.api import get_items_by_id, get_rmapi

    client = get_rmapi()
    items = client.get_meta_items()
    items_by_id = get_items_by_id(items)
    documents = [item for item in items if not item.is_folder]

    root = _get_root_path()
    if root != "/":
        logger.info(f"Root path filter: {root}")

    logger.info(f"Found {len(documents)} documents")

    # Pre-load all file types in a single SSH call (SSH mode optimization)
    file_types = {}
    if _is_ssh_mode() and hasattr(client, "get_all_file_types"):
        logger.info("Pre-loading file types for raw resources...")
        file_types = client.get_all_file_types()
        logger.info(f"Loaded {len(file_types)} file types")

    for doc in documents:
        try:
            _register_document(
                client,
                doc,
                items_by_id,
                file_types if _is_ssh_mode() else None,
                root=root,
            )
        except Exception as e:
            logger.debug(f"Failed to register '{doc.VissibleName}': {e}")

    logger.info(
        f"Registered {len(_registered_docs)} text resources"
        + (f", {len(_registered_raw)} raw resources (PDF/EPUB)" if _registered_raw else "")
        + (f", {len(_registered_img)} image resources (notebooks)" if _registered_img else "")
        + (f" (filtered to {root})" if root != "/" else "")
    )
    return len(_registered_docs)


async def _load_documents_background(shutdown_event: asyncio.Event):
    """
    Background task to load and register documents in batches.
    Used for Cloud mode only - SSH mode uses load_all_documents_sync().

    Respects REMARKABLE_ROOT_PATH environment variable.
    """
    try:
        from remarkable_mcp.api import get_items_by_id, get_rmapi

        client = get_rmapi()
        loop = asyncio.get_event_loop()

        batch_size = 10
        offset = 0
        consecutive_errors = 0
        max_consecutive_errors = 3
        items_by_id = {}  # Build incrementally

        root = _get_root_path()
        if root != "/":
            logger.info(f"Root path filter: {root}")

        while True:
            # Check for shutdown
            if shutdown_event.is_set():
                logger.info("Background document loader cancelled by shutdown")
                break

            # Fetch next batch - run sync code in executor to not block
            try:
                items = await loop.run_in_executor(
                    None, lambda: client.get_meta_items(limit=offset + batch_size)
                )
                # Update items_by_id with all items for path resolution
                items_by_id = get_items_by_id(items)
                consecutive_errors = 0  # Reset on success
            except Exception as e:
                consecutive_errors += 1
                logger.warning(f"Error fetching documents (attempt {consecutive_errors}): {e}")
                if consecutive_errors >= max_consecutive_errors:
                    logger.error(
                        f"Background loader stopping after {max_consecutive_errors} "
                        "consecutive errors"
                    )
                    break
                # Wait before retry
                await asyncio.sleep(2**consecutive_errors)
                continue

            # Get documents from this batch (skip folders and already-fetched)
            documents = [item for item in items if not item.is_folder]
            batch_docs = documents[offset : offset + batch_size]

            if not batch_docs:
                # No more documents
                logger.info(
                    f"Background loader complete: {len(_registered_docs)} documents registered"
                    + (f" (filtered to {root})" if root != "/" else "")
                )
                break

            # Register this batch (no file_types in cloud mode - raw resources not available)
            registered_count = 0
            for doc in batch_docs:
                if shutdown_event.is_set():
                    break
                try:
                    if _register_document(client, doc, items_by_id, file_types=None, root=root):
                        registered_count += 1
                except Exception as e:
                    logger.debug(f"Failed to register document '{doc.VissibleName}': {e}")

            if registered_count > 0:
                logger.debug(
                    f"Registered batch of {registered_count} documents "
                    f"(total: {len(_registered_docs)})"
                )

            offset += batch_size

            # Yield control - allow other async tasks to run
            # Small delay to be gentle on the API
            await asyncio.sleep(0.1)

    except asyncio.CancelledError:
        logger.info("Background document loader cancelled")
        raise
    except Exception as e:
        logger.warning(f"Background document loader error: {e}")


def start_background_loader() -> Optional[asyncio.Task]:
    """Start the background document loader task. Returns the task."""
    shutdown_event = asyncio.Event()

    try:
        task = asyncio.create_task(_load_documents_background(shutdown_event))
        # Store the event on the task so we can access it later
        task.shutdown_event = shutdown_event  # type: ignore[attr-defined]
        logger.info("Started background document loader")
        return task
    except Exception as e:
        logger.warning(f"Could not start background loader: {e}")
        return None


async def stop_background_loader(task: Optional[asyncio.Task]):
    """Stop the background document loader task."""
    if task is None:
        return

    # Signal shutdown via event
    if hasattr(task, "shutdown_event"):
        task.shutdown_event.set()

    # Cancel and wait
    if not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    logger.info("Stopped background document loader")


# Completion handler for resource templates
@mcp.completion()
async def handle_completion(ref, argument, context):
    """Provide completions for resource template parameters.

    Currently handles:
    - remarkableimg:// page parameter: looks up actual page count for the document
    - remarkablesvg:// page parameter: looks up actual page count for the document
    """
    if isinstance(ref, ResourceTemplateReference):
        uri = ref.uri if hasattr(ref, "uri") else str(ref)

        # Handle page completions for image resources (PNG and SVG)
        is_img = uri.startswith("remarkableimg://") and argument.name == "page"
        is_svg = uri.startswith("remarkablesvg://") and argument.name == "page"

        if is_img or is_svg:
            # Extract any partial value the user has typed
            partial = argument.value or ""

            # Try to find the matching URI template and get actual page count
            page_count = 1  # Default to 1 if we can't determine
            for template_uri, (client, doc) in _img_uri_to_doc.items():
                # Check if the request URI matches this template (ignoring the {page} part)
                # Template: remarkableimg:///Drawing/Frogalina.page-{page}.png
                # Request:  remarkableimg:///Drawing/Frogalina.page-{page}.png
                if template_uri == uri:
                    try:
                        # Download and count pages
                        from remarkable_mcp.extract import get_document_page_count

                        raw_doc = client.download(doc)
                        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                            tmp.write(raw_doc)
                            tmp_path = Path(tmp.name)
                        try:
                            page_count = get_document_page_count(tmp_path)
                        finally:
                            tmp_path.unlink(missing_ok=True)
                    except Exception as e:
                        logger.debug(f"Failed to get page count for completion: {e}")
                    break

            # Suggest page numbers up to the actual count
            suggestions = [str(i) for i in range(1, page_count + 1)]
            if partial:
                suggestions = [s for s in suggestions if s.startswith(partial)]

            return Completion(values=suggestions[:10], hasMore=len(suggestions) > 10)

    return None
