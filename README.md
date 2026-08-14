# reMarkable MCP Server

Access a reMarkable library from MCP clients such as Claude, VS Code Copilot,
OpenWebUI, and other compatible tools.

<!-- mcp-name: io.github.SamMorrowDrums/remarkable -->

## Features

- Browse folders and recent documents.
- Search names, tags, and extracted text.
- Read typed text, PDF and EPUB text, highlights, and annotations.
- Render notebooks and annotated PDFs as PNG or SVG.
- Run handwriting OCR through MCP sampling, Google Vision, or Tesseract.
- Upload files and manage folders in supported transports.
- Render Markdown as PDF and upload it to the tablet.
- Open an interactive canvas in clients that support MCP Apps.
- Serve MCP over stdio or local Streamable HTTP.
- Serve MCP `2026-07-28` and every earlier SDK-supported revision from one
  stable SDK 2.x `MCPServer`.

---

## Quick Install

### Prerequisite: install `uv`

The commands below use `uvx`, which is included with [`uv`](https://docs.astral.sh/uv/getting-started/installation/).
Page images are rasterized with PyMuPDF; no system Cairo, browser, or graphics
runtime needs to be installed on macOS, Linux, or Windows.

#### macOS and Linux

Use the [official standalone installer](https://docs.astral.sh/uv/getting-started/installation/#standalone-installer):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Or install with Homebrew:

```bash
brew install uv
```

#### Windows

Use the [official standalone installer](https://docs.astral.sh/uv/getting-started/installation/#standalone-installer) from PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Or install with WinGet:

```powershell
winget install --id=astral-sh.uv -e
```

### Choose a connection

Start with the first mode that fits your setup.

| Order | Mode | Setup | Writes | Use when |
|---:|---|---|---|---|
| 1 | USB web | Connect the tablet and enable USB web | Upload to root | You want the shortest setup without a subscription or developer mode |
| 2 | Local directory | Install and sign in to the desktop app | No | You want offline, device-free reads from the desktop cache |
| 3 | Cloud | Register once; Connect subscription required | Full document and folder management | You need wireless or remote access |
| 4 | SSH | Enable developer mode and configure SSH | Full management and native authoring | You need direct filesystem access or native ink writes |

### USB Web Interface

Connect via USB and enable the web interface in your tablet's Storage Settings.

[![Install USB Web Mode in VS Code](https://img.shields.io/badge/VS_Code-Install_USB_Web_Mode-0098FF?style=for-the-badge&logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect/mcp/install?name=remarkable&inputs=%5B%7B%22type%22%3A%22promptString%22%2C%22id%22%3A%22google_vision_api_key%22%2C%22description%22%3A%22Google%20Vision%20API%20Key%20(for%20handwriting%20OCR)%22%2C%22password%22%3Atrue%7D%5D&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22remarkable-mcp%22%2C%22--usb%22%5D%2C%22env%22%3A%7B%22GOOGLE_VISION_API_KEY%22%3A%22%24%7Binput%3Agoogle_vision_api_key%7D%22%7D%7D)
[![Install USB Web Mode in VS Code Insiders](https://img.shields.io/badge/VS_Code_Insiders-Install_USB_Web_Mode-24bfa5?style=for-the-badge&logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect/mcp/install?name=remarkable&inputs=%5B%7B%22type%22%3A%22promptString%22%2C%22id%22%3A%22google_vision_api_key%22%2C%22description%22%3A%22Google%20Vision%20API%20Key%20(for%20handwriting%20OCR)%22%2C%22password%22%3Atrue%7D%5D&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22remarkable-mcp%22%2C%22--usb%22%5D%2C%22env%22%3A%7B%22GOOGLE_VISION_API_KEY%22%3A%22%24%7Binput%3Agoogle_vision_api_key%7D%22%7D%7D&quality=insiders)

1. Connect your reMarkable via USB
2. On your tablet, open **Settings > Storage** and enable **USB web interface**
3. Install via the button above

<details>
<summary>Manual USB web configuration</summary>

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

- Make sure your reMarkable is connected via USB and unlocked
- Verify USB web interface is enabled in Settings > Storage
- The tablet should be accessible at `http://10.11.99.1`

</details>

---

### Local Directory Mode

Read the official reMarkable desktop app's sync folder from disk. This mode is
offline and read-only. The desktop app controls synchronization.

<details>
<summary>Local directory configuration</summary>

Add to `.vscode/mcp.json`:

```json
{
  "servers": {
    "remarkable": {
      "command": "uvx",
      "args": ["remarkable-mcp", "--local-dir"]
    }
  }
}
```

With no path, the desktop app's data folder is auto-detected:

- **macOS (current app):** `~/Library/Containers/com.remarkable.desktop/Data/Library/Application Support/remarkable/desktop`
- **macOS (legacy app):** `~/Library/Application Support/remarkable/desktop`
- **Windows:** `%APPDATA%\remarkable\desktop`
- **Linux:** `~/.local/share/remarkable/desktop`

Or point at any xochitl-style directory (e.g. a tablet backup):

```json
{
  "servers": {
    "remarkable": {
      "command": "uvx",
      "args": ["remarkable-mcp", "--local-dir", "/path/to/xochitl-data"]
    }
  }
}
```

- Content freshness depends on the desktop app. Keep it running and signed in.
- This mode is read-only. Direct writes could bypass synchronization and corrupt the app cache.
- If the directory can't be found and a cloud token is configured, the server falls back to cloud mode for read access (the server remains read-only because it was launched in local-directory mode)

</details>

---

### Cloud Mode

Cloud mode works without a device connection and requires a reMarkable Connect subscription.

It fetches metadata in parallel and caches content-addressed blobs on disk. See
[Cloud performance and caching](#cloud-performance--caching) for configuration.

<details>
<summary>Cloud mode setup</summary>

#### 1. Get a One-Time Code

Go to [my.remarkable.com/device/desktop/connect](https://my.remarkable.com/device/desktop/connect) and generate a code.

#### 2. Convert to Token

```bash
uvx remarkable-mcp --register YOUR_CODE
```

Registration saves the token to `~/.rmapi`. When your MCP client runs the server as the same user on the same machine, remarkable-mcp reads that file automatically, so you do not need to put a token in the client configuration. Set `REMARKABLE_TOKEN` only when `~/.rmapi` is unavailable, such as when the server runs under another user or on another machine.

#### 3. Configure your MCP client

##### Minimal client-neutral server definition

Add this server definition using the MCP configuration UI or wrapper required by your client:

```json
{
  "command": "uvx",
  "args": ["remarkable-mcp"]
}
```

No `env` entry is needed when the server can read the token from `~/.rmapi`.

##### VS Code

VS Code's `.vscode/mcp.json` uses a top-level `servers` object. The saved
`~/.rmapi` credential means no token input is required:

```json
{
  "servers": {
    "remarkable": {
      "command": "uvx",
      "args": ["remarkable-mcp"]
    }
  }
}
```

##### Claude Desktop and other clients

Claude Desktop and many other MCP clients use a top-level `mcpServers` object and literal values in `env`; they do not use VS Code's `inputs` array. With the token available in `~/.rmapi`, the minimal configuration is:

```json
{
  "mcpServers": {
    "remarkable": {
      "command": "uvx",
      "args": ["remarkable-mcp"]
    }
  }
}
```

If the token file is unavailable, add `"env": {"REMARKABLE_TOKEN": "your-token"}` to the `remarkable` server entry. Consult your client's documentation if it uses a different outer key or secret-storage mechanism.

</details>

---

### SSH Mode

SSH provides direct filesystem access and native notebook authoring. It requires
[developer mode](docs/ssh-setup.md), which factory-resets the tablet when enabled.
Use this mode only when USB web, local directory, or cloud mode does not provide the
operation you need.

```json
{
  "servers": {
    "remarkable": {
      "command": "uvx",
      "args": ["remarkable-mcp", "--ssh"]
    }
  }
}
```

See the [SSH setup guide](docs/ssh-setup.md) for authentication and device setup.

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

All modes support reading and rendering. Cloud and SSH support full library
management. USB web supports upload to the root folder. Local directory mode is
read-only.

| Mode | Setup | Subscription | Offline | Tablet required | Raw source | Upload | Folder operations¹ |
|---|---|---|---|---|---|---|---|
| Local directory | Desktop app installed | No² | Yes | No | PDF and EPUB | No | No |
| USB web | Enable in Settings | No | Yes | Yes | PDF | Root only | No |
| Cloud | One-time registration | Connect | No | No | PDF and EPUB | Yes | Yes |
| SSH | Developer mode | No | Yes | Yes | PDF and EPUB | Yes | Yes |

¹ Folder ops = create folder / move / rename / delete. Upload and folder ops are enabled by default; pass `--read-only` to expose a read-only server. Deletes move items to the trash and prompt for confirmation when your client supports elicitation, and are refused without it unless `REMARKABLE_SKIP_CONFIRM=1` is set.

² The desktop app itself signs in to the reMarkable cloud to sync; local directory mode just reads whatever the app has already synced to disk.

### Automatic cloud fallback

If a selected device transport is unavailable and a cloud credential is configured,
the server falls back to cloud mode. A server launched with `--local-dir` remains
read-only after fallback. `remarkable_status` reports the effective transport and
sets `fell_back_to_cloud`.

Pass `--no-cloud-fallback` (or set `REMARKABLE_DISABLE_CLOUD_FALLBACK=1`) to disable this and fail instead when the device is unreachable.

The transport is resolved once per server process. Restart the MCP server after
connecting a device or starting the desktop app if the process already fell back to
cloud.

Detailed setup:

- [USB web setup](docs/usb-web-setup.md)
- [SSH setup](docs/ssh-setup.md)
- [Cloud performance and caching](#cloud-performance--caching)

---

## OpenWebUI (Streamable HTTP)

OpenWebUI's native MCP connection uses Streamable HTTP. Start remarkable-mcp on
the same machine:

```bash
uvx remarkable-mcp --http
```

Then add `http://127.0.0.1:8000/mcp` as the MCP server URL in OpenWebUI. The
HTTP transport combines with `--usb`, `--ssh`, and `--read-only`; for example:

```bash
uvx remarkable-mcp --usb --http
```

The bind address and port can also be set with `--host` / `--port` or
`REMARKABLE_MCP_HOST` / `REMARKABLE_MCP_PORT`.

The server uses MCP Python SDK 2.x's dual-era endpoint. Modern
`2026-07-28` requests are sessionless; older clients continue to use the
initialize handshake and legacy MCP sessions on the same `/mcp` route. The
reMarkable token and selected tablet transport remain process-local CLI/env
configuration and are never inferred from MCP HTTP authorization headers.

> [!WARNING]
> Streamable HTTP has no built-in authentication. It binds to `127.0.0.1` by
> default. Keep that default unless you control the network boundary. Wildcard
> binds such as `0.0.0.0` and `::` are rejected because they cannot provide a
> strict Host allowlist. A concrete non-loopback address is allowed but exposes
> every enabled tool, including writes, to clients that can reach the port.

### Remote access through an authenticated reverse proxy

Keep remarkable-mcp on its default loopback bind and put the proxy on the same
host. MCPServer's DNS-rebinding protection only permits loopback `Host` and
`Origin` values by default. A proxy that forwards its public hostname unchanged
will therefore receive **421 Misdirected Request**; a public browser `Origin`
will receive **403 Forbidden**.

The proxy must authenticate requests, rewrite `Host` to the loopback upstream,
and clear `Origin`. For example, with nginx and an existing htpasswd file:

```nginx
location /mcp {
    auth_basic "reMarkable MCP";
    auth_basic_user_file /etc/nginx/remarkable-mcp.htpasswd;

    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_read_timeout 3600s;

    # Required by MCPServer's default DNS-rebinding protection.
    proxy_set_header Host 127.0.0.1:8000;
    proxy_set_header Origin "";
}
```

Do not expose the upstream port directly. Configure OpenWebUI with the
authenticated proxy URL, such as `https://mcp.example.com/mcp`.

For NixOS, `remarkable-mcp-bridge.nix` provides a systemd module with the same
loopback default:

```nix
{
  imports = [ ./remarkable-mcp-bridge.nix ];
  services.remarkable-mcp = {
    enable = true;
    command = "/opt/remarkable-mcp/.venv/bin/remarkable-mcp";
  };
}
```

The service user must have its reMarkable credentials under
`/var/lib/remarkable-mcp` (or the configured `home`).

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

These six tools are read-only and return structured JSON with next-step hints.
Supported transports also register write tools by default; pass `--read-only` to
disable them. See [Write Tools](#write-tools-by-transport). Clients that support
[MCP Apps](#interactive-canvas-app-mcp-apps) can also open `remarkable_canvas`.

[Full tools reference](docs/tools.md)

### Tool behavior

- Browsing a document path returns its content.
- Empty notebook text can trigger OCR automatically.
- Search can scan several matching documents.
- Image tools return visual content that text extraction misses.
- Sampling OCR uses the connected client's model when supported.
- Browse and search accept tag filters.

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

# Composite an imported PDF page with its reMarkable annotations
remarkable_image("Research Paper", page=3, render_merged=True)

# Transparent background for compositing
remarkable_image("Logo Sketch", background="#00000000")

# Compatibility mode: return resource URI instead of embedded resource
remarkable_image("Diagram", compatibility=True)
```

> **Note:** PNG rendering uses PyMuPDF for both notebook SVGs and PDF pages, so
> `remarkable_image` does not require Cairo, Inkscape, Chromium, or another
> system graphics runtime. If local stroke parsing cannot handle a page, USB
> and SSH modes fall back to the tablet's native PDF export and cloud mode falls
> back to an original source PDF when one exists.

---

## Resources

Documents are automatically registered as MCP resources:

| URI Scheme | Description |
|------------|-------------|
| `remarkable:///{path}.txt` | Extracted text content |
| `remarkableraw:///{path}.pdf.txt` | Extracted text from the original PDF |
| `remarkableraw:///{path}.epub.txt` | Extracted text from the original EPUB |
| `remarkableimg:///{path}.page-{N}.png` | PNG image of page N (notebooks only) |
| `remarkablesvg:///{path}.page-{N}.svg` | SVG vector image of page N (notebooks only) |

[Full resources reference](docs/resources.md)

---

## OCR for Handwriting

For handwritten content, remarkable-mcp offers several OCR backends. Choose based on your setup and requirements:

| Backend | Setup | Offline | Notes |
|---|---|---|---|
| Sampling | No API key | Depends on the client | Uses the connected client's model |
| Google Vision | API key | No | Good handwriting support |
| Tesseract | System install | Yes | Better suited to printed text |

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
<summary>Sampling OCR</summary>

Uses your MCP client's AI model for OCR. Works with clients that support MCP sampling (VS Code + Copilot, Claude Desktop, etc.).

On modern `2026-07-28` connections, the server returns a multi-round input
request and the client retries the tool with the sampled OCR result. On legacy
connections, the same resolver uses the established server-to-client sampling
request. This compatibility routing is handled by MCP SDK 2.x.

- No additional API keys needed
- Quality and data handling depend on your MCP client and model
- Only available with sampling-capable clients
- Falls back to Google Vision (if API key configured) or Tesseract if sampling unavailable

</details>

<details>
<summary>Google Cloud Vision</summary>

1. Enable [Cloud Vision API](https://console.cloud.google.com/apis/library/vision.googleapis.com)
2. Create an [API key](https://console.cloud.google.com/apis/credentials)
3. Add to config: `"GOOGLE_VISION_API_KEY": "your-key"`

**Cost:** 1,000 free requests/month, then ~$1.50 per 1,000.

[Google Vision setup guide](docs/google-vision-setup.md)

</details>

<details>
<summary>Tesseract</summary>

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

## Write Tools by Transport

Write tools let you upload, organize, and manage documents on your reMarkable. **Enabled by default on write-capable transports.** Cloud and SSH modes support the full set; USB web supports upload only (its firmware exposes no folder operations); local-directory mode is always read-only. Pass `--read-only` to expose a read-only server elsewhere.

| Feature | Local Directory | Cloud Mode | SSH Mode | USB Web Mode |
|---------|:---------------:|:----------:|:--------:|:------------:|
| Upload | ❌ | ✅ | ✅ | ✅ (to root) |
| Mkdir | ❌ | ✅ | ✅ | ❌ |
| Move | ❌ | ✅ | ✅ | ❌ |
| Rename | ❌ | ✅ | ✅ | ❌ |
| Delete | ❌ | ✅ (→ trash) | ✅ (→ trash; optional permanent) | ❌ |

### Disabling Write Tools (read-only mode)

Write tools are on by default in each write-capable mode. Local-directory mode is always read-only. To make another transport read-only, add the `--read-only` flag:

```json
{
  "servers": {
    "remarkable": {
      "command": "uvx",
      "args": ["remarkable-mcp", "--read-only"]
    }
  }
}
```

It combines with any transport flag (`--ssh`, `--usb`):
```json
{
  "servers": {
    "remarkable": {
      "command": "uvx",
      "args": ["remarkable-mcp", "--ssh", "--read-only"]
    }
  }
}
```

Or set the environment variable:
```json
{
  "env": {
    "REMARKABLE_READ_ONLY": "1"
  }
}
```

> The legacy `--write` flag and `REMARKABLE_ENABLE_WRITE` variable are still accepted for backward compatibility but are now no-ops (write is the default). `--write` and `--read-only` are mutually exclusive.

### Available Write Tools

| Tool | Description |
|------|-------------|
| `remarkable_upload(file_path, parent_folder, document_name, defer_restart)` | Upload a PDF or EPUB file (cloud, SSH, and USB web; USB web uses the requested name but uploads to root) |
| `remarkable_markdown_to_pdf(markdown, document_name, parent_folder, defer_restart)` | Render Markdown as a paginated PDF and upload it (cloud, SSH, and USB web) |
| `remarkable_mkdir(folder_name, parent, defer_restart)` | Create a new folder (cloud and SSH) |
| `remarkable_move(document, dest_folder, defer_restart)` | Move a document or folder (cloud and SSH) |
| `remarkable_rename(document, new_name, defer_restart)` | Rename a document or folder (cloud and SSH) |
| `remarkable_delete(document, defer_restart, permanent)` | Move a document/folder to Trash; SSH can permanently remove it with `permanent=True` |
| `remarkable_refresh()` | Restart `xochitl` once to apply writes made with `defer_restart=True` — **SSH only** |
| `remarkable_author(method, ..., defer_restart)` | Author native ink and notebooks — `draw` (append strokes), `add_page` (append a blank notebook page), `create_document` (new notebook) — **SSH only** |

### Safety

- **Upload registers in cloud, SSH, and USB web mode** — local-directory mode never exposes write tools.
- **mkdir, move, rename, delete register in cloud and SSH modes only** — they are not exposed on USB web (the tablet's USB web firmware has no folder/move/rename/delete endpoints), keeping the tool list scoped to what the active transport actually supports.
- **Delete prompts for confirmation when possible** — if the client supports MCP elicitation, `remarkable_delete` asks the user to confirm before deleting. MCP SDK 2.x carries that prompt as a multi-round input request for `2026-07-28` clients and as push elicitation for legacy clients. If the client can't show a prompt, the delete is **refused** (not performed) unless `REMARKABLE_SKIP_CONFIRM=1` is set — so write-on-by-default can't silently delete from clients that lack elicitation. Cloud and SSH move items to Trash by default. SSH also supports `permanent=True` for an explicitly requested permanent removal; cloud permanent deletion remains a Trash-management operation in the app/tablet. Set `REMARKABLE_SKIP_CONFIRM=1` to allow deletes without a prompt in automated setups. All write tools carry `ToolAnnotations(read_only_hint=False)` (and `destructive_hint=True` for delete) so an agent harness can gate writes at the MCP layer.
- After each write operation in SSH mode, the tablet UI (`xochitl`) restarts automatically to reflect changes; the call waits for it to come back before returning so the next write doesn't race a restarting daemon. For bulk operations, pass `defer_restart=True` to every write, including `remarkable_author` — or set `REMARKABLE_DEFER_RESTART=1` — and call `remarkable_refresh()` once at the end. Responses set `refresh_pending=true` until that refresh runs.

### Examples

```python
# Upload a PDF
remarkable_upload("paper.pdf", parent_folder="/Research")

# Render Markdown and upload the PDF without creating a local file
remarkable_markdown_to_pdf(
    "# Meeting Notes\n\n- Follow up with Sam",
    document_name="Meeting Notes",
    parent_folder="/Research",
)

# Create a folder
remarkable_mkdir("2024 Archive", parent="/Archive")

# Move a document
remarkable_move("Meeting Notes", "/Archive/2024 Archive")

# Rename a document
remarkable_rename("Untitled", "Q4 Planning Notes")

# Delete (destructive; confirms via elicitation when supported)
remarkable_delete("Old Draft")

# Permanently delete in SSH mode only
remarkable_delete("Disposable Test", permanent=True)

# Bulk import (SSH): defer the restart on each write, then refresh once.
# One xochitl restart for the whole batch instead of one per upload.
remarkable_upload("a.pdf", parent_folder="/Research", defer_restart=True)
remarkable_upload("b.pdf", parent_folder="/Research", defer_restart=True)
remarkable_refresh()

# Author native ink and notebooks (SSH only)
# Append pen/highlighter strokes to a page (coordinates normalized [0,1] from
# the page's top-left). The interactive canvas Save button calls this too.
remarkable_author(
    method="draw", document="Ideas", page=1,
    strokes=[{"points": [[0.1, 0.2], [0.8, 0.2]], "tool": "highlighter", "color": "yellow"}],
)

# Append a blank, drawable page to the end of a notebook
remarkable_author(method="add_page", document="Ideas")

# Create a new blank notebook
remarkable_author(method="create_document", name="Sketches")

# Only seed typed text when the user explicitly requested it.
remarkable_author(method="create_document", name="Meeting notes", text="Agenda\nFollow-ups")
```

---

## Interactive Canvas App (MCP Apps)

An interactive page viewer built on the [MCP Apps](https://github.com/modelcontextprotocol/ext-apps) extension (SEP-1865). Clients that support MCP Apps (such as ChatGPT, Claude, VS Code, and the MCP Inspector) render a canvas in a side panel where you can view a document page and navigate through it.

There is **no flag to enable it** — the `remarkable_canvas` tool and its `ui://remarkable/canvas` resource are always registered, and the capability is negotiated automatically in client capabilities (modern discovery or a legacy initialize handshake). App-capable clients open the interactive canvas; every other client simply receives the rendered page as an image, so the tool is safe and useful everywhere.

This registers one tool:

| Tool | Description |
|------|-------------|
| `remarkable_canvas(document, page)` | Open a page in the interactive canvas viewer |

How it behaves:

- **App-capable clients** open the canvas (declared at `ui://remarkable/canvas`, MIME `text/html;profile=mcp-app`) and can page through the document via the MCP Apps postMessage bridge — the server delivers each rendered page in the wire result's `structuredContent` (`structured_content` in Python).
- **Other clients** still get the rendered page back as an embedded PNG image, so the tool is useful everywhere; it just won't open the interactive panel. The `_meta.ui` / `ui://` metadata is inert to clients that don't advertise the MCP Apps UI extension.

### Drawing and authoring from the canvas

When write mode is on (the default) **and** the active transport is SSH, the canvas becomes a write surface:

- **Draw**: choose a pen or highlighter, draw, then save native `.rm` ink to the device.
- **Add page**: queue a blank native notebook page, draw on it, then save.
- One source of truth: the canvas calls the **same** `remarkable_author` tool a model would call (`method="draw"` on Save, `method="add_page"` for ＋Page), so the human path and the model path produce byte-identical results.

The Save / Draw / ＋Page controls are hidden when the page isn't writable (read-only mode, or a non-SSH transport), and the canvas falls back to a plain image viewer. The iframe bridge follows the MCP Apps spec but is best validated against your specific client.

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
- `#RRGGBB`: RGB hex, such as `#FFFFFF`
- `#RRGGBBAA`: RGBA hex, such as `#00000000`

Default is `#FBFBFB` (reMarkable paper color). This affects both the `remarkable_image` tool and image resources.

---

### Retry Configuration

Cloud API requests automatically retry on transient failures (HTTP 429, 500, 502, 503, 504) and network errors with exponential backoff and jitter. You can tune this via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `REMARKABLE_RETRY_ATTEMPTS` | `3` | Maximum number of request attempts (minimum 1) |
| `REMARKABLE_RETRY_DELAY` | `2.0` | Base delay in seconds for exponential backoff |

Rate-limit retries honor numeric and HTTP-date `Retry-After` values, capped at
20 seconds. Authentication failures trigger token renewal instead of retry.

---

### Cloud Performance & Caching

Cloud mode is built to make a device-free workflow fast:

- **Parallel traversal**: fetch document metadata concurrently.
- **Connection pooling**: reuse HTTP connections.
- **Content-addressed blob cache**: reuse immutable blobs by hash and fetch new hashes when documents change.

You normally don't need to configure any of this, but these environment variables let you tune it:

| Variable | Default | Description |
|----------|---------|-------------|
| `REMARKABLE_SYNC_WORKERS` | `16` | Parallel workers for cloud fetches (clamped to `64`). |
| `REMARKABLE_DISABLE_CACHE` | unset | Set to `1` to disable the on-disk blob cache entirely. |
| `REMARKABLE_CACHE_DIR` | `~/.remarkable/cache/blobs` | Where cached blobs are stored. |
| `REMARKABLE_CACHE_MAX_BLOB` | `4194304` (4 MiB) | Blobs larger than this are streamed through but not cached. |

The cache is purely a local accelerator: deleting `REMARKABLE_CACHE_DIR` only forces the next read to re-download. The mutable cloud root hash is always fetched fresh, so you never see a stale library.

---

## Common Workflows

- Read recent notes and extract action items.
- Search names, tags, typed text, and OCR output.
- Review annotated PDFs with the source page and annotations together.
- Move selected notes into another local workflow.
- Upload reference PDFs or generated Markdown documents.

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
uv run pytest -v
```

[Development guide](docs/development.md)

### Multi-transport smoke test

When something looks broken, run the deterministic, no-AI smoke test first. It
drives the real server over MCP and exercises every available tool in every
reachable transport in this order: local directory, cloud, USB web, then SSH.

```bash
uv run python smoke/run_smoke.py            # all available modes
uv run python smoke/run_smoke.py --read-only # connectivity + reads only
```

[Smoke test guide](smoke/README.md)
and per-mode expectations.

---

## License

MIT

---

Built with [rmscene](https://github.com/ricklupton/rmscene),
[PyMuPDF](https://pymupdf.readthedocs.io/),
[markdown-it-py](https://github.com/executablebooks/markdown-it-py), and
inspiration from [ddvk/rmapi](https://github.com/ddvk/rmapi).
