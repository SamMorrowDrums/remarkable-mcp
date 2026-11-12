"""Exercise the loopback MCP; --writes authorizes this script's disposable test data.

Every mutation is followed by a new browse. No failed write is retried. Deletion
elicitation accepts only the exact current, UUID-checked disposable artifact.
"""
import argparse
import asyncio
import base64
import datetime as dt
import io
import json
import logging
import pathlib
import re
import sys

import httpx2
import pymupdf
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from PIL import Image

ROOT = pathlib.Path(__file__).resolve().parent
URL = "http://127.0.0.1:8000/mcp"


class CheckFailed(Exception):
    pass


def require(condition, message):
    if not condition:
        raise CheckFailed(message)


def payload(result):
    require(not getattr(result, "is_error", getattr(result, "isError", False)), "MCP returned a tool error")
    for block in result.content:
        if block.type == "text":
            try:
                data = json.loads(block.text)
            except (ValueError, TypeError):
                continue
            if isinstance(data, dict):
                if data.get("_error") or data.get("error"):
                    error = data.get("_error", data.get("error"))
                    code = error.get("type", "tool_error") if isinstance(error, dict) else "tool_error"
                    raise CheckFailed(f"Tool reported {code}")
                return data
    data = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    require(isinstance(data, dict), "Tool response did not contain a JSON object")
    return data


def one_named(browse, name, kind):
    matches = [item for item in browse[kind] if item["name"] == name]
    require(len(matches) == 1, f"Expected exactly one {kind} entry named {name}")
    return matches[0]["id"]


def absent(browse, name):
    return not any(x["name"] == name for key in ("folders", "documents") for x in browse[key])


class Verification:
    def __init__(self, writes, document):
        run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self.out = ROOT / "cloud-verification" / run_id
        self.out.mkdir(parents=True)
        self.summary = {"started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "endpoint": URL, "writes_requested": writes, "checks_passed": [],
                        "completed": False, "test_folder": None, "created_ids": {},
                        "remaining_artifacts": [], "failure": None}
        self.writes = writes
        self.document = document
        self.test_name = "MCP Test " + run_id
        self.expected_delete = None
        self.counter = 0
        self.session = None

    def save(self):
        (self.out / "summary.json").write_text(json.dumps(self.summary, indent=2), encoding="utf-8")

    def passed(self, name):
        self.summary["checks_passed"].append(name)
        self.save()
        print("PASS:", name, flush=True)

    async def confirm(self, context, params):
        expected = self.expected_delete
        message = f"Delete '{expected}' from your reMarkable? This moves it to the trash."
        schema = getattr(params, "requested_schema", None) or getattr(params, "requestedSchema", None)
        if hasattr(schema, "model_dump"):
            schema = schema.model_dump(by_alias=True)
        if expected and params.message == message and isinstance(schema, dict):
            if schema.get("properties", {}).get("confirm", {}).get("type") == "boolean":
                return types.ElicitResult(action="accept", content={"confirm": True})
        return types.ElicitResult(action="decline")

    async def call(self, name, args):
        self.summary["last_request"] = {"tool": name, "arguments": args}
        self.save()
        result = await self.session.call_tool(name, args, read_timeout_seconds=240)
        self.counter += 1
        # These results contain notebook/test data, never authentication material.
        (self.out / f"{self.counter:02d}-{name}.json").write_text(
            re.sub(r"eyJ[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){1,2}", "[credential redacted]",
                   result.model_dump_json(by_alias=True, indent=2)), encoding="utf-8")
        return result

    async def browse(self, path="/"):
        value = payload(await self.call("remarkable_browse", {"path": path}))
        require(value.get("mode") == "browse", "Browse did not return a folder listing")
        require(isinstance(value.get("folders"), list) and isinstance(value.get("documents"), list),
                "Browse omitted its folder/document arrays")
        return value

    async def image(self, document, filename):
        result = await self.call("remarkable_image", {"document": document, "page": 1,
                                 "output_format": "png", "include_ocr": False})
        for block in result.content:
            raw = block.model_dump(by_alias=True)
            resource = raw.get("resource", {})
            data = raw.get("data") if raw.get("type") == "image" and raw.get("mimeType") == "image/png" else None
            data = data or (resource.get("blob") if resource.get("mimeType") == "image/png" else None)
            if data:
                png = base64.b64decode(data, validate=True)
                with Image.open(io.BytesIO(png)) as image:
                    image.verify()
                (self.out / filename).write_bytes(png)
                return
        payload(result)  # Surface a structured error without dumping the response.
        raise CheckFailed("Render did not return an embedded PNG")

    async def check_owned(self, parent, name, kind, expected_id):
        require(one_named(await self.browse(parent), name, kind) == expected_id,
                "Artifact UUID changed; refused to mutate it")

    async def delete(self, parent, name, kind, expected_id):
        await self.check_owned(parent, name, kind, expected_id)
        path = parent.rstrip("/") + "/" + name
        self.expected_delete = path
        try:
            result = payload(await self.call("remarkable_delete", {"document": path, "permanent": False}))
            require(result.get("deleted") is True, "Trash operation was refused or cancelled")
        finally:
            self.expected_delete = None
        require(absent(await self.browse(parent), name), "Trashed artifact still appears in a fresh browse")
        self.summary["remaining_artifacts"].remove(path)
        self.passed("trash " + kind.rstrip("s") + ": " + name)

    async def run(self):
        async with httpx2.AsyncClient(trust_env=False, timeout=httpx2.Timeout(250, connect=5)) as http:
            async with streamable_http_client(URL, http_client=http) as streams:
                async with ClientSession(*streams, elicitation_callback=self.confirm) as session:
                    self.session = session
                    init = await session.initialize()
                    self.summary["protocol"] = init.protocol_version
                    listed = await session.list_tools()
                    names = {t.name for t in listed.tools}
                    self.summary["tools"] = sorted(names)
                    require({"remarkable_mkdir", "remarkable_upload", "remarkable_move", "remarkable_rename", "remarkable_delete"} <= names,
                            "Required cloud write tools are missing")
                    require("remarkable_author" not in names, "Unexpected native authoring tool in cloud mode")
                    state = payload(await self.call("remarkable_status", {}))
                    require(state.get("transport") == "cloud" and state.get("authenticated") is True
                            and state.get("write_enabled") is True and state.get("status") == "connected",
                            "Expected authenticated cloud transport with writes enabled")
                    self.summary["status"] = state
                    self.passed("authenticated cloud status; writes enabled")
                    before = await self.browse()
                    self.passed("browse library")
                    reading = payload(await self.call("remarkable_read", {"document": self.document, "include_ocr": False}))
                    require(bool(reading.get("content")) and reading.get("total_chars", 0) > 0,
                            "Known-content read returned no text")
                    self.passed("read " + self.document)
                    await self.image(self.document, "known-content-page-1.png")
                    self.passed("render " + self.document)
                    if not self.writes:
                        self.summary["completed"] = True
                        return
                    require(absent(before, self.test_name), "Test folder already exists; no write attempted")
                    folder = "/" + self.test_name
                    self.summary["test_folder"] = folder
                    created = payload(await self.call("remarkable_mkdir", {"folder_name": self.test_name, "parent": "/"}))
                    require(created.get("created") is True, "Folder creation did not report success")
                    folder_id = one_named(await self.browse(), self.test_name, "folders")
                    require(created.get("uuid") == folder_id, "Created folder UUID did not match browse")
                    self.summary["created_ids"]["test_folder"] = folder_id
                    self.summary["remaining_artifacts"].append(folder)
                    self.passed("create folder")
                    moved_folder = folder + "/Moved"
                    created = payload(await self.call("remarkable_mkdir", {"folder_name": "Moved", "parent": folder}))
                    moved_id = one_named(await self.browse(folder), "Moved", "folders")
                    require(created.get("created") is True and created.get("uuid") == moved_id, "Destination folder verification failed")
                    self.summary["created_ids"]["move_destination"] = moved_id
                    self.summary["remaining_artifacts"].append(moved_folder)
                    self.passed("create destination subfolder")
                    pdf_path = self.out / "harmless-test.pdf"
                    marker = "reMarkable MCP local cloud write test " + self.test_name
                    with pymupdf.open() as pdf:
                        page = pdf.new_page(width=595, height=842)
                        page.insert_textbox(pymupdf.Rect(50, 50, 545, 200), marker + "\nDisposable test document.", fontsize=12)
                        pdf.save(pdf_path)
                    name = "MCP upload test"
                    uploaded = payload(await self.call("remarkable_upload", {"file_path": str(pdf_path),
                                       "parent_folder": folder, "document_name": name}))
                    require(uploaded.get("uploaded") is True, "Upload did not report success")
                    doc_id = one_named(await self.browse(folder), name, "documents")
                    require(uploaded.get("uuid") == doc_id, "Uploaded UUID did not match fresh browse")
                    self.summary["created_ids"]["pdf"] = doc_id
                    original = folder + "/" + name
                    self.summary["remaining_artifacts"].append(original)
                    self.passed("upload PDF to folder")
                    test_read = payload(await self.call("remarkable_read", {"document": original, "content_type": "raw", "include_ocr": False}))
                    require("Disposable test document" in json.dumps(test_read), "Uploaded PDF text did not round-trip")
                    await self.image(original, "uploaded-pdf-page-1.png")
                    self.passed("uploaded PDF read and render")
                    await self.check_owned(folder, name, "documents", doc_id)
                    renamed = "MCP renamed test"
                    response = payload(await self.call("remarkable_rename", {"document": original, "new_name": renamed}))
                    require(response.get("renamed") is True, "Rename did not report success")
                    listing = await self.browse(folder)
                    require(absent(listing, name) and one_named(listing, renamed, "documents") == doc_id, "Rename did not match fresh browse")
                    renamed_path = folder + "/" + renamed
                    self.summary["remaining_artifacts"].remove(original)
                    self.summary["remaining_artifacts"].append(renamed_path)
                    self.passed("rename PDF")
                    await self.check_owned(folder, renamed, "documents", doc_id)
                    response = payload(await self.call("remarkable_move", {"document": renamed_path, "dest_folder": moved_folder}))
                    require(response.get("moved") is True, "Move did not report success")
                    require(absent(await self.browse(folder), renamed), "Moved PDF still appears in its old folder")
                    await self.check_owned(moved_folder, renamed, "documents", doc_id)
                    moved_path = moved_folder + "/" + renamed
                    self.summary["remaining_artifacts"].remove(renamed_path)
                    self.summary["remaining_artifacts"].append(moved_path)
                    self.passed("move PDF")
                    await self.delete(moved_folder, renamed, "documents", doc_id)
                    require(not (await self.browse(moved_folder))["documents"], "Destination is not empty; refused cleanup")
                    await self.delete(folder, "Moved", "folders", moved_id)
                    empty = await self.browse(folder)
                    require(not empty["folders"] and not empty["documents"], "Test folder is not empty; refused cleanup")
                    await self.delete("/", self.test_name, "folders", folder_id)
                    after = await self.browse()
                    before_items = {(k, x["id"], x["name"]) for k in ("folders", "documents") for x in before[k]}
                    after_items = {(k, x["id"], x["name"]) for k in ("folders", "documents") for x in after[k]}
                    require(before_items == after_items, "Root library changed during the test; review concurrent tablet activity")
                    self.passed("final root browse matches initial IDs and names")
                    self.summary["completed"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--writes", action="store_true", help="Run the disposable-folder cloud write test and trash its artifacts")
    parser.add_argument("--document", required=True, help="Path of a notebook or PDF on the tablet to read and render, e.g. /Some Folder/Some Notebook")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    check = Verification(args.writes, args.document)
    try:
        asyncio.run(check.run())
    except BaseException as exc:
        check.summary["failure"] = str(exc) if isinstance(exc, CheckFailed) else type(exc).__name__
        print("STOP:", check.summary["failure"], flush=True)
        print("Writes were not retried. Inspect the saved report before resuming.", flush=True)
    finally:
        check.summary["ended_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        check.save()
        print("Report:", check.out / "summary.json", flush=True)
    return 0 if check.summary["completed"] else 1


if __name__ == "__main__":
    sys.exit(main())
