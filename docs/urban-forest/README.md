# Urban Forest docs for remarkable-mcp

This folder holds Urban Forest's own notes for its deployment of [SamMorrowDrums/remarkable-mcp](https://github.com/SamMorrowDrums/remarkable-mcp). Upstream's documentation lives in the project README and in upstream's `docs/`; nothing here replaces it.

| File | What it is |
|---|---|
| `spec-cloudflare.md` | The phased plan for running the server on Cloudflare: a Container behind a Worker gateway (phase 1), hardening and a second user (phase 2), a conditional Worker rewrite (phase 3). Includes the repository plan. |
| `spec-safety-and-permissions.md` | The original architecture spec. Its hosting sections are superseded by `spec-cloudflare.md`; its safety, permission, write-safety, caching and failure-handling sections still apply. |
| `research-2026-11-03-spec-verification.md` | A dated research note checking the original spec's assumptions against sources: the reMarkable cloud route, notebook editing, Cloudflare options. Every claim is labeled checked / follows / guess. |

Rules for this folder: it is public. Nothing from anyone's tablet goes in — no document or folder names, no listings, no page contents. Counts, formats and timings are fine. Tokens, registration codes and other secrets never appear anywhere in this repository.

Words used throughout: *reading the tablet* (browse, read, render, export) · *changing the library* (push a document or notebook file onto the tablet, create a folder, move, rename, trash) · *editing a notebook's pages* (changing strokes, text or pages inside a notebook — not in scope).
