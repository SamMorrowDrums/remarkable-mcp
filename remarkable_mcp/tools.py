"""
MCP Tools for reMarkable tablet access.

All tools are read-only and idempotent - they only retrieve data from the
reMarkable Cloud and do not modify any documents.
"""

import tempfile
from pathlib import Path
from typing import Optional

from mcp.types import ToolAnnotations

from remarkable_mcp.api import (
    REMARKABLE_TOKEN,
    get_item_path,
    get_items_by_id,
    get_items_by_parent,
    get_rmapi,
)
from remarkable_mcp.extract import (
    extract_text_from_document_zip,
    find_similar_documents,
)
from remarkable_mcp.responses import make_error, make_response
from remarkable_mcp.server import mcp

# Tool annotations for read-only operations
READ_ONLY_ANNOTATIONS = ToolAnnotations(
    title="Read-only reMarkable operation",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,  # Interacts with external reMarkable Cloud
)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def remarkable_read(document: str, include_ocr: bool = False) -> str:
    """
    <usecase>Read and extract text content from a reMarkable document.</usecase>
    <instructions>
    Extracts all readable text from a document:
    - Typed text from Type Folio or on-screen keyboard (automatic)
    - PDF/EPUB highlights and annotations (automatic)
    - Handwritten text via OCR (if include_ocr=True, slower)

    This is the recommended tool for getting document content.
    </instructions>
    <parameters>
    - document: Document name or path (use remarkable_browse to find documents)
    - include_ocr: Enable handwriting OCR (default: False, requires OCR extras)
    </parameters>
    <examples>
    - remarkable_read("Meeting Notes")
    - remarkable_read("Work/Project Plan", include_ocr=True)
    </examples>
    """
    try:
        from rmapy.document import Document

        client = get_rmapi()
        collection = client.get_meta_items()
        items_by_id = get_items_by_id(collection)

        # Find the document by name
        documents = [item for item in collection if isinstance(item, Document)]
        target_doc = None

        for doc in documents:
            if doc.VissibleName == document:
                target_doc = doc
                break

        if not target_doc:
            # Find similar documents for suggestion
            similar = find_similar_documents(document, documents)
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
            tmp.write(raw_doc.content)
            tmp_path = Path(tmp.name)

        try:
            # Extract text content
            content = extract_text_from_document_zip(tmp_path, include_ocr=include_ocr)
        finally:
            tmp_path.unlink(missing_ok=True)

        doc_path = get_item_path(target_doc, items_by_id)

        result = {
            "document": target_doc.VissibleName,
            "path": doc_path,
            "content": {
                "typed_text": content["typed_text"],
                "highlights": content["highlights"],
                "handwritten_text": content["handwritten_text"],
            },
            "pages": content["pages"],
            "modified": (
                target_doc.ModifiedClient if hasattr(target_doc, "ModifiedClient") else None
            ),
        }

        # Build contextual hint
        folder_path = "/".join(doc_path.split("/")[:-1]) or "/"
        hint_parts = ["Text extracted."]

        if content["typed_text"]:
            hint_parts.append(f"Found {len(content['typed_text'])} text segments.")
        else:
            hint_parts.append("No typed text found.")
            if not include_ocr:
                hint_parts.append("Try include_ocr=True for handwritten content.")

        hint_parts.append(f"To see other files: remarkable_browse('{folder_path}').")

        return make_response(result, " ".join(hint_parts))

    except Exception as e:
        return make_error(
            error_type="read_failed",
            message=str(e),
            suggestion="Check remarkable_status() to verify your connection.",
        )


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
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
        from rmapy.document import Document
        from rmapy.folder import Folder

        client = get_rmapi()
        collection = client.get_meta_items()
        items_by_id = get_items_by_id(collection)
        items_by_parent = get_items_by_parent(collection)

        # Search mode
        if query:
            query_lower = query.lower()
            matches = []

            for item in collection:
                if query_lower in item.VissibleName.lower():
                    matches.append(
                        {
                            "name": item.VissibleName,
                            "path": get_item_path(item, items_by_id),
                            "type": "folder" if isinstance(item, Folder) else "document",
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

        # Browse mode
        if path == "/" or path == "":
            target_parent = ""
        else:
            # Navigate to the folder
            path_parts = [p for p in path.strip("/").split("/") if p]
            current_parent = ""

            for part in path_parts:
                found = False
                for item in items_by_parent.get(current_parent, []):
                    if item.VissibleName == part and isinstance(item, Folder):
                        current_parent = item.ID
                        found = True
                        break

                if not found:
                    # Folder not found - suggest alternatives
                    available_folders = [
                        item.VissibleName
                        for item in items_by_parent.get(current_parent, [])
                        if isinstance(item, Folder)
                    ]
                    return make_error(
                        error_type="folder_not_found",
                        message=f"Folder not found: '{part}'",
                        suggestion=("Use remarkable_browse('/') to see root folder contents."),
                        did_you_mean=(available_folders[:5] if available_folders else None),
                    )

            target_parent = current_parent

        items = items_by_parent.get(target_parent, [])

        folders = []
        documents = []

        for item in sorted(items, key=lambda x: x.VissibleName.lower()):
            if isinstance(item, Folder):
                folders.append({"name": item.VissibleName, "id": item.ID})
            elif isinstance(item, Document):
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


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def remarkable_recent(limit: int = 10, include_preview: bool = False) -> str:
    """
    <usecase>Get your most recently modified documents.</usecase>
    <instructions>
    Returns documents sorted by modification date (newest first).
    Optionally includes a text preview of each document's content.

    Use this to quickly find what you were working on recently.
    </instructions>
    <parameters>
    - limit: Maximum documents to return (default: 10, max: 50)
    - include_preview: Include first ~200 chars of text content (default: False)
    </parameters>
    <examples>
    - remarkable_recent()  # Last 10 documents
    - remarkable_recent(limit=5, include_preview=True)  # With content preview
    </examples>
    """
    try:
        from rmapy.document import Document

        client = get_rmapi()
        collection = client.get_meta_items()
        items_by_id = get_items_by_id(collection)

        # Clamp limit
        limit = min(max(1, limit), 50)

        # Get documents sorted by modified date
        documents = [item for item in collection if isinstance(item, Document)]
        documents.sort(
            key=lambda x: (
                x.ModifiedClient if hasattr(x, "ModifiedClient") and x.ModifiedClient else ""
            ),
            reverse=True,
        )

        results = []
        for doc in documents[:limit]:
            doc_info = {
                "name": doc.VissibleName,
                "path": get_item_path(doc, items_by_id),
                "modified": (doc.ModifiedClient if hasattr(doc, "ModifiedClient") else None),
            }

            if include_preview:
                # Download and extract preview
                try:
                    raw_doc = client.download(doc)
                    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                        tmp.write(raw_doc.content)
                        tmp_path = Path(tmp.name)

                    try:
                        content = extract_text_from_document_zip(tmp_path, include_ocr=False)
                        preview_text = "\n".join(content["typed_text"])[:200]
                        if len(preview_text) == 200:
                            doc_info["preview"] = preview_text + "..."
                        else:
                            doc_info["preview"] = preview_text
                    finally:
                        tmp_path.unlink(missing_ok=True)
                except Exception:
                    doc_info["preview"] = None

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


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
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
    token_source = "environment variable" if REMARKABLE_TOKEN else "file (~/.rmapi)"

    try:
        from rmapy.document import Document

        client = get_rmapi()
        collection = client.get_meta_items()

        doc_count = sum(1 for item in collection if isinstance(item, Document))

        result = {
            "authenticated": True,
            "token_source": token_source,
            "cloud_status": "connected",
            "document_count": doc_count,
        }

        return make_response(
            result,
            (
                f"Connected successfully. Found {doc_count} documents. "
                "Use remarkable_browse() to see your files, "
                "or remarkable_recent() for recent documents."
            ),
        )

    except Exception as e:
        error_msg = str(e)

        result = {"authenticated": False, "error": error_msg, "token_source": token_source}

        hint = (
            "To authenticate: "
            "1) Go to https://my.remarkable.com/device/browser/connect "
            "2) Get a one-time code "
            "3) Run: uv run python server.py --register YOUR_CODE "
            "4) Add REMARKABLE_TOKEN to your MCP config."
        )

        return make_response(result, hint)
