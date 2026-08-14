"""Bounded temporary resources for locally generated exports."""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable, Generic, Literal, TypeVar
from uuid import uuid4

ExportFormat = Literal["pdf", "markdown"]
T = TypeVar("T")

EXPORT_TTL_SECONDS = 15 * 60
EXPORT_MAX_ENTRIES = 8

_FORMAT_DETAILS: dict[ExportFormat, tuple[str, str]] = {
    "pdf": (".pdf", "application/pdf"),
    "markdown": (".md", "text/markdown; charset=utf-8"),
}


@dataclass(frozen=True)
class ExportResource:
    """Public metadata for a temporary export resource."""

    export_id: str
    filename: str
    output_format: ExportFormat
    mime_type: str
    size: int
    uri: str
    created_at: datetime
    expires_at: datetime


@dataclass
class _ExportRecord:
    resource: ExportResource
    path: Path
    expires_monotonic: float


@dataclass(frozen=True)
class PublishedExport(Generic[T]):
    """A published resource and the exporter-specific build result."""

    resource: ExportResource
    result: T


class ExportResourceStore:
    """Store a bounded set of expiring export files under one private temp root."""

    def __init__(
        self,
        *,
        ttl_seconds: int = EXPORT_TTL_SECONDS,
        max_entries: int = EXPORT_MAX_ENTRIES,
        root: Path | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] | None = None,
    ):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")

        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._configured_root = root
        self._temporary_directory: TemporaryDirectory[str] | None = None
        self._records: OrderedDict[str, _ExportRecord] = OrderedDict()
        self._clock = monotonic_clock
        self._utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()

    def _root(self) -> Path:
        if self._configured_root is not None:
            self._configured_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            return self._configured_root
        if self._temporary_directory is None:
            self._temporary_directory = TemporaryDirectory(prefix="remarkable-mcp-export-")
        return Path(self._temporary_directory.name)

    @staticmethod
    def _safe_filename(filename: str, output_format: ExportFormat) -> str:
        suffix, _ = _FORMAT_DETAILS[output_format]
        leaf = Path(filename).name.strip().strip(".")
        leaf = re.sub(r"[^\w .-]+", "_", leaf, flags=re.UNICODE)
        leaf = re.sub(r"\s+", " ", leaf).strip()
        if not leaf:
            leaf = "remarkable-export"
        if not leaf.lower().endswith(suffix):
            leaf += suffix
        if len(leaf) > 160:
            stem = Path(leaf).stem[: 160 - len(suffix)]
            leaf = stem.rstrip() + suffix
        return leaf

    @staticmethod
    def _remove_record(record: _ExportRecord) -> None:
        record.path.unlink(missing_ok=True)
        try:
            record.path.parent.rmdir()
        except OSError:
            pass

    def _prune_locked(self, now: float, *, reserve_slot: bool = False) -> None:
        expired = [
            export_id
            for export_id, record in self._records.items()
            if now >= record.expires_monotonic
        ]
        for export_id in expired:
            record = self._records.pop(export_id)
            self._remove_record(record)

        limit = self.max_entries - 1 if reserve_slot else self.max_entries
        while len(self._records) > limit:
            _, record = self._records.popitem(last=False)
            self._remove_record(record)

    def publish(
        self,
        *,
        filename: str,
        output_format: ExportFormat,
        writer: Callable[[Path], T],
    ) -> PublishedExport[T]:
        """Build and publish one export, cleaning incomplete drafts on failure."""
        safe_filename = self._safe_filename(filename, output_format)
        export_id = uuid4().hex
        with self._lock:
            root = self._root()
        export_dir = root / export_id
        export_dir.mkdir(mode=0o700)
        path = export_dir / safe_filename

        try:
            result = writer(path)
            if not path.is_file():
                raise RuntimeError("Exporter did not create the requested file")
            size = path.stat().st_size
            if size <= 0:
                raise RuntimeError("Exporter created an empty file")

            now_monotonic = self._clock()
            created_at = self._utcnow()
            _, mime_type = _FORMAT_DETAILS[output_format]
            resource = ExportResource(
                export_id=export_id,
                filename=safe_filename,
                output_format=output_format,
                mime_type=mime_type,
                size=size,
                uri=f"remarkableexport:///{output_format}/{export_id}",
                created_at=created_at,
                expires_at=created_at + timedelta(seconds=self.ttl_seconds),
            )
            record = _ExportRecord(
                resource=resource,
                path=path,
                expires_monotonic=now_monotonic + self.ttl_seconds,
            )
            with self._lock:
                self._prune_locked(now_monotonic, reserve_slot=True)
                self._records[export_id] = record
            return PublishedExport(resource=resource, result=result)
        except Exception:
            path.unlink(missing_ok=True)
            try:
                export_dir.rmdir()
            except OSError:
                pass
            raise

    def read_bytes(self, export_id: str, output_format: ExportFormat) -> bytes:
        """Read a live export and update its LRU position."""
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            record = self._records.get(export_id)
            if record is None or record.resource.output_format != output_format:
                raise FileNotFoundError("Temporary export was not found or has expired")
            self._records.move_to_end(export_id)
            path = record.path

        try:
            return path.read_bytes()
        except FileNotFoundError:
            # Expiry/LRU cleanup may remove the captured path after the lock is
            # released. Keep the same educational resource-level failure without
            # serializing large file I/O behind the global store lock.
            raise FileNotFoundError("Temporary export was not found or has expired") from None

    def read_text(self, export_id: str, output_format: ExportFormat) -> str:
        """Read a UTF-8 text export."""
        return self.read_bytes(export_id, output_format).decode("utf-8")

    def cleanup(self) -> None:
        """Remove every managed export and the private temporary directory."""
        with self._lock:
            for record in self._records.values():
                self._remove_record(record)
            self._records.clear()
            if self._temporary_directory is not None:
                self._temporary_directory.cleanup()
                self._temporary_directory = None


export_store = ExportResourceStore()

from remarkable_mcp.server import mcp  # noqa: E402


@mcp.resource(
    "remarkableexport:///pdf/{export_id}",
    name="Temporary reMarkable PDF export",
    description="A generated PDF export. The link expires 15 minutes after creation.",
    mime_type="application/pdf",
)
def temporary_pdf_export(export_id: str) -> bytes:
    """Read a live PDF export from the bounded temporary store."""
    return export_store.read_bytes(export_id, "pdf")


@mcp.resource(
    "remarkableexport:///markdown/{export_id}",
    name="Temporary reMarkable Markdown export",
    description="A generated Markdown export. The link expires 15 minutes after creation.",
    mime_type="text/markdown; charset=utf-8",
)
def temporary_markdown_export(export_id: str) -> str:
    """Read a live Markdown export from the bounded temporary store."""
    return export_store.read_text(export_id, "markdown")


def cleanup_export_resources() -> None:
    """Clean process-local export artifacts during server shutdown."""
    export_store.cleanup()
