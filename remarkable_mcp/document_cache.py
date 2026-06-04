import logging
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_TTL_SECONDS = 300

_lock = threading.Lock()
_collection: List[Any] = []
_items_by_id: Dict[str, Any] = {}
_loaded_at: float = 0.0
_client: Optional[Any] = None


def set_collection(client: Any, collection: List[Any]) -> None:
    global _collection, _items_by_id, _loaded_at, _client
    with _lock:
        _client = client
        _collection = list(collection)
        _items_by_id = {item.ID: item for item in collection}
        _loaded_at = time.monotonic()
    logger.debug("document_cache: stored %d items", len(collection))


def get_collection() -> Optional[List[Any]]:
    with _lock:
        if _loaded_at == 0.0:
            return None
        age = time.monotonic() - _loaded_at
        if age > _TTL_SECONDS:
            logger.debug("document_cache: TTL expired (age=%.0fs)", age)
            return None
        return list(_collection)


def get_client() -> Optional[Any]:
    with _lock:
        return _client


def is_fresh() -> bool:
    with _lock:
        if _loaded_at == 0.0:
            return False
        return (time.monotonic() - _loaded_at) <= _TTL_SECONDS


def invalidate() -> None:
    global _collection, _items_by_id, _loaded_at, _client
    with _lock:
        _collection = []
        _items_by_id = {}
        _loaded_at = 0.0
        _client = None
    logger.debug("document_cache: invalidated")
