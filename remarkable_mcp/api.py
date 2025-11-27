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

    Get a code from: https://my.remarkable.com/device/desktop/connect
    """
    import json as json_module
    from uuid import uuid4

    import requests

    # Use the current remarkable API endpoint
    # (rmapy uses an outdated one, this is from ddvk/rmapi)
    device_token_url = "https://webapp-prod.cloud.remarkable.engineering/token/json/2/device/new"
    uuid = str(uuid4())
    body = {
        "code": one_time_code,
        "deviceDesc": "desktop-linux",
        "deviceID": uuid,
    }

    try:
        response = requests.post(device_token_url, json=body)

        if response.status_code == 200 and response.text:
            # Got a device token, save it in rmapy format
            device_token = response.text.strip()

            # rmapy expects a JSON file with devicetoken and usertoken
            rmapi_file = Path.home() / ".rmapi"
            token_data = {"devicetoken": device_token, "usertoken": ""}
            rmapi_file.write_text(json_module.dumps(token_data))

            return json_module.dumps(token_data)
        else:
            raise RuntimeError(
                f"Registration failed (HTTP {response.status_code})\n\n"
                "This usually means:\n"
                "  1. The code has expired (codes are single-use and expire quickly)\n"
                "  2. The code was already used\n"
                "  3. The code was typed incorrectly\n\n"
                "Get a new code from: https://my.remarkable.com/device/desktop/connect"
            )
    except requests.RequestException as e:
        raise RuntimeError(f"Network error during registration: {e}")


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
