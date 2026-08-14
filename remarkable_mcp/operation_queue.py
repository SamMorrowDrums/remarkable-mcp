"""Transport-neutral bounded operation scheduling."""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class OperationCancelled(RuntimeError):
    """A queued or active operation was cancelled."""


class OperationQueueClosed(RuntimeError):
    """The operation queue is closing and no longer accepts work."""


class OperationAwaitCancelled(asyncio.CancelledError):
    """Cancellation annotated with whether the operation had started."""

    def __init__(self, *, started: bool):
        super().__init__("Operation await cancelled")
        self.started = started


@dataclass
class OperationJob(Generic[T]):
    sequence: int
    operation: str
    callback: Callable[[], T]
    future: Future[T] = field(default_factory=Future)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    submitted_at: float = field(default_factory=time.monotonic)
    started_at: Optional[float] = None


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
        with self._state_lock:
            if self._closing:
                raise OperationQueueClosed(f"{self.name} is shutting down")
            self._sequence += 1
            job: OperationJob[T] = OperationJob(self._sequence, operation, callback)
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
        try:
            return await asyncio.wrap_future(job.future)
        except asyncio.CancelledError:
            self.cancel(job)
            try:
                await asyncio.shield(asyncio.wrap_future(job.future))
            except BaseException:
                pass
            raise OperationAwaitCancelled(started=job.started_at is not None)

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
                "max_concurrency": self.max_concurrency,
            }

    def close(self, timeout: float = 5.0) -> None:
        with self._state_lock:
            if self._closing:
                return
            self._closing = True
            active = list(self._active_jobs.values())
            hooks = list(self._active_cancel_hooks.values())

        while True:
            try:
                queued = self._queue.get_nowait()
            except queue.Empty:
                break
            if queued is not None:
                queued.cancel_event.set()
                if not queued.future.done():
                    queued.future.set_exception(
                        OperationQueueClosed(f"{self.name} shut down before operation started")
                    )
            self._queue.task_done()

        for job in active:
            job.cancel_event.set()
        for hook in hooks:
            hook()
        for _ in self._workers:
            self._queue.put(None)

        deadline = time.monotonic() + timeout
        for worker in self._workers:
            worker.join(max(0.0, deadline - time.monotonic()))

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
                    if job.cancel_event.is_set():
                        if not job.future.done():
                            job.future.set_exception(
                                OperationCancelled(f"{job.operation} cancelled before start")
                            )
                        continue
                    with self._state_lock:
                        self._active_jobs[ident] = job
                    job.started_at = time.monotonic()
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
