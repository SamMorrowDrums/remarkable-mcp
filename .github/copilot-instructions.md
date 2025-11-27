# Copilot Instructions for remarkable-mcp

## Project Overview

This is an MCP (Model Context Protocol) server that provides access to reMarkable tablet data. It's a Python project using FastMCP.

## Package Management

**Always use `uv` for all package management operations.**

```bash
# Install dependencies
uv sync

# Install with optional OCR support
uv sync --extra ocr

# Install with dev dependencies
uv sync --extra dev

# Add a new dependency
uv add <package>

# Add a dev dependency
uv add --dev <package>

# Add an optional dependency to a group
uv add --optional ocr <package>
```

**Never use:**
- `pip install` directly
- `pip freeze`
- `poetry`

## Running Tests

**Always run tests with:**

```bash
uv run pytest test_server.py -v
```

For specific test patterns:
```bash
# Run specific test class
uv run pytest test_server.py -v -k "TestClassName"

# Run with coverage
uv run pytest test_server.py -v --cov=server

# Skip slow tests if any
uv run pytest test_server.py -v -m "not slow"
```

**Important:** Tests use `pytest-asyncio` for async testing. All async tests should use the `@pytest.mark.asyncio` decorator.

## Building & Running

```bash
# Run the MCP server directly
uv run python server.py

# Register a new device (one-time setup)
uv run python server.py --register <one-time-code>

# Run as installed package
uv run remarkable-mcp
```

## Code Quality

**Before committing, always run:**

```bash
# 1. Lint code (REQUIRED - CI will fail without this)
uv run ruff check .

# 2. Format code
uv run ruff format .

# 3. Run tests
uv run pytest test_server.py -v
```

**Fix issues automatically:**
```bash
# Fix lint issues
uv run ruff check . --fix

# Fix formatting
uv run ruff format .
```

**All three checks must pass before any commit or PR.**

## Project Structure

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
├── pyproject.toml         # Project config and dependencies
├── README.md              # User documentation (KEEP UPDATED)
├── RESEARCH_NOTES.md      # Design decisions (do not commit)
└── .github/
    ├── copilot-instructions.md  # This file
    └── workflows/
        └── publish.yml    # PyPI + MCP Registry publishing
```

## Documentation Requirements

**README.md must always be kept in sync with the code:**

1. **Available Tools** - Update the tools table when adding/removing/renaming tools
2. **Example Usage** - Ensure examples work with current API
3. **Features** - Update feature list when capabilities change
4. **Installation** - Keep installation instructions accurate
5. **Dependencies** - Note any new required system dependencies (e.g., Tesseract for OCR)

When modifying `server.py`:
- If you add a new tool → Update README tools table and examples
- If you change tool parameters → Update README examples
- If you add new dependencies → Update README installation section

## MCP Tool Design Principles

When creating or modifying tools, follow these principles:

1. **Intent-based design** - Tools should map to user intents, not API endpoints
2. **XML-structured docstrings** - Use `<usecase>`, `<instructions>`, `<parameters>`, `<examples>` tags
3. **Response hints** - Always include `_hint` field suggesting next actions
4. **Educational errors** - Errors should explain what went wrong and how to fix it
5. **Minimal tool count** - Prefer fewer, more capable tools over many simple ones

Example tool structure:
```python
@mcp.tool()
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

## Key Dependencies

- `mcp` - Model Context Protocol SDK
- `rmapy` - reMarkable Cloud API client
- `rmscene` - Native .rm file parser for text extraction (v3+ format)
- `pytesseract` (optional) - OCR for handwritten content
- `rmc` (optional) - reMarkable file conversion utilities

## Environment Variables

- `REMARKABLE_TOKEN` - Authentication token for reMarkable Cloud

## Testing Patterns

```python
# Async test example
@pytest.mark.asyncio
async def test_something():
    result = await mcp.call_tool("tool_name", {"param": "value"})
    data = json.loads(result[0][0].text)
    assert "expected_key" in data

# Mocking the API client
@patch('remarkable_mcp.tools.get_rmapi')
async def test_with_mock(mock_get_rmapi):
    mock_client = Mock()
    mock_get_rmapi.return_value = mock_client
    # ... test code
```

## Common Tasks

### Adding a New Tool

1. Add the tool function in `remarkable_mcp/tools.py` with proper docstring and annotations
2. Add tests in `test_server.py`
3. Update README.md tools table
4. Update README.md examples if relevant
5. Run tests: `uv run pytest test_server.py -v`

### Updating Dependencies

1. Edit `pyproject.toml` or use `uv add`
2. Run `uv sync` to update lock file
3. Update README.md if it affects installation
4. Test that everything still works

### Making a Release

1. Update version in `pyproject.toml`
2. Ensure README.md is current
3. Run full test suite: `uv run pytest test_server.py -v`
4. Run linting: `uv run ruff check .`
5. Run formatting check: `uv run ruff format --check .`
6. Commit all changes
7. Create a GitHub release (triggers PyPI publish)
