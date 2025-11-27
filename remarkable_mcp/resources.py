"""
MCP Resources for reMarkable tablet access.

Provides:
- remarkable://doc/{name} - template for any document by name
- Individual resources loaded at startup (SSH) or in background batches (cloud)
"""

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional, Set

from remarkable_mcp.server import mcp

logger = logging.getLogger(__name__)

# Background loader state
_registered_docs: Set[str] = set()


def _is_ssh_mode() -> bool:
    """Check if SSH transport is enabled (evaluated at runtime)."""
    return os.environ.get("REMARKABLE_USE_SSH", "").lower() in ("1", "true", "yes")


@mcp.resource(
    "remarkable://doc/{name}",
    name="Document by Name",
    description="Read a reMarkable document by name. Use remarkable_browse() to find documents.",
    mime_type="text/plain",
)
def document_resource(name: str) -> str:
    """Return document content by name (fetched on demand)."""
    try:
        from remarkable_mcp.api import get_rmapi
        from remarkable_mcp.extract import extract_text_from_document_zip

        client = get_rmapi()
        collection = client.get_meta_items()

        # Find document by name
        target_doc = None
        for item in collection:
            if not item.is_folder and item.VissibleName == name:
                target_doc = item
                break

        if not target_doc:
            return f"Document not found: '{name}'"

        # Download and extract
        raw_doc = client.download(target_doc)

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp.write(raw_doc)
            tmp_path = Path(tmp.name)

        try:
            content = extract_text_from_document_zip(tmp_path, include_ocr=False)
        finally:
            tmp_path.unlink(missing_ok=True)

        # Combine all text content
        text_parts = []

        if content["typed_text"]:
            text_parts.extend(content["typed_text"])

        if content["highlights"]:
            text_parts.append("\n--- Highlights ---")
            text_parts.extend(content["highlights"])

        return "\n\n".join(text_parts) if text_parts else "(No text content found)"

    except Exception as e:
        return f"Error reading document: {e}"


# Completions handler for document names
@mcp.completion()
async def complete_document_name(ref, argument, context):
    """Provide completions for document names."""
    from mcp.types import Completion, ResourceTemplateReference

    # Only handle our document template
    if not isinstance(ref, ResourceTemplateReference):
        return None
    if ref.uri_template != "remarkable://doc/{name}":
        return None
    if argument.name != "name":
        return None

    try:
        from remarkable_mcp.api import get_rmapi

        client = get_rmapi()
        collection = client.get_meta_items()

        # Get all document names
        doc_names = [item.VissibleName for item in collection if not item.is_folder]

        # Filter by partial value if provided
        partial = argument.value or ""
        if partial:
            partial_lower = partial.lower()
            doc_names = [n for n in doc_names if partial_lower in n.lower()]

        # Return up to 50 matches, sorted
        return Completion(values=sorted(doc_names)[:50])

    except Exception:
        return Completion(values=[])


def _make_doc_resource(client, document):
    """Create a resource function for a document."""
    from remarkable_mcp.extract import extract_text_from_document_zip

    def doc_resource() -> str:
        try:
            raw = client.download(document)
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                tmp.write(raw)
                tmp_path = Path(tmp.name)
            try:
                # First try without OCR (faster)
                content = extract_text_from_document_zip(tmp_path, include_ocr=False)

                text_parts = []
                if content["typed_text"]:
                    text_parts.extend(content["typed_text"])
                if content["highlights"]:
                    text_parts.append("\n--- Highlights ---")
                    text_parts.extend(content["highlights"])

                # If no text found and document has pages, try OCR
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
    global _registered_docs

    doc_id = doc.ID

    # Skip if already registered (by ID)
    if doc_id in _registered_docs:
        return False

    # Get the full path for display
    doc_name = doc.VissibleName
    if items_by_id:
        from remarkable_mcp.api import get_item_path

        full_path = get_item_path(doc, items_by_id)
    else:
        full_path = f"/{doc_name}"

    # Use ID in URI for uniqueness, path for display
    uri = f"remarkable://doc/{doc_id}"
    desc = f"Content from '{full_path}'"
    if doc.ModifiedClient:
        desc += f" (modified: {doc.ModifiedClient})"

    mcp.resource(uri, name=full_path, description=desc, mime_type="text/plain")(
        _make_doc_resource(client, doc)
    )

    _registered_docs.add(doc_id)
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
