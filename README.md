# reMarkable MCP Server

Unlock the full potential of your reMarkable tablet as a **second brain** for AI assistants. This MCP server lets Claude, VS Code Copilot, and other AI tools read, search, and traverse your entire reMarkable library — including handwritten notes via OCR.

<!-- mcp-name: io.github.SamMorrowDrums/remarkable -->

## Why remarkable-mcp?

Your reMarkable tablet is a powerful tool for thinking, note-taking, and research. But that knowledge stays trapped on the device. This MCP server changes that:

- **Full library access** — Browse folders, search documents, read any file
- **Typed text extraction** — Native support for Type Folio and typed annotations
- **Handwriting OCR** — Convert handwritten notes to searchable text
- **PDF & EPUB support** — Extract text from documents, plus your annotations
- **Robust page rendering** — Renders pages locally and, in USB/SSH mode, automatically falls back to the tablet's own PDF export, so images work across firmware versions and even without system graphics libraries installed
- **Smart search** — Find content across your entire library
- **Second brain integration** — Use with Obsidian, note-taking apps, or any AI workflow

Whether you're researching, writing, or developing ideas, remarkable-mcp lets you leverage everything on your reMarkable through AI.

---

## Quick Install

### 🔌 USB Web Interface (Recommended)

Connect via USB and enable the web interface in your tablet's Storage Settings.

[![Install USB Web Mode in VS Code](https://img.shields.io/badge/VS_Code-Install_USB_Web_Mode-0098FF?style=for-the-badge&logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect/mcp/install?name=remarkable&inputs=%5B%7B%22type%22%3A%22promptString%22%2C%22id%22%3A%22google_vision_api_key%22%2C%22description%22%3A%22Google%20Vision%20API%20Key%20(for%20handwriting%20OCR)%22%2C%22password%22%3Atrue%7D%5D&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22remarkable-mcp%22%2C%22--usb%22%5D%2C%22env%22%3A%7B%22GOOGLE_VISION_API_KEY%22%3A%22%24%7Binput%3Agoogle_vision_api_key%7D%22%7D%7D)
[![Install USB Web Mode in VS Code Insiders](https://img.shields.io/badge/VS_Code_Insiders-Install_USB_Web_Mode-24bfa5?style=for-the-badge&logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect/mcp/install?name=remarkable&inputs=%5B%7B%22type%22%3A%22promptString%22%2C%22id%22%3A%22google_vision_api_key%22%2C%22description%22%3A%22Google%20Vision%20API%20Key%20(for%20handwriting%20OCR)%22%2C%22password%22%3Atrue%7D%5D&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22remarkable-mcp%22%2C%22--usb%22%5D%2C%22env%22%3A%7B%22GOOGLE_VISION_API_KEY%22%3A%22%24%7Binput%3Agoogle_vision_api_key%7D%22%7D%7D&quality=insiders)

**Setup:**
1. Connect your reMarkable via USB
2. On your tablet: **Settings → Storage** → Enable **"USB web interface"**
3. Install via the button above

**Why USB Web?**
- ✅ Fast offline access over USB
- ✅ No subscription required
- ✅ Simple — just enable in Storage Settings

<details>
<summary>📋 Manual USB Web Configuration</summary>

Add to `.vscode/mcp.json`:

```json
{
  "servers": {
    "remarkable": {
      "command": "uvx",
      "args": ["remarkable-mcp", "--usb"],
      "env": {
        "GOOGLE_VISION_API_KEY": "your-api-key"
      }
    }
  }
}
```

**Troubleshooting:**
- Make sure your reMarkable is connected via USB and unlocked
- Verify USB web interface is enabled in Settings → Storage
- The tablet should be accessible at `http://10.11.99.1`

</details>

---

### ⚡ SSH Mode (Advanced)

For power users who need direct filesystem access. Faster than USB Web but requires developer mode (factory reset).

[![Install SSH Mode in VS Code](https://img.shields.io/badge/VS_Code-Install_SSH_Mode-0098FF?style=for-the-badge&logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect/mcp/install?name=remarkable&inputs=%5B%7B%22type%22%3A%22promptString%22%2C%22id%22%3A%22google_vision_api_key%22%2C%22description%22%3A%22Google%20Vision%20API%20Key%20(for%20handwriting%20OCR)%22%2C%22password%22%3Atrue%7D%5D&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22remarkable-mcp%22%2C%22--ssh%22%5D%2C%22env%22%3A%7B%22GOOGLE_VISION_API_KEY%22%3A%22%24%7Binput%3Agoogle_vision_api_key%7D%22%7D%7D)
[![Install SSH Mode in VS Code Insiders](https://img.shields.io/badge/VS_Code_Insiders-Install_SSH_Mode-24bfa5?style=for-the-badge&logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect/mcp/install?name=remarkable&inputs=%5B%7B%22type%22%3A%22promptString%22%2C%22id%22%3A%22google_vision_api_key%22%2C%22description%22%3A%22Google%20Vision%20API%20Key%20(for%20handwriting%20OCR)%22%2C%22password%22%3Atrue%7D%5D&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22remarkable-mcp%22%2C%22--ssh%22%5D%2C%22env%22%3A%7B%22GOOGLE_VISION_API_KEY%22%3A%22%24%7Binput%3Agoogle_vision_api_key%7D%22%7D%7D&quality=insiders)

**Requirements:** [Developer mode enabled](docs/ssh-setup.md) + USB connection to your reMarkable

<details>
<summary>📋 Manual SSH Configuration</summary>

Add to `.vscode/mcp.json`:

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

See [SSH Setup Guide](docs/ssh-setup.md) for detailed instructions.

</details>

---

### ☁️ Cloud Mode (Wireless)

For wireless or remote access when USB isn't available. Requires a reMarkable Connect subscription and is significantly slower than USB modes.

<details>
<summary>📋 Cloud Mode Setup</summary>

#### 1. Get a One-Time Code

Go to [my.remarkable.com/device/desktop/connect](https://my.remarkable.com/device/desktop/connect) and generate a code.

#### 2. Convert to Token

```bash
uvx remarkable-mcp --register YOUR_CODE
```

#### 3. Install

[![Install Cloud Mode in VS Code](https://img.shields.io/badge/VS_Code-Install_Cloud_Mode-0098FF?style=flat-square&logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect/mcp/install?name=remarkable&inputs=%5B%7B%22type%22%3A%22promptString%22%2C%22id%22%3A%22token%22%2C%22description%22%3A%22reMarkable%20API%20token%22%2C%22password%22%3Atrue%7D%2C%7B%22type%22%3A%22promptString%22%2C%22id%22%3A%22google_vision_api_key%22%2C%22description%22%3A%22Google%20Vision%20API%20Key%20(for%20handwriting%20OCR)%22%2C%22password%22%3Atrue%7D%5D&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22remarkable-mcp%22%5D%2C%22env%22%3A%7B%22REMARKABLE_TOKEN%22%3A%22%24%7Binput%3Atoken%7D%22%2C%22GOOGLE_VISION_API_KEY%22%3A%22%24%7Binput%3Agoogle_vision_api_key%7D%22%7D%7D)
[![Install Cloud Mode in VS Code Insiders](https://img.shields.io/badge/VS_Code_Insiders-Install_Cloud_Mode-24bfa5?style=flat-square&logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect/mcp/install?name=remarkable&inputs=%5B%7B%22type%22%3A%22promptString%22%2C%22id%22%3A%22token%22%2C%22description%22%3A%22reMarkable%20API%20token%22%2C%22password%22%3Atrue%7D%2C%7B%22type%22%3A%22promptString%22%2C%22id%22%3A%22google_vision_api_key%22%2C%22description%22%3A%22Google%20Vision%20API%20Key%20(for%20handwriting%20OCR)%22%2C%22password%22%3Atrue%7D%5D&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22remarkable-mcp%22%5D%2C%22env%22%3A%7B%22REMARKABLE_TOKEN%22%3A%22%24%7Binput%3Atoken%7D%22%2C%22GOOGLE_VISION_API_KEY%22%3A%22%24%7Binput%3Agoogle_vision_api_key%7D%22%7D%7D&quality=insiders)

Or configure manually in `.vscode/mcp.json`:

```json
{
  "inputs": [
    {
      "type": "promptString",
      "id": "remarkable-token",
      "description": "reMarkable API Token",
      "password": true
    },
    {
      "type": "promptString",
      "id": "google-vision-key",
      "description": "Google Vision API Key",
      "password": true
    }
  ],
  "servers": {
    "remarkable": {
      "command": "uvx",
      "args": ["remarkable-mcp"],
      "env": {
        "REMARKABLE_TOKEN": "${input:remarkable-token}",
        "GOOGLE_VISION_API_KEY": "${input:google-vision-key}"
      }
    }
  }
}
```

</details>

---

<!-- Screenshots section - uncomment when screenshots are added
## Screenshots

### MCP Resources

Documents appear as resources that AI assistants can access directly:

![Resources in VS Code](docs/assets/resources-screenshot.png)

### Tool Calls in Action

AI assistants use the tools to read documents, search content, and more:

![Tool calls in VS Code](docs/assets/tool-calls-screenshot.png)
-->

---

## Connection Modes

Choose the connection method that works best for you:

| Mode | Setup Difficulty | Speed | Requirements | Best For |
|------|-----------------|-------|--------------|----------|
| **🔌 USB Web (Recommended)** | ✅ Easy | Fast | USB cable, enable in Storage Settings | Everyone |
| **⚡ SSH** | ⚠️ Advanced | Very Fast | Developer mode, USB connection | Power users |
| **☁️ Cloud** | ✅ Easy | Slow | reMarkable Connect subscription | Remote/wireless access |

**📖 Detailed Setup Guides:**
- [USB Web Interface Setup](docs/usb-web-setup.md) — **Recommended** — simple setup, full feature support
- [SSH Setup Guide](docs/ssh-setup.md) — For advanced users who need filesystem access
- Cloud setup is documented in the Quick Install section above

---

## OpenClaw Integration

remarkable-mcp works as an [OpenClaw](https://github.com/openclaw/openclaw) skill. Add to your `openclaw.json`:

```json
{
  "mcpServers": {
    "remarkable": {
      "command": "uvx",
      "args": ["remarkable-mcp", "--usb"]
    }
  }
}
```

Install from [ClawHub](https://clawhub.ai):

```bash
clawhub install remarkable-mcp
```

Or copy the `SKILL.md` from this repository into your `~/.openclaw/skills/remarkable-mcp/` directory.

---

## Tools

| Tool | Description |
|------|-------------|
| `remarkable_read` | Read and extract text from documents (with pagination and search) |
| `remarkable_browse` | Navigate folders, search by document name, or filter by tags |
| `remarkable_search` | Search content across multiple documents (with tag filtering) |
| `remarkable_recent` | Get recently modified documents |
| `remarkable_status` | Check connection status |
| `remarkable_image` | Get PNG/SVG images of pages (supports OCR via sampling) |

All tools are **read-only** and return structured JSON with hints for next actions.

📖 **[Full Tools Documentation](docs/tools.md)**

### Smart Features

- **Auto-redirect** — Browsing a document path returns its content automatically
- **Auto-OCR** — Notebooks with no typed text automatically enable OCR
- **Batch search** — Search across multiple documents in one call
- **Vision support** — Get page images for visual context (diagrams, mockups, sketches)
- **Sampling OCR** — Use client's AI for OCR on images (no API key needed)
- **Tag support** — Filter and organize documents by tags

### Example Usage

```python
# Read a document
remarkable_read("Meeting Notes")

# Search for keywords
remarkable_read("Project Plan", grep="deadline")

# Enable OCR for handwritten notes
remarkable_read("Journal", include_ocr=True)

# Browse your library
remarkable_browse("/Work/Projects")

# Filter by tags
remarkable_browse("/", tags=["important"])
remarkable_browse("/Work", tags=["project", "active"])

# Search across documents
remarkable_search("meeting", grep="action items")

# Search with tag filter
remarkable_search("project", tags=["work"])

# Get recent documents
remarkable_recent(limit=10)

# Get a page image (for visual content like UI mockups or diagrams)
remarkable_image("UI Mockup", page=1)

# Get SVG for editing in design tools
remarkable_image("Wireframe", output_format="svg")

# Get image with OCR text extraction (uses sampling if configured)
remarkable_image("Handwritten Notes", include_ocr=True)

# Transparent background for compositing
remarkable_image("Logo Sketch", background="#00000000")

# Compatibility mode: return resource URI instead of embedded resource
remarkable_image("Diagram", compatibility=True)
```

> **Note:** In USB and SSH modes, PNG rendering automatically falls back to the
> tablet's native PDF export when the local stroke renderer can't produce an
> image (empty pages, newer `.rm` formats, or a machine without `libcairo`).
> This keeps `remarkable_image` working across firmware versions and platforms.
> Cloud mode has no native export, so it relies on the local renderer.

---

## Resources

Documents are automatically registered as MCP resources:

| URI Scheme | Description |
|------------|-------------|
| `remarkable:///{path}.txt` | Extracted text content |
| `remarkableraw:///{path}.pdf` | Original PDF file (SSH only) |
| `remarkableraw:///{path}.epub` | Original EPUB file (SSH only) |
| `remarkableimg:///{path}.page-{N}.png` | PNG image of page N (notebooks only) |
| `remarkablesvg:///{path}.page-{N}.svg` | SVG vector image of page N (notebooks only) |

📖 **[Full Resources Documentation](docs/resources.md)**

---

## OCR for Handwriting

For handwritten content, remarkable-mcp offers several OCR backends. Choose based on your setup and requirements:

| Backend | Setup | Quality | Offline | Best For |
|---------|-------|---------|---------|----------|
| **Sampling** | No API key | Depends on client model | ✅ | Users with capable AI clients |
| **Google Vision** | API key | Excellent | ❌ | Best handwriting accuracy |
| **Tesseract** | System install | Poor for handwriting | ✅ | Printed text, offline fallback |

### Quick Setup

Set `REMARKABLE_OCR_BACKEND` in your MCP config:

```json
{
  "env": {
    "REMARKABLE_OCR_BACKEND": "sampling"
  }
}
```

**Options:** `sampling`, `google`, `tesseract`, `auto`

<details>
<summary>📖 Sampling OCR (No API Key)</summary>

Uses your MCP client's AI model for OCR. Works with clients that support MCP sampling (VS Code + Copilot, Claude Desktop, etc.).

**Pros:**
- No additional API keys needed
- Quality depends on your client's model (GPT-4, Claude, etc.)
- Private — handwriting stays local to your client

**Cons:**
- Only available with sampling-capable clients
- Falls back to Google Vision (if API key configured) or Tesseract if sampling unavailable

</details>

<details>
<summary>📖 Google Cloud Vision</summary>

Provides consistently excellent handwriting recognition.

**Setup:**
1. Enable [Cloud Vision API](https://console.cloud.google.com/apis/library/vision.googleapis.com)
2. Create an [API key](https://console.cloud.google.com/apis/credentials)
3. Add to config: `"GOOGLE_VISION_API_KEY": "your-key"`

**Cost:** 1,000 free requests/month, then ~$1.50 per 1,000.

📖 **[Full Google Vision Setup Guide](docs/google-vision-setup.md)**

</details>

<details>
<summary>📖 Tesseract (Fallback)</summary>

Open-source OCR designed for printed text. Poor results with handwriting, but useful as an offline fallback.

```bash
# Install Tesseract
# macOS
brew install tesseract

# Ubuntu/Debian
sudo apt install tesseract-ocr

# Windows
choco install tesseract
```

</details>

### Default Behavior (`auto`)

When `REMARKABLE_OCR_BACKEND=auto` (default):
1. Google Vision (if `GOOGLE_VISION_API_KEY` is set)
2. Tesseract (fallback)

---

## SSH vs USB Web vs Cloud Comparison

| Feature | SSH Mode | USB Web | Cloud API |
|---------|----------|---------|-----------|
| Speed | ⚡ 10-100x faster | ⚡ Fast | Slower |
| Offline | ✅ Yes | ✅ Yes | ❌ No |
| Subscription | ✅ Not required | ✅ Not required | ❌ Connect required |
| Raw files | ✅ PDFs, EPUBs | ✅ PDFs | ❌ Not available |
| Upload | ✅ With `--write` | ✅ With `--write` | ❌ Not available |
| mkdir/move/rename/delete | ✅ With `--write` | ❌ | ❌ |
| Setup | Developer mode | Enable in Settings | One-time code |

📖 **[SSH Setup Guide](docs/ssh-setup.md)**

---

## Write Tools (SSH & USB Web)

Opt-in write tools let you upload, organize, and manage documents directly on your reMarkable tablet. **Disabled by default** for safety.

| Feature | SSH Mode | USB Web Mode | Cloud Mode |
|---------|:--------:|:------------:|:----------:|
| Upload | ✅ | ✅ | ❌ |
| Mkdir | ✅ | ❌ | ❌ |
| Move | ✅ | ❌ | ❌ |
| Rename | ✅ | ❌ | ❌ |
| Delete | ✅ | ❌ | ❌ |

### Enabling Write Tools

Add the `--write` flag when running in SSH or USB web mode:

```json
{
  "servers": {
    "remarkable": {
      "command": "uvx",
      "args": ["remarkable-mcp", "--ssh", "--write"]
    }
  }
}
```

Or with USB web mode (upload only):
```json
{
  "servers": {
    "remarkable": {
      "command": "uvx",
      "args": ["remarkable-mcp", "--usb", "--write"]
    }
  }
}
```

Or set the environment variable:
```json
{
  "env": {
    "REMARKABLE_ENABLE_WRITE": "1"
  }
}
```

### Available Write Tools

| Tool | Description |
|------|-------------|
| `remarkable_upload(file_path, parent_folder, document_name)` | Upload a PDF or EPUB file |
| `remarkable_mkdir(folder_name, parent)` | Create a new folder (SSH only) |
| `remarkable_move(document, dest_folder)` | Move a document or folder (SSH only) |
| `remarkable_rename(document, new_name)` | Rename a document or folder (SSH only) |
| `remarkable_delete(document)` | Delete a document or folder — destructive (SSH only) |

### Safety

- **Upload registers in SSH and USB web mode** — cloud mode returns a clear error
- **mkdir, move, rename, delete are only registered in SSH mode** — they are not exposed at all on USB web or cloud, keeping the tool list scoped to what the active transport actually supports
- **Delete is destructive and immediate** — the MCP client is responsible for confirming with the user before invoking. All write tools carry `ToolAnnotations(readOnlyHint=False)` (and `destructiveHint=True` for delete) so an agent harness can gate writes at the MCP layer.
- After each write operation (SSH), the tablet UI restarts automatically to reflect changes

### Examples

```python
# Upload a PDF
remarkable_upload("/tmp/paper.pdf", parent_folder="/Research")

# Create a folder (SSH only)
remarkable_mkdir("2024 Archive", parent="/Archive")

# Move a document (SSH only)
remarkable_move("Meeting Notes", "/Archive/2024 Archive")

# Rename a document (SSH only)
remarkable_rename("Untitled", "Q4 Planning Notes")

# Delete (destructive — confirm with user first) (SSH only)
remarkable_delete("Old Draft")
```

---

## Advanced Configuration

### Root Path Filtering

Limit the MCP server to a specific folder on your reMarkable. All operations will be scoped to this folder:

```json
{
  "servers": {
    "remarkable": {
      "command": "uvx",
      "args": ["remarkable-mcp", "--ssh"],
      "env": {
        "REMARKABLE_ROOT_PATH": "/Work",
        "GOOGLE_VISION_API_KEY": "your-api-key"
      }
    }
  }
}
```

With this configuration:
- `remarkable_browse("/")` shows contents of `/Work`
- `remarkable_browse("/Projects")` shows `/Work/Projects`
- Documents outside `/Work` are not accessible

Useful for:
- Focusing on work documents during office hours
- Separating personal and professional notes
- Limiting scope for specific AI workflows

### Custom Background Color

Set the default background color for image rendering:

```json
{
  "servers": {
    "remarkable": {
      "command": "uvx",
      "args": ["remarkable-mcp", "--ssh"],
      "env": {
        "REMARKABLE_BACKGROUND_COLOR": "#FFFFFF"
      }
    }
  }
}
```

Supported formats:
- `#RRGGBB` — RGB hex (e.g., `#FFFFFF` for white)
- `#RRGGBBAA` — RGBA hex (e.g., `#00000000` for transparent)

Default is `#FBFBFB` (reMarkable paper color). This affects both the `remarkable_image` tool and image resources.

---

### Retry Configuration

Cloud API requests automatically retry on transient failures (HTTP 429, 500, 502, 503, 504) and network errors with exponential backoff and jitter. You can tune this via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `REMARKABLE_RETRY_ATTEMPTS` | `3` | Maximum number of request attempts (minimum 1) |
| `REMARKABLE_RETRY_DELAY` | `2.0` | Base delay in seconds for exponential backoff |

The retry logic honours the `Retry-After` header from rate-limited responses, capped at 20 seconds. Auth failures (401) are not retried — they trigger automatic token renewal instead.

---

## Use Cases

### Research & Writing

Use remarkable-mcp while working in an Obsidian vault or similar to transfer knowledge from your handwritten notes into structured documents. AI can read your research notes and help develop your ideas.

### Daily Review

Ask your AI assistant to summarize your recent notes, find action items, or identify patterns across your journal entries.

### Document Search

Find that half-remembered note by searching across your entire library — including handwritten content.

### Knowledge Management

Treat your reMarkable as a second brain that AI can access. Combined with tools like Obsidian, you can build a powerful personal knowledge system.

---

## Documentation

| Guide | Description |
|-------|-------------|
| [SSH Setup](docs/ssh-setup.md) | Enable developer mode and configure SSH |
| [Google Vision Setup](docs/google-vision-setup.md) | Set up handwriting OCR |
| [Tools Reference](docs/tools.md) | Detailed tool documentation |
| [Resources Reference](docs/resources.md) | MCP resources documentation |
| [Capability Negotiation](docs/capabilities.md) | MCP protocol capabilities |
| [Development](docs/development.md) | Contributing and development setup |
| [Future Plans](docs/future-plans.md) | Roadmap and planned features |

---

## Development

```bash
git clone https://github.com/SamMorrowDrums/remarkable-mcp.git
cd remarkable-mcp
uv sync --all-extras
uv run pytest test_server.py -v
```

📖 **[Development Guide](docs/development.md)**

---

## License

MIT

---

Built with [rmscene](https://github.com/ricklupton/rmscene), [PyMuPDF](https://pymupdf.readthedocs.io/), and inspiration from [ddvk/rmapi](https://github.com/ddvk/rmapi).
