#!/usr/bin/env python3
"""HTTP entry point for a self-hosted remarkable-mcp.

The packaged CLI only speaks stdio, which requires the MCP client to live on the
same machine. This entry point serves the same server over streamable HTTP so a
remote client — claude.ai, Claude Desktop, the mobile app — can reach it, with
OAuth in front (see single_user_auth.py).

Run it behind a TLS-terminating reverse proxy; it binds to 127.0.0.1 by default
and must never be exposed directly.

    RMMCP_PUBLIC_URL=https://rm.example.org \
    RMMCP_PASSPHRASE='...' \
    python deploy/http_server.py

Environment:
    RMMCP_PUBLIC_URL     Public HTTPS base URL (required)
    RMMCP_PASSPHRASE     Login secret (required, >= 16 chars)
    RMMCP_HOST           Bind address (default: 127.0.0.1)
    RMMCP_PORT           Bind port (default: 8080)
    RMMCP_ALLOW_WRITES   Set to 1 to expose the write tools (default: read-only)
    RMMCP_STATE_DIR      Where OAuth state is persisted (default: ~/.remarkable-mcp)

    plus the usual reMarkable variables (REMARKABLE_TOKEN or ~/.rmapi).
"""

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

logging.basicConfig(
    level=os.environ.get("RMMCP_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("remarkable-mcp-http")


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"error: {name} is required. See deploy/README or docs/self-hosting.md.")
    return value


def main() -> None:
    public_url = _require("RMMCP_PUBLIC_URL").rstrip("/")
    passphrase = _require("RMMCP_PASSPHRASE")

    if not public_url.startswith("https://"):
        sys.exit(
            "error: RMMCP_PUBLIC_URL must be an https:// URL. OAuth redirects and "
            "bearer tokens over plaintext http would expose your library."
        )

    # Read-only unless writes are explicitly requested. An internet-reachable
    # deployment is exactly where an unattended destructive tool call hurts
    # most, and the read tools are what makes this useful in the first place.
    if os.environ.get("RMMCP_ALLOW_WRITES", "").lower() not in ("1", "true", "yes"):
        os.environ["REMARKABLE_READ_ONLY"] = "1"
        logger.info("Write tools disabled (set RMMCP_ALLOW_WRITES=1 to enable).")
    else:
        logger.warning(
            "Write tools ENABLED. Anything the model reads can drive uploads, moves, "
            "renames and deletes on your library."
        )

    # Imported after the env is settled: remarkable_mcp.server decides which
    # tools to register at import time.
    from mcp.server.auth.provider import ProviderTokenVerifier
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
    from pydantic import AnyHttpUrl
    from single_user_auth import SingleUserAuthProvider, register_login_routes

    from remarkable_mcp.server import mcp

    state_dir = os.environ.get("RMMCP_STATE_DIR")
    provider = SingleUserAuthProvider(
        public_url=public_url,
        passphrase=passphrase,
        state_dir=Path(state_dir) if state_dir else None,
    )

    # Attach the authorization server to the FastMCP instance the package built
    # at import time. FastMCP reads these when it assembles the HTTP app, so
    # setting them here is equivalent to passing them to its constructor.
    mcp.settings.auth = AuthSettings(
        issuer_url=AnyHttpUrl(public_url),
        resource_server_url=AnyHttpUrl(public_url),
        client_registration_options=ClientRegistrationOptions(enabled=True),
    )
    mcp._auth_server_provider = provider
    mcp._token_verifier = ProviderTokenVerifier(provider)

    register_login_routes(mcp, provider)

    mcp.settings.host = os.environ.get("RMMCP_HOST", "127.0.0.1")
    mcp.settings.port = int(os.environ.get("RMMCP_PORT", "8080"))

    logger.info("Public URL:  %s", public_url)
    logger.info("MCP endpoint: %s/mcp", public_url)
    logger.info("Listening on %s:%s", mcp.settings.host, mcp.settings.port)

    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
