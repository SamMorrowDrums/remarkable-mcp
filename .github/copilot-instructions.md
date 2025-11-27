# Copilot Instructions for remarkable-mcp

## Project Overview

This is an MCP (Model Context Protocol) server that provides access to reMarkable tablet data. It's a Python project using FastMCP.

## Git Workflow

**Always work on feature branches and submit PRs. Never push directly to main.**

```bash
# Create a feature branch
git checkout -b feature/my-feature

# After making changes, push and create PR
git push origin feature/my-feature
# Then create PR via GitHub
```

Branch protection is enabled on `main` - all changes must go through pull requests with passing CI checks.

## Package Management

**Always use `uv` for all package management operations.**

```bash
# Install dependencies
uv sync

# Install with dev dependencies
uv sync --extra dev

# Add a new dependency
uv add <package>

# Add a dev dependency
uv add --dev <package>
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

# 2. Check formatting (REQUIRED - CI will fail without this)
uv run ruff format --check .

# 3. Run tests (REQUIRED - CI will fail without this)
uv run pytest test_server.py -v
```

**Fix issues automatically:**
```bash
# Fix lint issues
uv run ruff check . --fix

# Fix formatting
uv run ruff format .
```

**All three checks must pass before any commit or PR. CI runs on all PRs and must pass before merging.**

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
- `pytesseract` - OCR for handwritten content
- `rmc` - reMarkable file conversion utilities

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

Releases are automated via GitHub Actions. The version is derived from the git tag.

**Steps:**
1. Ensure all changes are merged to `main`
2. Ensure README.md is current
3. Ensure CI is passing on `main`
4. Create and push a version tag:
   ```bash
   # Get the current version from pyproject.toml or latest tag
   git tag -l 'v*' | sort -V | tail -1
   
   # Create next version tag (e.g., v0.2.0)
   git tag v0.2.0
   git push origin v0.2.0
   ```
5. The workflow automatically:
   - Creates a GitHub release with generated notes
   - Builds the package with the tag version
   - Publishes to PyPI
   - Publishes to MCP Registry

**Note:** The version in `pyproject.toml` is updated automatically during the build from the git tag. You don't need to manually update it.
