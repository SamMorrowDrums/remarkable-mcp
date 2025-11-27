# reMarkable MCP Server

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server that provides access to your reMarkable tablet data through the reMarkable Cloud API.

## Features

- 🔐 **Authentication** - Register once, use token in config
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

# Create venv and install
uv venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
uv pip install -e .
```

### Using pip

```bash
pip install -e .
```

## Setup

### Step 1: Get a One-Time Code

1. Go to https://my.remarkable.com/device/browser/connect
2. Generate a one-time code (8 characters like `abcd1234`)

### Step 2: Convert to Token

Run the registration command to convert your one-time code to a persistent token:

```bash
cd remarkable-mcp
source .venv/bin/activate
python server.py --register YOUR_CODE
```

This will output your token and show you how to configure it.

### Step 3: Configure MCP

Add to your `.vscode/mcp.json` with the token:

```json
{
  "servers": {
    "remarkable": {
      "command": "/path/to/remarkable-mcp/.venv/bin/python",
      "args": ["/path/to/remarkable-mcp/server.py"],
      "env": {
        "REMARKABLE_TOKEN": "your-token-from-step-2"
      }
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
      "command": "/path/to/remarkable-mcp/.venv/bin/python",
      "args": ["/path/to/remarkable-mcp/server.py"],
      "env": {
        "REMARKABLE_TOKEN": "your-token-from-step-2"
      }
    }
  }
}
```

## Available Tools

| Tool | Description |
|------|-------------|
| `remarkable_auth_status` | Check authentication status |
| `remarkable_list_files` | List files in a folder (use "/" for root) |
| `remarkable_search` | Search for documents by name |
| `remarkable_recent` | Get recently modified documents |
| `remarkable_get_document` | Get document details and extract text |
| `remarkable_download_pdf` | Download a document as a zip archive |

## Example Usage

```python
# Check if authenticated
remarkable_auth_status()

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

## Authentication

The server supports two authentication methods:

1. **Environment Variable** (recommended): Set `REMARKABLE_TOKEN` in your MCP config
2. **File-based**: Token stored in `~/.rmapi` (created by `--register`)

The environment variable takes precedence if both are present.

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
