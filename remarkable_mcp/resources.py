"""
MCP Resources for reMarkable tablet access.

Dynamically registers recent documents as resources on startup.
"""

import json
import logging
import tempfile
from pathlib import Path

from remarkable_mcp.server import mcp

logger = logging.getLogger(__name__)


def register_document_resources():
    """
    Dynamically register recent documents as MCP resources.

    Called on startup if API connection exists. Each recent document
    becomes its own resource with URI like remarkable://doc/{name}.
    """
    try:
        from rmapy.document import Document

        from remarkable_mcp.api import get_item_path, get_items_by_id, get_rmapi
        from remarkable_mcp.extract import extract_text_from_document_zip

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

        # Register each recent document as a resource
        for doc in documents[:10]:
            doc_name = doc.VissibleName
            doc_modified = doc.ModifiedClient if hasattr(doc, "ModifiedClient") else None

            # Create a closure to capture the document
            def make_resource_fn(document):
                def resource_fn() -> str:
                    try:
                        raw_doc = client.download(document)

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

                return resource_fn

            # Register the resource
            resource_uri = f"remarkable://doc/{doc_name}"
            description = f"Content from '{doc_name}'"
            if doc_modified:
                description += f" (modified: {doc_modified})"

            mcp.resource(
                resource_uri,
                name=doc_name,
                description=description,
                mime_type="text/plain",
            )(make_resource_fn(doc))

        logger.info(f"Registered {min(len(documents), 10)} document resources")

        # Also register a folder structure resource
        @mcp.resource(
            "remarkable://folders",
            name="Folder Structure",
            description="Your reMarkable folder hierarchy",
            mime_type="application/json",
        )
        def folders_resource() -> str:
            """Return folder structure as a resource."""
            from rmapy.folder import Folder

            try:
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

    except Exception as e:
        logger.warning(f"Could not register document resources: {e}")
        logger.info("Resources will be available after authentication via remarkable_status()")


# Register resources on module load (if API is available)
register_document_resources()
