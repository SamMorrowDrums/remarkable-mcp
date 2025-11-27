"""
MCP Resources for reMarkable tablet access.

Provides automatic access to recent documents as resources.
"""

from remarkable_mcp.api import get_item_path, get_items_by_id, get_rmapi
from remarkable_mcp.server import mcp


@mcp.resource(
    "remarkable://recent",
    name="Recent Documents",
    description="List of your 10 most recently modified reMarkable documents",
    mime_type="application/json",
)
def recent_documents_resource() -> str:
    """Return recent documents as a resource."""
    import json

    try:
        from rmapy.document import Document

        client = get_rmapi()
        collection = client.get_meta_items()
        items_by_id = get_items_by_id(collection)

        # Get documents sorted by modified date
        documents = [item for item in collection if isinstance(item, Document)]
        documents.sort(
            key=lambda x: (
                x.ModifiedClient if hasattr(x, "ModifiedClient") and x.ModifiedClient else ""
            ),
            reverse=True,
        )

        results = []
        for doc in documents[:10]:
            results.append(
                {
                    "name": doc.VissibleName,
                    "path": get_item_path(doc, items_by_id),
                    "modified": (doc.ModifiedClient if hasattr(doc, "ModifiedClient") else None),
                }
            )

        return json.dumps({"documents": results}, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.resource(
    "remarkable://folders",
    name="Folder Structure",
    description="Your reMarkable folder hierarchy",
    mime_type="application/json",
)
def folders_resource() -> str:
    """Return folder structure as a resource."""
    import json

    try:
        from rmapy.folder import Folder

        client = get_rmapi()
        collection = client.get_meta_items()
        items_by_id = get_items_by_id(collection)

        folders = []
        for item in collection:
            if isinstance(item, Folder):
                folders.append(
                    {
                        "name": item.VissibleName,
                        "path": get_item_path(item, items_by_id),
                        "id": item.ID,
                    }
                )

        folders.sort(key=lambda x: x["path"])
        return json.dumps({"folders": folders}, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.resource(
    "remarkable://document/{name}",
    name="Document Content",
    description="Content of a specific reMarkable document",
    mime_type="text/plain",
)
def document_resource(name: str) -> str:
    """Return document content as a resource."""
    import tempfile
    from pathlib import Path

    from remarkable_mcp.extract import extract_text_from_document_zip

    try:
        from rmapy.document import Document

        client = get_rmapi()
        collection = client.get_meta_items()

        # Find the document by name
        documents = [item for item in collection if isinstance(item, Document)]
        target_doc = None

        for doc in documents:
            if doc.VissibleName == name:
                target_doc = doc
                break

        if not target_doc:
            return f"Document not found: {name}"

        # Download and extract
        raw_doc = client.download(target_doc)

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp.write(raw_doc.content)
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
