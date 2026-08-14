"""Fair, bounded execution for SSH work against a reMarkable tablet."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, TypeVar

from remarkable_mcp.operation_queue import (
    OperationCancelled,
    OperationDispatcher,
    OperationQueueClosed,
    consume_operation_cancel_dirty,
)

logger = logging.getLogger(__name__)
T = TypeVar("T")

_TRUE_VALUES = {"1", "true", "yes"}
_PRE_EXECUTION_PATTERNS = (
    ("connection_refused", re.compile(r"^ssh: connect to host .+: Connection refused$", re.I)),
    (
        "connection_timeout",
        re.compile(
            r"^ssh: connect to host .+: (?:Connection timed out|Operation timed out)$",
            re.I,
        ),
    ),
    (
        "network_unreachable",
        re.compile(
            r"^ssh: connect to host .+: "
            r"(?:No route to host|Network is unreachable|Host is down)$",
            re.I,
        ),
    ),
    (
        "banner_timeout",
        re.compile(
            r"^(?:Connection timed out during banner exchange|"
            r"kex_exchange_identification: Connection timed out)$",
            re.I,
        ),
    ),
)


def _uncancel_current_task() -> None:
    """Clear one cancellation request when supported (Python 3.11+)."""
    task = asyncio.current_task()
    uncancel = getattr(task, "uncancel", None)
    if callable(uncancel):
        uncancel()


class SSHReliabilityError(RuntimeError):
    """Base class for failures with known SSH execution semantics."""


class SSHPreExecutionError(SSHReliabilityError):
    """OpenSSH proved that no remote command started."""

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        attempts: int = 1,
        elapsed: float = 0.0,
    ):
        super().__init__(message)
        self.reason = reason
        self.attempts = attempts
        self.elapsed = elapsed


class SSHExecutionUnknownError(SSHReliabilityError):
    """The remote command may have started, so replay is unsafe."""


class SSHRemoteCommandError(SSHReliabilityError):
    """The SSH session started and the remote command failed."""


class SSHJobCancelled(OperationCancelled, SSHReliabilityError):
    """A queued or active SSH job was cancelled."""


class SSHDispatcherClosed(OperationQueueClosed, SSHReliabilityError):
    """The dispatcher is closing and no longer accepts work."""


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


@dataclass(frozen=True)
class SSHRetryPolicy:
    """Bounded retry settings for proven pre-execution connection failures."""

    max_attempts: int = 4
    initial_delay: float = 0.5
    multiplier: float = 2.0
    max_delay: float = 2.0
    wake_grace: float = 8.0

    @classmethod
    def from_env(cls) -> SSHRetryPolicy:
        return cls(
            max_attempts=_env_int("REMARKABLE_SSH_MAX_ATTEMPTS", 4, minimum=1, maximum=10),
            initial_delay=_env_float(
                "REMARKABLE_SSH_BACKOFF_INITIAL", 0.5, minimum=0.0, maximum=30.0
            ),
            multiplier=_env_float(
                "REMARKABLE_SSH_BACKOFF_MULTIPLIER", 2.0, minimum=1.0, maximum=10.0
            ),
            max_delay=_env_float("REMARKABLE_SSH_BACKOFF_MAX", 2.0, minimum=0.0, maximum=60.0),
            wake_grace=_env_float("REMARKABLE_SSH_WAKE_GRACE", 8.0, minimum=0.0, maximum=120.0),
        )

    def delay(self, retry_index: int) -> float:
        """Return the delay before retry number ``retry_index`` (1-based)."""
        return min(self.initial_delay * self.multiplier ** (retry_index - 1), self.max_delay)

    def diagnostics(self) -> dict[str, float | int]:
        return {
            "max_attempts": self.max_attempts,
            "initial_delay": self.initial_delay,
            "multiplier": self.multiplier,
            "max_delay": self.max_delay,
            "wake_grace": self.wake_grace,
        }


def classify_pre_execution_failure(
    returncode: int,
    stdout: bytes,
    stderr: bytes,
    *,
    execution_marker: bytes,
) -> Optional[str]:
    """Classify only failures that prove the remote command never started."""
    if returncode != 255 or stdout or execution_marker in stderr:
        return None
    text = stderr.decode("utf-8", errors="replace").strip()
    for line in text.splitlines():
        stripped = line.strip()
        for reason, pattern in _PRE_EXECUTION_PATTERNS:
            if pattern.fullmatch(stripped):
                return reason
    return None


class SSHDispatcher(OperationDispatcher):
    """Single-worker operation queue with SSH retry diagnostics."""

    def __init__(self, *, retry_policy: Optional[SSHRetryPolicy] = None):
        self.retry_policy = retry_policy or SSHRetryPolicy.from_env()
        self._retry_lock = threading.Lock()
        self._retries = 0
        self._last_connection_failure: Optional[str] = None
        super().__init__(
            name="SSH",
            max_concurrency=1,
            trace_enabled=lambda: os.environ.get("REMARKABLE_SSH_TRACE", "").lower()
            in _TRUE_VALUES,
        )

    def record_retry(self, reason: str) -> None:
        with self._retry_lock:
            self._retries += 1
            self._last_connection_failure = reason

    def diagnostics(self) -> dict:
        result = super().diagnostics()
        with self._retry_lock:
            result.update(
                {
                    "pre_execution_retries": self._retries,
                    "last_connection_failure": self._last_connection_failure,
                    "retry_policy": self.retry_policy.diagnostics(),
                }
            )
        return result

    @staticmethod
    def shutdown_timeout() -> float:
        return _env_float(
            "REMARKABLE_SSH_SHUTDOWN_TIMEOUT",
            5.0,
            minimum=0.1,
            maximum=60.0,
        )

    def close(self, timeout: Optional[float] = None) -> None:
        if timeout is None:
            timeout = self.shutdown_timeout()
        super().close(timeout)


@dataclass
class _RefreshGeneration:
    number: int
    created_at: float
    future: asyncio.Future[None]
    all_mutations_done: asyncio.Event
    participants: int = 0
    completed: int = 0
    dirty: bool = False
    closed: bool = False
    leader_taken: bool = False


class SSHRefreshCoordinator:
    """Coalesce concurrent non-deferred writes into bounded refresh generations."""

    def __init__(self):
        self.debounce = _env_float(
            "REMARKABLE_SSH_REFRESH_DEBOUNCE", 0.15, minimum=0.0, maximum=5.0
        )
        self.max_wait = _env_float(
            "REMARKABLE_SSH_REFRESH_MAX_WAIT", 1.0, minimum=0.01, maximum=30.0
        )
        self._lock = asyncio.Lock()
        self._generation: Optional[_RefreshGeneration] = None
        self._generations: dict[int, _RefreshGeneration] = {}
        self._next_generation = 1
        self._deferred_dirty = False
        self._pending_state_known = False
        self._dirty_epoch = 0
        self._refresh_count = 0
        self._last_refresh_error: Optional[str] = None
        self._closing = False
        self._explicit_future: Optional[asyncio.Future[bool]] = None
        self._snapshot_lock = threading.Lock()
        self._snapshot: dict = {
            "generation": None,
            "participants": 0,
            "dirty": False,
            "deferred_dirty": False,
            "pending_state_known": False,
            "closing": False,
            "debounce": self.debounce,
            "max_wait": self.max_wait,
            "refreshes": 0,
            "last_error": None,
        }

    async def run_write(
        self,
        mutation: Callable[[], Awaitable[T]],
        refresh: Callable[[], Awaitable[None]],
        *,
        deferred: bool,
        persisted: Callable[[T], bool],
        observe_dirty: Optional[Callable[[bool], None]] = None,
    ) -> tuple[T, bool]:
        consume_operation_cancel_dirty()
        if deferred:
            try:
                result = await mutation()
                operation_dirty = bool(consume_operation_cancel_dirty())
                dirty = persisted(result) or operation_dirty
            except BaseException as exc:
                operation_dirty = bool(consume_operation_cancel_dirty())
                if isinstance(exc, asyncio.CancelledError):
                    dirty = operation_dirty
                else:
                    dirty = operation_dirty or not isinstance(
                        exc,
                        (
                            SSHPreExecutionError,
                            SSHJobCancelled,
                            OperationCancelled,
                            OperationQueueClosed,
                        ),
                    )
                if observe_dirty is not None:
                    observe_dirty(dirty)
                if dirty:
                    async with self._lock:
                        self._mark_pending_locked()
                        self._update_snapshot_locked()
                raise
            if dirty:
                async with self._lock:
                    self._mark_pending_locked()
                    self._update_snapshot_locked()
            if observe_dirty is not None:
                observe_dirty(dirty)
            return result, False

        generation = await self._join_generation()
        result: Optional[T] = None
        mutation_error: Optional[BaseException] = None
        dirty = False
        try:
            result = await mutation()
            operation_dirty = bool(consume_operation_cancel_dirty())
            dirty = persisted(result) or operation_dirty
        except BaseException as exc:
            mutation_error = exc
            operation_dirty = bool(consume_operation_cancel_dirty())
            if isinstance(exc, asyncio.CancelledError):
                dirty = operation_dirty
            else:
                dirty = operation_dirty or not isinstance(
                    exc,
                    (
                        SSHPreExecutionError,
                        SSHJobCancelled,
                        OperationCancelled,
                        OperationQueueClosed,
                    ),
                )
        if observe_dirty is not None:
            observe_dirty(dirty)

        leader = False
        async with self._lock:
            generation.completed += 1
            generation.dirty = generation.dirty or dirty
            if generation.closed and generation.completed == generation.participants:
                generation.all_mutations_done.set()
            if not generation.leader_taken:
                generation.leader_taken = True
                leader = True
            self._update_snapshot_locked()

        cancellation: Optional[asyncio.CancelledError] = None
        leader_task: Optional[asyncio.Task[None]] = None
        refresh_error: Optional[BaseException] = None
        try:
            if leader:
                leader_task = asyncio.create_task(self._lead_generation(generation, refresh))
                await asyncio.shield(leader_task)
            else:
                await asyncio.shield(generation.future)
        except asyncio.CancelledError as exc:
            cancellation = exc
            _uncancel_current_task()
            try:
                if leader:
                    await leader_task
                else:
                    await asyncio.shield(generation.future)
            except BaseException as drain_error:
                if not isinstance(drain_error, asyncio.CancelledError):
                    logger.warning(
                        "SSH safety refresh failed while preserving cancellation: %s",
                        drain_error,
                    )
        except BaseException as exc:
            refresh_error = exc

        if cancellation is not None:
            raise cancellation
        if refresh_error is not None and dirty:
            raise refresh_error
        if mutation_error is not None:
            raise mutation_error
        return result, generation.dirty

    async def refresh_explicit(self, refresh: Callable[[], Awaitable[None]]) -> bool:
        refresh_epoch = 0
        async with self._lock:
            existing = self._explicit_future
            if existing is not None and not existing.done():
                follower = existing
                leader = False
            elif self._pending_state_known and not self._deferred_dirty:
                return False
            else:
                follower = asyncio.get_running_loop().create_future()
                self._explicit_future = follower
                refresh_epoch = self._dirty_epoch
                leader = True
        if not leader:
            return await asyncio.shield(follower)
        refresh_task = asyncio.create_task(refresh())
        cancellation: Optional[asyncio.CancelledError] = None
        failure: Optional[BaseException] = None
        try:
            await asyncio.shield(refresh_task)
        except asyncio.CancelledError as exc:
            cancellation = exc
            _uncancel_current_task()
            try:
                await refresh_task
            except BaseException as refresh_exc:
                failure = refresh_exc
        except BaseException as exc:
            failure = exc

        try:
            if failure is not None:
                async with self._lock:
                    self._mark_pending_locked(failure)
                    self._update_snapshot_locked()
                if not follower.done():
                    follower.set_exception(failure)
                    follower.exception()
                if cancellation is not None:
                    raise cancellation
                raise failure
            async with self._lock:
                self._pending_state_known = True
                if self._dirty_epoch == refresh_epoch:
                    self._deferred_dirty = False
                self._refresh_count += 1
                self._last_refresh_error = None
                self._update_snapshot_locked()
            if not follower.done():
                follower.set_result(True)
            if cancellation is not None:
                raise cancellation
            return True
        except BaseException:
            if not follower.done():
                follower.set_exception(SSHDispatcherClosed("Explicit refresh did not complete"))
            raise
        finally:
            async with self._lock:
                if self._explicit_future is follower:
                    self._explicit_future = None

    async def close(self, timeout: float) -> bool:
        """Stop new generations and give in-flight refresh work a bounded drain."""
        async with self._lock:
            self._closing = True
            explicit = self._explicit_future
            self._update_snapshot_locked()
            futures = [generation.future for generation in self._generations.values()]
            if explicit is not None and explicit not in futures:
                futures.append(explicit)
        if not futures:
            return True

        waiter = asyncio.gather(*futures, return_exceptions=True)
        try:
            results = await asyncio.wait_for(asyncio.shield(waiter), timeout=timeout)
        except asyncio.TimeoutError:
            failure = SSHDispatcherClosed(
                f"Server shutdown timed out after {timeout:.1f}s waiting for refresh"
            )
            async with self._lock:
                self._mark_pending_locked(failure)
                for future in futures:
                    if not future.done():
                        future.set_exception(failure)
                        future.exception()
                self._update_snapshot_locked()
            await waiter
            logger.warning("%s; explicit refresh is required on the next connection", failure)
            return False

        for result in results:
            if isinstance(result, BaseException) and not isinstance(
                result, (SSHDispatcherClosed, asyncio.CancelledError)
            ):
                logger.warning("SSH refresh did not complete during shutdown: %s", result)
        return True

    def diagnostics(self) -> dict:
        with self._snapshot_lock:
            return dict(self._snapshot)

    async def _join_generation(self) -> _RefreshGeneration:
        async with self._lock:
            if self._closing:
                raise SSHDispatcherClosed("Refresh coordinator is shutting down")
            now = time.monotonic()
            generation = self._generation
            if (
                generation is None
                or generation.closed
                or now - generation.created_at >= self.max_wait
            ):
                generation = _RefreshGeneration(
                    number=self._next_generation,
                    created_at=now,
                    future=asyncio.get_running_loop().create_future(),
                    all_mutations_done=asyncio.Event(),
                )
                self._next_generation += 1
                self._generation = generation
                self._generations[generation.number] = generation
            generation.participants += 1
            self._update_snapshot_locked()
            return generation

    async def _lead_generation(
        self,
        generation: _RefreshGeneration,
        refresh: Callable[[], Awaitable[None]],
    ) -> None:
        remaining = max(0.0, self.max_wait - (time.monotonic() - generation.created_at))
        await asyncio.sleep(min(self.debounce, remaining))
        async with self._lock:
            generation.closed = True
            if generation.completed == generation.participants:
                generation.all_mutations_done.set()
            self._update_snapshot_locked()
        await generation.all_mutations_done.wait()

        did_refresh = False
        refresh_epoch = 0
        try:
            if generation.dirty:
                async with self._lock:
                    refresh_epoch = self._dirty_epoch
                await refresh()
                did_refresh = True
        except BaseException as exc:
            try:
                setattr(exc, "_remarkable_refresh_failed", True)
            except (AttributeError, TypeError):
                pass
            async with self._lock:
                self._mark_pending_locked(exc)
                self._update_snapshot_locked()
            if not generation.future.done():
                generation.future.set_exception(exc)
                generation.future.exception()
            raise
        else:
            if did_refresh:
                async with self._lock:
                    self._pending_state_known = True
                    if self._dirty_epoch == refresh_epoch:
                        self._deferred_dirty = False
                    self._refresh_count += 1
                    self._last_refresh_error = None
                    self._update_snapshot_locked()
            if not generation.future.done():
                generation.future.set_result(None)
        finally:
            async with self._lock:
                self._generations.pop(generation.number, None)
                if self._generation is generation:
                    self._generation = None
                self._update_snapshot_locked()

    def _mark_pending_locked(self, error: Optional[BaseException] = None) -> None:
        self._deferred_dirty = True
        self._pending_state_known = True
        self._dirty_epoch += 1
        if error is not None:
            self._last_refresh_error = f"{type(error).__name__}: {error}"

    def _update_snapshot_locked(self) -> None:
        generation = self._generation
        snapshot = {
            "generation": generation.number if generation else None,
            "participants": generation.participants if generation else 0,
            "dirty": generation.dirty if generation else False,
            "deferred_dirty": self._deferred_dirty,
            "pending_state_known": self._pending_state_known,
            "closing": self._closing,
            "debounce": self.debounce,
            "max_wait": self.max_wait,
            "refreshes": self._refresh_count,
            "last_error": self._last_refresh_error,
        }
        with self._snapshot_lock:
            self._snapshot = snapshot
