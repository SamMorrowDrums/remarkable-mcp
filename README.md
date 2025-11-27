# reMarkable MCP Server

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server for accessing your reMarkable tablet data through the reMarkable Cloud.

<!-- mcp-name: io.github.sammorrowdrums/remarkable -->

## Quick Install

[![Install with UVX in VS Code](https://img.shields.io/badge/VS_Code-UVX-0098FF?style=flat-square&logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect/mcp/install?name=remarkable&inputs=%5B%7B%22type%22%3A%22promptString%22%2C%22id%22%3A%22token%22%2C%22description%22%3A%22reMarkable%20API%20token%20(run%20uvx%20remarkable-mcp%20--register%20CODE%20to%20get%20one)%22%2C%22password%22%3Atrue%7D%5D&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22remarkable-mcp%22%5D%2C%22env%22%3A%7B%22REMARKABLE_TOKEN%22%3A%22%24%7Binput%3Atoken%7D%22%7D%7D) [![Install with UVX in VS Code Insiders](https://img.shields.io/badge/VS_Code_Insiders-UVX-24bfa5?style=flat-square&logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect/mcp/install?name=remarkable&inputs=%5B%7B%22type%22%3A%22promptString%22%2C%22id%22%3A%22token%22%2C%22description%22%3A%22reMarkable%20API%20token%20(run%20uvx%20remarkable-mcp%20--register%20CODE%20to%20get%20one)%22%2C%22password%22%3Atrue%7D%5D&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22remarkable-mcp%22%5D%2C%22env%22%3A%7B%22REMARKABLE_TOKEN%22%3A%22%24%7Binput%3Atoken%7D%22%7D%7D&quality=insiders)

## What It Does

- Read typed text directly from notebooks (v3+ software, no OCR needed)
- Browse and search your document library
- Access recent files with content previews
- OCR for handwritten content via pytesseract
- MCP resources and prompts for deeper integration

## Installation

### Using uvx (Recommended)

```bash
# Get your reMarkable token
uvx remarkable-mcp --register YOUR_ONE_TIME_CODE
```

Click the **Quick Install** badges above, or configure manually.

### From Source

```bash
git clone https://github.com/SamMorrowDrums/remarkable-mcp.git
cd remarkable-mcp
uv sync
uv run python server.py --register YOUR_ONE_TIME_CODE
```

## Setup

### 1. Get a One-Time Code

Go to [my.remarkable.com/device/browser/connect](https://my.remarkable.com/device/browser/connect) and generate a code.

### 2. Convert to Token

```bash
uvx remarkable-mcp --register YOUR_CODE
```

### 3. Configure MCP

**VS Code** — Add to `.vscode/mcp.json`:

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

Your token is stored securely using VS Code's input system with `password: true`.

**Claude Desktop** — Add to `claude_desktop_config.json`:

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

## Tools

| Tool | Description |
|------|-------------|
| `remarkable_read` | Extract text from a document |
| `remarkable_browse` | List files or search by name |
| `remarkable_recent` | Get recently modified documents |
| `remarkable_status` | Check connection status |

All tools are read-only and return structured JSON with hints for next actions.

## Resources

Recent documents are automatically registered as MCP resources on startup (if authenticated). Each document becomes available at `remarkable://doc/{name}`.

| URI | Description |
|-----|-------------|
| `remarkable://doc/{name}` | Content of a recent document |
| `remarkable://folders` | Complete folder hierarchy |

## Prompts

`summarize_recent` · `find_notes` · `daily_review` · `export_document` · `organize_library` · `meeting_notes`

## Usage

```python
remarkable_read("Meeting Notes - Nov 2025")
remarkable_browse("/")
remarkable_browse(query="meeting")
remarkable_recent(limit=5, include_preview=True)
```

## Text Extraction

**Typed text** from v3+ notebooks is extracted natively via `rmscene` — no OCR required.

**Handwritten content** uses pytesseract for OCR. Make sure Tesseract is installed on your system:

```bash
# macOS
brew install tesseract

# Ubuntu/Debian
sudo apt-get install tesseract-ocr

# Arch
sudo pacman -S tesseract
```

PDF highlights and annotations are also extracted.

## Design

Intent-based tools that map to what you actually want to do. Responses include hints for logical next steps. Errors explain what went wrong and how to fix it. Four tools cover most use cases.

## Authentication

Set `REMARKABLE_TOKEN` in your MCP config (recommended), or the server will fall back to `~/.rmapi`.

## Development

```bash
uv sync --all-extras
uv run pytest test_server.py -v
uv run ruff check .
uv run ruff format .
```

### Project Structure

```
remarkable-mcp/
├── server.py           # Entry point
├── remarkable_mcp/     # Main package
│   ├── server.py       # FastMCP server
│   ├── api.py          # Cloud API helpers
│   ├── extract.py      # Text extraction
│   ├── tools.py        # MCP tools
│   ├── resources.py    # MCP resources
│   └── prompts.py      # MCP prompts
└── test_server.py      # Tests
```

## License

MIT

---

Built with [rmapy](https://github.com/subutux/rmapy), [rmscene](https://github.com/ricklupton/rmscene), and inspiration from [Scrybble](https://github.com/Scrybbling-together/scrybble).
