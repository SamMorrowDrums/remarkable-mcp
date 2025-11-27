"""
MCP Resources for reMarkable tablet access.

Provides:
- remarkable://doc/{name} - template for any document by name
- Individual resources lazily loaded in background (batches of 10)
"""

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Optional, Set

from remarkable_mcp.server import mcp

logger = logging.getLogger(__name__)

# Background loader state
_registered_docs: Set[str] = set()


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
                content = extract_text_from_document_zip(tmp_path, include_ocr=False)
            finally:
                tmp_path.unlink(missing_ok=True)

            text_parts = []
            if content["typed_text"]:
                text_parts.extend(content["typed_text"])
            if content["highlights"]:
                text_parts.append("\n--- Highlights ---")
                text_parts.extend(content["highlights"])
            return "\n\n".join(text_parts) if text_parts else "(No text content)"
        except Exception as e:
            return f"Error: {e}"

    return doc_resource


def _register_document(client, doc) -> bool:
    """Register a single document as a resource."""
    global _registered_docs

    doc_name = doc.VissibleName

    # Skip if already registered
    if doc_name in _registered_docs:
        return False

    uri = f"remarkable://doc/{doc_name}"
    desc = f"Content from '{doc_name}'"
    if doc.ModifiedClient:
        desc += f" (modified: {doc.ModifiedClient})"

    mcp.resource(uri, name=doc_name, description=desc, mime_type="text/plain")(
        _make_doc_resource(client, doc)
    )

    _registered_docs.add(doc_name)
    return True


async def _load_documents_background(shutdown_event: asyncio.Event):
    """
    Background task to lazily load all documents in batches of 10.

    Runs asynchronously, yielding control between batches to keep
    the server responsive. Cancels gracefully on shutdown.
    """
    batch_size = 10
    offset = 0
    consecutive_errors = 0
    max_consecutive_errors = 3

    try:
        from remarkable_mcp.api import get_rmapi

        client = get_rmapi()

        while True:
            # Check for shutdown
            if shutdown_event.is_set():
                logger.info("Background document loader cancelled by shutdown")
                break

            # Fetch next batch - run sync code in executor to not block
            loop = asyncio.get_event_loop()
            try:
                items = await loop.run_in_executor(
                    None, lambda: client.get_meta_items(limit=offset + batch_size)
                )
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
                    if _register_document(client, doc):
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
