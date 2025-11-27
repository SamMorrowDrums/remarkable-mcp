# reMarkable MCP Server

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server for accessing your reMarkable tablet data through the reMarkable Cloud.

<!-- mcp-name: io.github.SamMorrowDrums/remarkable -->

## Quick Install

[![Install with UVX in VS Code](https://img.shields.io/badge/VS_Code-UVX-0098FF?style=flat-square&logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect/mcp/install?name=remarkable&inputs=%5B%7B%22type%22%3A%22promptString%22%2C%22id%22%3A%22token%22%2C%22description%22%3A%22reMarkable%20API%20token%20(run%20uvx%20remarkable-mcp%20--register%20CODE%20to%20get%20one)%22%2C%22password%22%3Atrue%7D%5D&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22remarkable-mcp%22%5D%2C%22env%22%3A%7B%22REMARKABLE_TOKEN%22%3A%22%24%7Binput%3Atoken%7D%22%7D%7D) [![Install with UVX in VS Code Insiders](https://img.shields.io/badge/VS_Code_Insiders-UVX-24bfa5?style=flat-square&logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect/mcp/install?name=remarkable&inputs=%5B%7B%22type%22%3A%22promptString%22%2C%22id%22%3A%22token%22%2C%22description%22%3A%22reMarkable%20API%20token%20(run%20uvx%20remarkable-mcp%20--register%20CODE%20to%20get%20one)%22%2C%22password%22%3Atrue%7D%5D&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22remarkable-mcp%22%5D%2C%22env%22%3A%7B%22REMARKABLE_TOKEN%22%3A%22%24%7Binput%3Atoken%7D%22%7D%7D&quality=insiders)

## What It Does

- Read typed text directly from notebooks (v3+ software, no OCR needed)
- Browse and search your document library
- Access recent files with content previews
- OCR for handwritten content (Google Vision API recommended)
- MCP resources and prompts for deeper integration

## ⚡ Recommended: SSH Mode + Google Vision

For the best experience, we strongly recommend:

1. **SSH Mode** — 10-100x faster than Cloud API, works offline, no subscription needed
2. **Google Vision API** — Far superior handwriting recognition compared to Tesseract

See [SSH Mode Setup](#ssh-mode-recommended) and [OCR Configuration](#ocr-configuration) below.

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

### SSH Mode (Recommended)

Connect directly to your reMarkable via USB — **10-100x faster** than Cloud API, works offline, and doesn't require a Connect subscription.

> 📖 **Detailed SSH setup guide:** [remarkable.guide/guide/access/ssh.html](https://remarkable.guide/guide/access/ssh.html)

#### Requirements

1. **Developer Mode enabled** on your reMarkable tablet
   - Go to Settings → General → Software → Developer mode
   - This is required even if you have a Connect subscription
   - ⚠️ Enabling developer mode will factory reset your device (back up first!)

2. **USB connection** to your computer
   - Connect via the USB-C cable
   - Your tablet must be on and unlocked

3. **SSH access** (automatic with developer mode)
   - Default IP over USB: `10.11.99.1`
   - Password shown in Settings → General → Software → Developer mode

#### Configure MCP for SSH

**VS Code** — Add to `.vscode/mcp.json`:

```json
{
  "servers": {
    "remarkable": {
      "command": "uvx",
      "args": ["remarkable-mcp", "--ssh"],
      "env": {
        "GOOGLE_VISION_API_KEY": "your-api-key"
      }
    }
  }
}
```

That's it! Default connection is `root@10.11.99.1` (standard USB IP).

#### Custom SSH Host

Set up passwordless SSH for convenience:

```bash
# Copy your SSH key to the tablet
ssh-copy-id root@10.11.99.1

# Or add to ~/.ssh/config:
Host remarkable
    HostName 10.11.99.1
    User root
```

Then use the alias:

```json
{
  "servers": {
    "remarkable": {
      "command": "uvx",
      "args": ["remarkable-mcp", "--ssh"],
      "env": {
        "REMARKABLE_SSH_HOST": "remarkable",
        "GOOGLE_VISION_API_KEY": "your-api-key"
      }
    }
  }
}
```

#### SSH Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `REMARKABLE_SSH_HOST` | `10.11.99.1` | SSH hostname or IP |
| `REMARKABLE_SSH_USER` | `root` | SSH username |
| `REMARKABLE_SSH_PORT` | `22` | SSH port |

### Cloud API (Alternative)

If you can't enable developer mode, you can use the Cloud API. This is slower and requires a reMarkable Connect subscription.

#### 1. Get a One-Time Code

Go to [my.remarkable.com/device/desktop/connect](https://my.remarkable.com/device/desktop/connect) and generate a code.

#### 2. Convert to Token

```bash
uvx remarkable-mcp --register YOUR_CODE
```

#### 3. Configure MCP

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
        "REMARKABLE_TOKEN": "${input:remarkable-token}",
        "GOOGLE_VISION_API_KEY": "your-api-key"
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
        "REMARKABLE_TOKEN": "your-token-from-step-2",
        "GOOGLE_VISION_API_KEY": "your-api-key"
      }
    }
  }
}
```

## Tools

| Tool | Description |
|------|-------------|
| `remarkable_read` | Extract text from a document (with pagination and search) |
| `remarkable_browse` | List files or search by name |
| `remarkable_recent` | Get recently modified documents |
| `remarkable_status` | Check connection status |

All tools are read-only and return structured JSON with hints for next actions.

### `remarkable_read` Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `document` | string | *required* | Document name or path |
| `content_type` | string | `"all"` | `"all"`, `"annotations"`, or `"raw"` |
| `page` | int | `1` | Page number for pagination |
| `grep` | string | `None` | Search for keywords (returns matches with context) |
| `include_ocr` | bool | `False` | Enable OCR for handwritten content |

**Content Types:**
- `"all"` — Raw document text plus annotations/highlights (default)
- `"annotations"` — Only typed text, highlights, and OCR content from notebooks
- `"raw"` — Only raw PDF/EPUB text (no annotations)

**Pagination Output:**
```json
{
  "content": "...",
  "page": 1,
  "total_pages": 42,
  "more": true,
  "next_page": 2
}
```

**Grep Output:**
```json
{
  "content": "...context around matches...",
  "grep_term": "search term",
  "grep_matches": 15,
  "page": 1
}
```

## Resources

Documents are automatically registered as MCP resources on startup.

| URI Scheme | Description |
|------------|-------------|
| `remarkable://{path}.txt` | Extracted text content from any document |
| `remarkableraw://{path}.pdf` | Raw PDF file (SSH mode only) |
| `remarkableraw://{path}.epub` | Raw EPUB file (SSH mode only) |

### Text Resources (`remarkable://`)

Each document is registered with its full path. Returns extracted text content:
- **PDF/EPUB**: Full text content extracted from the source file
- **Notebooks**: Typed text (Type Folio), highlights, and annotations
- Handwritten content via OCR (if enabled)

### Raw Resources (`remarkableraw://`)

PDF and EPUB files are also registered as raw resources in SSH mode. Returns the original file as base64-encoded data. Cloud API doesn't provide access to source files, so raw resources are only available in SSH mode.

## Prompts

`summarize_recent` · `find_notes` · `daily_review` · `export_document` · `organize_library` · `meeting_notes`

## Usage

```python
# Read first page of a document
remarkable_read("Meeting Notes - Nov 2025")

# Read page 3 of a long document
remarkable_read("My Book.epub", page=3)

# Search for keywords in a document
remarkable_read("Project Plan", grep="deadline")

# Only get annotations (typed text, highlights, OCR)
remarkable_read("November journal 2025", content_type="annotations", include_ocr=True)

# Only get raw PDF/EPUB text (no annotations)
remarkable_read("Research Paper.pdf", content_type="raw")

# Browse and search
remarkable_browse("/")
remarkable_browse(query="meeting")
remarkable_recent(limit=5, include_preview=True)
```

## Text Extraction

### PDF and EPUB Documents

Text is extracted directly from PDF and EPUB files using PyMuPDF and ebooklib. This provides the full document content without needing OCR.

### Notebooks

**Typed text** from v3+ notebooks is extracted natively via `rmscene` — no OCR required.

**Handwritten content** uses OCR. Two backends are supported:

### Google Cloud Vision (Strongly Recommended)

**Google Vision is far superior to Tesseract for handwriting recognition.** Unless your handwriting is exceptionally clear and print-like, Tesseract will produce mostly gibberish. Google Vision handles cursive, messy handwriting, and mixed text/drawings much better.

#### Quick Setup with API Key (Easiest)

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project (or select existing)
3. Enable the [Cloud Vision API](https://console.cloud.google.com/apis/library/vision.googleapis.com)
4. Go to [Credentials](https://console.cloud.google.com/apis/credentials) → Create Credentials → API Key
5. Add the key to your MCP config:

```json
{
  "env": {
    "GOOGLE_VISION_API_KEY": "your-api-key"
  }
}
```

**Cost:** Vision API offers 1,000 free requests/month. After that, ~$1.50 per 1,000 images.

#### Alternative: Service Account Credentials

For production use or tighter security:

```bash
# Set up credentials (one of these methods):
# 1. Set GOOGLE_APPLICATION_CREDENTIALS to your service account JSON file
# 2. Run `gcloud auth application-default login` for development
# 3. Use a GCP environment with default credentials (Cloud Run, GKE, etc.)

# Install the optional SDK dependency
pip install remarkable-mcp[ocr]
```

### Tesseract (Fallback — Not Recommended for Handwriting)

Tesseract is designed for **printed text**, not handwriting. It will be used as a fallback if Google Vision is not configured, but expect poor results on handwritten notes.

Only use Tesseract if:
- You have very clear, print-like handwriting
- You need fully offline OCR
- You're only processing printed documents

```bash
# macOS
brew install tesseract

# Ubuntu/Debian
sudo apt-get install tesseract-ocr

# Arch
sudo pacman -S tesseract
```

### OCR Configuration

| Environment Variable | Values | Description |
|---------------------|--------|-------------|
| `GOOGLE_VISION_API_KEY` | API key string | Google Vision API key (recommended) |
| `REMARKABLE_OCR_BACKEND` | `auto`, `google`, `tesseract` | Force a specific backend. Default: `auto` (uses Google if configured) |

PDF highlights and annotations are also extracted automatically.

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

Built with [rmscene](https://github.com/ricklupton/rmscene), inspiration from [ddvk/rmapi](https://github.com/ddvk/rmapi) for the sync protocol, and [Scrybble](https://github.com/Scrybbling-together/scrybble) for text extraction ideas.
