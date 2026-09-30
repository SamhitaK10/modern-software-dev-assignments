"""Thin async Linear GraphQL client. Converts every failure into a structured ToolError."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

import auth

GRAPHQL_URL = "https://api.linear.app/graphql"


@dataclass
class ToolError(Exception):
    """Structured, agent-readable failure. `retryable` tells the agent whether to try again."""

    code: str
    message: str
    retryable: bool
    retry_after_seconds: int | None = None
    action: str | None = None  # what the agent (or user) should do next

    def to_dict(self) -> dict[str, Any]:
        err: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.retry_after_seconds is not None:
            err["retry_after_seconds"] = self.retry_after_seconds
        if self.action:
            err["action"] = self.action
        return {"ok": False, "error": err}


def _rate_limit_wait(resp: httpx.Response) -> int:
    header = resp.headers.get("retry-after")
    if header and header.isdigit():
        return max(1, int(header))
    reset = resp.headers.get("x-ratelimit-requests-reset")  # epoch milliseconds
    if reset and reset.isdigit():
        return max(1, int(int(reset) / 1000 - time.time()) + 1)
    return 30


class LinearClient:
    def __init__(self, store: auth.TokenStore, transport: httpx.AsyncBaseTransport | None = None):
        self.store = store
        self._transport = transport

    async def _send(self, query: str, variables: dict, token: str) -> httpx.Response:
        async with httpx.AsyncClient(transport=self._transport, timeout=20) as http:
            return await http.post(
                GRAPHQL_URL,
                json={"query": query, "variables": variables},
                headers={"Authorization": f"Bearer {token}"},
            )

    async def query(self, query: str, variables: dict | None = None) -> dict[str, Any]:
        variables = variables or {}
        try:
            token = await self.store.get_access_token()
            resp = await self._send(query, variables, token)
            if resp.status_code == 401:
                # Token died mid-session (revoked, or expired earlier than we thought):
                # refresh once silently and retry once. Never open a browser here.
                token = await self.store.get_access_token(force_refresh=True)
                resp = await self._send(query, variables, token)
        except auth.AuthRequired as exc:
            raise ToolError(
                code="AUTH_REQUIRED",
                message=str(exc),
                retryable=False,
                action=f"Tell the user to run `{auth.LOGIN_COMMAND}` in a terminal, then retry.",
            ) from exc
        except auth.OAuthConfigError as exc:
            raise ToolError("SERVER_MISCONFIGURED", str(exc), retryable=False) from exc
        except httpx.TransportError as exc:
            raise ToolError(
                code="NETWORK_ERROR",
                message=f"Could not reach Linear ({type(exc).__name__}).",
                retryable=True,
                retry_after_seconds=2,
            ) from exc
        return self._parse(resp)

    def _parse(self, resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code == 429:
            wait = _rate_limit_wait(resp)
            raise ToolError("RATE_LIMITED", f"Rate limited by Linear; retry after {wait}s.", True, wait)
        if resp.status_code == 401:
            raise ToolError(
                "AUTH_REQUIRED",
                "Linear rejected the access token even after a refresh.",
                False,
                action=f"Tell the user to run `{auth.LOGIN_COMMAND}` in a terminal, then retry.",
            )
        if resp.status_code >= 500:
            raise ToolError("UPSTREAM_ERROR", f"Linear returned HTTP {resp.status_code}.", True, 5)
        try:
            body = resp.json()
        except ValueError:
            raise ToolError("UPSTREAM_ERROR", "Linear returned a non-JSON response.", True, 5) from None

        errors = body.get("errors")
        if errors:
            first = errors[0]
            ext = first.get("extensions") or {}
            message = first.get("message", "Unknown Linear error")
            code = str(ext.get("code", "")).upper()
            if code == "RATELIMITED" or resp.status_code == 429:
                wait = _rate_limit_wait(resp)
                raise ToolError("RATE_LIMITED", f"Rate limited by Linear; retry after {wait}s.", True, wait)
            if code in {"AUTHENTICATION_ERROR", "AUTHENTICATION"} or "authentication" in message.lower():
                raise ToolError(
                    "AUTH_REQUIRED",
                    message,
                    False,
                    action=f"Tell the user to run `{auth.LOGIN_COMMAND}` in a terminal, then retry.",
                )
            if code == "FORBIDDEN" or "forbidden" in message.lower() or "scope" in message.lower():
                raise ToolError(
                    "FORBIDDEN",
                    f"{message} (the OAuth app lacks a required scope or the user lacks access).",
                    False,
                )
            if "not found" in message.lower() or "entity not found" in message.lower():
                raise ToolError(
                    "NOT_FOUND",
                    message,
                    False,
                    action="Do not retry with the same ID. Get a valid ID from search_issues or list_teams.",
                )
            # Everything else from a 4xx GraphQL error is a bad request: retrying is pointless.
            raise ToolError("INVALID_REQUEST", message, False)
        if resp.status_code >= 400:
            raise ToolError("INVALID_REQUEST", f"Linear returned HTTP {resp.status_code}.", False)
        return body.get("data") or {}
