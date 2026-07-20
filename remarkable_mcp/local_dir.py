"""
reMarkable Local Directory Client

Read documents from a local reMarkable data directory — most usefully the
official desktop app's sync folder, which mirrors the tablet's xochitl store
on your computer. This gives fully offline, device-free access: no cable, no
cloud round-trip, no subscription. The desktop app keeps the folder fresh
whenever it is running.

Default locations probed (first one containing ``*.metadata`` wins):

- macOS (sandboxed app): ``~/Library/Containers/com.remarkable.desktop/Data/
  Library/Application Support/remarkable/desktop``
- macOS (legacy app): ``~/Library/Application Support/remarkable/desktop``
- Windows: ``%APPDATA%/remarkable/desktop``
- Linux: ``~/.local/share/remarkable/desktop``

Set ``REMARKABLE_LOCAL_DIR`` to point at any other xochitl-style directory
(e.g. a backup copy of ``/home/root/.local/share/remarkable/xochitl``).

The directory layout is the standard xochitl store:

- ``{uuid}.metadata`` — JSON with visibleName, type, parent, etc.
- ``{uuid}.content`` — JSON with file info (fileType, pages, ...)
- ``{uuid}/`` — folder with .rm page files
- ``{uuid}.pdf`` / ``{uuid}.epub`` — original source file, when present

This transport is strictly read-only: the desktop app owns the directory and
treats it as a private sync cache, so writing to it directly would not sync
and could corrupt the app's state. Write tools are never registered in this
mode.
"""

import io
import json
import logging
import os
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# Reuse the SSH transport's Document dataclass: the local directory has the
# exact same xochitl layout, so documents carry the same fields. Importing it
# performs no SSH activity.
from remarkable_mcp.ssh import Document

logger = logging.getLogger(__name__)

# Candidate default locations for the reMarkable desktop app's sync folder.
DEFAULT_DIR_CANDIDATES = (
    # macOS sandboxed (Mac App Store / current) app
    "~/Library/Containers/com.remarkable.desktop/Data/"
    "Library/Application Support/remarkable/desktop",
    # macOS legacy (non-sandboxed) app
    "~/Library/Application Support/remarkable/desktop",
    # Windows
    "%APPDATA%/remarkable/desktop",
    # Linux (community builds / synced copies)
    "~/.local/share/remarkable/desktop",
)


def find_default_local_dir() -> Optional[Path]:
    """Locate the desktop app's data directory on this machine.

    Returns the first candidate that exists and contains at least one
    ``*.metadata`` file, or None when nothing plausible is found.
    """
    for candidate in DEFAULT_DIR_CANDIDATES:
        path = Path(os.path.expandvars(os.path.expanduser(candidate)))
        try:
            if path.is_dir() and next(path.glob("*.metadata"), None) is not None:
                return path
        except OSError:
            continue
    return None


class LocalDirClient:
    """Client reading a local xochitl-style directory (read-only)."""

    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(os.path.expandvars(os.path.expanduser(str(base_dir))))
        self._documents: List[Document] = []
        self._documents_by_id: Dict[str, Document] = {}
        self._file_type_cache: Optional[Dict[str, Optional[str]]] = None

    def check_connection(self) -> bool:
        """A local directory is "connected" when it exists and holds metadata."""
        try:
            return (
                self.base_dir.is_dir() and next(self.base_dir.glob("*.metadata"), None) is not None
            )
        except OSError as e:
            logger.debug(f"Local dir check failed: {e}")
            return False

    def get_meta_items(self, limit: Optional[int] = None) -> List[Document]:
        """Read all document/folder metadata from the local directory."""
        if self._documents and limit is None:
            return self._documents
        if self._documents and limit is not None and len(self._documents) >= limit:
            return self._documents[:limit]

        documents: List[Document] = []
        for meta_file in sorted(self.base_dir.glob("*.metadata")):
            if limit is not None and len(documents) >= limit:
                break
            doc_id = meta_file.stem
            try:
                metadata = json.loads(meta_file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                logger.debug(f"Failed to parse metadata for {doc_id}: {e}")
                continue

            if metadata.get("deleted", False):
                continue

            last_modified = None
            if "lastModified" in metadata:
                try:
                    ts = int(metadata["lastModified"]) / 1000
                    last_modified = datetime.fromtimestamp(ts)
                except (ValueError, TypeError, OSError):
                    pass

            documents.append(
                Document(
                    id=doc_id,
                    hash=doc_id,  # No content hash locally; ID is stable enough
                    name=metadata.get("visibleName", doc_id),
                    doc_type=metadata.get("type", "DocumentType"),
                    parent=metadata.get("parent", ""),
                    deleted=metadata.get("deleted", False),
                    pinned=metadata.get("pinned", False),
                    synced=metadata.get("synced", True),
                    last_modified=last_modified,
                    size=0,
                    tags=metadata.get("tags", []),
                    local_path=str(self.base_dir / doc_id),
                )
            )

        self._documents = documents
        self._documents_by_id = {d.id: d for d in documents}
        logger.info(f"Loaded {len(documents)} documents from local dir {self.base_dir}")
        return documents

    def get_doc(self, doc_id: str) -> Optional[Document]:
        """Get a document by ID."""
        if not self._documents_by_id:
            self.get_meta_items()
        return self._documents_by_id.get(doc_id)

    def download(self, doc: Document) -> bytes:
        """Package a document as a zip, mirroring SSHClient.download().

        Contains the document folder's files (page .rm files etc.), the
        ``.content`` metadata, and any source ``.pdf``/``.epub`` so merged
        rendering works. ZIP_STORED because the archive is an in-memory
        transport container, never persisted.
        """
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_STORED) as zf:
            doc_dir = self.base_dir / doc.id
            if doc_dir.is_dir():
                for path in sorted(doc_dir.rglob("*")):
                    if path.is_file():
                        try:
                            zf.write(path, str(path.relative_to(doc_dir)))
                        except OSError as e:
                            logger.debug(f"Skipping unreadable file {path}: {e}")
            for suffix in (".content", ".pdf", ".epub"):
                side_file = self.base_dir / f"{doc.id}{suffix}"
                if side_file.is_file():
                    try:
                        zf.write(side_file, side_file.name)
                    except OSError as e:
                        logger.debug(f"Skipping unreadable file {side_file}: {e}")

        zip_buffer.seek(0)
        return zip_buffer.read()

    def download_raw_file(self, doc: Document, extension: str) -> Optional[bytes]:
        """Return the original source file (PDF/EPUB) bytes, if present."""
        ext = extension.lower().lstrip(".")
        raw_file = self.base_dir / f"{doc.id}.{ext}"
        try:
            if raw_file.is_file():
                return raw_file.read_bytes()
        except OSError as e:
            logger.debug(f"Failed to read raw file {raw_file}: {e}")
        return None

    def get_file_type(self, doc: Document) -> Optional[str]:
        """Read fileType from the document's .content JSON."""
        if self._file_type_cache is not None and doc.id in self._file_type_cache:
            return self._file_type_cache[doc.id]
        content_file = self.base_dir / f"{doc.id}.content"
        try:
            data = json.loads(content_file.read_text(encoding="utf-8"))
            return data.get("fileType")
        except (OSError, ValueError):
            return None

    def get_all_file_types(self) -> Dict[str, Optional[str]]:
        """Batch-read fileType for every document (single directory pass)."""
        if self._file_type_cache is not None:
            return self._file_type_cache

        cache: Dict[str, Optional[str]] = {}
        for content_file in self.base_dir.glob("*.content"):
            try:
                data = json.loads(content_file.read_text(encoding="utf-8"))
                cache[content_file.stem] = data.get("fileType")
            except (OSError, ValueError):
                cache[content_file.stem] = None
        self._file_type_cache = cache
        return cache


def check_local_dir_available(base_dir: Optional[str] = None) -> bool:
    """Check whether a usable local data directory is present."""
    if base_dir:
        return LocalDirClient(base_dir).check_connection()
    return find_default_local_dir() is not None


def create_local_dir_client(base_dir: Optional[str] = None) -> LocalDirClient:
    """
    Create a local directory client.

    Resolution order for the directory:
    1. Explicit ``base_dir`` argument
    2. ``REMARKABLE_LOCAL_DIR`` environment variable
    3. Auto-detected desktop app location (see ``find_default_local_dir``)

    Raises RuntimeError when no directory can be resolved, with guidance.
    """
    resolved = base_dir or os.environ.get("REMARKABLE_LOCAL_DIR")
    if resolved and resolved.lower() not in ("1", "true", "yes", "auto"):
        return LocalDirClient(resolved)

    detected = find_default_local_dir()
    if detected is None:
        raise RuntimeError(
            "Could not locate a reMarkable desktop app data directory. "
            "Install and sign in to the reMarkable desktop app, or set "
            "REMARKABLE_LOCAL_DIR to a folder containing xochitl-style "
            "document data (*.metadata files)."
        )
    return LocalDirClient(detected)
