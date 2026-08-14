"""Transport-neutral bounded operation scheduling."""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from concurrent.futures import Future
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")
_operation_cancel_dirty: ContextVar[Optional[bool]] = ContextVar(
    "operation_cancel_dirty",
    default=None,
)


class OperationCancelled(RuntimeError):
    """A queued or active operation was cancelled."""


class OperationQueueClosed(RuntimeError):
    """The operation queue is closing and no longer accepts work."""


def consume_operation_cancel_dirty() -> Optional[bool]:
    """Return and clear whether the current task's cancelled operation may have mutated."""
    dirty = _operation_cancel_dirty.get()
    _operation_cancel_dirty.set(None)
    return dirty


def _uncancel_current_task() -> None:
    task = asyncio.current_task()
    uncancel = getattr(task, "uncancel", None)
    if callable(uncancel):
        uncancel()


@dataclass
class OperationJob(Generic[T]):
    sequence: int
    operation: str
    callback: Callable[[], T]
    allow_during_close: bool = False
    future: Future[T] = field(default_factory=Future)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    submitted_at: float = field(default_factory=time.monotonic)
    started_at: Optional[float] = None
    cancel_dirty: Optional[bool] = None


class OperationDispatcher:
    """A fair queue backed by dedicated workers, independent of asyncio's executor."""

    def __init__(
        self,
        *,
        name: str,
        max_concurrency: int = 1,
        trace_enabled: Optional[Callable[[], bool]] = None,
    ):
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self.name = name
        self.max_concurrency = max_concurrency
        self._trace_enabled = trace_enabled or (lambda: False)
        self._queue: queue.Queue[Optional[OperationJob[Any]]] = queue.Queue()
        self._state_lock = threading.Lock()
        self._sequence = 0
        self._closing = False
        self._stopping = False
        self._stop_signals_sent = False
        self._active_jobs: dict[int, OperationJob[Any]] = {}
        self._active_cancel_hooks: dict[int, Callable[[], None]] = {}
        self._worker_ids: set[int] = set()
        self._workers = [
            threading.Thread(
                target=self._run,
                name=f"{name}-{index + 1}",
                daemon=True,
            )
            for index in range(max_concurrency)
        ]
        for worker in self._workers:
            worker.start()

    def in_worker_thread(self) -> bool:
        ident = threading.get_ident()
        with self._state_lock:
            return ident in self._worker_ids

    def submit(self, operation: str, callback: Callable[[], T]) -> OperationJob[T]:
        return self._submit(operation, callback, allow_during_close=False)

    def _submit(
        self,
        operation: str,
        callback: Callable[[], T],
        *,
        allow_during_close: bool,
    ) -> OperationJob[T]:
        with self._state_lock:
            if self._stopping or (self._closing and not allow_during_close):
                raise OperationQueueClosed(f"{self.name} is shutting down")
            self._sequence += 1
            job: OperationJob[T] = OperationJob(
                self._sequence,
                operation,
                callback,
                allow_during_close=allow_during_close,
            )
            self._queue.put(job)
            return job

    def call(self, operation: str, callback: Callable[[], T]) -> T:
        if self.in_worker_thread():
            return callback()
        job = self.submit(operation, callback)
        try:
            return job.future.result()
        except BaseException:
            if not job.future.done():
                job.cancel_event.set()
            raise

    async def call_async(self, operation: str, callback: Callable[[], T]) -> T:
        if self.in_worker_thread():
            return callback()
        job = self.submit(operation, callback)
        return await self._await_job(job)

    async def call_async_during_close(self, operation: str, callback: Callable[[], T]) -> T:
        """Run shutdown-critical cleanup after ordinary submissions are closed."""
        if self.in_worker_thread():
            return callback()
        job = self._submit(operation, callback, allow_during_close=True)
        return await self._await_job(job)

    async def _await_job(self, job: OperationJob[T]) -> T:
        wrapped = asyncio.wrap_future(job.future)
        try:
            return await asyncio.shield(wrapped)
        except asyncio.CancelledError as cancellation:
            self.cancel(job)
            _uncancel_current_task()
            while not wrapped.done():
                try:
                    await asyncio.shield(wrapped)
                except asyncio.CancelledError:
                    _uncancel_current_task()
                except BaseException:
                    break
            if wrapped.done():
                try:
                    wrapped.exception()
                except BaseException:
                    pass
            dirty = job.cancel_dirty
            _operation_cancel_dirty.set(job.started_at is not None if dirty is None else dirty)
            raise cancellation

    def cancel(self, job: OperationJob[Any]) -> None:
        job.cancel_event.set()
        with self._state_lock:
            hook = self._active_cancel_hooks.get(job.sequence)
        if hook is not None:
            hook()

    def current_cancel_event(self) -> threading.Event:
        ident = threading.get_ident()
        with self._state_lock:
            job = self._active_jobs.get(ident)
            return job.cancel_event if job is not None else threading.Event()

    def set_current_cancel_hook(self, hook: Optional[Callable[[], None]]) -> None:
        ident = threading.get_ident()
        with self._state_lock:
            job = self._active_jobs.get(ident)
            if job is None:
                return
            if hook is None:
                self._active_cancel_hooks.pop(job.sequence, None)
            else:
                self._active_cancel_hooks[job.sequence] = hook

    def set_current_cancel_dirty(self, dirty: bool) -> None:
        ident = threading.get_ident()
        with self._state_lock:
            job = self._active_jobs.get(ident)
            if job is not None:
                job.cancel_dirty = dirty

    def diagnostics(self) -> dict[str, Any]:
        with self._state_lock:
            active = sorted(
                (
                    {"sequence": job.sequence, "operation": job.operation}
                    for job in self._active_jobs.values()
                ),
                key=lambda item: item["sequence"],
            )
            return {
                "queue_depth": self._queue.qsize(),
                "active": active,
                "closing": self._closing,
                "stopping": self._stopping,
                "max_concurrency": self.max_concurrency,
            }

    def close(self, timeout: float = 5.0) -> None:
        self.begin_close()
        self.finish_close(timeout)

    def begin_close(self) -> None:
        """Reject ordinary work and cancel it while keeping cleanup workers alive."""
        with self._state_lock:
            if self._closing:
                return
            self._closing = True
            active = [job for job in self._active_jobs.values() if not job.allow_during_close]
            active_sequences = {job.sequence for job in active}
            hooks = [
                hook
                for sequence, hook in self._active_cancel_hooks.items()
                if sequence in active_sequences
            ]

        self._drain_queued(preserve_close_allowed=True)
        for job in active:
            job.cancel_event.set()
        for hook in hooks:
            hook()

    def finish_close(self, timeout: float = 5.0) -> None:
        """Stop workers after shutdown-critical cleanup has drained."""
        self.begin_close()
        with self._state_lock:
            first_stop = not self._stopping
            self._stopping = True
            active = list(self._active_jobs.values())
            hooks = list(self._active_cancel_hooks.values())

        if first_stop:
            self._drain_queued(preserve_close_allowed=False)
            for job in active:
                job.cancel_event.set()
            for hook in hooks:
                hook()
            with self._state_lock:
                if not self._stop_signals_sent:
                    self._stop_signals_sent = True
                    worker_count = len(self._workers)
                else:
                    worker_count = 0
            for _ in range(worker_count):
                self._queue.put(None)

        deadline = time.monotonic() + timeout
        current_ident = threading.get_ident()
        for worker in self._workers:
            if worker.ident == current_ident:
                continue
            worker.join(max(0.0, deadline - time.monotonic()))

    def is_closing(self) -> bool:
        with self._state_lock:
            return self._closing

    def _drain_queued(self, *, preserve_close_allowed: bool) -> None:
        preserved: list[OperationJob[Any]] = []
        while True:
            try:
                queued = self._queue.get_nowait()
            except queue.Empty:
                break
            if queued is not None and preserve_close_allowed and queued.allow_during_close:
                preserved.append(queued)
            elif queued is not None:
                queued.cancel_event.set()
                if not queued.future.done():
                    queued.future.set_exception(
                        OperationQueueClosed(f"{self.name} shut down before operation started")
                    )
            self._queue.task_done()
        for queued in preserved:
            self._queue.put(queued)

    def _run(self) -> None:
        ident = threading.get_ident()
        with self._state_lock:
            self._worker_ids.add(ident)
        try:
            while True:
                job = self._queue.get()
                try:
                    if job is None:
                        return
                    with self._state_lock:
                        cannot_start = self._stopping or (
                            self._closing and not job.allow_during_close
                        )
                        cancelled = job.cancel_event.is_set()
                        if not cannot_start and not cancelled:
                            self._active_jobs[ident] = job
                            job.started_at = time.monotonic()
                    if cannot_start or cancelled:
                        if not job.future.done():
                            if cannot_start:
                                error = OperationQueueClosed(
                                    f"{self.name} shut down before operation started"
                                )
                            else:
                                error = OperationCancelled(
                                    f"{job.operation} cancelled before start"
                                )
                            job.future.set_exception(error)
                        continue
                    if self._trace_enabled():
                        logger.warning(
                            "%s queue start id=%d operation=%s wait=%.3fs depth=%d",
                            self.name,
                            job.sequence,
                            job.operation,
                            job.started_at - job.submitted_at,
                            self._queue.qsize(),
                        )
                    try:
                        result = job.callback()
                    except BaseException as exc:
                        if not job.future.done():
                            job.future.set_exception(exc)
                    else:
                        if not job.future.done():
                            job.future.set_result(result)
                    finally:
                        with self._state_lock:
                            self._active_jobs.pop(ident, None)
                            self._active_cancel_hooks.pop(job.sequence, None)
                finally:
                    self._queue.task_done()
        finally:
            with self._state_lock:
                self._worker_ids.discard(ident)
