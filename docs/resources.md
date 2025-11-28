# MCP Resources Reference

Documents in your reMarkable library are automatically registered as MCP resources, allowing AI assistants to access them directly.

## Resource Types

| URI Scheme | Description | Mode |
|------------|-------------|------|
| `remarkable:///` | Your annotations, typed text, and handwriting | Both |
| `remarkableraw:///` | Original PDF/EPUB text | SSH only |

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
| **PDF** | Your annotations, highlights, and typed notes |
| **EPUB** | Your annotations, highlights, and typed notes |
| **Notebook** | Typed text, highlights, and handwritten content (OCR) |

**Note:** Text resources contain only user-created content—not the original PDF/EPUB text. Use raw resources (`remarkableraw:///`) for the original document content. OCR is automatically applied for notebooks with handwritten content.

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

Original PDF and EPUB text content is available as raw resources in SSH mode. These resources extract and return the full text from the original document file—the actual content of the PDF or EPUB, not your annotations.

### URI Format

```
remarkableraw:///{path}.pdf.txt
remarkableraw:///{path}.epub.txt
```

### Examples

```
remarkableraw:///Research%20Paper.pdf.txt
remarkableraw:///Books/Deep%20Work.epub.txt
```

### Response

Raw resources return extracted text from the original file:

```
Chapter 1: Introduction

This paper explores the relationship between...
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

![Resources in VS Code](assets/resources-screenshot.png)

### Programmatically

MCP clients can request resources by URI:

```python
# Request a text resource (includes annotations)
content = await client.read_resource("remarkable:///Meeting%20Notes.txt")

# Request raw PDF text (original document only, no annotations)
pdf_text = await client.read_resource("remarkableraw:///Paper.pdf.txt")
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

### Root Path Filtering

When `REMARKABLE_ROOT_PATH` is configured, only documents within that folder are registered as resources. Paths in URIs are relative to the root:

```json
{
  "env": {
    "REMARKABLE_ROOT_PATH": "/Work"
  }
}
```

With this configuration:
- `/Work/Meeting Notes` → `remarkable:///Meeting%20Notes.txt`
- Documents outside `/Work` are not registered

## Performance Considerations

- Resources are registered at startup (slight delay for large libraries)
- Text extraction happens on-demand when a resource is accessed
- Results are cached per session
- SSH mode is significantly faster than Cloud for resource access
