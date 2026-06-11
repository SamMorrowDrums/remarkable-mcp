# reMarkable MCP Server

Unlock the full potential of your reMarkable tablet as a **second brain** for AI assistants. This MCP server lets Claude, VS Code Copilot, and other AI tools read, search, and traverse your entire reMarkable library — including handwritten notes via OCR.

<!-- mcp-name: io.github.SamMorrowDrums/remarkable -->

## Why remarkable-mcp?

Your reMarkable tablet is a powerful tool for thinking, note-taking, and research. But that knowledge stays trapped on the device. This MCP server changes that:

- **Full library access** — Browse folders, search documents, read any file
- **Typed text extraction** — Native support for Type Folio and typed annotations
- **Handwriting OCR** — Convert handwritten notes to searchable text
- **PDF & EPUB support** — Extract text from documents, plus your annotations
- **Robust page rendering** — Renders pages locally and automatically falls back to a source PDF when the local stroke renderer can't (USB/SSH use the tablet's own PDF export; cloud uses the original source PDF), so images work across firmware versions and even without system graphics libraries installed
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

Wireless access with **no device connection required** — your reMarkable syncs to the cloud and the MCP reads from there, so it works from anywhere. Requires a reMarkable Connect subscription.

Cloud mode fetches your whole library in parallel and caches content-addressed blobs on disk, so after the first run startups and document reads are near-instant (a 388-document library lists in ~4s cold, ~0.5s warm). See [Cloud Performance & Caching](#cloud-performance--caching) to tune it.

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

All three modes share the same read, render, and upload tools. **Cloud and SSH additionally support full library management** — create folders, move, rename, and delete (with `--write`) — so capability is near-identical and you can genuinely pick whichever matches how your tablet is connected:

- **☁️ Cloud** — *device-free, works from anywhere.* Reads your library straight from reMarkable's cloud over Wi‑Fi with a Connect subscription — no cable, no developer mode. Full read/render plus full write (upload, create folder, move, rename, delete → trash). Parallel fetching and an on-disk blob cache make it fast after the first sync. Best for remote/headless setups or when you don't want to plug in.
- **🔌 USB Web Interface** — *best when the tablet is plugged in.* Enable the web interface in Storage Settings — no subscription, no developer mode. Full read/render plus upload (to your root folder). The tablet's USB web firmware exposes no folder/move/rename/delete endpoints, so for those over a cable use SSH.
- **⚡ SSH** — *for power users who want filesystem-level access.* Requires developer mode over USB. Full read/render plus full write including folder create/move/rename/delete, straight from the tablet filesystem.

| Mode | Setup | Subscription | Offline | Read + render | Raw PDF/EPUB | Upload | Folder ops¹ |
|------|-------|--------------|---------|---------------|--------------|--------|-------------|
| **☁️ Cloud** | One-time code | Connect | ❌ | ✅ | ✅ PDF/EPUB | ✅ | ✅ |
| **🔌 USB Web** | Enable in Settings | Not required | ✅ | ✅ | ✅ PDF | ✅ (to root) | ❌ |
| **⚡ SSH** | Developer mode | Not required | ✅ | ✅ | ✅ PDF/EPUB | ✅ | ✅ |

¹ Folder ops = create folder / move / rename / delete. Upload and folder ops require the `--write` flag (off by default). Deletes move items to the trash and can prompt for confirmation when your client supports elicitation.

### Automatic cloud fallback

If you select a device transport (`--usb` or `--ssh`) but the tablet isn't reachable at startup **and** a cloud token is configured (`REMARKABLE_TOKEN` or `~/.rmapi`), the server automatically falls back to cloud mode and logs a warning. This means a single configuration works whether or not the tablet is plugged in — plug in for fast local access, unplug to keep working over the cloud. `remarkable_status` reports the effective transport and a `fell_back_to_cloud` flag when this happens.

Pass `--no-cloud-fallback` (or set `REMARKABLE_DISABLE_CLOUD_FALLBACK=1`) to disable this and fail instead when the device is unreachable.

**📖 Detailed Setup Guides:**
- [USB Web Interface Setup](docs/usb-web-setup.md) — **recommended** — simple setup, full feature support
- [SSH Setup Guide](docs/ssh-setup.md) — for advanced users who need filesystem access
- Cloud setup is documented in the Quick Install section above; tuning in [Cloud Performance & Caching](#cloud-performance--caching)

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
| `remarkable_status` | Check connection status and the per-transport capability matrix |
| `remarkable_image` | Get PNG/SVG images of pages (supports OCR via sampling) |

These six tools are **read-only** and return structured JSON with hints for next actions. Opt-in **write tools** (`remarkable_upload`, `remarkable_mkdir`, `remarkable_move`, `remarkable_rename`, `remarkable_delete`) are also available with the `--write` flag — see [Write Tools](#write-tools-cloud-ssh--usb-web). An interactive **canvas app** (`remarkable_canvas`) is also registered automatically for clients that support [MCP Apps](#interactive-canvas-app-mcp-apps).

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

> **Note:** PNG rendering automatically falls back to a source PDF when the
> local stroke renderer can't produce an image (empty pages, newer `.rm`
> formats, or a machine without `libcairo`). USB and SSH modes use the tablet's
> native PDF export; cloud mode uses the document's original source PDF. This
> keeps `remarkable_image` working across firmware versions and platforms.
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
| Speed | ⚡ 10-100x faster | ⚡ Fast | ⚡ Fast (parallel + cached) |
| Offline | ✅ Yes | ✅ Yes | ❌ No |
| Subscription | ✅ Not required | ✅ Not required | ❌ Connect required |
| Raw files | ✅ PDFs, EPUBs | ✅ PDFs | ✅ PDFs, EPUBs |
| Upload | ✅ With `--write` | ✅ With `--write` | ✅ With `--write` |
| mkdir/move/rename/delete | ✅ With `--write` | ❌ | ✅ With `--write` |
| Setup | Developer mode | Enable in Settings | One-time code |

📖 **[SSH Setup Guide](docs/ssh-setup.md)**

---

## Write Tools (Cloud, SSH & USB Web)

Opt-in write tools let you upload, organize, and manage documents on your reMarkable. **Disabled by default** for safety. Cloud and SSH modes support the full set; USB web supports upload only (its firmware exposes no folder operations).

| Feature | Cloud Mode | SSH Mode | USB Web Mode |
|---------|:----------:|:--------:|:------------:|
| Upload | ✅ | ✅ | ✅ (to root) |
| Mkdir | ✅ | ✅ | ❌ |
| Move | ✅ | ✅ | ❌ |
| Rename | ✅ | ✅ | ❌ |
| Delete | ✅ (→ trash) | ✅ | ❌ |

### Enabling Write Tools

Add the `--write` flag. It works in any mode (cloud is the default — no flag needed to select it):

```json
{
  "servers": {
    "remarkable": {
      "command": "uvx",
      "args": ["remarkable-mcp", "--write"]
    }
  }
}
```

For SSH or USB web, combine `--write` with the transport flag:
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

USB web mode (upload only):
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
| `remarkable_upload(file_path, parent_folder, document_name)` | Upload a PDF or EPUB file (all modes; USB web ignores folder/name and uploads to root) |
| `remarkable_mkdir(folder_name, parent)` | Create a new folder (cloud and SSH) |
| `remarkable_move(document, dest_folder)` | Move a document or folder (cloud and SSH) |
| `remarkable_rename(document, new_name)` | Rename a document or folder (cloud and SSH) |
| `remarkable_delete(document)` | Delete a document or folder — destructive (cloud and SSH) |

### Safety

- **Upload registers in all modes** — cloud, SSH, and USB web.
- **mkdir, move, rename, delete register in cloud and SSH modes only** — they are not exposed on USB web (the tablet's USB web firmware has no folder/move/rename/delete endpoints), keeping the tool list scoped to what the active transport actually supports.
- **Delete prompts for confirmation when possible** — if the client supports MCP elicitation, `remarkable_delete` asks the user to confirm before deleting; otherwise it relies on the host to gate the call. In cloud mode delete moves the item to the trash (recoverable from your device); set `REMARKABLE_SKIP_CONFIRM=1` to bypass the prompt in automated setups. All write tools carry `ToolAnnotations(readOnlyHint=False)` (and `destructiveHint=True` for delete) so an agent harness can gate writes at the MCP layer.
- After each write operation in SSH mode, the tablet UI restarts automatically to reflect changes.

### Examples

```python
# Upload a PDF
remarkable_upload("paper.pdf", parent_folder="/Research")

# Create a folder
remarkable_mkdir("2024 Archive", parent="/Archive")

# Move a document
remarkable_move("Meeting Notes", "/Archive/2024 Archive")

# Rename a document
remarkable_rename("Untitled", "Q4 Planning Notes")

# Delete (destructive — confirms via elicitation when supported)
remarkable_delete("Old Draft")
```

---

## Interactive Canvas App (MCP Apps)

An interactive page viewer built on the [MCP Apps](https://github.com/modelcontextprotocol/ext-apps) extension (SEP-1865). Clients that support MCP Apps (such as ChatGPT, Claude, VS Code, and the MCP Inspector) render a canvas in a side panel where you can view a document page and navigate through it.

There is **no flag to enable it** — the `remarkable_canvas` tool and its `ui://remarkable/canvas` resource are always registered, and the capability is negotiated automatically at the MCP `initialize` handshake. App-capable clients open the interactive canvas; every other client simply receives the rendered page as an image, so the tool is safe and useful everywhere.

This registers one tool:

| Tool | Description |
|------|-------------|
| `remarkable_canvas(document, page)` | Open a page in the interactive canvas viewer |

How it behaves:

- **App-capable clients** open the canvas (declared at `ui://remarkable/canvas`, MIME `text/html;profile=mcp-app`) and can page through the document via the MCP Apps postMessage bridge — the server delivers each rendered page in the tool result's `structuredContent`.
- **Other clients** still get the rendered page back as an embedded PNG image, so the tool is useful everywhere; it just won't open the interactive panel. The `_meta.ui` / `ui://` metadata is inert to clients that don't advertise the MCP Apps UI extension.

> **Note:** This phase is a **read-only** viewer (render + page navigation). Pen
> capture, local undo, and an explicit Save button that writes annotations back
> to the device are planned as later, device-validated phases; write-back will
> ride the existing `--write` gate rather than adding a new flag. The iframe
> bridge follows the MCP Apps spec but is best validated against your specific
> client.

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

The retry logic honours the `Retry-After` header from rate-limited responses — both the numeric (seconds) form and the HTTP-date form (which Cloudflare, fronting the reMarkable cloud, often sends) — capped at 20 seconds. Auth failures (401) are not retried — they trigger automatic token renewal instead.

---

### Cloud Performance & Caching

Cloud mode is built to make a device-free workflow fast:

- **Parallel traversal** — document metadata is fetched concurrently instead of one document at a time, turning a multi-minute first load into a few seconds.
- **Connection pooling** — HTTP connections are reused (keep-alive), avoiding a fresh TLS handshake per request.
- **Content-addressed blob cache** — reMarkable's cloud is an immutable, hash-addressed store (like Git), so a blob's bytes can never change for a given hash. Downloaded blobs are cached on disk and reused on later runs; changed documents get new hashes and are re-fetched automatically. This makes warm startups and repeat document reads near-instant, and it is invalidation-safe by construction.

You normally don't need to configure any of this, but these environment variables let you tune it:

| Variable | Default | Description |
|----------|---------|-------------|
| `REMARKABLE_SYNC_WORKERS` | `16` | Parallel workers for cloud fetches (clamped to `64`). |
| `REMARKABLE_DISABLE_CACHE` | unset | Set to `1` to disable the on-disk blob cache entirely. |
| `REMARKABLE_CACHE_DIR` | `~/.remarkable/cache/blobs` | Where cached blobs are stored. |
| `REMARKABLE_CACHE_MAX_BLOB` | `4194304` (4 MiB) | Blobs larger than this are streamed through but not cached. |

The cache is purely a local accelerator: deleting `REMARKABLE_CACHE_DIR` only forces the next read to re-download. The mutable cloud root hash is always fetched fresh, so you never see a stale library.

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
