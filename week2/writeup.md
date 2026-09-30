# Week 2 Write-up

## Part I: The Server

**API chosen**, and why:
> Linear (GraphQL, OAuth 2.0). It has a real authorization-code flow with scopes, expiring access tokens and refresh tokens, and a natural read → read → write workflow (find an issue, inspect it, file a new one). Its `issues` query also returns a lot of fields an agent does not need, so output shaping matters.

**How to run it** (one command, stdio):
```powershell
python week2/server.py
```
One-time setup (Windows PowerShell, from the repo root). First create a Linear OAuth app with the redirect URI `http://localhost:8765/callback`, then:
```powershell
pip install -r week2/requirements.txt
$env:LINEAR_CLIENT_ID = "<client id from the Linear OAuth app>"
$env:LINEAR_CLIENT_SECRET = "<client secret>"   # optional with PKCE
python week2/login.py                           # opens the browser once and caches the token
```

| Tool | What it does | Read/Write | Composes with |
|---|---|---|---|
| `list_teams` | Lists teams: `{id, key, name}` | Read | `key` feeds `search_issues(team_key)`; `id` feeds `create_issue(team_id)` |
| `search_issues` | Filter by text, team, state type, assigned-to-me; shaped results | Read | Returns `identifier` that feeds `get_issue` |
| `get_issue` | Full detail: description, labels, 5 latest comments | Read | Takes `identifier` from `search_issues` / `create_issue` |
| `create_issue` | Creates an issue; `dry_run=true` by default | **Write** | Takes `team_id` from `list_teams`; returns an `identifier` for `get_issue` |

## Part II: Agent Ergonomics

| Decision | Where | Why |
|---|---|---|
| Schema-level constraint | `server.py:107-113` (`state_type` and `order_by` are `Literal`, `limit` is `ge=1, le=50`); `server.py:202` (`priority` is `Literal`) | Bad values are rejected by schema validation before any API call (`test_schema_rejects_bad_enum_before_hitting_linear`). The allowed values are listed only in the schema, not repeated in the docstrings. |
| Output shaping (fields kept vs. dropped) | `server.py:71-83` `_shape_issue` | Kept: identifier, title, state name/type, priority label, assignee name, team key, updated_at, url. Dropped: internal UUIDs, sort orders, avatars, and the rest of Linear's payload. Responses are smaller, and the agent receives a stable subset of Linear's fields. The `internalJunk` assertion in `test_end_to_end_chain_search_then_get` checks that extra fields are dropped. |
| Structured errors (retry vs. don't-retry) | `linear_client.py:17-37` `ToolError`; mapping in `linear_client.py` `_parse` | Every failure is `{"ok": false, "error": {code, message, retryable, retry_after_seconds?, action?}}`. `RATE_LIMITED` / `NETWORK_ERROR` / `UPSTREAM_ERROR` are retryable (with a wait); `NOT_FOUND`, `FORBIDDEN`, `INVALID_REQUEST`, `AUTH_REQUIRED` are not. API failures handled by the client return structured errors. |
| Docstring that chains tools together | `server.py:91-95` (`list_teams`), `server.py:115-121` (`search_issues`), `server.py:154-158` (`get_issue`), and `INSTRUCTIONS` at `server.py:22` | Each docstring says where its input ID comes from ("`identifier` returned by `search_issues`", "`id` (UUID) from `list_teams`, NOT the key"). `FastMCP(instructions=...)` gives the four-step workflow. |
| Brake on the write tool | `server.py:198-248` `create_issue` (docstring at `:208`) | `dry_run` defaults to `true`: the first call resolves the team name and returns a preview without writing. The agent has to repeat the call with `dry_run=false`. Annotations: `readOnlyHint=True` on the three readers, `readOnlyHint=False, destructiveHint=False, idempotentHint=False` on the writer. The docstring warns that a real call is not idempotent. |

**One thing you changed after watching the agent misuse a tool:**

*What I observed (live Claude Code session, 2026-09-30).* When asked "What are my in-progress issues?", the agent called `search_issues {"assigned_to_me": true, "state_type": "started", "limit": 50}`, got `count: 0`, and reported that there were no in-progress issues. It did not consider unassigned issues. The only in-progress issue, SAM-5, has `assignee: null`, so a search with `assigned_to_me=true` can never return it. There was also a timing caveat: a follow-up `search_issues {"state_type": "started", "limit": 10}` with no assignee filter also returned 0 issues. That suggests SAM-5 had not yet been moved to In Progress (it was created at 23:09 and last updated at 23:10 UTC). So the empty answer can't be blamed on the filter alone. The underlying problem is still real: an empty `assigned_to_me` result gives the agent no sign that unassigned issues exist.

Other things I saw in the same session:
- The claude.ai Linear connector was also registered, and the agent tried it before this server.
- On `get_issue ENG-9999` → `NOT_FOUND`, the agent correctly did not retry. But it asked the user what to do instead of following the `action` field. When later told to follow the recovery guidance for SAM-9999, it did.

*What I changed.* When `assigned_to_me=true` matches nothing, `search_issues` now keeps the usual `{ok, count, issues}` fields and adds a `hint` field (`server.py:56-59`, `server.py:145-146`; the docstring mentions it at `server.py:119-120`):
> "No issues matched these filters. Issues without an assignee were excluded. To include them, search again with assigned_to_me=false."

*How it's validated.* `test_empty_assigned_to_me_search_hints_at_unassigned_issues` (`tests/test_protocol.py:129`) goes through an MCP client session. It checks that an empty `assigned_to_me` search returns `count: 0`, `issues: []` and the exact hint, and that a search without `assigned_to_me` has no hint. I have not re-run the live agent session with this change, so its effect on the agent's behavior is not yet observed.

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
> `week2/.mcp.json.example` (client: Claude Code). The server was registered in Claude Code as `linear`, and its tools appear as `mcp__linear__*`. The committed file contains only `${LINEAR_CLIENT_ID}` / `${LINEAR_CLIENT_SECRET}` placeholders. To register it (Windows PowerShell, repo root), use either option:
```powershell
Copy-Item week2/.mcp.json.example .mcp.json      # .mcp.json is gitignored
# or
claude mcp add linear -e LINEAR_CLIENT_ID=$env:LINEAR_CLIENT_ID -e LINEAR_CLIENT_SECRET=$env:LINEAR_CLIENT_SECRET -- python week2/server.py
```

**End-to-end transcript**: the prompt, the tools that fired with their arguments, the result.
Source: live Claude Code session on 2026-09-30, using the local `linear` server against the real Linear workspace. The claude.ai Linear connector was not used. Results marked *abridged output* are shortened and written as plain text; all others are the captured results, verbatim.

Prompt: *"Call list_teams, then search_issues for team SAM with state_type="started", then get_issue using the returned identifier. Report the latest comment."*

1. `mcp__linear__list_teams` with arguments `{}`
   ```json
   {"ok": true, "teams": [{"id": "86772644-c2ab-4272-986c-657d8a2c8a39", "key": "SAM", "name": "Samhita"}]}
   ```
2. `mcp__linear__search_issues` with arguments `{"team_key": "SAM", "state_type": "started"}` (key from step 1)
   ```json
   {"ok": true, "count": 1, "issues": [{"identifier": "SAM-5", "title": "Week 2 MCP live demonstration",
     "state": "In Progress", "state_type": "started", "priority": "No priority", "assignee": null,
     "team_key": "SAM", "updated_at": "2026-09-30T23:10:33.896Z",
     "url": "https://linear.app/samhitak10/issue/SAM-5/week-2-mcp-live-demonstration"}]}
   ```
3. `mcp__linear__get_issue` with arguments `{"issue_id": "SAM-5"}` (identifier from step 2). *Abridged output:*
   ```text
   ok: true
   issue: SAM-5 "Week 2 MCP live demonstration", In Progress (started), No priority, assignee null,
          labels [], created 2026-09-30T23:09:06.931Z
   description: "Next actions:" followed by a 7-item unchecked checklist (create sample issues,
          capture the list_teams → search_issues → get_issue chain, capture a nonexistent-issue
          failure, capture create_issue with dry_run=true, ...)
   recent_comments (1):
     author: samhitakondareddy@gmail.com   created_at: 2026-09-30T23:10:39.694Z
     body: "Ready to test the local MCP tool chain. Next step: capture search_issues followed by get_issue."
   ```

Answer: SAM-5 "Week 2 MCP live demonstration" is In Progress (no priority, unassigned). Its latest comment, from samhitakondareddy@gmail.com at 23:10 UTC on 2026-09-30, reads: "Ready to test the local MCP tool chain. Next step: capture search_issues followed by get_issue."

**Write tool brake (dry run, declined)**

Prompt: *"Call create_issue with team_id="86772644-c2ab-4272-986c-657d8a2c8a39", title="MCP test", and dry_run=true. Show the preview. I decline creation. Never call dry_run=false."*

`mcp__linear__create_issue` with arguments `{"team_id": "86772644-c2ab-4272-986c-657d8a2c8a39", "title": "MCP test", "dry_run": true}`
```json
{"ok": true, "dry_run": true,
 "would_create": {"team": "Samhita (SAM)", "title": "MCP test", "description": "", "priority": "none"},
 "next": "Show this to the user; if approved, call again with dry_run=false."}
```
The agent showed the preview and, because the user declined, did not make the `dry_run=false` call. No issue was created.

**A failure, handled**: what you provoked, what the agent saw, what it did next.

Prompt: *"Call get_issue for SAM-9999. Explain the returned error and follow its recovery guidance."*

1. `mcp__linear__get_issue` with arguments `{"issue_id": "SAM-9999"}` (a nonexistent ID with the real team key)
   ```json
   {"ok": false, "error": {"code": "NOT_FOUND", "message": "Entity not found: Issue", "retryable": false,
     "action": "Do not retry with the same ID. Get a valid ID from search_issues or list_teams."}}
   ```
   The agent explained that the issue doesn't exist and that `retryable: false` means repeating the call can't succeed. It did not retry, and followed `action` instead.
2. `mcp__linear__search_issues` with arguments `{"team_key": "SAM", "limit": 10}`. *Abridged output:*
   ```text
   ok: true, count: 6
   SAM-5  Week 2 MCP live demonstration   In Progress (started)
   SAM-6  Week 2 writeup and submission   Backlog
   SAM-3  Import your data                Todo
   SAM-1  Get familiar with Linear        Todo
   SAM-4  Set up your teams               Todo
   SAM-2  Connect your tools              Todo
   (all: No priority, assignee null, team_key SAM)
   ```

Result: the agent reported that SAM-1 through SAM-6 are the valid identifiers. There was no retry loop and no traceback. The tests cover the other error paths: HTTP 429 becomes a retryable `RATE_LIMITED` error with a 42 s wait, and a revoked refresh token becomes `AUTH_REQUIRED`.

**Protocol-level test**: what it covers and how to run it:
> `tests/test_protocol.py` drives the server only through an MCP client session (`fastmcp.Client`): tool listing, annotations and schema constraints, the search→get chain, dry-run vs. real create, NOT_FOUND / rate-limit / token-refresh / dead-refresh-token paths, and one test that spawns `python week2/server.py` as a real stdio subprocess. It also covers the empty-`assigned_to_me` hint. In these tests Linear is faked with `httpx.MockTransport`, so no network access or credentials are needed.
```powershell
cd week2
python -m pytest -q
```
Result on 2026-09-30: `14 passed in 3.24s`.


## Submission
1. Confirm that no tokens, client secrets, cached token file, or real `.mcp.json` are committed.
2. Push all changes to the remote repository.
3. Add `mihail911`, `isaackann`, and `vdaita` as collaborators on the GitHub repository.
4. Submit via Gradescope.
5. Optional cleanup: remove the server from the agent config (`claude mcp remove linear`), delete the cached token (`~/.config/linear-mcp/token.json`), and revoke the OAuth app's access in Linear's settings.
