"""Protocol-level tests: every call goes through an MCP client session, not Python functions.

* In-memory transport: fast, exercises schemas, annotations, tool dispatch, error shapes.
* Stdio subprocess: proves `python week2/server.py` really speaks MCP over stdio.
Linear is faked with httpx.MockTransport; no network or real credentials needed.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

import auth
import server
from linear_client import LinearClient

WEEK2 = Path(__file__).resolve().parent.parent
TEAM = {"id": "11111111-aaaa-bbbb-cccc-222222222222", "key": "ENG", "name": "Engineering"}


def issue_node(n=1, title="Fix login"):
    return {
        "identifier": f"ENG-{n}", "title": title, "priorityLabel": "High", "url": f"https://linear.app/x/issue/ENG-{n}",
        "updatedAt": "2026-09-01T00:00:00Z", "state": {"name": "In Progress", "type": "started"},
        "assignee": {"name": "Sarat"}, "team": {"key": "ENG"},
        "internalJunk": "should never leak",
    }


class FakeLinear:
    """Routes GraphQL + token requests; records calls; lets tests script failures."""

    def __init__(self):
        self.graphql_calls: list[dict] = []
        self.token_calls: list[dict] = []
        self.created = 0
        self.rate_limit_next = False
        self.valid_tokens = {"good-token"}
        self.refresh_ok = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            self.token_calls.append(form)
            if form["grant_type"] == "refresh_token" and self.refresh_ok:
                self.valid_tokens.add("refreshed-token")
                return httpx.Response(200, json={"access_token": "refreshed-token", "refresh_token": "r2", "expires_in": 86400, "scope": "read issues:create"})
            return httpx.Response(400, json={"error": "invalid_grant"})
        token = request.headers["authorization"].removeprefix("Bearer ")
        if token not in self.valid_tokens:
            return httpx.Response(401, json={"errors": [{"message": "Authentication required"}]})
        if self.rate_limit_next:
            self.rate_limit_next = False
            return httpx.Response(429, headers={"retry-after": "42"}, json={})
        body = json.loads(request.content)
        self.graphql_calls.append(body)
        q = body["query"]
        if "teams(first" in q:
            return httpx.Response(200, json={"data": {"teams": {"nodes": [TEAM]}}})
        if "issueCreate" in q:
            self.created += 1
            return httpx.Response(200, json={"data": {"issueCreate": {"success": True, "issue": issue_node(99, body["variables"]["input"]["title"])}}})
        if "issues(filter" in q:
            return httpx.Response(200, json={"data": {"issues": {"nodes": [issue_node(1), issue_node(2, "Other")]}}})
        if "issue(id" in q:
            if body["variables"]["id"] != "ENG-1":
                return httpx.Response(200, json={"errors": [{"message": "Entity not found: Issue", "extensions": {"code": "INVALID_INPUT"}}]})
            node = issue_node(1) | {"description": "Login broken", "createdAt": "2026-08-01T00:00:00Z",
                                    "labels": {"nodes": [{"name": "bug"}]},
                                    "comments": {"nodes": [{"body": "repro'd", "createdAt": "2026-08-02T00:00:00Z", "user": {"name": "Sam"}}]}}
            return httpx.Response(200, json={"data": {"issue": node}})
        return httpx.Response(400, json={"errors": [{"message": "unexpected query"}]})


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_CLIENT_ID", "cid")
    monkeypatch.setenv("LINEAR_CLIENT_SECRET", "secret")
    fake = FakeLinear()
    transport = httpx.MockTransport(fake)
    store = auth.TokenStore(path=tmp_path / "token.json", transport=transport)
    store.save(auth.Token("good-token", "r1", time.time() + 3600))
    server.configure(LinearClient(store, transport=transport))
    fake.store = store
    yield fake
    server.configure(None)


async def call(client, name, args=None):
    result = await client.call_tool(name, args or {}, raise_on_error=False)
    return result.data if result.data is not None else result


async def test_tools_listed_with_annotations_and_schema_constraints(fake):
    async with Client(server.mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert set(tools) == {"list_teams", "search_issues", "get_issue", "create_issue"}
    for name in ("list_teams", "search_issues", "get_issue"):
        assert tools[name].annotations.read_only_hint is True
    assert tools["create_issue"].annotations.read_only_hint is False
    # Constraints live in the JSON schema, not just prose:
    props = tools["search_issues"].input_schema["properties"]
    assert "enum" in json.dumps(props["state_type"]) and "started" in json.dumps(props["state_type"])
    assert props["limit"]["minimum"] == 1 and props["limit"]["maximum"] == 50
    assert tools["create_issue"].input_schema["properties"]["dry_run"]["default"] is True


async def test_end_to_end_chain_search_then_get(fake):
    async with Client(server.mcp) as client:
        found = await call(client, "search_issues", {"text": "login", "state_type": "started"})
        assert found["ok"] and found["count"] == 2
        assert "internalJunk" not in json.dumps(found)  # output is shaped, not passthrough
        ident = found["issues"][0]["identifier"]
        detail = await call(client, "get_issue", {"issue_id": ident})
    assert detail["issue"]["labels"] == ["bug"] and detail["issue"]["recent_comments"][0]["author"] == "Sam"
    sent = fake.graphql_calls[0]["variables"]["filter"]
    assert sent["state"] == {"type": {"eq": "started"}}


async def test_schema_rejects_bad_enum_before_hitting_linear(fake):
    async with Client(server.mcp) as client:
        result = await client.call_tool("search_issues", {"state_type": "doing"}, raise_on_error=False)
    assert result.is_error
    assert fake.graphql_calls == []


async def test_create_issue_dry_run_is_default_and_writes_nothing(fake):
    async with Client(server.mcp) as client:
        preview = await call(client, "create_issue", {"team_id": TEAM["id"], "title": "New bug"})
        assert preview["dry_run"] is True and preview["would_create"]["team"] == "Engineering (ENG)"
        assert fake.created == 0
        real = await call(client, "create_issue", {"team_id": TEAM["id"], "title": "New bug", "dry_run": False})
    assert fake.created == 1 and real["issue"]["identifier"] == "ENG-99"


async def test_failure_bad_id_is_structured_and_not_retryable(fake):
    async with Client(server.mcp) as client:
        out = await call(client, "get_issue", {"issue_id": "ENG-9999"})
    assert out["ok"] is False
    assert out["error"]["code"] == "NOT_FOUND" and out["error"]["retryable"] is False
    assert "search_issues" in out["error"]["action"]


async def test_bad_team_id_points_agent_at_list_teams(fake):
    async with Client(server.mcp) as client:
        out = await call(client, "create_issue", {"team_id": "ENG-not-a-uuid", "title": "x", "dry_run": False})
    assert out["error"]["code"] == "NOT_FOUND" and "list_teams" in out["error"]["action"]
    assert fake.created == 0


async def test_rate_limit_is_retryable_with_wait(fake):
    fake.rate_limit_next = True
    async with Client(server.mcp) as client:
        out = await call(client, "list_teams")
    assert out["error"] == {"code": "RATE_LIMITED", "message": "Rate limited by Linear; retry after 42s.",
                            "retryable": True, "retry_after_seconds": 42}


async def test_expired_token_refreshes_silently_and_rotates(fake):
    fake.store.save(auth.Token("stale", "r1", time.time() - 10))  # expired
    async with Client(server.mcp) as client:
        out = await call(client, "list_teams")
    assert out["ok"] is True
    assert fake.token_calls[0]["grant_type"] == "refresh_token"
    assert fake.store.load().refresh_token == "r2"  # rotated token was cached


async def test_token_revoked_mid_session_refreshes_once_and_retries(fake):
    fake.store.save(auth.Token("revoked", "r1", time.time() + 3600))  # looks valid, server says 401
    async with Client(server.mcp) as client:
        out = await call(client, "list_teams")
    assert out["ok"] is True and len(fake.token_calls) == 1


async def test_dead_refresh_token_gives_actionable_auth_error_no_browser(fake, monkeypatch):
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: pytest.fail("server must never open a browser"))
    fake.store.save(auth.Token("stale", "dead", time.time() - 10))
    fake.refresh_ok = False
    async with Client(server.mcp) as client:
        out = await call(client, "list_teams")
    err = out["error"]
    assert err["code"] == "AUTH_REQUIRED" and err["retryable"] is False
    assert "login.py" in err["action"]


async def test_stdio_subprocess_speaks_mcp_without_a_token(tmp_path):
    """Spawn the real server the way Claude Code would. No token cached -> AUTH_REQUIRED."""
    transport = StdioTransport(
        command=sys.executable, args=[str(WEEK2 / "server.py")],
        env={"LINEAR_MCP_TOKEN_PATH": str(tmp_path / "none.json"), "LINEAR_CLIENT_ID": "cid", "PATH": "/usr/bin:/bin"},
    )
    async with Client(transport) as client:
        names = {t.name for t in await client.list_tools()}
        out = await call(client, "list_teams")
    assert names == {"list_teams", "search_issues", "get_issue", "create_issue"}
    assert out["error"]["code"] == "AUTH_REQUIRED"
