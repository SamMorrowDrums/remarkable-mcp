"""Single-user OAuth 2.1 authorization server for a self-hosted remarkable-mcp.

Claude connects to a remote MCP server over HTTPS and expects it to speak OAuth
2.1 with dynamic client registration — there is no field for pasting a static
token. This module implements the smallest authorization server that satisfies
that contract for a deployment with exactly one human user: you prove who you
are with a single passphrase, and every token issued belongs to that same
subject.

The MCP SDK already implements the OAuth endpoints (``/authorize``, ``/token``,
``/register``, and the discovery metadata) and verifies PKCE itself. What is
left to supply is storage and a login screen, which is all this file is.

Deliberately out of scope: multiple users, roles, per-scope consent, account
recovery. If you need any of those, put a real identity provider in front and
run FastMCP as a pure resource server (``token_verifier=``) instead.

Configuration (environment):
    RMMCP_PUBLIC_URL   Public HTTPS base URL, e.g. https://rm.example.org
    RMMCP_PASSPHRASE   The login secret. Required. Use a long random string.
    RMMCP_STATE_DIR    Where to persist clients/refresh tokens
                       (default: ~/.remarkable-mcp)
"""

import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

logger = logging.getLogger(__name__)

# Lifetimes. Authorization codes are single-use and short-lived; access tokens
# are refreshed silently by the client; refresh tokens are what keep the
# connector working across restarts without a new login.
AUTH_CODE_TTL = 300  # 5 minutes
ACCESS_TOKEN_TTL = 3600  # 1 hour
REFRESH_TOKEN_TTL = 30 * 24 * 3600  # 30 days
PENDING_LOGIN_TTL = 600  # 10 minutes to complete the login form

# Brute-force protection for the login form. A single passphrase is the only
# thing between the internet and the library, so failures are throttled and
# eventually locked out for a cooling-off period.
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 900

SUBJECT = "owner"  # the one and only user


@dataclass
class _PendingLogin:
    """An authorization request parked while the human proves themselves."""

    client_id: str
    params: AuthorizationParams
    created_at: float


class SingleUserAuthProvider(OAuthAuthorizationServerProvider):
    """OAuth authorization server backed by one passphrase.

    Registered clients and refresh tokens are persisted to disk so that
    restarting the service does not force you to reconnect the connector.
    Authorization codes and access tokens are kept in memory only: they are
    short-lived, and losing them on restart costs one silent refresh.
    """

    def __init__(
        self,
        public_url: str,
        passphrase: str,
        state_dir: Optional[Path] = None,
    ):
        if not passphrase:
            raise ValueError("RMMCP_PASSPHRASE must be set to a non-empty value.")
        if len(passphrase) < 16:
            raise ValueError(
                "RMMCP_PASSPHRASE is too short (minimum 16 characters). "
                "Generate one with: openssl rand -base64 32"
            )

        self.public_url = public_url.rstrip("/")
        self._passphrase = passphrase
        self._state_path = (state_dir or Path.home() / ".remarkable-mcp") / "auth-state.json"

        self._clients: Dict[str, OAuthClientInformationFull] = {}
        self._refresh_tokens: Dict[str, RefreshToken] = {}
        self._auth_codes: Dict[str, AuthorizationCode] = {}
        self._access_tokens: Dict[str, AccessToken] = {}
        self._pending: Dict[str, _PendingLogin] = {}

        self._failed_attempts = 0
        self._locked_until = 0.0

        self._load_state()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        if not self._state_path.is_file():
            return
        try:
            data = json.loads(self._state_path.read_text())
        except Exception as e:
            logger.warning("Could not read auth state (%s); starting fresh.", e)
            return

        for raw in data.get("clients", []):
            try:
                client = OAuthClientInformationFull.model_validate(raw)
                self._clients[client.client_id] = client
            except Exception as e:
                logger.debug("Skipping unreadable client record: %s", e)

        now = time.time()
        for raw in data.get("refresh_tokens", []):
            try:
                token = RefreshToken.model_validate(raw)
            except Exception as e:
                logger.debug("Skipping unreadable refresh token: %s", e)
                continue
            if token.expires_at and token.expires_at < now:
                continue
            self._refresh_tokens[token.token] = token

        logger.info(
            "Loaded %d client(s) and %d refresh token(s) from %s",
            len(self._clients),
            len(self._refresh_tokens),
            self._state_path,
        )

    def _save_state(self) -> None:
        """Persist clients and refresh tokens, owner-readable only.

        These records are bearer credentials for the whole library, so the file
        is created 0600 and the directory 0700 before anything is written.
        """
        payload = {
            "clients": [
                c.model_dump(mode="json", exclude_none=True) for c in self._clients.values()
            ],
            "refresh_tokens": [
                t.model_dump(mode="json", exclude_none=True) for t in self._refresh_tokens.values()
            ],
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = self._state_path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, self._state_path)
        except Exception as e:
            logger.error("Failed to persist auth state: %s", e)

    # ------------------------------------------------------------------
    # Client registration (RFC 7591) — Claude registers itself on first connect
    # ------------------------------------------------------------------

    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info
        self._save_state()
        logger.info(
            "Registered OAuth client %s (%s)", client_info.client_id, client_info.client_name
        )

    # ------------------------------------------------------------------
    # Authorization: park the request, send the human to the login form
    # ------------------------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        self._expire_pending()
        request_id = secrets.token_urlsafe(24)
        self._pending[request_id] = _PendingLogin(
            client_id=client.client_id,
            params=params,
            created_at=time.time(),
        )
        return f"{self.public_url}/login?rid={request_id}"

    def _expire_pending(self) -> None:
        cutoff = time.time() - PENDING_LOGIN_TTL
        for rid in [r for r, p in self._pending.items() if p.created_at < cutoff]:
            del self._pending[rid]

    # ------------------------------------------------------------------
    # Login form (wired up by register_login_routes below)
    # ------------------------------------------------------------------

    def _lockout_remaining(self) -> int:
        return max(0, int(self._locked_until - time.time()))

    def render_login(self, request_id: str, error: Optional[str] = None) -> Response:
        if request_id not in self._pending:
            return HTMLResponse(
                _page(
                    "Lien expiré",
                    "Cette demande de connexion a expiré. Relancez la connexion depuis Claude.",
                ),
                status_code=400,
            )
        remaining = self._lockout_remaining()
        if remaining:
            return HTMLResponse(
                _page(
                    "Trop de tentatives",
                    f"Réessayez dans {remaining // 60 + 1} minute(s).",
                ),
                status_code=429,
            )
        return HTMLResponse(_login_form(request_id, error))

    def complete_login(self, request_id: str, passphrase: str) -> Response:
        """Validate the passphrase and hand an authorization code back to Claude."""
        if self._lockout_remaining():
            return self.render_login(request_id)

        pending = self._pending.get(request_id)
        if pending is None:
            return HTMLResponse(
                _page(
                    "Lien expiré",
                    "Cette demande de connexion a expiré. Relancez la connexion depuis Claude.",
                ),
                status_code=400,
            )

        if not secrets.compare_digest(passphrase, self._passphrase):
            self._failed_attempts += 1
            if self._failed_attempts >= MAX_FAILED_ATTEMPTS:
                self._locked_until = time.time() + LOCKOUT_SECONDS
                self._failed_attempts = 0
                logger.warning("Login locked out after repeated failures.")
            else:
                logger.warning(
                    "Failed login attempt (%d/%d)", self._failed_attempts, MAX_FAILED_ATTEMPTS
                )
            return self.render_login(request_id, error="Phrase secrète incorrecte.")

        # Success: burn the pending request and mint a single-use code.
        del self._pending[request_id]
        self._failed_attempts = 0

        code = secrets.token_urlsafe(32)
        self._auth_codes[code] = AuthorizationCode(
            code=code,
            client_id=pending.client_id,
            redirect_uri=pending.params.redirect_uri,
            redirect_uri_provided_explicitly=pending.params.redirect_uri_provided_explicitly,
            scopes=pending.params.scopes or [],
            expires_at=time.time() + AUTH_CODE_TTL,
            code_challenge=pending.params.code_challenge,
            resource=pending.params.resource,
            subject=SUBJECT,
        )
        logger.info("Login accepted; issuing authorization code to %s", pending.client_id)

        return RedirectResponse(
            construct_redirect_uri(
                str(pending.params.redirect_uri),
                code=code,
                state=pending.params.state,
            ),
            status_code=302,
        )

    # ------------------------------------------------------------------
    # Token issuance
    # ------------------------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> Optional[AuthorizationCode]:
        code = self._auth_codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        if code.expires_at < time.time():
            del self._auth_codes[authorization_code]
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # Single use: the code is spent whether or not what follows succeeds.
        self._auth_codes.pop(authorization_code.code, None)
        return self._issue_tokens(
            client.client_id, authorization_code.scopes, authorization_code.resource
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> Optional[RefreshToken]:
        token = self._refresh_tokens.get(refresh_token)
        if token is None or token.client_id != client.client_id:
            return None
        if token.expires_at and token.expires_at < time.time():
            del self._refresh_tokens[refresh_token]
            self._save_state()
            return None
        return token

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Rotate: the presented refresh token is retired as the new pair is issued.
        self._refresh_tokens.pop(refresh_token.token, None)
        return self._issue_tokens(client.client_id, scopes or refresh_token.scopes, None)

    def _issue_tokens(
        self, client_id: str, scopes: list[str], resource: Optional[str]
    ) -> OAuthToken:
        self._expire_access_tokens()
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        now = time.time()

        self._access_tokens[access] = AccessToken(
            token=access,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(now + ACCESS_TOKEN_TTL),
            resource=resource,
            subject=SUBJECT,
        )
        self._refresh_tokens[refresh] = RefreshToken(
            token=refresh,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(now + REFRESH_TOKEN_TTL),
            subject=SUBJECT,
        )
        self._save_state()

        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(scopes) if scopes else None,
            refresh_token=refresh,
        )

    def _expire_access_tokens(self) -> None:
        now = time.time()
        for tok in [
            t for t, a in self._access_tokens.items() if a.expires_at and a.expires_at < now
        ]:
            del self._access_tokens[tok]

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        access = self._access_tokens.get(token)
        if access is None:
            return None
        if access.expires_at and access.expires_at < time.time():
            del self._access_tokens[token]
            return None
        return access

    async def revoke_token(self, token: Any) -> None:
        value = getattr(token, "token", token)
        self._access_tokens.pop(value, None)
        if self._refresh_tokens.pop(value, None) is not None:
            self._save_state()


# ----------------------------------------------------------------------
# Login pages — plain HTML, no external assets (nothing to fetch, nothing to
# leak a referrer to).
# ----------------------------------------------------------------------

_STYLE = """
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #1e1e1e; color: #e8e8e8; display: flex; min-height: 100vh;
         align-items: center; justify-content: center; margin: 0; }
  .card { background: #262626; padding: 2rem; border-radius: .6rem; width: min(26rem, 90vw);
          box-shadow: 0 2px 16px rgba(0,0,0,.4); }
  h1 { font-size: 1.15rem; margin: 0 0 .5rem; }
  p { opacity: .75; font-size: .9rem; line-height: 1.5; }
  input { width: 100%; box-sizing: border-box; font: inherit; padding: .6rem;
          border-radius: .4rem; border: 1px solid rgba(255,255,255,.2);
          background: #1a1a1a; color: inherit; margin: .75rem 0; }
  button { width: 100%; font: inherit; padding: .6rem; border-radius: .4rem; cursor: pointer;
           border: none; background: #4a7ec7; color: #fff; font-weight: 600; }
  .err { color: #ff8a8a; font-size: .85rem; }
"""


def _page(title: str, body: str) -> str:
    return (
        f"<!doctype html><html lang=fr><head><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{title}</title><style>{_STYLE}</style></head>"
        f"<body><div class=card><h1>{title}</h1><p>{body}</p></div></body></html>"
    )


def _login_form(request_id: str, error: Optional[str] = None) -> str:
    err = f"<p class=err>{error}</p>" if error else ""
    return (
        f"<!doctype html><html lang=fr><head><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>Connexion reMarkable</title><style>{_STYLE}</style></head>"
        f"<body><div class=card>"
        f"<h1>Connecter Claude à votre reMarkable</h1>"
        f"<p>Saisissez la phrase secrète de votre serveur pour autoriser l'accès.</p>"
        f"{err}"
        f'<form method="post" action="/login">'
        f'<input type="hidden" name="rid" value="{request_id}">'
        f'<input type="password" name="passphrase" placeholder="Phrase secrète" '
        f'autocomplete="current-password" autofocus required>'
        f"<button type=submit>Autoriser</button>"
        f"</form></div></body></html>"
    )


def register_login_routes(mcp, provider: SingleUserAuthProvider) -> None:
    """Attach the login form routes to a FastMCP instance."""

    @mcp.custom_route("/login", methods=["GET"])
    async def login_form(request: Request) -> Response:
        return provider.render_login(request.query_params.get("rid", ""))

    @mcp.custom_route("/login", methods=["POST"])
    async def login_submit(request: Request) -> Response:
        form = await request.form()
        return provider.complete_login(str(form.get("rid", "")), str(form.get("passphrase", "")))

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> Response:
        return Response("ok", media_type="text/plain")
