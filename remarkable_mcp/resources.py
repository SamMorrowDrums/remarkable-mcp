"""
MCP Resources for reMarkable tablet access.

Provides:
- remarkable://doc/{name} - template for any document by name
"""

import logging
import tempfile
from pathlib import Path

from remarkable_mcp.server import mcp

logger = logging.getLogger(__name__)


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
