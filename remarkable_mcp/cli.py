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
import ipaddress
import json
import os
import sys


def _is_loopback_host(host: str) -> bool:
    """Return whether an HTTP bind host is limited to this machine."""
    normalized = host.strip().strip("[]")
    if normalized.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _warn_for_http_binding(host: str) -> None:
    """Warn prominently when unauthenticated HTTP is exposed off-host."""
    if not _is_loopback_host(host):
        print(
            "WARNING: remarkable-mcp Streamable HTTP has no authentication and "
            f"is binding to non-loopback address {host!r}. Any network client "
            "that can reach this port may invoke enabled tools, including writes. "
            f"Only Host and Origin values matching {host!r} are accepted. Prefer "
            "127.0.0.1 with a local OpenWebUI instance or the authenticated reverse "
            "proxy configuration in the README.",
            file=sys.stderr,
        )


def main():
    """Main entry point - handle CLI args or run MCP server."""
    parser = argparse.ArgumentParser(
        description="reMarkable MCP Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Register and get token (run once)
  uvx remarkable-mcp --register abcd1234

  # Run as MCP server (cloud API, write-enabled by default)
  uvx remarkable-mcp

  # Run as a read-only server (no upload/mkdir/move/rename/delete)
  uvx remarkable-mcp --read-only

  # Run Streamable HTTP for a local OpenWebUI instance
  uvx remarkable-mcp --http

  # Run with token from environment
  REMARKABLE_TOKEN="your-token" uvx remarkable-mcp

  # Run with USB web interface
  uvx remarkable-mcp --usb

  # Run with SSH transport (direct USB connection, requires dev mode)
  uvx remarkable-mcp --ssh

  # Read the reMarkable desktop app's local sync folder (offline, read-only)
  uvx remarkable-mcp --local-dir

  # Same, with an explicit xochitl-style directory
  uvx remarkable-mcp --local-dir /path/to/remarkable/desktop

  # SSH with custom host (e.g., using SSH config)
  REMARKABLE_SSH_HOST="remarkable" uvx remarkable-mcp --ssh

  # SSH pinning an explicit key (ignores ssh-agent, e.g. 1Password)
  uvx remarkable-mcp --ssh --ssh-key ~/.ssh/id_ed25519

USB Web Interface Environment Variables:
  REMARKABLE_USB_HOST      USB web interface host (default: http://10.11.99.1)
  REMARKABLE_USB_TIMEOUT   Request timeout in seconds (default: 10)

Local Directory Environment Variables:
  REMARKABLE_USE_LOCAL_DIR Enable the local directory transport (1/true/yes)
  REMARKABLE_LOCAL_DIR     Data directory path (default: auto-detect the
                           reMarkable desktop app's sync folder)

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

Streamable HTTP Security:
  HTTP has no built-in authentication and binds to 127.0.0.1 by default. Keep
  it on loopback for a local OpenWebUI instance. Non-loopback bindings expose
  every enabled MCP tool and print a prominent warning at startup. A reverse
  proxy must authenticate requests, rewrite Host to the loopback upstream, and
  clear Origin; see the README example.
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
        "--local-dir",
        nargs="?",
        const="auto",
        metavar="PATH",
        help=(
            "Read from a local reMarkable data directory (read-only). With no "
            "PATH, auto-detects the official desktop app's sync folder. Fully "
            "offline and device-free; the desktop app keeps the folder synced."
        ),
    )
    write_group = parser.add_mutually_exclusive_group()
    write_group.add_argument(
        "--write",
        action="store_true",
        help=(
            "Deprecated no-op. Write tools (upload, mkdir, move, rename, delete) "
            "are enabled by default, so this flag has no effect. Kept for backward "
            "compatibility. Mutually exclusive with --read-only."
        ),
    )
    write_group.add_argument(
        "--read-only",
        action="store_true",
        help=(
            "Disable all write tools (upload, mkdir, move, rename, delete) and "
            "expose a read-only server. Mutually exclusive with --write."
        ),
    )
    parser.add_argument(
        "--no-cloud-fallback",
        action="store_true",
        help=(
            "Disable the automatic cloud fallback. By default, if --local-dir, "
            "--usb, or --ssh is selected but unavailable at startup and a cloud "
            "token is configured, the server falls back to cloud mode."
        ),
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve MCP over Streamable HTTP for a local OpenWebUI instance",
    )
    parser.add_argument(
        "--host",
        help=(
            "Streamable HTTP bind address (default: REMARKABLE_MCP_HOST or "
            "127.0.0.1). Use a concrete address; wildcard addresses are refused. "
            "Non-loopback addresses are unauthenticated and unsafe unless protected "
            "by a correctly configured reverse proxy; see README."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        help="Streamable HTTP port (default: REMARKABLE_MCP_PORT or 8000)",
    )
    args = parser.parse_args()

    if not args.http and (args.host is not None or args.port is not None):
        parser.error("--host and --port require --http")
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

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
    else:
        if args.local_dir:
            # Local directory mode - read-only access to a local data directory
            os.environ["REMARKABLE_USE_LOCAL_DIR"] = "1"
            if args.local_dir != "auto":
                os.environ["REMARKABLE_LOCAL_DIR"] = args.local_dir
        elif args.usb:
            # USB web mode - set environment variable and run server
            os.environ["REMARKABLE_USE_USB_WEB"] = "1"
        elif args.ssh:
            # SSH mode - set environment variable and run server
            os.environ["REMARKABLE_USE_SSH"] = "1"
            if args.ssh_key:
                os.environ["REMARKABLE_SSH_KEY"] = args.ssh_key

        if args.read_only:
            os.environ["REMARKABLE_READ_ONLY"] = "1"
        if args.no_cloud_fallback and (args.local_dir or args.usb or args.ssh):
            os.environ["REMARKABLE_DISABLE_CLOUD_FALLBACK"] = "1"

        from remarkable_mcp.server import _transport_security_for_host, run

        if args.http:
            host = args.host or os.environ.get("REMARKABLE_MCP_HOST", "127.0.0.1")
            try:
                port = (
                    args.port
                    if args.port is not None
                    else int(os.environ.get("REMARKABLE_MCP_PORT", "8000"))
                )
            except ValueError:
                parser.error("REMARKABLE_MCP_PORT must be an integer")
            if not 1 <= port <= 65535:
                parser.error("REMARKABLE_MCP_PORT must be between 1 and 65535")
            try:
                _transport_security_for_host(host)
            except ValueError as e:
                parser.error(str(e))
            _warn_for_http_binding(host)
            run(transport="streamable-http", host=host, port=port)
        else:
            run()


if __name__ == "__main__":
    main()
