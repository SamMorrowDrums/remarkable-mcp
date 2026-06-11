#!/usr/bin/env python3
"""
CLI entry point for reMarkable MCP Server.

Usage:
    # As MCP server (default, uses cloud API)
    remarkable-mcp

    # Use SSH transport (direct connection via USB)
    remarkable-mcp --ssh

    # Convert one-time code to token (run once)
    remarkable-mcp --register <one-time-code>
"""

import argparse
import json
import os
import sys


def main():
    """Main entry point - handle CLI args or run MCP server."""
    parser = argparse.ArgumentParser(
        description="reMarkable MCP Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Register and get token (run once)
  uvx remarkable-mcp --register abcd1234

  # Run as MCP server (cloud API)
  uvx remarkable-mcp

  # Run with token from environment
  REMARKABLE_TOKEN="your-token" uvx remarkable-mcp

  # Run with USB web interface
  uvx remarkable-mcp --usb

  # Run with SSH transport (direct USB connection, requires dev mode)
  uvx remarkable-mcp --ssh

  # SSH with custom host (e.g., using SSH config)
  REMARKABLE_SSH_HOST="remarkable" uvx remarkable-mcp --ssh

  # SSH pinning an explicit key (ignores ssh-agent, e.g. 1Password)
  uvx remarkable-mcp --ssh --ssh-key ~/.ssh/id_ed25519

USB Web Interface Environment Variables:
  REMARKABLE_USB_HOST      USB web interface host (default: http://10.11.99.1)
  REMARKABLE_USB_TIMEOUT   Request timeout in seconds (default: 10)

SSH Environment Variables:
  REMARKABLE_SSH_HOST      SSH host (default: 10.11.99.1 for USB)
  REMARKABLE_SSH_USER      SSH user (default: root)
  REMARKABLE_SSH_PORT      SSH port (default: 22)
  REMARKABLE_SSH_PASSWORD  SSH password (optional, requires sshpass)
  REMARKABLE_SSH_KEY       Private key path for key auth (optional). Pins this
                           on-disk identity and ignores any ssh-agent, avoiding
                           hangs with interactive agents like 1Password.

Security Note:
  For better security, set up SSH key authentication instead of using
  a password. See: https://github.com/SamMorrowDrums/remarkable-mcp/blob/main/docs/ssh-setup.md
""",
    )
    parser.add_argument(
        "--register",
        metavar="CODE",
        help="Register with reMarkable using a one-time code and print the token",
    )
    parser.add_argument(
        "--ssh",
        action="store_true",
        help="Use SSH transport instead of cloud API (requires developer mode)",
    )
    parser.add_argument(
        "--ssh-key",
        metavar="PATH",
        help=(
            "Path to a private key for SSH key auth (sets REMARKABLE_SSH_KEY). "
            "Pins this on-disk identity and ignores any ssh-agent, avoiding hangs "
            "with interactive agents like 1Password in a headless server."
        ),
    )
    parser.add_argument(
        "--usb",
        action="store_true",
        help="Use USB web interface (connect via USB cable, enable in Storage Settings)",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help=(
            "Enable write tools (upload, mkdir, move, rename, delete). "
            "Cloud and SSH support all of them; USB web supports upload only."
        ),
    )
    parser.add_argument(
        "--no-cloud-fallback",
        action="store_true",
        help=(
            "Disable the automatic cloud fallback. By default, if --usb/--ssh is "
            "selected but the tablet is unreachable at startup and a cloud token "
            "is configured, the server falls back to cloud mode so the same "
            "configuration works with or without the device connected."
        ),
    )
    args = parser.parse_args()

    if args.register:
        # Registration mode - convert one-time code to token
        # Only import what's needed for registration
        from remarkable_mcp.api import register_and_get_token

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
    elif args.usb:
        # USB web mode - set environment variable and run server
        os.environ["REMARKABLE_USE_USB_WEB"] = "1"
        if args.write:
            os.environ["REMARKABLE_ENABLE_WRITE"] = "1"
        if args.no_cloud_fallback:
            os.environ["REMARKABLE_DISABLE_CLOUD_FALLBACK"] = "1"
        from remarkable_mcp.server import run

        run()
    elif args.ssh:
        # SSH mode - set environment variable and run server
        os.environ["REMARKABLE_USE_SSH"] = "1"
        if args.ssh_key:
            os.environ["REMARKABLE_SSH_KEY"] = args.ssh_key
        if args.write:
            os.environ["REMARKABLE_ENABLE_WRITE"] = "1"
        if args.no_cloud_fallback:
            os.environ["REMARKABLE_DISABLE_CLOUD_FALLBACK"] = "1"
        from remarkable_mcp.server import run

        run()
    else:
        # Cloud mode (default) - now write-capable via the sync protocol
        if args.write:
            os.environ["REMARKABLE_ENABLE_WRITE"] = "1"
        from remarkable_mcp.server import run

        run()


if __name__ == "__main__":
    main()
