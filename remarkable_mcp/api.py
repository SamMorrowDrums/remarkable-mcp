"""
reMarkable Cloud API client helpers.
"""

import json as json_module
import os
from pathlib import Path
from typing import Any, Dict, List

# Configuration - check env var first, then fall back to file
REMARKABLE_TOKEN = os.environ.get("REMARKABLE_TOKEN")
REMARKABLE_USE_SSH = os.environ.get("REMARKABLE_USE_SSH", "").lower() in ("1", "true", "yes")
REMARKABLE_CONFIG_DIR = Path.home() / ".remarkable"
REMARKABLE_TOKEN_FILE = REMARKABLE_CONFIG_DIR / "token"
CACHE_DIR = REMARKABLE_CONFIG_DIR / "cache"


def get_rmapi():
    """
    Get or initialize the reMarkable API client.

    Uses SSH transport if REMARKABLE_USE_SSH=1, otherwise cloud API.
    Returns either RemarkableClient or SSHClient (both have compatible interfaces).
    """
    # Check if SSH mode is enabled
    if REMARKABLE_USE_SSH:
        from remarkable_mcp.ssh import create_ssh_client

        return create_ssh_client()

    # Cloud API mode
    from remarkable_mcp.sync import load_client_from_token

    # If token is provided via environment, use it
    if REMARKABLE_TOKEN:
        # Also save to ~/.rmapi for compatibility
        rmapi_file = Path.home() / ".rmapi"
        rmapi_file.write_text(REMARKABLE_TOKEN)
        return load_client_from_token(REMARKABLE_TOKEN)

    # Load from file
    rmapi_file = Path.home() / ".rmapi"
    if not rmapi_file.exists():
        raise RuntimeError(
            "No reMarkable token found. Register first:\n"
            "  uvx remarkable-mcp --register <code>\n\n"
            "Get a code from: https://my.remarkable.com/device/desktop/connect\n\n"
            "Or use SSH mode (requires USB connection):\n"
            "  uvx remarkable-mcp --ssh"
        )

    try:
        token_json = rmapi_file.read_text()
        return load_client_from_token(token_json)
    except Exception as e:
        raise RuntimeError(f"Failed to initialize reMarkable client: {e}")


def ensure_config_dir():
    """Ensure configuration directory exists."""
    REMARKABLE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def register_and_get_token(one_time_code: str) -> str:
    """
    Register with reMarkable using a one-time code and return the token.

    Get a code from: https://my.remarkable.com/device/desktop/connect
    """
    from remarkable_mcp.sync import register_device

    try:
        token_data = register_device(one_time_code)

        # Save to ~/.rmapi for compatibility
        rmapi_file = Path.home() / ".rmapi"
        token_json = json_module.dumps(token_data)
        rmapi_file.write_text(token_json)

        return token_json
    except Exception as e:
        raise RuntimeError(str(e))


def get_items_by_id(collection) -> Dict[str, Any]:
    """Build a lookup dict of items by ID."""
    return {item.ID: item for item in collection}


def get_items_by_parent(collection) -> Dict[str, List]:
    """Build a lookup dict of items grouped by parent ID."""
    items_by_parent: Dict[str, List] = {}
    for item in collection:
        parent = item.Parent if hasattr(item, "Parent") else ""
        if parent not in items_by_parent:
            items_by_parent[parent] = []
        items_by_parent[parent].append(item)
    return items_by_parent


def get_item_path(item, items_by_id: Dict[str, Any]) -> str:
    """Get the full path of an item."""
    path_parts = [item.VissibleName]
    parent_id = item.Parent if hasattr(item, "Parent") else ""
    while parent_id and parent_id in items_by_id:
        parent = items_by_id[parent_id]
        path_parts.insert(0, parent.VissibleName)
        parent_id = parent.Parent if hasattr(parent, "Parent") else ""
    return "/" + "/".join(path_parts)
