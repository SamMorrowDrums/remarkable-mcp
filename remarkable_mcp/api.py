"""
reMarkable Cloud API client helpers.
"""

import json as json_module
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# Configuration - check env var first, then fall back to file
REMARKABLE_TOKEN = os.environ.get("REMARKABLE_TOKEN")
REMARKABLE_USE_SSH = os.environ.get("REMARKABLE_USE_SSH", "").lower() in (
    "1",
    "true",
    "yes",
)
REMARKABLE_USE_USB_WEB = os.environ.get("REMARKABLE_USE_USB_WEB", "").lower() in (
    "1",
    "true",
    "yes",
)
# When a device transport (USB web / SSH) is selected but the tablet is not
# reachable at startup, automatically fall back to cloud mode if a cloud token
# is configured. This lets the *same* configuration work whether or not the
# physical device is connected. Set REMARKABLE_DISABLE_CLOUD_FALLBACK=1 to
# keep the strict behaviour (fail instead of falling back).
REMARKABLE_DISABLE_CLOUD_FALLBACK = os.environ.get(
    "REMARKABLE_DISABLE_CLOUD_FALLBACK", ""
).lower() in (
    "1",
    "true",
    "yes",
)
REMARKABLE_CONFIG_DIR = Path.home() / ".remarkable"
REMARKABLE_TOKEN_FILE = REMARKABLE_CONFIG_DIR / "token"
CACHE_DIR = REMARKABLE_CONFIG_DIR / "cache"

# Process-level cloud client cache. The cloud client exchanges its long-lived
# device token for a short-lived user token on first use; caching the client
# keeps that user token in memory so we don't re-exchange it (an extra network
# round-trip, and a rate-limit risk) on every single tool call.
_cloud_client = None
_cloud_client_key = None
_cloud_client_lock = threading.Lock()

# Process-level cache for the device-transport resolution. Resolving the
# transport probes the device once (USB/SSH check_connection), so we cache the
# outcome to avoid re-probing on every tool call. `_fell_back_to_cloud` records
# that a device transport was selected but unreachable and we switched to cloud.
_device_client = None
_fell_back_to_cloud = False
_device_resolve_lock = threading.Lock()


def reset_client_cache() -> None:
    """Clear the cached clients (e.g. after re-registering a token)."""
    global _cloud_client, _cloud_client_key
    global _device_client, _fell_back_to_cloud
    with _cloud_client_lock:
        _cloud_client = None
        _cloud_client_key = None
    with _device_resolve_lock:
        _device_client = None
        _fell_back_to_cloud = False


def _is_cloud_token_available() -> bool:
    """Return True if a cloud token is configured (env var or saved file)."""
    if REMARKABLE_TOKEN:
        return True
    return (Path.home() / ".rmapi").exists()


def _safe_check_connection(client) -> bool:
    """Probe a device client's reachability without raising.

    Returns True when the client has no ``check_connection`` (can't probe, so
    assume usable) or the probe succeeds; False when the probe returns falsy or
    raises (network/subprocess failure => treat the device as unreachable).
    """
    check = getattr(client, "check_connection", None)
    if not callable(check):
        return True
    try:
        return bool(check())
    except Exception as e:
        logger.debug("Device connectivity probe failed: %s", e)
        return False


def _get_cloud_client():
    """Resolve (and cache) the cloud client from the configured token."""
    from remarkable_mcp.sync import load_client_from_token

    # Resolve the token: env var wins, else the saved ~/.rmapi file.
    if REMARKABLE_TOKEN:
        # Also save to ~/.rmapi for compatibility
        rmapi_file = Path.home() / ".rmapi"
        rmapi_file.write_text(REMARKABLE_TOKEN)
        token_json = REMARKABLE_TOKEN
    else:
        rmapi_file = Path.home() / ".rmapi"
        if not rmapi_file.exists():
            raise RuntimeError(
                "No reMarkable token found. Register first:\n"
                "  uvx remarkable-mcp --register <code>\n\n"
                "Get a code from: https://my.remarkable.com/device/desktop/connect\n\n"
                "Or use USB web interface (no dev mode required):\n"
                "  uvx remarkable-mcp --usb-web\n\n"
                "Or use SSH mode (requires USB connection + developer mode):\n"
                "  uvx remarkable-mcp --ssh"
            )
        try:
            token_json = rmapi_file.read_text()
        except Exception as e:
            raise RuntimeError(f"Failed to initialize reMarkable client: {e}")

    # Reuse one client per process so the renewed user token is cached in
    # memory. The cache is keyed on the token string, so re-registering a new
    # token transparently rebuilds the client.
    global _cloud_client, _cloud_client_key
    with _cloud_client_lock:
        if _cloud_client is None or _cloud_client_key != token_json:
            _cloud_client = load_client_from_token(token_json)
            _cloud_client_key = token_json
        return _cloud_client


def get_rmapi():
    """
    Get or initialize the reMarkable API client.

    Priority order:
    1. USB web interface (if REMARKABLE_USE_USB_WEB=1)
    2. SSH (if REMARKABLE_USE_SSH=1)
    3. Cloud API (default, requires token)

    When a device transport (USB/SSH) is selected but the tablet is unreachable
    at startup, this falls back to cloud mode if a cloud token is configured
    (unless REMARKABLE_DISABLE_CLOUD_FALLBACK is set), so the same configuration
    works with or without the physical device connected.

    Returns RemarkableClient, SSHClient, or USBWebClient (all have compatible interfaces).
    """
    global _device_client, _fell_back_to_cloud

    # Device transports (USB web / SSH) — probe once, cache the resolution, and
    # optionally fall back to cloud if the device is unreachable.
    if REMARKABLE_USE_USB_WEB or REMARKABLE_USE_SSH:
        with _device_resolve_lock:
            if _fell_back_to_cloud:
                return _get_cloud_client()
            if _device_client is not None:
                return _device_client

            if REMARKABLE_USE_USB_WEB:
                from remarkable_mcp.usb_web import create_usb_web_client

                client = create_usb_web_client()
                mode_label = "USB web interface"
            else:
                from remarkable_mcp.ssh import create_ssh_client

                client = create_ssh_client()
                mode_label = "SSH"

            if _safe_check_connection(client):
                _device_client = client
                return client

            # Device unreachable — fall back to cloud if allowed and possible.
            if not REMARKABLE_DISABLE_CLOUD_FALLBACK and _is_cloud_token_available():
                logger.warning(
                    "%s is not reachable; falling back to cloud mode because a "
                    "cloud token is configured. Set "
                    "REMARKABLE_DISABLE_CLOUD_FALLBACK=1 to disable this fallback.",
                    mode_label,
                )
                _fell_back_to_cloud = True
                return _get_cloud_client()

            # No fallback: return the (unreachable) device client so its own
            # operation errors surface, and don't cache it so a later call
            # works once the device is connected.
            return client

    # Cloud API mode (default).
    return _get_cloud_client()


def get_active_transport() -> str:
    """Return the transport actually in use ("cloud", "ssh", or "usb-web").

    Reflects the resolution performed by get_rmapi(): if a device transport was
    selected but the tablet was unreachable and we fell back to cloud, this
    returns "cloud". Note the fallback decision is only made once get_rmapi()
    has been called, so callers should resolve the client first if they need an
    accurate post-fallback value.
    """
    if _fell_back_to_cloud:
        return "cloud"
    if REMARKABLE_USE_USB_WEB:
        return "usb-web"
    if REMARKABLE_USE_SSH:
        return "ssh"
    return "cloud"


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

        # A new token invalidates any cached cloud client.
        reset_client_cache()

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


def download_raw_file(client, doc, extension: str):
    """
    Download a raw file (PDF or EPUB) for a document.

    Args:
        client: The reMarkable API client (SSH or Cloud)
        doc: The document to download
        extension: File extension without dot (e.g., 'pdf', 'epub')

    Returns:
        Raw file bytes, or None if file doesn't exist or not supported
    """
    # All transports (cloud, SSH, USB web) implement download_raw_file. The
    # cloud store keeps the original source blob alongside the notebook data, so
    # PDFs/EPUBs are downloadable in every mode.
    if hasattr(client, "download_raw_file"):
        return client.download_raw_file(doc, extension)

    return None


def get_file_type(client, doc) -> str:
    """
    Get the file type (pdf, epub, notebook) for a document.

    Args:
        client: The reMarkable API client (SSH or Cloud)
        doc: The document to check

    Returns:
        File type string: 'pdf', 'epub', or 'notebook'
    """
    # Every transport implements get_file_type. Cloud derives it from the blob
    # index, SSH/USB read the .content fileType field.
    if hasattr(client, "get_file_type"):
        file_type = client.get_file_type(doc)
        if file_type:
            return file_type

    # Last-resort fallback: infer from the document name.
    name = doc.VissibleName.lower()
    if name.endswith(".pdf"):
        return "pdf"
    elif name.endswith(".epub"):
        return "epub"

    return "notebook"
