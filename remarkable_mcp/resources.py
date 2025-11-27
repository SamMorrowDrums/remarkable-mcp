"""
MCP Resources for reMarkable tablet access.

Provides:
- remarkable://{path}.txt - extracted text from any document
- remarkableraw://{path} - raw PDF/EPUB file download (SSH mode only, enumerated)

Resources are loaded at startup (SSH) or in background batches (cloud).
"""

import asyncio
import base64
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional, Set
from urllib.parse import quote

from remarkable_mcp.server import mcp

logger = logging.getLogger(__name__)

# Background loader state
_registered_docs: Set[str] = set()  # Track document IDs for text resources
_registered_raw: Set[str] = set()  # Track document IDs for raw resources
_registered_uris: Set[str] = set()  # Track URIs for collision detection


def _is_ssh_mode() -> bool:
    """Check if SSH transport is enabled (evaluated at runtime)."""
    return os.environ.get("REMARKABLE_USE_SSH", "").lower() in ("1", "true", "yes")


def _get_mime_type(file_type: str) -> str:
    """Get MIME type for file extension."""
    mime_types = {
        "pdf": "application/pdf",
        "epub": "application/epub+zip",
    }
    return mime_types.get(file_type, "application/octet-stream")


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


def _make_raw_resource(client, document, file_type: str):
    """Create a resource function for raw PDF/EPUB download."""
    from remarkable_mcp.api import download_raw_file

    def raw_resource() -> str:
        try:
            if not _is_ssh_mode():
                return "Error: Raw file download only available in SSH mode"

            raw_data = download_raw_file(client, document, file_type)

            if not raw_data:
                return f"Raw {file_type.upper()} file not found"

            # Return base64 encoded with data URI
            encoded = base64.b64encode(raw_data).decode("ascii")
            return f"data:{_get_mime_type(file_type)};base64,{encoded}"
        except Exception as e:
            return f"Error: {e}"

    return raw_resource


def _register_document(client, doc, items_by_id=None, file_types: dict = None) -> bool:
    """Register a single document as a text resource (and raw resource if PDF/EPUB)."""
    global _registered_docs, _registered_raw, _registered_uris

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
    uri_path = full_path.lstrip("/")
    encoded_path = quote(uri_path, safe="/")  # Keep slashes unencoded for paths

    # Register text resource
    base_uri = f"remarkable://{encoded_path}.txt"
    counter = 1
    final_uri = base_uri
    display_name = f"{full_path}.txt"
    while final_uri in _registered_uris:
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

    # Also register raw resource for PDF/EPUB files (SSH mode only)
    if _is_ssh_mode() and file_types is not None:
        # Use pre-loaded file types (fast)
        file_type = file_types.get(doc_id)
        if file_type in ("pdf", "epub"):
            raw_uri = f"remarkableraw://{encoded_path}.{file_type}"
            raw_counter = 1
            final_raw_uri = raw_uri
            raw_display = f"{full_path}.{file_type}"
            while final_raw_uri in _registered_uris:
                final_raw_uri = f"remarkableraw://{encoded_path}_{raw_counter}.{file_type}"
                raw_display = f"{full_path} ({raw_counter}).{file_type}"
                raw_counter += 1

            raw_desc = f"Raw {file_type.upper()} file: '{full_path}'"
            if doc.ModifiedClient:
                raw_desc += f" (modified: {doc.ModifiedClient})"

            mcp.resource(
                final_raw_uri,
                name=raw_display,
                description=raw_desc,
                mime_type=_get_mime_type(file_type),
            )(_make_raw_resource(client, doc, file_type))

            _registered_raw.add(doc_id)
            _registered_uris.add(final_raw_uri)

    return True


def load_all_documents_sync() -> int:
    """
    Load and register all documents synchronously.
    Used for SSH mode where loading is fast.
    Returns the number of documents registered.
    """
    global _registered_docs, _registered_raw

    from remarkable_mcp.api import get_items_by_id, get_rmapi

    client = get_rmapi()
    items = client.get_meta_items()
    items_by_id = get_items_by_id(items)
    documents = [item for item in items if not item.is_folder]

    logger.info(f"Found {len(documents)} documents")

    # Pre-load all file types in a single SSH call (SSH mode optimization)
    file_types = {}
    if _is_ssh_mode() and hasattr(client, "get_all_file_types"):
        logger.info("Pre-loading file types for raw resources...")
        file_types = client.get_all_file_types()
        logger.info(f"Loaded {len(file_types)} file types")

    for doc in documents:
        try:
            _register_document(client, doc, items_by_id, file_types if _is_ssh_mode() else None)
        except Exception as e:
            logger.debug(f"Failed to register '{doc.VissibleName}': {e}")

    logger.info(
        f"Registered {len(_registered_docs)} text resources"
        + (f", {len(_registered_raw)} raw resources (PDF/EPUB)" if _registered_raw else "")
    )
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

            # Register this batch (no file_types in cloud mode - raw resources not available)
            registered_count = 0
            for doc in batch_docs:
                if shutdown_event.is_set():
                    break
                try:
                    if _register_document(client, doc, items_by_id, file_types=None):
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
