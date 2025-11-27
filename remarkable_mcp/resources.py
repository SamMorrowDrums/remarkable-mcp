"""
MCP Resources for reMarkable tablet access.

Provides:
- remarkable://{path}.txt - extracted text from any document by path
- remarkable-raw://{path} - raw PDF/EPUB file download (SSH mode only)
- Individual resources loaded at startup (SSH) or in background batches (cloud)
"""

import asyncio
import base64
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional, Set
from urllib.parse import quote, unquote

from remarkable_mcp.server import mcp

logger = logging.getLogger(__name__)

# Background loader state
_registered_docs: Set[str] = set()  # Track document IDs
_registered_uris: Set[str] = set()  # Track URIs for collision detection


def _is_ssh_mode() -> bool:
    """Check if SSH transport is enabled (evaluated at runtime)."""
    return os.environ.get("REMARKABLE_USE_SSH", "").lower() in ("1", "true", "yes")


@mcp.resource(
    "remarkable://{path}.txt",
    name="Document by Path",
    description="Read a reMarkable document by path. Use remarkable_browse() to find documents.",
    mime_type="text/plain",
)
def document_resource(path: str) -> str:
    """Return document content by path (fetched on demand)."""
    try:
        from remarkable_mcp.api import (
            download_raw_file,
            get_file_type,
            get_item_path,
            get_items_by_id,
            get_rmapi,
        )
        from remarkable_mcp.extract import (
            extract_text_from_document_zip,
            extract_text_from_epub,
            extract_text_from_pdf,
        )

        # URL-decode the path (spaces are encoded as %20, etc.)
        decoded_path = unquote(path)

        client = get_rmapi()
        collection = client.get_meta_items()
        items_by_id = get_items_by_id(collection)

        # Find document by path
        target_doc = None
        for item in collection:
            if not item.is_folder:
                item_path = get_item_path(item, items_by_id).lstrip("/")
                if item_path == decoded_path:
                    target_doc = item
                    break

        if not target_doc:
            return f"Document not found: '{decoded_path}'"

        # Check file type and try to extract from raw file first
        file_type = get_file_type(client, target_doc)
        text_parts = []

        if file_type == "pdf":
            # Try to download and extract from raw PDF
            raw_pdf = download_raw_file(client, target_doc, "pdf")
            if raw_pdf:
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    tmp.write(raw_pdf)
                    tmp_path = Path(tmp.name)
                try:
                    pdf_text = extract_text_from_pdf(tmp_path)
                    if pdf_text:
                        text_parts.append(pdf_text)
                finally:
                    tmp_path.unlink(missing_ok=True)

        elif file_type == "epub":
            # Try to download and extract from raw EPUB
            raw_epub = download_raw_file(client, target_doc, "epub")
            if raw_epub:
                with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
                    tmp.write(raw_epub)
                    tmp_path = Path(tmp.name)
                try:
                    epub_text = extract_text_from_epub(tmp_path)
                    if epub_text:
                        text_parts.append(epub_text)
                finally:
                    tmp_path.unlink(missing_ok=True)

        # Also extract annotations/highlights from the notebook layer
        raw_doc = client.download(target_doc)

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp.write(raw_doc)
            tmp_path = Path(tmp.name)

        try:
            content = extract_text_from_document_zip(tmp_path, include_ocr=False)
        finally:
            tmp_path.unlink(missing_ok=True)

        # Add typed text (from notebooks or Type Folio)
        if content["typed_text"]:
            if text_parts:
                text_parts.append("\n--- Annotations/Notes ---")
            text_parts.extend(content["typed_text"])

        # Add highlights
        if content["highlights"]:
            text_parts.append("\n--- Highlights ---")
            text_parts.extend(content["highlights"])

        return "\n\n".join(text_parts) if text_parts else "(No text content found)"

    except Exception as e:
        return f"Error reading document: {e}"


@mcp.resource(
    "remarkable-raw://{path}",
    name="Raw Document File",
    description="Download raw PDF/EPUB file. SSH mode only. Returns base64-encoded file.",
    mime_type="application/octet-stream",
)
def raw_document_resource(path: str) -> str:
    """Return raw document file (PDF/EPUB) as base64."""
    try:
        from remarkable_mcp.api import (
            download_raw_file,
            get_file_type,
            get_item_path,
            get_items_by_id,
            get_rmapi,
        )

        if not _is_ssh_mode():
            return "Error: Raw file download only available in SSH mode"

        # URL-decode the path
        decoded_path = unquote(path)

        client = get_rmapi()
        collection = client.get_meta_items()
        items_by_id = get_items_by_id(collection)

        # Find document by path
        target_doc = None
        for item in collection:
            if not item.is_folder:
                item_path = get_item_path(item, items_by_id).lstrip("/")
                if item_path == decoded_path:
                    target_doc = item
                    break

        if not target_doc:
            return f"Document not found: '{decoded_path}'"

        # Get file type
        file_type = get_file_type(client, target_doc)

        if file_type not in ("pdf", "epub"):
            return f"No raw file available for this document type: {file_type or 'notebook'}"

        # Download raw file
        raw_data = download_raw_file(client, target_doc, file_type)

        if not raw_data:
            return f"Raw {file_type.upper()} file not found for this document"

        # Return base64 encoded with metadata header
        encoded = base64.b64encode(raw_data).decode("ascii")
        return f"data:{_get_mime_type(file_type)};base64,{encoded}"

    except Exception as e:
        return f"Error downloading raw file: {e}"


def _get_mime_type(file_type: str) -> str:
    """Get MIME type for file extension."""
    mime_types = {
        "pdf": "application/pdf",
        "epub": "application/epub+zip",
    }
    return mime_types.get(file_type, "application/octet-stream")


# Completions handler for document paths
@mcp.completion()
async def complete_document_path(ref, argument, context):
    """Provide completions for document paths."""
    from mcp.types import Completion, ResourceTemplateReference

    # Only handle our document template
    if not isinstance(ref, ResourceTemplateReference):
        return None
    if ref.uri_template != "remarkable://{path}.txt":
        return None
    if argument.name != "path":
        return None

    try:
        from remarkable_mcp.api import get_item_path, get_items_by_id, get_rmapi

        client = get_rmapi()
        collection = client.get_meta_items()
        items_by_id = get_items_by_id(collection)

        # Get all document paths (without leading slash)
        doc_paths = []
        for item in collection:
            if not item.is_folder:
                path = get_item_path(item, items_by_id).lstrip("/")
                doc_paths.append(path)

        # Filter by partial value if provided
        partial = argument.value or ""
        if partial:
            partial_lower = partial.lower()
            doc_paths = [p for p in doc_paths if partial_lower in p.lower()]

        # Return up to 50 matches, sorted
        return Completion(values=sorted(doc_paths)[:50])

    except Exception:
        return Completion(values=[])


def _make_doc_resource(client, document):
    """Create a resource function for a document."""
    from remarkable_mcp.api import download_raw_file, get_file_type
    from remarkable_mcp.extract import (
        extract_text_from_document_zip,
        extract_text_from_epub,
        extract_text_from_pdf,
    )

    def doc_resource() -> str:
        try:
            text_parts = []

            # Check file type and extract from raw file first
            file_type = get_file_type(client, document)

            if file_type == "pdf":
                raw_pdf = download_raw_file(client, document, "pdf")
                if raw_pdf:
                    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                        tmp.write(raw_pdf)
                        tmp_path = Path(tmp.name)
                    try:
                        pdf_text = extract_text_from_pdf(tmp_path)
                        if pdf_text:
                            text_parts.append(pdf_text)
                    finally:
                        tmp_path.unlink(missing_ok=True)

            elif file_type == "epub":
                raw_epub = download_raw_file(client, document, "epub")
                if raw_epub:
                    with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
                        tmp.write(raw_epub)
                        tmp_path = Path(tmp.name)
                    try:
                        epub_text = extract_text_from_epub(tmp_path)
                        if epub_text:
                            text_parts.append(epub_text)
                    finally:
                        tmp_path.unlink(missing_ok=True)

            # Download notebook data for annotations/typed text
            raw = client.download(document)
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                tmp.write(raw)
                tmp_path = Path(tmp.name)
            try:
                # First try without OCR (faster)
                content = extract_text_from_document_zip(tmp_path, include_ocr=False)

                if content["typed_text"]:
                    if text_parts:
                        text_parts.append("\n--- Annotations/Notes ---")
                    text_parts.extend(content["typed_text"])
                if content["highlights"]:
                    text_parts.append("\n--- Highlights ---")
                    text_parts.extend(content["highlights"])

                # If no text found and document has pages (notebook), try OCR
                if not text_parts and content["pages"] > 0:
                    content = extract_text_from_document_zip(tmp_path, include_ocr=True)
                    if content["handwritten_text"]:
                        text_parts.append("--- Handwritten (OCR) ---")
                        text_parts.extend(content["handwritten_text"])

                return "\n\n".join(text_parts) if text_parts else "(No text content)"
            finally:
                tmp_path.unlink(missing_ok=True)
        except Exception as e:
            return f"Error: {e}"

    return doc_resource


def _register_document(client, doc, items_by_id=None) -> bool:
    """Register a single document as a resource."""
    global _registered_docs, _registered_uris

    doc_id = doc.ID

    # Skip if already registered (by ID)
    if doc_id in _registered_docs:
        return False

    # Get the full path
    doc_name = doc.VissibleName
    if items_by_id:
        from remarkable_mcp.api import get_item_path

        full_path = get_item_path(doc, items_by_id)
    else:
        full_path = f"/{doc_name}"

    # URL-encode the path for valid URI (spaces -> %20, etc.)
    # Use safe="" to encode all special chars including slashes initially,
    # then we'll handle slashes specially
    uri_path = full_path.lstrip("/")
    encoded_path = quote(uri_path, safe="/")  # Keep slashes unencoded for paths
    base_uri = f"remarkable://{encoded_path}.txt"

    # Handle duplicate paths by appending counter
    counter = 1
    final_uri = base_uri
    display_name = f"{full_path}.txt"
    while final_uri in _registered_uris:
        # Increment counter for each collision
        final_uri = f"remarkable://{encoded_path}_{counter}.txt"
        display_name = f"{full_path} ({counter}).txt"
        counter += 1

    desc = f"Content from '{full_path}'"
    if doc.ModifiedClient:
        desc += f" (modified: {doc.ModifiedClient})"

    mcp.resource(final_uri, name=display_name, description=desc, mime_type="text/plain")(
        _make_doc_resource(client, doc)
    )

    _registered_docs.add(doc_id)
    _registered_uris.add(final_uri)
    return True


def load_all_documents_sync() -> int:
    """
    Load and register all documents synchronously.
    Used for SSH mode where loading is fast.
    Returns the number of documents registered.
    """
    global _registered_docs

    from remarkable_mcp.api import get_items_by_id, get_rmapi

    client = get_rmapi()
    items = client.get_meta_items()
    items_by_id = get_items_by_id(items)
    documents = [item for item in items if not item.is_folder]

    logger.info(f"Found {len(documents)} documents")

    for doc in documents:
        try:
            _register_document(client, doc, items_by_id)
        except Exception as e:
            logger.debug(f"Failed to register '{doc.VissibleName}': {e}")

    logger.info(f"Registered {len(_registered_docs)} document resources")
    return len(_registered_docs)


async def _load_documents_background(shutdown_event: asyncio.Event):
    """
    Background task to load and register documents in batches.
    Used for Cloud mode only - SSH mode uses load_all_documents_sync().
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
                )
                break

            # Register this batch
            registered_count = 0
            for doc in batch_docs:
                if shutdown_event.is_set():
                    break
                try:
                    if _register_document(client, doc, items_by_id):
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
