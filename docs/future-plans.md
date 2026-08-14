# Future Work

This file summarizes accepted problem areas. GitHub issues contain the current
scope and acceptance criteria.

## Near-term work

### SSH reliability

Track intermittent large-library SSH failures in
[#157](https://github.com/SamMorrowDrums/remarkable-mcp/issues/157).

### Cloud authoring

Add native notebook creation to cloud mode before considering page mutation.
See [#118](https://github.com/SamMorrowDrums/remarkable-mcp/issues/118).

### Export

Provide reusable PDF and Markdown export for one document, then add folder and
filter batching. See
[#27](https://github.com/SamMorrowDrums/remarkable-mcp/issues/27).

## Search and OCR

### Persistent indexing

Build a local full-text index with incremental updates. Semantic search should
remain optional and use the same indexed chunks. See
[#26](https://github.com/SamMorrowDrums/remarkable-mcp/issues/26).

### Persistent cache

Cache OCR and extracted content with bounded storage, explicit invalidation, and
corruption recovery. See
[#28](https://github.com/SamMorrowDrums/remarkable-mcp/issues/28).

### OCR providers

Unify the existing Google Vision and Tesseract backends behind one provider
interface before adding more integrations. See
[#25](https://github.com/SamMorrowDrums/remarkable-mcp/issues/25).

## Workflow integrations

### Processed-page marker

Define an idempotent on-device marker for pages that have already been exported
or reviewed. See
[#24](https://github.com/SamMorrowDrums/remarkable-mcp/issues/24).

### Obsidian synchronization

Define stable identity, path mapping, conflict handling, and dry-run behavior
before implementing synchronization. See
[#158](https://github.com/SamMorrowDrums/remarkable-mcp/issues/158).

## Non-goals

- Firmware modification
- DRM bypass
- Subscription circumvention
- Silent overwrite or deletion during synchronization
