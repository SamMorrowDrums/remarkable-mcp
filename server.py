#!/usr/bin/env python3
"""
reMarkable MCP Server

An MCP server that provides access to reMarkable tablet data through the reMarkable Cloud API.
Uses rmapy for cloud sync and rmscene for native text extraction.

Usage:
    # As MCP server (default)
    python server.py

    # Convert one-time code to token (run once)
    python server.py --register <one-time-code>

This is the entry point script. The actual implementation is in the remarkable_mcp package.
"""

import argparse
import json
import sys

from remarkable_mcp.api import register_and_get_token
from remarkable_mcp.server import mcp, run


def main():
    """Main entry point - handle CLI args or run MCP server."""
    parser = argparse.ArgumentParser(
        description="reMarkable MCP Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Register and get token (run once)
  uv run python server.py --register abcd1234

  # Run as MCP server
  uv run python server.py

  # Run with token from environment
  REMARKABLE_TOKEN="your-token" uv run python server.py
""",
    )
    parser.add_argument(
        "--register",
        metavar="CODE",
        help="Register with reMarkable using a one-time code and print the token",
    )

    args = parser.parse_args()

    if args.register:
        # Registration mode - convert one-time code to token
        try:
            print(f"Registering with reMarkable using code: {args.register}")
            token = register_and_get_token(args.register)
            print("\n✅ Successfully registered!\n")
            print("Your token (add to mcp.json env):")
            print("-" * 50)
            print(token)
            print("-" * 50)
            print("\nAdd to your .vscode/mcp.json:")
            print(
                json.dumps(
                    {
                        "servers": {
                            "remarkable": {
                                "command": "uvx",
                                "args": ["remarkable-mcp"],
                                "env": {"REMARKABLE_TOKEN": token},
                            }
                        }
                    },
                    indent=2,
                )
            )
        except Exception as e:
            print(f"❌ Registration failed: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        # MCP server mode
        run()


# Keep the mcp object accessible for testing
__all__ = ["mcp", "main"]

if __name__ == "__main__":
    main()
