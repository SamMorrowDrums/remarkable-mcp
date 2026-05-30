"""Regression tests for cloud document type detection + raw extraction.

Bug: the cloud path of api.get_file_type ignored the .content fileType and
defaulted every document to "notebook", so PDFs/EPUBs (including ones pushed
via remarkable_upload) were misrouted to the .rm parser and failed to read.
And download_raw_file returned None for cloud, so PDF/EPUB text never extracted.
"""

import json

from remarkable_mcp import api


class FakeCloudClient:
    """Mimics the cloud RemarkableClient: has _get_file, NOT get_file_type/download_raw_file."""

    def __init__(self, blobs):
        self._blobs = blobs  # id -> bytes

    def _get_file(self, file_hash, file_id):
        return self._blobs[file_id]


class FakeDoc:
    def __init__(self, files, name="Doc"):
        self.files = files
        self.VissibleName = name


def _doc(entries):
    return FakeDoc([{"id": fid, "hash": "h_" + fid} for fid in entries])


def test_pdf_detected_from_content_filetype():
    client = FakeCloudClient({
        "u.content": json.dumps({"fileType": "pdf", "pageCount": 3}).encode(),
        "u.pdf": b"%PDF-1.4 ...",
    })
    doc = _doc(["u.content", "u.pdf"])
    assert api.get_file_type(client, doc) == "pdf"


def test_notebook_detected_from_content_filetype():
    client = FakeCloudClient({
        "u.content": json.dumps({"fileType": "notebook"}).encode(),
        "u/0.rm": b"reMarkable .lines file, version=6",
    })
    doc = _doc(["u.content", "u/0.rm"])
    assert api.get_file_type(client, doc) == "notebook"


def test_epub_detected_from_content_filetype():
    client = FakeCloudClient({"u.content": json.dumps({"fileType": "epub"}).encode()})
    doc = _doc(["u.content"])
    assert api.get_file_type(client, doc) == "epub"


def test_pdf_fallback_when_content_lacks_filetype():
    # .content has no fileType, but a .pdf payload is present
    client = FakeCloudClient({"u.content": b"{}", "u.pdf": b"%PDF"})
    doc = _doc(["u.content", "u.pdf"])
    assert api.get_file_type(client, doc) == "pdf"


def test_download_raw_file_extracts_pdf_from_package():
    pdf_bytes = b"%PDF-1.4 hello"
    client = FakeCloudClient({"u.content": b'{"fileType":"pdf"}', "u.pdf": pdf_bytes})
    doc = _doc(["u.content", "u.pdf"])
    assert api.download_raw_file(client, doc, "pdf") == pdf_bytes


def test_download_raw_file_none_when_no_payload():
    client = FakeCloudClient({"u.content": b'{"fileType":"notebook"}', "u/0.rm": b"x"})
    doc = _doc(["u.content", "u/0.rm"])
    assert api.download_raw_file(client, doc, "pdf") is None
