#!/usr/bin/env python3
"""
reMarkable MCP Server

An MCP server that provides access to reMarkable tablet data through the reMarkable Cloud API.
Uses rmapy for authentication and file access.
"""

import os
import json
import tempfile
import shutil
from pathlib import Path
from typing import Optional
from datetime import datetime

from mcp.server.fastmcp import FastMCP

# Initialize FastMCP server
mcp = FastMCP("remarkable")

# Configuration
REMARKABLE_CONFIG_DIR = Path.home() / ".remarkable"
REMARKABLE_TOKEN_FILE = REMARKABLE_CONFIG_DIR / "token"
CACHE_DIR = REMARKABLE_CONFIG_DIR / "cache"


def get_rmapi():
    """Get or initialize the reMarkable API client."""
    try:
        from rmapy.api import Client
        from rmapy.document import Document
        from rmapy.folder import Folder
        
        client = Client()
        
        # Check if we have a valid token
        if REMARKABLE_TOKEN_FILE.exists():
            client.renew_token()
        
        return client
    except ImportError:
        raise RuntimeError("rmapy not installed. Run: pip install rmapy")
    except Exception as e:
        raise RuntimeError(f"Failed to initialize reMarkable client: {e}")


def ensure_config_dir():
    """Ensure configuration directory exists."""
    REMARKABLE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


@mcp.tool()
def remarkable_auth_status() -> str:
    """
    Check the authentication status with reMarkable Cloud.
    Returns whether you're authenticated and token expiry info.
    """
    ensure_config_dir()
    
    if not REMARKABLE_TOKEN_FILE.exists():
        return json.dumps({
            "authenticated": False,
            "message": "Not authenticated. Use remarkable_register with a one-time code from https://my.remarkable.com/device/browser/connect"
        }, indent=2)
    
    try:
        client = get_rmapi()
        # Try to refresh token to verify it's valid
        client.renew_token()
        return json.dumps({
            "authenticated": True,
            "message": "Successfully authenticated with reMarkable Cloud",
            "config_dir": str(REMARKABLE_CONFIG_DIR)
        }, indent=2)
    except Exception as e:
        return json.dumps({
            "authenticated": False,
            "error": str(e),
            "message": "Token may be expired. Use remarkable_register to re-authenticate."
        }, indent=2)


@mcp.tool()
def remarkable_register(one_time_code: str) -> str:
    """
    Register this device with reMarkable Cloud using a one-time code.
    
    Get a code from: https://my.remarkable.com/device/browser/connect
    
    Args:
        one_time_code: The 8-character one-time code from reMarkable
    """
    ensure_config_dir()
    
    try:
        from rmapy.api import Client
        
        client = Client()
        client.register_device(one_time_code)
        
        return json.dumps({
            "success": True,
            "message": "Successfully registered with reMarkable Cloud!",
            "config_dir": str(REMARKABLE_CONFIG_DIR)
        }, indent=2)
    except ImportError:
        return json.dumps({
            "success": False,
            "error": "rmapy not installed. Run: pip install rmapy"
        }, indent=2)
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e)
        }, indent=2)


@mcp.tool()
def remarkable_list_files(path: str = "/") -> str:
    """
    List files and folders in reMarkable Cloud.
    
    Args:
        path: The path to list. Use "/" for root, or folder names like "/My Notes"
    """
    try:
        from rmapy.api import Client
        from rmapy.folder import Folder
        from rmapy.document import Document
        
        client = get_rmapi()
        client.renew_token()
        
        # Get all items
        collection = client.get_meta_items()
        
        # Build a tree structure
        items_by_parent = {}
        items_by_id = {}
        
        for item in collection:
            items_by_id[item.ID] = item
            parent = item.Parent if hasattr(item, 'Parent') else ""
            if parent not in items_by_parent:
                items_by_parent[parent] = []
            items_by_parent[parent].append(item)
        
        # Find the target folder
        if path == "/" or path == "":
            target_parent = ""
        else:
            # Search for the folder by name
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
                    return json.dumps({
                        "error": f"Folder not found: {part}",
                        "path": path
                    }, indent=2)
            target_parent = current_parent
        
        # List items in target folder
        items = items_by_parent.get(target_parent, [])
        
        result = {
            "path": path,
            "items": []
        }
        
        for item in sorted(items, key=lambda x: (not isinstance(x, Folder), x.VissibleName)):
            item_info = {
                "name": item.VissibleName,
                "id": item.ID,
                "type": "folder" if isinstance(item, Folder) else "document",
                "modified": item.ModifiedClient if hasattr(item, 'ModifiedClient') else None
            }
            if isinstance(item, Document):
                item_info["version"] = item.Version if hasattr(item, 'Version') else None
            result["items"].append(item_info)
        
        return json.dumps(result, indent=2)
        
    except Exception as e:
        return json.dumps({
            "error": str(e),
            "hint": "Make sure you're authenticated. Use remarkable_auth_status to check."
        }, indent=2)


@mcp.tool()
def remarkable_get_document(document_name: str, include_text: bool = True) -> str:
    """
    Get details and optionally extract text content from a reMarkable document.
    
    This will download the document and attempt to extract any typed text,
    highlights, and annotations.
    
    Args:
        document_name: The name of the document to retrieve
        include_text: Whether to extract and include text content (default: True)
    """
    try:
        from rmapy.api import Client
        from rmapy.document import Document
        
        client = get_rmapi()
        client.renew_token()
        
        # Find the document
        collection = client.get_meta_items()
        target_doc = None
        
        for item in collection:
            if isinstance(item, Document) and item.VissibleName == document_name:
                target_doc = item
                break
        
        if not target_doc:
            return json.dumps({
                "error": f"Document not found: {document_name}",
                "hint": "Use remarkable_list_files to see available documents"
            }, indent=2)
        
        result = {
            "name": target_doc.VissibleName,
            "id": target_doc.ID,
            "version": target_doc.Version if hasattr(target_doc, 'Version') else None,
            "modified": target_doc.ModifiedClient if hasattr(target_doc, 'ModifiedClient') else None,
            "type": target_doc.Type if hasattr(target_doc, 'Type') else None
        }
        
        if include_text:
            # Download and extract content
            try:
                with tempfile.TemporaryDirectory() as tmpdir:
                    # Download the raw document
                    raw_doc = client.download(target_doc)
                    
                    # Save to temp file
                    doc_path = Path(tmpdir) / f"{target_doc.ID}.zip"
                    with open(doc_path, 'wb') as f:
                        f.write(raw_doc.content)
                    
                    # Try to extract text content
                    import zipfile
                    
                    text_content = []
                    highlights = []
                    
                    with zipfile.ZipFile(doc_path, 'r') as zf:
                        for name in zf.namelist():
                            # Look for .rm files (contain stroke data)
                            if name.endswith('.rm'):
                                # Note: Full .rm parsing requires remarks library
                                text_content.append(f"[Handwritten content in {name}]")
                            
                            # Look for text files
                            elif name.endswith('.txt') or name.endswith('.md'):
                                content = zf.read(name).decode('utf-8', errors='ignore')
                                text_content.append(content)
                            
                            # Look for highlights/annotations in content files
                            elif name.endswith('.content'):
                                try:
                                    content_data = json.loads(zf.read(name).decode('utf-8'))
                                    if 'text' in content_data:
                                        text_content.append(content_data['text'])
                                except:
                                    pass
                            
                            # Look for PDF highlights
                            elif 'highlights' in name.lower() or name.endswith('.json'):
                                try:
                                    data = json.loads(zf.read(name).decode('utf-8'))
                                    if isinstance(data, dict) and 'highlights' in data:
                                        for h in data.get('highlights', []):
                                            if 'text' in h:
                                                highlights.append(h['text'])
                                except:
                                    pass
                    
                    result["text_content"] = text_content if text_content else ["No extractable text found"]
                    result["highlights"] = highlights if highlights else []
                    result["note"] = "For full handwriting extraction, use remarkable_download_and_process"
                    
            except Exception as e:
                result["text_extraction_error"] = str(e)
        
        return json.dumps(result, indent=2)
        
    except Exception as e:
        return json.dumps({
            "error": str(e)
        }, indent=2)


@mcp.tool()
def remarkable_search(query: str) -> str:
    """
    Search for documents by name in reMarkable Cloud.
    
    Args:
        query: Search term to match against document names (case-insensitive)
    """
    try:
        from rmapy.api import Client
        from rmapy.document import Document
        from rmapy.folder import Folder
        
        client = get_rmapi()
        client.renew_token()
        
        collection = client.get_meta_items()
        
        query_lower = query.lower()
        matches = []
        
        # Build parent path lookup
        items_by_id = {item.ID: item for item in collection}
        
        def get_path(item):
            path_parts = [item.VissibleName]
            parent_id = item.Parent if hasattr(item, 'Parent') else ""
            while parent_id and parent_id in items_by_id:
                parent = items_by_id[parent_id]
                path_parts.insert(0, parent.VissibleName)
                parent_id = parent.Parent if hasattr(parent, 'Parent') else ""
            return "/" + "/".join(path_parts)
        
        for item in collection:
            if query_lower in item.VissibleName.lower():
                matches.append({
                    "name": item.VissibleName,
                    "path": get_path(item),
                    "type": "folder" if isinstance(item, Folder) else "document",
                    "id": item.ID,
                    "modified": item.ModifiedClient if hasattr(item, 'ModifiedClient') else None
                })
        
        return json.dumps({
            "query": query,
            "count": len(matches),
            "results": sorted(matches, key=lambda x: x["name"])
        }, indent=2)
        
    except Exception as e:
        return json.dumps({
            "error": str(e)
        }, indent=2)


@mcp.tool()
def remarkable_recent(limit: int = 10) -> str:
    """
    Get recently modified documents from reMarkable Cloud.
    
    Args:
        limit: Maximum number of documents to return (default: 10)
    """
    try:
        from rmapy.api import Client
        from rmapy.document import Document
        
        client = get_rmapi()
        client.renew_token()
        
        collection = client.get_meta_items()
        
        # Build parent path lookup
        items_by_id = {item.ID: item for item in collection}
        
        def get_path(item):
            path_parts = [item.VissibleName]
            parent_id = item.Parent if hasattr(item, 'Parent') else ""
            while parent_id and parent_id in items_by_id:
                parent = items_by_id[parent_id]
                path_parts.insert(0, parent.VissibleName)
                parent_id = parent.Parent if hasattr(parent, 'Parent') else ""
            return "/" + "/".join(path_parts)
        
        # Filter to documents only and sort by modified date
        documents = [item for item in collection if isinstance(item, Document)]
        documents.sort(
            key=lambda x: x.ModifiedClient if hasattr(x, 'ModifiedClient') and x.ModifiedClient else "",
            reverse=True
        )
        
        results = []
        for doc in documents[:limit]:
            results.append({
                "name": doc.VissibleName,
                "path": get_path(doc),
                "id": doc.ID,
                "modified": doc.ModifiedClient if hasattr(doc, 'ModifiedClient') else None
            })
        
        return json.dumps({
            "count": len(results),
            "documents": results
        }, indent=2)
        
    except Exception as e:
        return json.dumps({
            "error": str(e)
        }, indent=2)


@mcp.tool()
def remarkable_download_pdf(document_name: str, output_path: Optional[str] = None) -> str:
    """
    Download a document as PDF (if it's a PDF) or export notebook pages.
    
    Args:
        document_name: The name of the document to download
        output_path: Optional path to save the file. If not provided, saves to current directory.
    """
    try:
        from rmapy.api import Client
        from rmapy.document import Document
        
        client = get_rmapi()
        client.renew_token()
        
        # Find the document
        collection = client.get_meta_items()
        target_doc = None
        
        for item in collection:
            if isinstance(item, Document) and item.VissibleName == document_name:
                target_doc = item
                break
        
        if not target_doc:
            return json.dumps({
                "error": f"Document not found: {document_name}"
            }, indent=2)
        
        # Download the document
        raw_doc = client.download(target_doc)
        
        # Determine output path
        if output_path:
            out_path = Path(output_path)
        else:
            out_path = Path.cwd() / f"{target_doc.VissibleName}.zip"
        
        # Save the raw content (it's a zip containing all document data)
        with open(out_path, 'wb') as f:
            f.write(raw_doc.content)
        
        return json.dumps({
            "success": True,
            "document": target_doc.VissibleName,
            "saved_to": str(out_path),
            "size_bytes": len(raw_doc.content),
            "note": "File is a zip archive containing document data. Use remarks library to convert to PDF."
        }, indent=2)
        
    except Exception as e:
        return json.dumps({
            "error": str(e)
        }, indent=2)


if __name__ == "__main__":
    mcp.run()
