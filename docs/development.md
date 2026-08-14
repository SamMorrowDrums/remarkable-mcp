# Development Guide

This guide covers setting up a development environment for contributing to remarkable-mcp.

## Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) package manager
- A reMarkable tablet (for testing)

## Setup

```bash
# Clone the repository
git clone https://github.com/SamMorrowDrums/remarkable-mcp.git
cd remarkable-mcp

# Install dependencies (including dev extras)
uv sync --all-extras

# Verify setup
uv run pytest -v
```

## Project Structure

```
remarkable-mcp/
├── server.py              # Entry point (backwards compatible)
├── remarkable_mcp/
│   ├── server.py           # MCPServer and transports
│   ├── tools.py            # Read and render tools
│   ├── write_tools.py      # Upload, management, and authoring tools
│   ├── api.py              # Transport selection and cloud fallback
│   ├── sync.py             # Cloud sync client
│   ├── ssh.py              # SSH client
│   ├── usb_web.py          # USB web client
│   ├── local_dir.py        # Desktop cache client
│   ├── extract.py          # Text and image extraction
│   ├── markdown_pdf.py     # Markdown-to-PDF rendering
│   └── app_canvas.py       # MCP Apps canvas
├── test_server.py
├── test_mcp_v2.py          # Modern/legacy protocol compatibility tests
├── test_page_mapping.py
├── test_local_dir.py
├── test_integration.py
├── smoke/
├── docs/
├── server.json
└── pyproject.toml
```

## Running Tests

```bash
# Run all tests
uv run pytest -v

# Run specific test class
uv run pytest -v -k "TestClassName"

# Run with coverage
uv run pytest -v --cov=remarkable_mcp
```

Tests use `pytest-asyncio` for async testing. All async tests use the `@pytest.mark.asyncio` decorator.

## Code Quality

Before committing, always run:

```bash
# Lint (required - CI will fail without this)
uv run ruff check .

# Format (required - CI will fail without this)
uv run ruff format --check .

# Fix issues automatically
uv run ruff check . --fix
uv run ruff format .
```

## Git Workflow

**Always work on feature branches and submit PRs. Never push directly to main.**

```bash
# Create a feature branch
git checkout -b feature/my-feature

# After making changes
git add -A
git commit -m "feat: description of change"
git push origin feature/my-feature
# Then create PR via GitHub
```

Branch protection is enabled on `main` - all changes must go through pull requests with passing CI.

## Adding a New Tool

1. Add the tool function in `remarkable_mcp/tools.py` with proper docstring and annotations
2. Create unique `ToolAnnotations` with a descriptive title
3. Add tests in `test_server.py`
4. Update the tools table in README.md
5. Update `docs/tools.md` with detailed documentation
6. Run tests: `uv run pytest -v`

### Tool Design Principles

- **Intent-based design**: tools should map to user intents, not API endpoints
- **XML-structured docstrings**: use `<usecase>`, `<instructions>`, `<parameters>`, and `<examples>`
- **Response hints**: include an actionable `_hint`
- **Educational errors**: explain the failure and a safe next step
- **Minimal tool count**: prefer fewer, more capable tools

Example tool structure:

```python
EXAMPLE_ANNOTATIONS = ToolAnnotations(
    title="Descriptive Tool Name",  # Shown in VS Code
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
)

@mcp.tool(annotations=EXAMPLE_ANNOTATIONS)
def remarkable_example(param: str) -> str:
    """
    <usecase>Brief description of when to use this tool.</usecase>
    <instructions>
    Detailed instructions for the AI model on how to use this tool effectively.
    </instructions>
    <parameters>
    - param: Description of the parameter
    </parameters>
    <examples>
    - remarkable_example("value")
    </examples>
    """
```

## Making a Release

Releases are automated via GitHub Actions. The version is derived from the git tag.

1. Ensure all changes are merged to `main`
2. Ensure README.md and docs are current
3. Ensure CI is passing on `main`
4. Create and push a version tag:

```bash
# Check current version
git tag -l 'v*' | sort -V | tail -1

# Create next version tag
git tag v0.X.0
git push origin v0.X.0
```

The workflow automatically:
- Creates a GitHub release with generated notes
- Builds the package with the tag version
- Publishes to PyPI
- Publishes to MCP Registry

## Key Dependencies

| Package | Purpose |
|---------|---------|
| `mcp>=2,<3` | Stable Model Context Protocol Python SDK 2.x |
| `requests` | HTTP client for reMarkable Cloud API |
| system `ssh` and `scp` | Direct tablet access; `sshpass` is optional for password auth |
| `rmscene` | Native .rm file parser for text extraction |
| `pymupdf` | PDF text extraction, Cairo-free SVG/PDF rendering, and Markdown PDF generation |
| `markdown-it-py` | Safe Markdown-to-HTML parsing for PDF writeback |
| `ebooklib` | EPUB text extraction |
| `pytesseract` | OCR fallback |
| `google-cloud-vision` | OCR (recommended) |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `REMARKABLE_TOKEN` | Cloud API authentication token |
| `REMARKABLE_LOCAL_DIR` | Desktop cache or xochitl-style directory |
| `REMARKABLE_USB_HOST` | USB web base URL |
| `REMARKABLE_SSH_HOST` | SSH hostname (default: `10.11.99.1`) |
| `REMARKABLE_SSH_USER` | SSH username (default: `root`) |
| `REMARKABLE_SSH_PORT` | SSH port (default: `22`) |
| `REMARKABLE_SSH_KEY` | Explicit private key path |
| `REMARKABLE_READ_ONLY` | Disable write tools |
| `REMARKABLE_ROOT_PATH` | Scope document access to one folder |
| `REMARKABLE_MCP_HOST` | Streamable HTTP bind address |
| `REMARKABLE_MCP_PORT` | Streamable HTTP port |
| `GOOGLE_VISION_API_KEY` | Google Vision API key for OCR |
| `REMARKABLE_OCR_BACKEND` | OCR backend: `auto`, `sampling`, `google`, `tesseract` |
