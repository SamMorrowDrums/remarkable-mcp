# MCP Tools Reference

This document describes the read and render tools. Write tools are listed in the
[README](../README.md#write-tools-by-transport).

## Overview

| Tool | Purpose |
|------|---------|
| [`remarkable_read`](#remarkable_read) | Read and search document content |
| [`remarkable_browse`](#remarkable_browse) | Navigate folders and find documents |
| [`remarkable_search`](#remarkable_search) | Search across multiple documents |
| [`remarkable_recent`](#remarkable_recent) | Get recently modified documents |
| [`remarkable_status`](#remarkable_status) | Check connection status |
| [`remarkable_image`](#remarkable_image) | Get page images (PNG or SVG) |

These tools are read-only and return structured JSON with next-step hints.

## Root Path Filtering

All tools respect the `REMARKABLE_ROOT_PATH` environment variable. When configured, operations are scoped to that folder:

```json
{
  "env": {
    "REMARKABLE_ROOT_PATH": "/Work"
  }
}
```

With this configuration:
- Paths in responses are relative to the root (e.g., `/Work/Project` appears as `/Project`)
- Documents outside the root are not accessible
- `remarkable_status()` shows the configured root and document count within that folder

---

## remarkable_read

**Read and extract text from a document.**

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `document` | string | *required* | Document name or full path |
| `content_type` | string | `"text"` | What content to extract |
| `page` | int | `1` | Extracted-content page number for bounded text pagination |
| `grep` | string | `None` | Search for keywords in content |
| `include_ocr` | bool | `False` | Enable OCR for handwritten content |

### Content Types

- **`"text"`**: source text plus annotations, highlights, and typed text (default)
- **`"raw"`**: original PDF/EPUB text without annotations
- **`"annotations"`**: highlights, typed notebook text, and OCR content

### Examples

```python
# Read first page of a document
remarkable_read("Meeting Notes")

# Read the third extracted-content chunk
remarkable_read("Research Paper.pdf", page=3)

# Search for keywords
remarkable_read("Project Plan", grep="deadline")

# Get only annotations and highlights
remarkable_read("Book.pdf", content_type="annotations")

# Enable OCR for handwritten notes
remarkable_read("Journal", include_ocr=True)

# Read by full path
remarkable_read("/Work/Projects/Q4 Planning")
```

### Response Format

```json
{
  "document": "Meeting Notes",
  "path": "/Work/Meeting Notes",
  "file_type": "notebook",
  "content_type": "text",
  "content": "Extracted text content...",
  "page": 1,
  "total_pages": 5,
  "total_pages_known": true,
  "content_pages": 2,
  "total_chars": 12500,
  "more": true,
  "next_page": 2,
  "modified": "2025-11-28T10:30:00Z",
  "_hint": "Content page 1/2; document has 5 physical pages. Next: remarkable_read('Meeting Notes', page=2)."
}
```

### Smart Features

- **Auto-OCR**: If a notebook has no typed text and `include_ocr=False`, OCR is automatically enabled and you're notified via `_ocr_auto_enabled: true`
- **Fuzzy matching**: If the exact document isn't found, similar names are suggested
- **Path resolution**: Works with document names or full paths

### Pagination

- `total_pages` reports physical document pages, matching `remarkable_image`,
  and `total_pages_known` says whether that count was available.
- `content_pages` reports extracted-text pagination units. PDF/EPUB source text
  uses ~8000-character chunks; notebook OCR may produce fewer content pages than
  physical pages when blank pages have no text.
- `page`, `more`, and `next_page` refer to those content pages. This preserves
  bounded text responses without overloading the physical page count.
- Raw PDF reads count pages from the same source bytes used for text extraction
  and do not fetch the full document archive. A raw EPUB may need that archive
  for its physical page count; if the archive is unavailable, the extracted
  content still succeeds with `total_pages: null`, `total_pages_known: false`,
  and a `page_count_note`.
- On older USB firmware that returns a native PDF instead of an rmdoc archive,
  `content_type="text"` returns source text without annotations, while
  `content_type="annotations"` returns `annotations_not_available`.

When `more: true`, use the `page` parameter to continue reading.

---

## remarkable_browse

**Navigate your document library.**

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `path` | string | `"/"` | Folder path to browse |
| `query` | string | `None` | Search documents by name |
| `tags` | list[string] | `None` | Require all listed tags (case-insensitive) |

### Examples

```python
# List root folder
remarkable_browse("/")

# Browse a specific folder
remarkable_browse("/Work/Projects")

# Search for documents by name
remarkable_browse(query="meeting")

# Combine path and search
remarkable_browse("/Work", query="report")

# Filter by tag
remarkable_browse("/Work", tags=["project", "active"])
```

### Response Format

```json
{
  "path": "/Work",
  "folders": [
    {"name": "Projects", "path": "/Work/Projects"},
    {"name": "Archive", "path": "/Work/Archive"}
  ],
  "documents": [
    {
      "name": "Weekly Report",
      "path": "/Work/Weekly Report",
      "type": "pdf",
      "modified": "2025-11-28T10:30:00Z"
    }
  ],
  "_hint": "Found 2 folders, 1 document. To read: remarkable_read('Weekly Report')."
}
```

### Smart Features

- **Auto-redirect**: If `path` points to a document instead of a folder, automatically returns the document content (like calling `remarkable_read`)
- **Case-insensitive**: Paths and searches are case-insensitive

---

## remarkable_search

**Search across multiple documents.**

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | string | *required* | Search term for document names |
| `grep` | string | `None` | Pattern to search within content |
| `limit` | int | `5` | Maximum documents to search (max: 5) |
| `include_ocr` | bool | `False` | Enable OCR for handwritten content |
| `tags` | list[string] | `None` | Require all listed tags (case-insensitive) |

### Examples

```python
# Find documents with "meeting" in the name
remarkable_search("meeting")

# Find "action items" inside meeting documents
remarkable_search("meeting", grep="action items")

# Search journals for a specific topic
remarkable_search("journal", grep="project idea", include_ocr=True)

# Search only tagged documents
remarkable_search("project", tags=["work"])
```

### Response Format

```json
{
  "query": "meeting",
  "grep": "action items",
  "count": 3,
  "documents": [
    {
      "name": "Team Meeting Nov",
      "path": "/Work/Team Meeting Nov",
      "modified": "2025-11-28T10:30:00Z",
      "content": "...context around matches...",
      "total_pages": 2,
      "content_pages": 1,
      "grep_matches": 5,
      "truncated": true
    }
  ],
  "_hint": "Found 3 document(s) with 12 grep match(es). To read more: remarkable_read('/Work/Team Meeting Nov')."
}
```

### Limits

- Maximum 5 documents per search
- Content is truncated to ~2000 characters per document
- Designed for quick discovery, use `remarkable_read` for full content

---

## remarkable_recent

**Get recently modified documents.**

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | `10` | Maximum documents to return |
| `include_preview` | bool | `False` | Include text preview for each document |

### Examples

```python
# Get last 10 documents
remarkable_recent()

# Get last 5 with previews
remarkable_recent(limit=5, include_preview=True)
```

### Response Format

```json
{
  "count": 5,
  "documents": [
    {
      "name": "Meeting Notes",
      "path": "/Work/Meeting Notes",
      "modified": "2025-11-28T10:30:00Z",
      "preview": "First 200 characters of content..."
    }
  ],
  "_hint": "Showing 5 recent documents. To read one: remarkable_read('Meeting Notes')."
}
```

### Notes

- With `include_preview=True`, limit is capped at 10 (performance)
- Notebooks skip preview (require OCR), showing `preview_skipped` instead
- PDFs and EPUBs have fast text extraction for previews

---

## remarkable_status

**Check connection and authentication status.**

### Parameters

None.

### Examples

```python
remarkable_status()
```

### Response Format

```json
{
  "authenticated": true,
  "transport": "ssh",
  "connection": "SSH to root@10.11.99.1:22",
  "status": "connected",
  "document_count": 142,
  "root_path": "/Work",
  "ocr_backend": "google",
  "_hint": "Connection healthy. Filtered to root: /Work. Use remarkable_browse('/') to explore your library."
}
```

### Fields

| Field | Description |
|-------|-------------|
| `authenticated` | Whether authentication succeeded |
| `transport` | `"local-dir"`, `"cloud"`, `"usb-web"`, or `"ssh"` |
| `connection` | Connection details |
| `document_count` | Total documents in library (filtered by root if configured) |
| `write_enabled` | Whether write tools are enabled (the default; `false` only with `--read-only`) |
| `capabilities` | Effective capabilities for the active transport (read/render/upload/mkdir/move/rename/delete) |
| `capabilities_by_transport` | The full per-transport capability matrix |
| `root_path` | Configured root path filter (only present if set) |
| `ocr_backend` | Which OCR backend is configured |

---

## remarkable_image

**Get a PNG or SVG image of a specific page.**

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `document` | string | *required* | Document name or full path |
| `page` | int | `1` | Page number (1-indexed) |
| `background` | string | `"#FBFBFB"` | Background color (hex RGB or RGBA) |
| `output_format` | string | `"png"` | Output format: `"png"` or `"svg"` |
| `include_ocr` | bool | `False` | Enable OCR on the image |
| `compatibility` | bool | `False` | Return resource URI instead of embedded resource |
| `render_merged` | bool \| null | `None` (auto) | PNG compositing mode: auto-merge PDF-backed pages, `True` to request merging explicitly, or `False` for annotations only |

### Background Colors

- **`"#FBFBFB"`**: default reMarkable paper color
- **`"#FFFFFF"`**: white
- **`"#00000000"`**: fully transparent
- **`"#80008080"`**: semi-transparent purple

**Tip:** Set `REMARKABLE_BACKGROUND_COLOR` environment variable to change the default for all image operations.

### Examples

```python
# Get first page with default paper background
remarkable_image("UI Mockup")

# Get specific page
remarkable_image("Meeting Notes", page=2)

# White background
remarkable_image("Diagram", background="#FFFFFF")

# Transparent background for compositing
remarkable_image("Logo Sketch", background="#00000000")

# SVG format for editing in design tools
remarkable_image("Wireframe", output_format="svg")

# SVG with custom background
remarkable_image("Sketch", output_format="svg", background="#F0F0F0")

# Enable OCR
remarkable_image("Handwritten Notes", include_ocr=True)

# Compatibility mode: return resource URI instead of embedded resource
remarkable_image("Diagram", compatibility=True)

# PDF-backed PNG pages merge the source page and annotations automatically
remarkable_image("Research Paper", page=3)

# Return only the reMarkable annotation layer
remarkable_image("Research Paper", page=3, render_merged=False)
```

### Response Format

For PNG format, returns an embedded image resource that can be displayed inline.

For SVG format, returns an embedded text resource with the SVG content.

When `compatibility=True`, returns a JSON object with the resource URI:

```json
{
  "resource_uri": "remarkableimg:///path/doc.page-1.merged.png",
  "page": 1,
  "total_pages": 5,
  "merged": true,
  "_hint": "Page 1/5. Use resource URI to access the image."
}
```

When `include_ocr=True`, OCR text is included in the response. Google Vision is
used when configured; otherwise OCR runs locally with Tesseract.

### Notes

- Native notebooks retain their existing stroke and blank-page rendering.
- PDF-backed PNG pages merge the source page and annotations by default. Explicit
  `render_merged=False` preserves annotation-only access, including user-added
  PDF pages that have no underlay.
- SVG remains annotation-only; explicit `render_merged=True` with SVG returns
  SVG plus an explanatory note.
- Compatibility responses preserve the same merge choice and expose `merged`.
- If older USB firmware returns only a native PDF export, PNG still renders via
  that export. SVG and explicit `render_merged=False` require the unavailable
  rmdoc annotation layer and return clear errors.
- RGBA colors (8-digit hex) allow transparency control

---

## Error Handling

All tools return structured errors with suggestions:

```json
{
  "_error": {
    "type": "document_not_found",
    "message": "Document 'Meting Notes' not found",
    "suggestion": "Did you mean: 'Meeting Notes', 'Meeting Notes 2'?",
    "did_you_mean": ["Meeting Notes", "Meeting Notes 2"]
  }
}
```

Common error types:
- `document_not_found`: document does not exist (includes suggestions)
- `authentication_failed`: token invalid or SSH connection failed
- `connection_error`: network or SSH connection issue
