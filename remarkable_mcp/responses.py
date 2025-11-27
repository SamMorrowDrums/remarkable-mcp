"""
Response helpers for MCP tools.
"""

import json
from typing import Any, Dict, List, Optional


def make_response(data: Dict[str, Any], hint: str) -> str:
    """Create a JSON response with a hint for the model."""
    data["_hint"] = hint
    return json.dumps(data, indent=2)


def make_error(
    error_type: str, message: str, suggestion: str, did_you_mean: Optional[List[str]] = None
) -> str:
    """Create an educational error response."""
    error: Dict[str, Any] = {
        "_error": {"type": error_type, "message": message, "suggestion": suggestion}
    }
    if did_you_mean:
        error["_error"]["did_you_mean"] = did_you_mean
    return json.dumps(error, indent=2)
