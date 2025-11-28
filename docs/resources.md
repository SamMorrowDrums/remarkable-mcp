# MCP Resources Reference

Documents in your reMarkable library are automatically registered as MCP resources, allowing AI assistants to access them directly.

## Resource Types

| URI Scheme | Description | Mode |
|------------|-------------|------|
| `remarkable:///` | Extracted text content | Both |
| `remarkableraw:///` | Original PDF/EPUB files | SSH only |

## Text Resources (`remarkable:///`)

Every document is registered as a text resource with its full path.

### URI Format

```
remarkable:///{path}.txt
```

### Examples

```
remarkable:///Meeting%20Notes.txt
remarkable:///Work/Projects/Q4%20Planning.txt
remarkable:///Journals/November.txt
```

### What's Extracted

| Document Type | Content |
|---------------|---------|
| **PDF** | Full text extracted from PDF |
| **EPUB** | Full text extracted from EPUB |
| **Notebook** | Typed text (Type Folio), highlights |

**Note:** OCR for handwritten content is not included in resources by default. Use `remarkable_read` with `include_ocr=True` for handwritten content.

### Response

Text resources return the extracted content as plain text:

```
Meeting Notes - November 28, 2025

Attendees: Alice, Bob, Charlie

Action Items:
- Review Q4 targets
- Schedule follow-up
...
```

## Raw Resources (`remarkableraw:///`)

Original PDF and EPUB files are available as raw resources in SSH mode.

### URI Format

```
remarkableraw:///{path}.pdf
remarkableraw:///{path}.epub
```

### Examples

```
remarkableraw:///Research%20Paper.pdf
remarkableraw:///Books/Deep%20Work.epub
```

### Response

Raw resources return the original file as base64-encoded data:

```json
{
  "content": "base64-encoded-file-data...",
  "mimeType": "application/pdf"
}
```

### Availability

| Mode | Text Resources | Raw Resources |
|------|----------------|---------------|
| SSH | ✅ Yes | ✅ Yes |
| Cloud | ✅ Yes | ❌ No |

The Cloud API doesn't provide access to original source files, so raw resources are only available when using SSH mode.

## How Resources Are Registered

On server startup, remarkable-mcp:

1. Connects to your reMarkable (via SSH or Cloud)
2. Fetches the document list
3. Registers each document as an MCP resource
4. For SSH mode, also registers raw PDF/EPUB resources

Resources are registered once at startup. If you add new documents, restart the MCP server to pick them up.

## Using Resources

### In Claude Desktop

Resources appear in Claude's context when you mention them or use the "Attach" feature.

### In VS Code

MCP resources can be accessed through the Copilot chat interface. The screenshot below shows resources appearing with the `mcpr` prefix:

![Resources in VS Code](../assets/resources-screenshot.png)

### Programmatically

MCP clients can request resources by URI:

```python
# Request a text resource
content = await client.read_resource("remarkable:///Meeting%20Notes.txt")

# Request a raw PDF
pdf_data = await client.read_resource("remarkableraw:///Paper.pdf")
```

## Path Encoding

Paths in URIs must be URL-encoded:

| Character | Encoded |
|-----------|---------|
| Space | `%20` |
| `/` | `%2F` (in filename only) |
| `&` | `%26` |

Examples:
- `Meeting Notes` → `Meeting%20Notes`
- `/Work/Q4 Report` → `/Work/Q4%20Report`

## Filtering

Archived documents and trash items are **not** registered as resources. Only documents that are actively synced appear.

## Performance Considerations

- Resources are registered at startup (slight delay for large libraries)
- Text extraction happens on-demand when a resource is accessed
- Results are cached per session
- SSH mode is significantly faster than Cloud for resource access
