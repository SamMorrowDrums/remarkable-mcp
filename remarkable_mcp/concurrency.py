"""Concurrency helpers for offloading blocking work from the asyncio event loop.

FastMCP awaits ``async def`` tool handlers directly on the asyncio event loop
without dispatching them to a thread pool, and it also calls plain ``def``
handlers inline. Any blocking I/O performed inside a tool handler — SSH
subprocess calls, ``requests`` HTTP requests, ``pymupdf``/``cairosvg``
rendering, ``pytesseract`` OCR, ``zipfile`` extraction — therefore blocks the
entire event loop. While that handler is running, no other ``call_tool``
request can make progress, which serializes concurrent requests and can make
the server appear to hang under parallel tool calls.

The fix is to push every blocking call onto a worker thread via
:func:`asyncio.to_thread`. :func:`run_blocking` is a thin wrapper for the
common case (call a single blocking function with args), so tool handlers can
write ``await run_blocking(ssh_client.get_meta_items)`` instead of repeating
``await asyncio.to_thread(...)`` everywhere.

Tools whose entire body is blocking (no inner ``await``) can alternatively
wrap their body in a nested ``def _impl()`` and ``return await
asyncio.to_thread(_impl)``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")


def run_blocking(func: Callable[..., T], *args: Any, **kwargs: Any) -> Awaitable[T]:
    """Run ``func(*args, **kwargs)`` in a worker thread.

    Returns the awaitable produced by :func:`asyncio.to_thread`. Use this to
    offload blocking I/O from an ``async def`` MCP tool handler so other
    concurrent tool calls can make progress on the event loop.
    """
    return asyncio.to_thread(func, *args, **kwargs)
