"""OAuth 2.0 (authorization code + PKCE) for Linear: acquire, cache, silently refresh.

Design rules
------------
* The MCP server process NEVER opens a browser. Interactive consent lives only in
  ``login.py``. If the server has no usable token it raises ``AuthRequired`` and the tool
  layer turns that into an actionable error for the agent.
* Secrets come from the environment (``LINEAR_CLIENT_ID`` / ``LINEAR_CLIENT_SECRET``).
  The token cache lives outside the repo (``~/.config/linear-mcp/token.json``, mode 0600).
* Scopes are the minimum the tools need (see ``SCOPES``).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

AUTHORIZE_URL = "https://linear.app/oauth/authorize"
TOKEN_URL = "https://api.linear.app/oauth/token"
REDIRECT_URI = os.environ.get("LINEAR_REDIRECT_URI", "http://localhost:8765/callback")

# Minimal scopes, one line of justification each:
#   read          -> list_teams, search_issues, get_issue (all read-only queries)
#   issues:create -> create_issue only. Narrower than `write`: cannot edit/delete anything.
SCOPES = ["read", "issues:create"]

REFRESH_MARGIN_SECONDS = 60
LOGIN_COMMAND = "python week2/login.py"


class AuthRequired(Exception):
    """No usable token; a human must run the login command. Never retryable by the agent."""

    def __init__(self, message: str, reason: str = "no_token"):
        super().__init__(message)
        self.reason = reason


class OAuthConfigError(Exception):
    """Client id/secret missing from the environment."""


@dataclass
class Token:
    access_token: str
    refresh_token: str | None
    expires_at: float  # unix seconds; 0 means "unknown / non-expiring"
    scope: str = ""

    def expired(self, now: float | None = None) -> bool:
        if not self.expires_at:
            return False
        return (now or time.time()) >= self.expires_at - REFRESH_MARGIN_SECONDS


def default_token_path() -> Path:
    override = os.environ.get("LINEAR_MCP_TOKEN_PATH")
    if override:
        return Path(override).expanduser()
    return Path("~/.config/linear-mcp/token.json").expanduser()


def client_credentials() -> tuple[str, str | None]:
    client_id = os.environ.get("LINEAR_CLIENT_ID")
    if not client_id:
        raise OAuthConfigError(
            "LINEAR_CLIENT_ID is not set. Create an OAuth app in Linear "
            "(Settings > API > OAuth applications) and export LINEAR_CLIENT_ID / "
            "LINEAR_CLIENT_SECRET."
        )
    return client_id, os.environ.get("LINEAR_CLIENT_SECRET")


# --------------------------------------------------------------------------- PKCE helpers
def make_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def build_authorize_url(client_id: str, state: str, code_challenge: str) -> str:
    query = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": ",".join(SCOPES),  # Linear takes comma-separated scopes
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "consent",
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(query)}"


# --------------------------------------------------------------------------- token store
class TokenStore:
    """Loads, caches and refreshes the token. Safe for concurrent tool calls."""

    def __init__(self, path: Path | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.path = path or default_token_path()
        self._transport = transport
        self._lock = asyncio.Lock()
        self._token: Token | None = None

    # -- persistence
    def load(self) -> Token | None:
        if self._token:
            return self._token
        try:
            data = json.loads(self.path.read_text())
            self._token = Token(**data)
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            return None
        return self._token

    def save(self, token: Token) -> None:
        self._token = token
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(token)))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def clear(self) -> None:
        self._token = None
        self.path.unlink(missing_ok=True)

    # -- HTTP to the token endpoint
    async def _post_token(self, form: dict[str, str]) -> dict:
        async with httpx.AsyncClient(transport=self._transport, timeout=15) as http:
            resp = await http.post(TOKEN_URL, data=form)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400:
            body.setdefault("error", f"http_{resp.status_code}")
        return body

    @staticmethod
    def _token_from_response(body: dict, previous: Token | None = None) -> Token:
        expires_in = body.get("expires_in")
        return Token(
            access_token=body["access_token"],
            # Providers may or may not rotate the refresh token; keep the old one if omitted.
            refresh_token=body.get("refresh_token") or (previous.refresh_token if previous else None),
            expires_at=time.time() + float(expires_in) if expires_in else 0,
            scope=body.get("scope", "") if isinstance(body.get("scope"), str) else " ".join(body.get("scope", [])),
        )

    # -- the two flows
    async def exchange_code(self, code: str, code_verifier: str) -> Token:
        client_id, client_secret = client_credentials()
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": code_verifier,
        }
        if client_secret:
            form["client_secret"] = client_secret
        body = await self._post_token(form)
        if "access_token" not in body:
            raise AuthRequired(
                f"Code exchange failed: {body.get('error_description') or body.get('error')}",
                reason="exchange_failed",
            )
        token = self._token_from_response(body)
        self.save(token)
        return token

    async def refresh(self, current: Token) -> Token:
        if not current.refresh_token:
            raise AuthRequired(
                f"Access token expired and there is no refresh token. Run `{LOGIN_COMMAND}`.",
                reason="expired_no_refresh",
            )
        client_id, client_secret = client_credentials()
        form = {
            "grant_type": "refresh_token",
            "refresh_token": current.refresh_token,
            "client_id": client_id,
        }
        if client_secret:
            form["client_secret"] = client_secret
        try:
            body = await self._post_token(form)
        except httpx.TransportError as exc:
            # Network blip: NOT an auth failure; let the caller treat it as retryable.
            raise exc
        if "access_token" not in body:
            # invalid_grant => refresh token revoked/expired: only a human can fix this.
            self.clear()
            raise AuthRequired(
                "Linear refused the refresh token (revoked or expired). "
                f"Ask the user to run `{LOGIN_COMMAND}` in a terminal, then retry.",
                reason="refresh_rejected",
            )
        token = self._token_from_response(body, previous=current)
        self.save(token)
        return token

    # -- what the API client calls
    async def get_access_token(self, force_refresh: bool = False) -> str:
        async with self._lock:
            token = self.load()
            if token is None:
                raise AuthRequired(
                    f"Not signed in to Linear. Ask the user to run `{LOGIN_COMMAND}` in a terminal, then retry.",
                    reason="no_token",
                )
            if force_refresh or token.expired():
                token = await self.refresh(token)
            return token.access_token
