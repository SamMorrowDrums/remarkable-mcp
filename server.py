#!/usr/bin/env python3
"""
reMarkable MCP Server

An MCP server that provides access to reMarkable tablet data through the reMarkable Cloud API.
Uses rmscene for native text extraction.

Usage:
    # As MCP server (default)
    python server.py

    # Convert one-time code to token (run once)
    python server.py --register <one-time-code>

This is a backwards-compatible entry point. The actual CLI is in remarkable_mcp/cli.py.
"""

from remarkable_mcp.cli import main

if __name__ == "__main__":
    main()
