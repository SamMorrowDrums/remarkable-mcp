"""
reMarkable Cloud API client helpers.
"""

import os
from pathlib import Path
from typing import Any, Dict, List

# Configuration - check env var first, then fall back to file
REMARKABLE_TOKEN = os.environ.get("REMARKABLE_TOKEN")
REMARKABLE_CONFIG_DIR = Path.home() / ".remarkable"
REMARKABLE_TOKEN_FILE = REMARKABLE_CONFIG_DIR / "token"
CACHE_DIR = REMARKABLE_CONFIG_DIR / "cache"


def get_rmapi():
    """Get or initialize the reMarkable API client."""
    try:
        from rmapy.api import Client

        client = Client()

        # If token is provided via environment, use it
        if REMARKABLE_TOKEN:
            # rmapy stores token in ~/.rmapi, we need to write it there
            rmapi_file = Path.home() / ".rmapi"
            rmapi_file.write_text(REMARKABLE_TOKEN)

        # Renew/validate the token
        client.renew_token()

        return client
    except ImportError:
        raise RuntimeError("rmapy not installed. Run: uv add rmapy")
    except Exception as e:
        raise RuntimeError(f"Failed to initialize reMarkable client: {e}")


def ensure_config_dir():
    """Ensure configuration directory exists."""
    REMARKABLE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def register_and_get_token(one_time_code: str) -> str:
    """
    Register with reMarkable using a one-time code and return the token.
    """
    from rmapy.api import Client

    client = Client()
    client.register_device(one_time_code)

    rmapi_file = Path.home() / ".rmapi"
    if rmapi_file.exists():
        return rmapi_file.read_text().strip()
    else:
        raise RuntimeError("Registration succeeded but token file not found")


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
