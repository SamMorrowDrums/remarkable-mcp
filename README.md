# reMarkable MCP Server

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server that provides access to your reMarkable tablet data through the reMarkable Cloud API.

## Features

- 🔐 **Authentication** - Register and authenticate with reMarkable Cloud
- 📁 **Browse Files** - List and navigate your reMarkable folders and documents
- 🔍 **Search** - Search for documents by name
- 📄 **Get Documents** - Download and extract text content from documents
- ⏰ **Recent Files** - Get recently modified documents
- 📥 **Download** - Download documents for local processing

## Installation

### Using uv (recommended)

```bash
# Clone the repository
git clone https://github.com/SamMorrowDrums/remarkable-mcp.git
cd remarkable-mcp

# Install with uv
uv pip install -e .
```

### Using pip

```bash
pip install -e .
```

## Configuration

### VS Code / Cursor

Add to your `.vscode/mcp.json`:

```json
{
  "servers": {
    "remarkable": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/remarkable-mcp", "python", "server.py"]
    }
  }
}
```

Or if installed globally:

```json
{
  "servers": {
    "remarkable": {
      "command": "python",
      "args": ["/path/to/remarkable-mcp/server.py"]
    }
  }
}
```

### Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "remarkable": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/remarkable-mcp", "python", "server.py"]
    }
  }
}
```

## Usage

### First-time Setup

1. Go to https://my.remarkable.com/device/browser/connect
2. Generate a one-time code
3. Use the `remarkable_register` tool with your code:

```
remarkable_register("abcd1234")
```

### Available Tools

| Tool | Description |
|------|-------------|
| `remarkable_auth_status` | Check authentication status |
| `remarkable_register` | Register with a one-time code |
| `remarkable_list_files` | List files in a folder (use "/" for root) |
| `remarkable_search` | Search for documents by name |
| `remarkable_recent` | Get recently modified documents |
| `remarkable_get_document` | Get document details and extract text |
| `remarkable_download_pdf` | Download a document as a zip archive |

### Example Workflow

```python
# Check if authenticated
remarkable_auth_status()

# If not, register with one-time code from remarkable.com
remarkable_register("your-code")

# List all files
remarkable_list_files("/")

# Search for a specific document
remarkable_search("meeting notes")

# Get recent documents
remarkable_recent(limit=5)

# Extract text from a document
remarkable_get_document("My Notes", include_text=True)
```

## Text Extraction

The server can extract:
- ✅ Typed text (from Type Folio keyboard)
- ✅ PDF highlights and annotations
- ✅ Document metadata
- ⚠️ Handwritten content (indicated but not OCR'd - requires external tools)

For full handwriting OCR, consider using the [remarks](https://github.com/lucasrla/remarks) library on downloaded documents.

## Configuration Directory

The server stores authentication tokens and cache in:
- `~/.remarkable/token` - Authentication token
- `~/.remarkable/cache/` - Local cache

## Development

```bash
# Install dev dependencies
uv pip install -e ".[dev]"

# Format code
black .

# Lint
ruff check .
```

## License

MIT License - see [LICENSE](LICENSE) for details.

## Acknowledgments

- [rmapy](https://github.com/subutux/rmapy) - Python client for reMarkable Cloud
- [remarks](https://github.com/lucasrla/remarks) - Extract annotations from reMarkable
- [rmapi](https://github.com/ddvk/rmapi) - Go client for reMarkable Cloud
- [Scrybble](https://github.com/Scrybbling-together/scrybble) - Inspiration for this project
