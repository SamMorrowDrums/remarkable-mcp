import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from remarkable_mcp.usb_web import DOCUMENTS_URL, Document, USBWebClient


def _response(entries=None, *, content=b""):
    response = Mock()
    response.status_code = 200
    response.json.return_value = [] if entries is None else entries
    response.content = content
    return response


def _entry(doc_id, name=None):
    return {
        "ID": doc_id,
        "VissibleName": name or doc_id,
        "Type": "DocumentType",
        "fileType": "pdf",
    }


def _folder_entry(doc_id, name=None):
    return {
        "ID": doc_id,
        "VissibleName": name or doc_id,
        "Type": "CollectionType",
    }


class _EntryTrackingClient(USBWebClient):
    def __init__(self):
        super().__init__()
        self._metadata_entries = 0
        self._metadata_entries_lock = threading.Lock()
        self.second_metadata_entry = threading.Event()

    def get_meta_items(self, limit=None):
        with self._metadata_entries_lock:
            self._metadata_entries += 1
            if self._metadata_entries == 2:
                self.second_metadata_entry.set()
        return super().get_meta_items(limit)


class TestUSBWebMetadataConcurrency:
    def test_concurrent_initial_loads_share_one_traversal(self):
        client = _EntryTrackingClient()
        load_started = threading.Event()
        release_load = threading.Event()
        attempts = 0
        attempts_lock = threading.Lock()

        def request(endpoint):
            nonlocal attempts
            assert endpoint == DOCUMENTS_URL
            with attempts_lock:
                attempts += 1
            load_started.set()
            assert release_load.wait(2)
            return _response([_entry("doc-1", "One")])

        client._request = request
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(client.get_meta_items)
                assert load_started.wait(1)
                second = executor.submit(client.get_meta_items)
                assert client.second_metadata_entry.wait(1)
                release_load.set()

                assert [doc.id for doc in first.result(timeout=2)] == ["doc-1"]
                assert [doc.id for doc in second.result(timeout=2)] == ["doc-1"]

            assert attempts == 1
        finally:
            client.close()

    def test_reader_cannot_observe_half_published_cache(self):
        class PausingClient(USBWebClient):
            def __init__(self):
                super().__init__()
                self.list_published = threading.Event()
                self.release_publication = threading.Event()

            def _publish_metadata_locked(self, documents, *, loaded_all):
                self._documents = documents
                self._metadata_loaded_all = loaded_all
                self.list_published.set()
                assert self.release_publication.wait(2)
                self._documents_by_id = {document.id: document for document in documents}

        client = PausingClient()
        client._request = lambda endpoint: _response([_entry("doc-1", "One")])
        reader_started = threading.Event()

        def read_document():
            reader_started.set()
            return client.get_doc("doc-1")

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                fill = executor.submit(client.get_meta_items)
                assert client.list_published.wait(1)
                reader = executor.submit(read_document)
                assert reader_started.wait(1)
                assert not reader.done()

                client.release_publication.set()
                assert [doc.id for doc in fill.result(timeout=2)] == ["doc-1"]
                assert reader.result(timeout=2).id == "doc-1"
        finally:
            client.release_publication.set()
            client.close()

    def test_invalidation_during_fill_rejects_stale_publication(self):
        client = USBWebClient()
        load_started = threading.Event()
        release_load = threading.Event()
        attempts = 0

        def request(endpoint):
            nonlocal attempts
            assert endpoint == DOCUMENTS_URL
            attempts += 1
            if attempts == 1:
                load_started.set()
                assert release_load.wait(2)
                return _response([_entry("stale")])
            return _response([_entry("fresh")])

        client._request = request
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                fill = executor.submit(client.get_meta_items)
                assert load_started.wait(1)
                client.invalidate_metadata_cache()
                release_load.set()
                assert [doc.id for doc in fill.result(timeout=2)] == ["stale"]

            with client._metadata_lock:
                assert client._documents == []
                assert client._documents_by_id == {}
                assert client._metadata_loaded_all is False

            assert [doc.id for doc in client.get_meta_items()] == ["fresh"]
            assert attempts == 2
        finally:
            release_load.set()
            client.close()

    def test_failed_fill_is_not_published_and_next_call_retries(self):
        client = USBWebClient()
        root_attempts = 0
        folder_attempts = 0

        def request(endpoint):
            nonlocal folder_attempts, root_attempts
            if endpoint == DOCUMENTS_URL:
                root_attempts += 1
                return _response([_folder_entry("folder-1"), _entry("root-doc")])
            assert endpoint == "/documents/folder-1"
            folder_attempts += 1
            if folder_attempts == 1:
                raise RuntimeError("temporary read failure")
            return _response([_entry("child-doc")])

        client._request = request
        try:
            with pytest.raises(RuntimeError, match="temporary read failure"):
                client.get_meta_items()

            with client._metadata_lock:
                assert client._documents == []
                assert client._documents_by_id == {}
                assert client._metadata_loaded_all is False
                assert client._metadata_loading is False

            assert [doc.id for doc in client.get_meta_items()] == [
                "folder-1",
                "root-doc",
                "child-doc",
            ]
            assert root_attempts == 2
            assert folder_attempts == 2
        finally:
            client.close()

    def test_successful_empty_fill_is_cached(self):
        client = USBWebClient()
        attempts = 0

        def request(endpoint):
            nonlocal attempts
            attempts += 1
            return _response()

        client._request = request
        try:
            assert client.get_meta_items() == []
            assert client.get_meta_items() == []
            assert client.get_doc("missing") is None
            assert attempts == 1
        finally:
            client.close()

    def test_limited_fill_does_not_claim_complete_cache(self):
        client = USBWebClient()
        attempts = 0

        def request(endpoint):
            nonlocal attempts
            attempts += 1
            return _response([_entry("doc-1"), _entry("doc-2")])

        client._request = request
        try:
            assert [doc.id for doc in client.get_meta_items(limit=1)] == ["doc-1"]
            with client._metadata_lock:
                assert list(client._documents_by_id) == ["doc-1"]
                assert client._metadata_loaded_all is False

            assert client.get_doc("doc-2").id == "doc-2"
            assert [doc.id for doc in client.get_meta_items(limit=1)] == ["doc-1"]
            assert attempts == 2
        finally:
            client.close()

    @pytest.mark.asyncio
    async def test_metadata_waiter_does_not_block_independent_download(self, monkeypatch):
        monkeypatch.setenv("REMARKABLE_USB_MAX_CONCURRENCY", "2")
        client = _EntryTrackingClient()
        metadata_started = threading.Event()
        release_metadata = threading.Event()
        download_started = threading.Event()
        doc = Document(
            id="download-1",
            hash="download-1",
            name="Download",
            doc_type="DocumentType",
        )

        def request_unqueued(endpoint, method, timeout):
            assert method == "GET"
            if endpoint == DOCUMENTS_URL:
                metadata_started.set()
                assert release_metadata.wait(2)
                return _response([_entry("doc-1")])
            download_started.set()
            return _response(content=b"downloaded")

        client._request_unqueued = request_unqueued
        try:
            first_fill = asyncio.create_task(client.run_method_async(client.get_meta_items))
            assert await asyncio.to_thread(metadata_started.wait, 1)
            second_fill = asyncio.create_task(client.run_method_async(client.get_meta_items))
            assert await asyncio.to_thread(client.second_metadata_entry.wait, 1)

            download = asyncio.create_task(client.run_method_async(client.download, doc))
            assert await asyncio.to_thread(download_started.wait, 1)
            assert await asyncio.wait_for(download, 1) == b"downloaded"

            release_metadata.set()
            first_result, second_result = await asyncio.wait_for(
                asyncio.gather(first_fill, second_fill), 2
            )
            assert [item.id for item in first_result] == ["doc-1"]
            assert [item.id for item in second_result] == ["doc-1"]
        finally:
            release_metadata.set()
            client.close()

    @pytest.mark.asyncio
    async def test_close_and_aclose_are_idempotent(self):
        client = USBWebClient()

        await client.aclose()
        await client.aclose()
        client.close()
