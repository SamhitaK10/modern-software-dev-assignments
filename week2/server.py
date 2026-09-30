"""Linear MCP server (stdio). Run: `python week2/server.py`  (after `python week2/login.py`).

Tools (they compose):
    list_teams    -> team ids/keys          -> feed search_issues(team_key) and create_issue(team_id)
    search_issues -> issue identifiers      -> feed get_issue(issue_id)
    get_issue     -> full detail of one issue
    create_issue  -> WRITE; dry_run=True by default (preview, then commit with dry_run=False)
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

import httpx
from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

import auth
from linear_client import LinearClient, ToolError

INSTRUCTIONS = """\
Tools for reading and filing Linear issues.

Typical workflow:
1. list_teams to learn team keys (e.g. "ENG") and team ids.
2. search_issues (filter by team_key / state_type / text) to find issues; each result has an
   `identifier` like "ENG-123".
3. get_issue with that identifier for full detail (description, labels, recent comments).
4. To file a new issue: call create_issue with dry_run=true (the default) and show the user
   the preview. Only call it again with dry_run=false after the user approves.

Every tool returns {"ok": true, ...} or {"ok": false, "error": {code, message, retryable,
retry_after_seconds?, action?}}. Retry only when error.retryable is true, and wait
retry_after_seconds first. AUTH_REQUIRED means the human must run a login command; do not retry.
"""

mcp = FastMCP("linear", instructions=INSTRUCTIONS)

# Injected in tests; in production built lazily from env/token cache.
_client: LinearClient | None = None


def configure(client: LinearClient | None) -> None:
    global _client
    _client = client


def _get_client() -> LinearClient:
    global _client
    if _client is None:
        _client = LinearClient(auth.TokenStore())
    return _client


PRIORITY_TO_INT = {"none": 0, "urgent": 1, "high": 2, "medium": 3, "low": 4}

ISSUE_FIELDS = """
  identifier title priorityLabel url updatedAt
  state { name type }
  assignee { name }
  team { key }
"""


def _shape_issue(node: dict) -> dict[str, Any]:
    """Keep only what an agent needs; drop internal ids, avatars, sort orders, etc."""
    return {
        "identifier": node["identifier"],
        "title": node["title"],
        "state": (node.get("state") or {}).get("name"),
        "state_type": (node.get("state") or {}).get("type"),
        "priority": node.get("priorityLabel"),
        "assignee": (node.get("assignee") or {}).get("name"),
        "team_key": (node.get("team") or {}).get("key"),
        "updated_at": node.get("updatedAt"),
        "url": node.get("url"),
    }


READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)


@mcp.tool(annotations=READ_ONLY)
async def list_teams() -> dict[str, Any]:
    """List the Linear teams the user can see.

    Call this first. Returns `teams`: [{id, key, name}]. Use `key` (e.g. "ENG") as
    `team_key` in search_issues, and `id` (a UUID) as `team_id` in create_issue.
    """
    try:
        data = await _get_client().query("query { teams(first: 50) { nodes { id key name } } }")
    except ToolError as err:
        return err.to_dict()
    return {"ok": True, "teams": data["teams"]["nodes"]}


@mcp.tool(annotations=READ_ONLY)
async def search_issues(
    text: Annotated[str | None, Field(description="Case-insensitive match on title or description")] = None,
    team_key: Annotated[str | None, Field(description='Team key from list_teams, e.g. "ENG"')] = None,
    state_type: Annotated[
        Literal["triage", "backlog", "unstarted", "started", "completed", "canceled"] | None,
        Field(description="Workflow state category"),
    ] = None,
    assigned_to_me: Annotated[bool, Field(description="Only issues assigned to the current user")] = False,
    order_by: Literal["updated", "created"] = "updated",
    limit: Annotated[int, Field(ge=1, le=50, description="Max results (1-50)")] = 10,
) -> dict[str, Any]:
    """Search Linear issues. All filters are optional and combined with AND.

    Returns `issues`: [{identifier, title, state, state_type, priority, assignee, team_key,
    updated_at, url}], newest first. Pass an `identifier` (e.g. "ENG-123") to get_issue for
    full detail. `team_key` comes from list_teams.
    """
    flt: dict[str, Any] = {}
    if text:
        flt["or"] = [
            {"title": {"containsIgnoreCase": text}},
            {"description": {"containsIgnoreCase": text}},
        ]
    if team_key:
        flt["team"] = {"key": {"eq": team_key.upper()}}
    if state_type:
        flt["state"] = {"type": {"eq": state_type}}
    if assigned_to_me:
        flt["assignee"] = {"isMe": {"eq": True}}
    order = "updatedAt" if order_by == "updated" else "createdAt"
    query = f"""
    query($filter: IssueFilter, $first: Int, $orderBy: PaginationOrderBy) {{
      issues(filter: $filter, first: $first, orderBy: $orderBy) {{ nodes {{ {ISSUE_FIELDS} }} }}
    }}"""
    try:
        data = await _get_client().query(query, {"filter": flt, "first": limit, "orderBy": order})
    except ToolError as err:
        return err.to_dict()
    issues = [_shape_issue(n) for n in data["issues"]["nodes"]]
    return {"ok": True, "count": len(issues), "issues": issues}


@mcp.tool(annotations=READ_ONLY)
async def get_issue(
    issue_id: Annotated[str, Field(description='Issue identifier such as "ENG-123"', min_length=3)],
) -> dict[str, Any]:
    """Get full detail for one issue: description, labels, and the 5 most recent comments.

    `issue_id` is the `identifier` field returned by search_issues (or create_issue),
    e.g. "ENG-123". A wrong identifier returns error.code NOT_FOUND (do not retry).
    """
    query = f"""
    query($id: String!) {{
      issue(id: $id) {{
        {ISSUE_FIELDS}
        description createdAt
        labels {{ nodes {{ name }} }}
        comments(first: 5) {{ nodes {{ body createdAt user {{ name }} }} }}
      }}
    }}"""
    try:
        data = await _get_client().query(query, {"id": issue_id})
    except ToolError as err:
        return err.to_dict()
    node = data.get("issue")
    if not node:
        return ToolError(
            "NOT_FOUND",
            f"No issue {issue_id!r}.",
            False,
            action="Get a valid identifier from search_issues.",
        ).to_dict()
    issue = _shape_issue(node)
    issue.update(
        description=node.get("description") or "",
        created_at=node.get("createdAt"),
        labels=[label["name"] for label in node["labels"]["nodes"]],
        recent_comments=[
            {"author": (c.get("user") or {}).get("name"), "body": c["body"], "created_at": c["createdAt"]}
            for c in node["comments"]["nodes"]
        ],
    )
    return {"ok": True, "issue": issue}


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
    )
)
async def create_issue(
    team_id: Annotated[str, Field(description="Team `id` (UUID) from list_teams; NOT the key", min_length=8)],
    title: Annotated[str, Field(min_length=1, max_length=255)],
    description: Annotated[str | None, Field(description="Markdown body", max_length=20000)] = None,
    priority: Literal["none", "urgent", "high", "medium", "low"] = "none",
    dry_run: Annotated[
        bool,
        Field(description="If true (default) nothing is created; returns a preview. Set false only after the user approves."),
    ] = True,
) -> dict[str, Any]:
    """Create a Linear issue. THIS WRITES TO THE USER'S WORKSPACE when dry_run=false.

    Two-step use: call with dry_run=true (default) to get a preview (the team name is
    resolved so you can confirm the target), show it to the user, then repeat the same call
    with dry_run=false. A real call is NOT idempotent: calling it twice creates two issues.
    `team_id` is the `id` from list_teams. On success returns `issue.identifier`, which
    works with get_issue.
    """
    try:
        teams = await _get_client().query("query { teams(first: 50) { nodes { id key name } } }")
        team = next((t for t in teams["teams"]["nodes"] if t["id"] == team_id), None)
        if team is None:
            return ToolError(
                "NOT_FOUND",
                f"team_id {team_id!r} matches no team.",
                False,
                action="Use the `id` (UUID) of a team returned by list_teams, not its key.",
            ).to_dict()
        payload = {"team": f"{team['name']} ({team['key']})", "title": title,
                   "description": description or "", "priority": priority}
        if dry_run:
            return {"ok": True, "dry_run": True, "would_create": payload,
                    "next": "Show this to the user; if approved, call again with dry_run=false."}
        mutation = f"""
        mutation($input: IssueCreateInput!) {{
          issueCreate(input: $input) {{ success issue {{ {ISSUE_FIELDS} }} }}
        }}"""
        inp: dict[str, Any] = {"teamId": team_id, "title": title, "priority": PRIORITY_TO_INT[priority]}
        if description:
            inp["description"] = description
        data = await _get_client().query(mutation, {"input": inp})
    except ToolError as err:
        return err.to_dict()
    result = data.get("issueCreate") or {}
    if not result.get("success") or not result.get("issue"):
        return ToolError("UPSTREAM_ERROR", "Linear reported the issue was not created.", True, 5).to_dict()
    return {"ok": True, "dry_run": False, "issue": _shape_issue(result["issue"])}


if __name__ == "__main__":
    mcp.run()  # stdio transport
