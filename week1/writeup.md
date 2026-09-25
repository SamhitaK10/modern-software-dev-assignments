# Week 1 Write-up

## Part I: Capture

**Setup** (enough for a reader to reproduce your capture):
```
claude --version: 2.1.42 (Claude Code) 
mitmproxy version: 12.2.3
proxy command:     mitmweb --listen-host 127.0.0.1 --listen-port 58888 --web-open-browser --mode reverse:https://api.anthropic.com -w session2.flows
settings file:     C:\Users\samhi\claude-trace-test\.claude\settings.json
```
```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:58888",
    "ENABLE_TOOL_SEARCH": "true"
  }
}
```
**The session.** What task, against what repo, and how many `POST /v1/messages` requests did it produce?
> I used a scratch repo called `claude-trace-test`. I asked Claude Code to plan first, fix the divide-by-zero bug in `calculator.py`, add regression tests in `test_calculator.py`, and rerun the tests. The session produced 12 successful `POST /v1/messages` requests. Two of them were extra naming requests from Claude Code, not part of the actual coding work.


| Requirement | Evidence |
|---|---|
| Touched ≥ 2 files | Request 10 changed both `calculator.py` and `test_calculator.py`. |
| Failed at least once | Request 9 ran pytest and got `1 failed, 2 passed` because `divide(10, 0)` raised `ZeroDivisionError`.  |
| Long enough to plan | Request 4 entered plan mode, Request 6 had the written plan, and Request 8 exited plan mode before the code changes. |
| Your own repo | The environment section shows the working directory was my `claude-trace-test` git repo.|

**What you redacted** from the excerpts quoted below, and why:
> I redacted my email and account/session identifiers. I also left out any authentication headers or credentials. I only kept the parts of the trace needed to explain what Claude did.


## Part II: System Prompt Annotation

**a. Structure.** Major sections in order, one line each on what it does, and why this order.
> The prompt starts with Claude Code’s role and safety rules. Next, the `Harness` section explains how tools, permissions, pasted content, and system messages work. After that come coding and action rules, session-specific guidance, memory instructions, environment information, context management, and browser automation. The general rules come first, while the more specific runtime and tool instructions come later.

**b. Tone and verbosity.** Quote the controlling instructions, then say what failure mode they defend against.
```
"When you have enough information to act, act."

"Do not re-derive facts already established in the conversation, re-litigate a decision the user has already made, or narrate options you will not pursue."

"If you are weighing a choice, give a recommendation, not an exhaustive survey."
```
> These instructions are meant to stop Claude from being too wordy, repeating earlier reasoning, or listing unnecessary options instead of moving the task forward.

**c. When not to act.** Quote the destructive-operation gates, scope limits, or refusal conditions, and what each buys.
```
"For actions that are hard to reverse or outward-facing, confirm first unless durably authorized or explicitly told to proceed without asking."

"Before deleting or overwriting, look at the target."

"Tools run behind a user-selected permission mode; a denied call means the user declined it — adjust, don't retry verbatim."

"Refuse requests for destructive techniques, DoS attacks, mass targeting, supply chain compromise, or detection evasion for malicious purposes."

```
> These rules keep Claude from making irreversible changes without checking, repeatedly trying actions the user already denied, or helping with clearly harmful requests. They add safety boundaries around destructive, external, and high-risk actions.

**d. Environment context.** What the agent is told about machine/repo/session, and where it lives in the request (`system` field or a `role: "system"` message).
> The agent is told the working directory, that it is a git repo, the operating system, that PowerShell is the main shell, that Bash is also available, the scratchpad directory, the model being used, and which tools are available or deferred. This information appears in a `role: "system"` message under the `# Environment` section.

**e. `<system-reminder>`.** Where they appear (cite an example), two distinct purposes you can evidence, and why they are injected mid-conversation rather than stated once.
```
Example: In Request 2, `<system-reminder>` appears inside the user message content and provides the current git status and commit-attribution instructions.
```
> One purpose is to inject changing session context, like the current git branch and untracked files. Another is to add behavior rules that matter for later actions, like how commits should be attributed. They are injected during the conversation because some of this information changes over time or only becomes relevant at certain points, so putting it all once at the start could make it stale or unnecessary.


## Part III: Tool Design Annotation

**Inventory.** Did the set change across requests? If so, what triggered it?

| Built-in | MCP | Deferred | **Total** | Changed mid-session? |
|---|---|---|---|---|
| 16 | 3 | 285 | **304** | Yes |

The set changed when Claude used ToolSearch to load EnterPlanMode and ExitPlanMode, which were initially deferred.

**Two tools.** Pick tools that differ from each other.

| | Tool 1 | Tool 2 |
|---|---|---|
| Name | Bash | EnterPlanMode |
| Key schema fields | `command`: string (required); `timeout`: number; `description`: string; `run_in_background`: boolean; `dangerouslyDisableSandbox`: boolean | `type`: object; `properties`: {} |
| Required vs. optional vs. not exposed, and why | `command` is required because Bash needs a command to run. `timeout`, `description`, `run_in_background`, and `dangerouslyDisableSandbox` are optional because they only change how the command runs. Lower-level runtime details are not exposed because the harness controls those. | There are no required or optional parameters because the tool only changes Claude into plan mode. The details of plan mode are handled by the runtime instead of being exposed as options. |
| Description is defending against… (quote + the wrong behavior) | "This tool runs Git Bash (POSIX sh), not cmd.exe or PowerShell."

This prevents Claude from using PowerShell or Windows command syntax inside the Bash tool. | "Prefer using EnterPlanMode for implementation tasks unless they're simple."

This prevents Claude from jumping straight into implementation on a task that should be planned first. |
| Deliberately does *not* do… and what that implies | Bash does not decide whether a command is safe or allowed. It only executes commands, while permissions and safety checks are handled by the surrounding system. | EnterPlanMode does not edit files or implement the solution. It only changes Claude into planning mode, which shows that planning and execution are handled as separate stages. |

Why these two?
> I chose these because they do very different things. Bash directly runs commands on the machine, while EnterPlanMode controls the agent’s workflow before implementation starts.


## Part IV: Behavioral Analysis

**Every answer must be labeled `[OBSERVED]` or `[INFERRED]` and cite its evidence. Unlabeled answers earn no credit.**

**a. Error recovery**: [OBSERVED] · evidence: Request 9, messages 17–18, PowerShell test run and failing result; Request 10, messages 20–21, Edit calls and results; Request 11, messages 23–24, PowerShell rerun and passing result

What the agent saw, verbatim:
```
test_calculator.py::test_divide_by_zero FAILED

ZeroDivisionError: division by zero

1 failed, 2 passed
```
What it tried next, and turns to recover:
> Claude confirmed the failure, edited `calculator.py` to return `None` when dividing by zero, added regression tests in `test_calculator.py`, and then reran the full test suite. Recovery took two action turns: one turn for the edits and one turn for verification.

**b. Planning**: [OBSERVED] · evidence: Request 2, message 0 user prompt; Request 4, messages 5–6, EnterPlanMode call and result; Request 6, messages 11–12, Write call and plan-file result; Request 8, messages 14–16, ExitPlanMode call, approval result, and exited-plan-mode system message
> Planning came from both the user prompt and Claude Code's plan-mode tools. The prompt explicitly told Claude to enter plan mode before making changes. Claude then used EnterPlanMode, wrote a step-by-step plan, and exited plan mode before editing the files. This shows the planning was not just emergent behavior.

**c. Plans and task state**: [OBSERVED] · evidence: Request 4, messages 5–7, EnterPlanMode and plan-mode system state; Request 6, messages 11–12, Write call creating the plan file; Request 8, messages 14–16, ExitPlanMode, approved plan, and exited-plan-mode state
How does one get created and advanced? What does the model see about task state each turn, and where does it live in the request:
> The plan is created after Claude enters plan mode and writes out the implementation steps. The model then sees system messages showing whether plan mode is active or finished, and later requests include the approved plan in the message history. In this session there was no separate task-list tool being updated, so the main task state came from the plan file and the plan-mode system messages.

**d. Subagents**: [INFERRED] · evidence: Request 2, message 0 session context and Agent tool definition; Requests 2–12 contain no Agent tool call
When the agent delegates, what the subagent is told, and what comes back:
> The Agent tool says Claude should only spawn a subagent when the user explicitly asks for one or names an agent type. A fresh subagent receives its task through the Agent tool's prompt field, while a fork inherits the conversation context. Its final report comes back to the parent agent, which relays the relevant result to the user. No subagent was actually launched in this session.

**e. Context management**: [OBSERVED] · evidence: Request 2, messages 0–1 contain the initial user and environment context; Request 12, messages 0–26 replay the earlier plan-mode calls, pytest failure, edits, and successful rerun
What changed in the payloads as the session grew:
> As the session grew, later requests included more of the earlier conversation and tool history. The model kept seeing the plan, test failure, edits, and later test results in the messages array. The system prompt also says long conversations can be summarized when context gets too large, but that did not happen in this short session.


## Part V: Reflection

**Two decisions you would copy**, and the problem each solves:
1. I would copy the explicit plan mode step because it forces the agent to think through the task before editing files, which reduces rushed or unnecessary changes.
2. I would copy the practice of rerunning the full test suite after the fix because it verifies that the change actually worked and did not break anything else.

**One you would make differently** (engage with why it might be there):
> I would make the available tool set smaller for a simple local coding task. The large deferred tool list is useful because Claude can load extra capabilities when needed, but most of those tools were unrelated to fixing two Python files and added extra clutter to the request.

**One thing the trace changed** about how you will steer a coding agent:
> The trace changed how I would prompt a coding agent by making me more specific about checkpoints. Instead of only asking it to fix a bug, I would tell it to plan first, reproduce the failure, make the change, and verify the result with tests.


## Submission
1. `Command (⌘) + F` for `TODO`. No results means you're done.
2. Confirm no credentials or `x-api-key` headers made it into your quoted excerpts.
3. Push all changes to your remote repository and submit via Gradescope.
4. Don't forget to remove `ANTHROPIC_BASE_URL` from your repo's `.claude/settings.json`!
