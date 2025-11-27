# reMarkable MCP Server

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server that provides access to your reMarkable tablet data through the reMarkable Cloud API.

<!-- mcp-name: io.github.sammorrowdrums/remarkable -->

## Quick Install

[![Install with UVX in VS Code](https://img.shields.io/badge/VS_Code-UVX-0098FF?style=flat-square&logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect/mcp/install?name=remarkable&inputs=%5B%7B%22type%22%3A%22promptString%22%2C%22id%22%3A%22token%22%2C%22description%22%3A%22reMarkable%20API%20token%20(run%20uvx%20remarkable-mcp%20--register%20CODE%20to%20get%20one)%22%2C%22password%22%3Atrue%7D%5D&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22remarkable-mcp%22%5D%2C%22env%22%3A%7B%22REMARKABLE_TOKEN%22%3A%22%24%7Binput%3Atoken%7D%22%7D%7D) [![Install with UVX in VS Code Insiders](https://img.shields.io/badge/VS_Code_Insiders-UVX-24bfa5?style=flat-square&logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect/mcp/install?name=remarkable&inputs=%5B%7B%22type%22%3A%22promptString%22%2C%22id%22%3A%22token%22%2C%22description%22%3A%22reMarkable%20API%20token%20(run%20uvx%20remarkable-mcp%20--register%20CODE%20to%20get%20one)%22%2C%22password%22%3Atrue%7D%5D&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22remarkable-mcp%22%5D%2C%22env%22%3A%7B%22REMARKABLE_TOKEN%22%3A%22%24%7Binput%3Atoken%7D%22%7D%7D&quality=insiders)

## Features

- 🔐 **Secure Authentication** - Token stored safely via VS Code's input system
- 📖 **Read Documents** - Extract typed text directly from reMarkable notebooks (v3+ software)
- 📁 **Browse Files** - List and navigate your reMarkable folders and documents
- 🔍 **Search** - Search for documents by name
- ⏰ **Recent Files** - Get recently modified documents with content previews
- 📚 **MCP Resources** - Automatic access to recent documents and folder structure
- 💡 **MCP Prompts** - Pre-built prompts for common workflows
- 🔤 **OCR Support** - Optional handwriting recognition via pytesseract

## Installation

### Option 1: Using uvx (Recommended)

The easiest way to use this server is with `uvx`, which runs the package directly from PyPI:

```bash
# First, get your reMarkable token
uvx remarkable-mcp --register YOUR_ONE_TIME_CODE
```

Then click the **Quick Install** badges above, or manually configure VS Code.

### Option 2: From Source (for Development)

```bash
# Clone the repository
git clone https://github.com/SamMorrowDrums/remarkable-mcp.git
cd remarkable-mcp

# Install dependencies
uv sync

# Or with OCR support for handwriting
uv sync --extra ocr

# Get your token
uv run python server.py --register YOUR_ONE_TIME_CODE
```

## Setup

### Step 1: Get a One-Time Code

1. Go to https://my.remarkable.com/device/browser/connect
2. Generate a one-time code (8 characters like `abcd1234`)

### Step 2: Convert to Token

```bash
# Using uvx (recommended)
uvx remarkable-mcp --register YOUR_CODE

# Or from source
uv run python server.py --register YOUR_CODE
```

This outputs your token and shows how to configure it.

### Step 3: Configure MCP

#### VS Code (Recommended)

Add to your `.vscode/mcp.json` or use `MCP: Open User Configuration`:

```json
{
  "inputs": [
    {
      "type": "promptString",
      "id": "remarkable-token",
      "description": "reMarkable API Token",
      "password": true
    }
  ],
  "servers": {
    "remarkable": {
      "command": "uvx",
      "args": ["remarkable-mcp"],
      "env": {
        "REMARKABLE_TOKEN": "${input:remarkable-token}"
      }
    }
  }
}
```

> **Security Note**: Using `inputs` with `password: true` ensures your token is stored securely and prompted on first use, rather than being stored in plain text.

#### Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "remarkable": {
      "command": "uvx",
      "args": ["remarkable-mcp"],
      "env": {
        "REMARKABLE_TOKEN": "your-token-from-step-2"
      }
    }
  }
}
```

## Available Tools

| Tool | Description | When to Use |
|------|-------------|-------------|
| `remarkable_read` | Extract text content from a document | "Read my meeting notes", "Get text from X" |
| `remarkable_browse` | List files or search by name | "Show my folders", "Find documents about X" |
| `remarkable_recent` | Get recently modified documents | "What did I work on recently?" |
| `remarkable_status` | Check authentication and connection | "Am I connected?", debugging |

### Tool Annotations

All tools are marked with MCP annotations:
- `readOnlyHint: true` - Tools only read data, never modify
- `idempotentHint: true` - Safe to retry
- `openWorldHint: true` - Interacts with reMarkable Cloud

### Tool Response Format

All tools return structured JSON with:
- **Data** - The requested information
- **`_hint`** - Suggested next actions for the AI model
- **`_error`** - Educational error messages with recovery suggestions

## Available Resources

| Resource | Description |
|----------|-------------|
| `remarkable://recent` | Your 10 most recently modified documents |
| `remarkable://folders` | Your complete folder hierarchy |
| `remarkable://document/{name}` | Content of a specific document |

## Available Prompts

| Prompt | Description |
|--------|-------------|
| `summarize_recent` | Get an AI summary of recent notes |
| `find_notes` | Search for notes on a specific topic |
| `daily_review` | Review what you worked on today |
| `export_document` | Extract and format a document as markdown |
| `organize_library` | Get suggestions for organizing your library |
| `meeting_notes` | Find and extract meeting notes |

## Example Usage

```python
# Read text from a specific document
remarkable_read("Meeting Notes - Nov 2025")
# Returns: typed text, highlights, metadata + hints for next steps

# Browse your library
remarkable_browse("/")  # List root folder
remarkable_browse("/Work")  # List specific folder
remarkable_browse(query="meeting")  # Search by name

# Get recent documents
remarkable_recent(limit=5, include_preview=True)

# Check connection status
remarkable_status()
```

## Text Extraction

The server can extract:
- ✅ **Typed text** - Native extraction from reMarkable v3+ notebooks (via `rmscene`)
- ✅ **PDF highlights and annotations** - From annotated PDFs
- ✅ **Document metadata** - Names, dates, folder structure
- 🔤 **Handwritten content** - Optional OCR via pytesseract

### How Text Extraction Works

reMarkable tablets running software v3+ store typed text (from Type Folio keyboard or on-screen keyboard) in a structured format that can be read directly - no OCR needed! The server uses `rmscene` to parse these files natively.

For handwritten content, you can optionally enable OCR by installing the `ocr` extras.

## MCP Design Philosophy

This server follows modern MCP design principles:

- **Intent-based tools** - Tools designed around what users want to accomplish, not API endpoints
- **Guided responses** - Every response includes hints for logical next steps
- **Educational errors** - Error messages explain what went wrong and how to fix it
- **Minimal tool count** - Focused set of 4 tools that cover 95% of use cases
- **Resources & Prompts** - Additional MCP capabilities for richer integration

## Authentication

The server supports two authentication methods:

1. **Environment Variable** (recommended): Set `REMARKABLE_TOKEN` in your MCP config
2. **File-based**: Token stored in `~/.rmapi` (created by `--register`)

The environment variable takes precedence if both are present.

## Development

```bash
# Install all dependencies (including dev)
uv sync --all-extras

# Run tests
uv run pytest test_server.py -v

# Lint
uv run ruff check .

# Format
uv run ruff format .

# Fix lint issues
uv run ruff check . --fix
```

### Project Structure

```
remarkable-mcp/
├── server.py              # Entry point (backwards compatible)
├── remarkable_mcp/        # Main package
│   ├── __init__.py
│   ├── server.py          # FastMCP server initialization
│   ├── api.py             # reMarkable Cloud API helpers
│   ├── extract.py         # Text extraction utilities
│   ├── responses.py       # Response formatting
│   ├── tools.py           # MCP tools with annotations
│   ├── resources.py       # MCP resources
│   └── prompts.py         # MCP prompts
├── test_server.py         # Test suite
└── pyproject.toml         # Project configuration
```

### Testing

The project includes comprehensive tests using FastMCP's testing capabilities:

- **Unit tests** - Test individual tool functionality with mocked API calls
- **E2E tests** - Test server initialization and tool listing
- **Integration tests** - Test error handling and edge cases

Run the test suite:

```bash
uv run pytest test_server.py -v
```

All tests use async/await with pytest-asyncio and mock the reMarkable API to avoid requiring actual credentials during testing.

## License

MIT License - see [LICENSE](LICENSE) for details.

## Acknowledgments

- [rmapy](https://github.com/subutux/rmapy) - Python client for reMarkable Cloud
- [rmscene](https://github.com/ricklupton/rmscene) - Native .rm file parser
- [rmapi](https://github.com/ddvk/rmapi) - Go client for reMarkable Cloud
- [Scrybble](https://github.com/Scrybbling-together/scrybble) - Inspiration for this project
