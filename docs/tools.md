# MCP Tools Reference

This document provides detailed documentation for all MCP tools provided by remarkable-mcp.

## Overview

| Tool | Purpose |
|------|---------|
| [`remarkable_read`](#remarkable_read) | Read and search document content |
| [`remarkable_browse`](#remarkable_browse) | Navigate folders and find documents |
| [`remarkable_search`](#remarkable_search) | Search across multiple documents |
| [`remarkable_recent`](#remarkable_recent) | Get recently modified documents |
| [`remarkable_status`](#remarkable_status) | Check connection status |

All tools are **read-only** and return structured JSON with hints for logical next actions.

---

## remarkable_read

**Read and extract text from a document.**

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `document` | string | *required* | Document name or full path |
| `content_type` | string | `"text"` | What content to extract |
| `page` | int | `1` | Page number for pagination |
| `grep` | string | `None` | Search for keywords in content |
| `include_ocr` | bool | `False` | Enable OCR for handwritten content |

### Content Types

- **`"text"`** — Full extracted text: raw document content plus annotations, highlights, and typed text (default)
- **`"raw"`** — Only the original PDF/EPUB text, no annotations. SSH mode only.
- **`"annotations"`** — Only annotations: highlights, typed text from notebooks, and OCR content

### Examples

```python
# Read first page of a document
remarkable_read("Meeting Notes")

# Read a specific page
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
  "total_chars": 2500,
  "more": true,
  "modified": "2025-11-28T10:30:00Z",
  "_hint": "Page 1/5. Next: remarkable_read('Meeting Notes', page=2)."
}
```

### Smart Features

- **Auto-OCR**: If a notebook has no typed text and `include_ocr=False`, OCR is automatically enabled and you're notified via `_ocr_auto_enabled: true`
- **Fuzzy matching**: If the exact document isn't found, similar names are suggested
- **Path resolution**: Works with document names or full paths

### Pagination

- **PDF/EPUB**: Pages are ~8000 character chunks of extracted text
- **Notebooks**: Pages correspond to actual notebook pages (especially useful with OCR)

When `more: true`, use the `page` parameter to continue reading.

---

## remarkable_browse

**Navigate your document library.**

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `path` | string | `"/"` | Folder path to browse |
| `query` | string | `None` | Search documents by name |

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

### Examples

```python
# Find documents with "meeting" in the name
remarkable_search("meeting")

# Find "action items" inside meeting documents
remarkable_search("meeting", grep="action items")

# Search journals for a specific topic
remarkable_search("journal", grep="project idea", include_ocr=True)
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
  "ocr_backend": "google",
  "_hint": "Connection healthy. Use remarkable_browse('/') to explore your library."
}
```

### Fields

| Field | Description |
|-------|-------------|
| `authenticated` | Whether authentication succeeded |
| `transport` | `"ssh"` or `"cloud"` |
| `connection` | Connection details |
| `document_count` | Total documents in library |
| `ocr_backend` | Which OCR backend is configured |

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
- `document_not_found` — Document doesn't exist (includes suggestions)
- `authentication_failed` — Token invalid or SSH connection failed
- `connection_error` — Network or SSH connection issue
