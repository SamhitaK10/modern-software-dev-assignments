# Week 2 Write-up

## Part I: The Server

**API chosen**, and why:
> Linear (GraphQL, OAuth 2.0). It has a real authorization-code flow with scopes, expiring access tokens and refresh tokens, and a natural read → read → write workflow (find an issue, inspect it, file a new one). Its `issues` query also returns a lot of fields an agent does not need, so output shaping matters.

**How to run it** (one command):
```
python week2/server.py        # stdio; run `python week2/login.py` once first to sign in
```
Setup: `pip install -r week2/requirements.txt`, create a Linear OAuth app (redirect URI `http://localhost:8765/callback`), `export LINEAR_CLIENT_ID=... LINEAR_CLIENT_SECRET=...`, run `python week2/login.py`.

| Tool | What it does | Read/Write | Composes with |
|---|---|---|---|
| `list_teams` | Lists teams: `{id, key, name}` | Read | `key` feeds `search_issues(team_key)`; `id` feeds `create_issue(team_id)` |
| `search_issues` | Filter by text, team, state type, assigned-to-me; shaped results | Read | Returns `identifier` that feeds `get_issue` |
| `get_issue` | Full detail: description, labels, 5 latest comments | Read | Takes `identifier` from `search_issues` / `create_issue` |
| `create_issue` | Creates an issue; `dry_run=true` by default | **Write** | Takes `team_id` from `list_teams`; returns an `identifier` for `get_issue` |

## Part II: Agent Ergonomics

| Decision | Where | Why |
|---|---|---|
| Schema-level constraint | `server.py:103-108` (`state_type` and `order_by` are `Literal`, `limit` is `ge=1, le=50`); `server.py:193` (`priority` is `Literal`) | Bad values are rejected by schema validation before any API call (`test_schema_rejects_bad_enum_before_hitting_linear`). The docstring and schema cannot disagree because the enum appears only in the schema. |
| Output shaping (fields kept vs. dropped) | `server.py:66-78` `_shape_issue` | Kept: identifier, title, state name/type, priority label, assignee name, team key, updated_at, url. Dropped: internal UUIDs, sort orders, avatars, and the rest of Linear's payload. Smaller responses, and the contract no longer depends on Linear's schema (`internalJunk` test). |
| Structured errors (retry vs. don't-retry) | `linear_client.py:17-37` `ToolError`; mapping in `linear_client.py` `_parse` | Every failure is `{"ok": false, "error": {code, message, retryable, retry_after_seconds?, action?}}`. `RATE_LIMITED` / `NETWORK_ERROR` / `UPSTREAM_ERROR` are retryable (with a wait); `NOT_FOUND`, `FORBIDDEN`, `INVALID_REQUEST`, `AUTH_REQUIRED` are not. No tool raises a traceback. |
| Docstring that chains tools together | `server.py:86-91` (`list_teams`), `server.py:110-115` (`search_issues`), `server.py:145-150` (`get_issue`), and `INSTRUCTIONS` at `server.py:22` | Each docstring says where its input ID comes from ("`identifier` returned by `search_issues`", "`id` (UUID) from `list_teams`, NOT the key"). `FastMCP(instructions=...)` gives the four-step workflow. |
| Brake on the write tool | `server.py:184-235` `create_issue` (docstring at `:199`) | `dry_run` defaults to `true`: the first call resolves the team name and returns a preview without writing. The agent has to repeat the call with `dry_run=false`. Annotations: `readOnlyHint=True` on the three readers, `readOnlyHint=False, destructiveHint=False, idempotentHint=False` on the writer. The docstring warns that a real call is not idempotent. |

**One thing you changed after watching the agent misuse a tool:**
> I have not made a code change in response to live use yet, so there is no before/after to report. What I did see in the live Claude Code session (2026-09-30):
>
> - **The `assigned_to_me` filter hid the only in-progress issue.** Asked "What are my in-progress issues?", the agent called `search_issues {"assigned_to_me": true, "state_type": "started", "limit": 50}` and got `count: 0`, then answered that there were no in-progress issues. The one in-progress issue, SAM-5, has `assignee: null`. So the answer was correct for issues assigned to me, but the user meant issues in their own workspace. The agent had to be told the team key (`SAM`) before it found SAM-5. A possible fix, not yet made: when `assigned_to_me=true` returns nothing, add a hint to the result saying unassigned issues were excluded.
> - **Two Linear servers were registered, and the agent picked the other one first.** The claude.ai Linear connector and this local server were both active. The agent went to the claude.ai connector first, which returned nothing, and only then tried this server. For the demonstration below, the agent was told to use only the local server.
> - **The NOT_FOUND recovery hint was followed only when asked.** When `get_issue {"issue_id": "ENG-9999"}` returned `NOT_FOUND` earlier in the session, the agent did not retry, which is correct. But it asked the user what to do next instead of following the `action` field's advice to call `search_issues`. In the demonstration below, where it was told to follow the recovery guidance, it did.
> - **Validation from an earlier change:** `create_issue` needs the team's UUID, not its key. Before any live use, I made it look the team up first and return `NOT_FOUND` with `action: "Use the id (UUID) of a team returned by list_teams, not its key."` (`test_bad_team_id_points_agent_at_list_teams`). In the live run, the agent passed the UUID from `list_teams`, so this path was not triggered.

## Part III: OAuth

**Flow**: how a token is obtained, cached, and refreshed:
> `login.py` is the only place a browser is opened. It builds the authorize URL with PKCE (S256) and a random `state`, catches the redirect on `localhost:8765`, verifies `state`, and calls `TokenStore.exchange_code` (`auth.py`). The token (access, refresh, absolute `expires_at`) is cached at `~/.config/linear-mcp/token.json` (mode 0600, outside the repo; written atomically). In the server, `TokenStore.get_access_token` refreshes silently when the token is within 60 s of expiry (`auth.py:63`, `auth.py:216-226`), persists the rotated refresh token, and is guarded by an `asyncio.Lock` so concurrent tool calls trigger one refresh. A 401 from the API forces one refresh and one retry (`linear_client.py:65-72`).

**Scopes requested**, and why each is necessary:
> `read`: needed by `list_teams`, `search_issues`, `get_issue`. `issues:create`: needed by `create_issue` only. It is deliberately narrower than `write` (cannot edit or delete issues, comment, or touch projects) and `admin` is not requested. `test_authorize_url_uses_minimal_scopes_and_pkce` pins this (`auth.py:35`).

**Secrets**: what's in env, what's gitignored:
> Env: `LINEAR_CLIENT_ID`, `LINEAR_CLIENT_SECRET` (optional with PKCE), optionally `LINEAR_MCP_TOKEN_PATH`, `LINEAR_REDIRECT_URI`. Committed: `.mcp.json.example` only, which references `${VAR}` placeholders. Gitignored (root `.gitignore`): `.mcp.json`, `week2/.mcp.json`, `token.json`. The token cache lives in `~/.config`, never in the repo.

**Token dies mid-session**: what the agent sees:
> If refresh succeeds the agent notices nothing. If the refresh token is revoked or there is no token, the server never opens a browser; the tool returns `{"ok": false, "error": {"code": "AUTH_REQUIRED", "retryable": false, "action": "Tell the user to run `python week2/login.py` in a terminal, then retry."}}` (`test_dead_refresh_token_gives_actionable_auth_error_no_browser`, which also fails if `webbrowser.open` is called). A network blip during refresh is reported as retryable `NETWORK_ERROR`, not as an auth failure.

## Part IV: Integration

**Registration config** (`.mcp.json.example`) and the client you used:
> `week2/.mcp.json.example` (client: Claude Code). To use it: copy to `.mcp.json` at the repo root (gitignored) or run `claude mcp add linear -e LINEAR_CLIENT_ID=$LINEAR_CLIENT_ID -e LINEAR_CLIENT_SECRET=$LINEAR_CLIENT_SECRET -- python week2/server.py`.

**End-to-end transcript**: the prompt, the tools that fired with their arguments, the result:
```
Source: live Claude Code session, 2026-09-30, local `linear` MCP server (python week2/server.py)
against the real Linear workspace. The claude.ai Linear connector was not used in this run.

Prompt: "Call list_teams, then search_issues for team SAM with state_type="started", then
get_issue using the returned identifier. Report the latest comment."

1. mcp__linear__list_teams {}
   -> {"ok": true, "teams": [{"id": "86772644-c2ab-4272-986c-657d8a2c8a39",
       "key": "SAM", "name": "Samhita"}]}
2. mcp__linear__search_issues {"team_key": "SAM", "state_type": "started"}   # key from step 1
   -> {"ok": true, "count": 1, "issues": [{"identifier": "SAM-5",
       "title": "Week 2 MCP live demonstration", "state": "In Progress",
       "state_type": "started", "priority": "No priority", "assignee": null,
       "team_key": "SAM", "updated_at": "2026-09-30T23:10:33.896Z",
       "url": "https://linear.app/samhitak10/issue/SAM-5/week-2-mcp-live-demonstration"}]}
3. mcp__linear__get_issue {"issue_id": "SAM-5"}                   # identifier from step 2
   -> {"ok": true, "issue": {"identifier": "SAM-5", "title": "Week 2 MCP live demonstration",
       "state": "In Progress", "assignee": null, "labels": [],
       "description": "Next actions:

- [ ] Create two sample issues. ... (7-item checklist)",
       "created_at": "2026-09-30T23:09:06.931Z",
       "recent_comments": [{"author": "samhitakondareddy@gmail.com",
         "body": "Ready to test the local MCP tool chain. Next step: capture search_issues
                  followed by get_issue.",
         "created_at": "2026-09-30T23:10:39.694Z"}]}}

Answer: SAM-5 "Week 2 MCP live demonstration" is In Progress (no priority, unassigned).
Its latest comment (2026-09-30 23:10 UTC, samhitakondareddy@gmail.com): "Ready to test the
local MCP tool chain. Next step: capture search_issues followed by get_issue."
```

**Write tool brake (dry run, declined)**:
```
Prompt: "Call create_issue with team_id="86772644-c2ab-4272-986c-657d8a2c8a39",
title="MCP test", and dry_run=true. Show the preview. I decline creation. Never call dry_run=false."

mcp__linear__create_issue {"team_id": "86772644-c2ab-4272-986c-657d8a2c8a39",
                           "title": "MCP test", "dry_run": true}   # team_id from list_teams above
-> {"ok": true, "dry_run": true,
    "would_create": {"team": "Samhita (SAM)", "title": "MCP test", "description": "",
                     "priority": "none"},
    "next": "Show this to the user; if approved, call again with dry_run=false."}

Agent: showed the preview. Because the user declined, it did not call dry_run=false,
and no issue was created. A later search_issues for team SAM still returned SAM-1 through SAM-6.
```

**A failure, handled**: what you provoked, what the agent saw, what it did next:
```
Source: same live session, local server, real Linear workspace.

Prompt: "Call get_issue for SAM-9999. Explain the returned error and follow its recovery guidance."

1. mcp__linear__get_issue {"issue_id": "SAM-9999"}           # nonexistent ID, real team key
   -> {"ok": false, "error": {"code": "NOT_FOUND", "message": "Entity not found: Issue",
       "retryable": false,
       "action": "Do not retry with the same ID. Get a valid ID from search_issues or list_teams."}}

Agent's explanation: Linear has no SAM-9999. retryable=false means repeating the call cannot
succeed, so the agent did not retry it. It followed `action` and searched for valid IDs.

2. mcp__linear__search_issues {"team_key": "SAM", "limit": 10}
   -> {"ok": true, "count": 6, "issues": [SAM-5 "Week 2 MCP live demonstration" (In Progress),
       SAM-6 "Week 2 writeup and submission" (Backlog), SAM-3, SAM-1, SAM-4, SAM-2 (Todo)]}

Result: the agent reported that the valid identifiers are SAM-1 to SAM-6, with no tracebacks
and no retry loop. Tests also cover other failures: 429 -> retryable RATE_LIMITED after 42s;
revoked refresh token -> AUTH_REQUIRED.
```

**Protocol-level test**: what it covers and how to run it:
> `tests/test_protocol.py` drives the server only through an MCP client session (`fastmcp.Client`): tool listing, annotations and schema constraints, the search→get chain, dry-run vs. real create, NOT_FOUND / rate-limit / token-refresh / dead-refresh-token paths, and one test that spawns `python week2/server.py` as a real stdio subprocess. Linear is faked with `httpx.MockTransport`. Run: `pip install -r week2/requirements.txt && cd week2 && python -m pytest -q` (13 tests).


## Submission
1. `Command (⌘) + F` for the to-do marker. No results means you're done.
2. Confirm no tokens, client secrets, cached token file, or real `.mcp.json` are committed.
3. Push all changes to your remote repository and submit via Gradescope.
4. Clean up (optional): remove the server from your agent config, delete your cached token, and revoke the OAuth app's access.
