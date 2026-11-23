# Out-of-scope write approval and `linear_query` — design

Linear: RET-119

Status: approved in the RET-119 thread (Q1–Q4 and design sections 1–2), 2026-11-23.
Sub-project A of the RET-118 umbrella; B, C and D build on the approval path defined here.

## Goal

A write tool aimed at an issue outside the session's write scope asks a human in the
Linear thread instead of refusing, and writes only after an explicit approval. One
approval covers exactly one call: a later call with any other argument asks again.
In-scope writes (the session ticket, its descendants, issues the app created from it)
need no prompt, as today. A new `linear_query` tool runs read-only GraphQL as the app.

Constraints: public Hermes plugin API only, no core changes; tools never raise; every
write is attributed to the app.

## Decisions

| # | Question | Decision |
|---|----------|----------|
| Q1 | Where the scope decision runs | In the tool handler. It walks the scope once on the adapter loop and, for an out-of-scope target, calls Hermes's `tools.approval.request_tool_approval` itself from the tool thread (the function the `pre_tool_call` `approve` directive calls). No hook I/O, no hook→handler token, no check/write gap. |
| Q2 | What one approval covers | **Approve once / Deny** only. `rule_key = <tool>:<digest>`; no session grain. A deferred answer grants one re-run bound to the argument digest. |
| Q3 | Default of `out_of_scope_writes` | `approve`. |
| Q4 | `linear_query` response cap | Refuse when the serialized `data` exceeds 50,000 chars (`RESPONSE_TOO_LARGE`). |

Coverage (proposed with Q3, accepted):

- **Gated:** `linear_comment` (`id`), `linear_issue_update` (`id` and a new `parent`),
  `linear_issue_create` (`parent`). An issue created under an approved out-of-scope
  parent is recorded as app-created (RET-110) and so is in scope afterwards.
- **Scope-only, refused without a prompt:** `linear_sync_spec`, `linear_sync_plan`,
  `linear_task_state`. They are workflow tools for the session's own tree.
- **No Linear session** (e.g. a Slack session calling `linear_comment`): still
  `NOT_LINEAR_SESSION`, before any gate. A Slack approval must never authorize a Linear
  write ("answers only count in the ticket thread").
- **No new hard-deny list.** The typed tools cannot reach destructive mutations; the
  deny list comes with D's generic mutate.

## 1. Configuration

`out_of_scope_writes: approve | deny | allow` in the plugin config
(`plugins.entries.linear.config` / `ctx.get_config`), read into `Settings`:

| Value | Out-of-scope target in a Linear session |
|-------|-----------------------------------------|
| `approve` (default) | Ask a human (§2). |
| `deny` | Refuse `OUT_OF_SCOPE` (the behaviour before RET-119). |
| `allow` | Write without a prompt. Trusted single-user setups only. |

The value is case-insensitive and trimmed; anything else is a `SettingsError` naming
`out_of_scope_writes`. The handler reads the mode from the owning adapter's settings at
call time.

## 2. The approval path

### 2.1 Two passes

`_tool(..., gated=True)` wraps the gated bodies. Each call gets a fresh `Gate` object on
its `ToolContext`.

1. **Pass 1 (adapter loop).** The body runs as today. In a Linear session `_writable(ref)`
   resolves the issue and runs `check_write_scope`. If the target is out of scope it no
   longer raises: it records `{uuid, identifier, title}` on the gate and returns the
   issue. Each gated body calls `gate.check()` immediately before its first Linear write.
   If the gate recorded anything, `check()` raises `_NeedsApproval(targets)`; nothing has
   been written. Every gated body already does all scope checks before any write; the
   update body is the one whose order is checked by a test (`id` and `parent` both
   checked before `check()`).
   - A scope *error* (a `LinearError` code from the walk, `NOT_LINEAR_SESSION`) still
     refuses at once; only the plain `OUT_OF_SCOPE` verdict is gateable.
   - Non-gated tools (sync, task_state) keep raising `OUT_OF_SCOPE`.
2. **Mode (tool thread).** On `_NeedsApproval`:
   - `deny`: return `OUT_OF_SCOPE`.
   - `allow`: go to pass 2.
   - `approve`: call `request_tool_approval(tool, message, rule_key=f"{tool}:{digest}")`
     (§2.2) on the tool thread. It blocks until the human answers or Hermes's approval
     timeout ends the wait. Approved → pass 2. Otherwise → §2.4.
3. **Pass 2 (adapter loop).** The body re-runs from the start with
   `approved = {target uuids}`. `_writable` accepts an out-of-scope issue whose UUID is in
   `approved`. A target that is out of scope and not approved (the issue moved between
   the passes, or a reference resolved to another issue) is refused `OUT_OF_SCOPE`:
   no second prompt, no loop.

Pass 1 only reads, so running it twice costs one extra round of reads, and only for
out-of-scope calls. In-scope calls are a single pass, exactly as today.

### 2.2 Digest, message and rule key

- `digest` = first 16 hex chars of `sha256` over the canonical JSON
  (`sort_keys=True, separators=(",", ":"), ensure_ascii=False`) of
  `{"tool": <name>, "args": <parsed args>, "targets": <sorted target UUIDs>}`.
  Identical arguments give the identical digest; any other argument (body, title,
  target) gives a new one.
- `message` (Hermes's `description`, shown in the Linear select):
  - the tool name and what it will do, e.g. ``linear_comment on RET-50 "Title"``;
  - each target as identifier and title;
  - a preview of the parsed args, with long strings (bodies, descriptions) cut at 1,500
    chars and marked as cut;
  - a last line `digest: <hex>`.
- `rule_key = f"{tool}:{digest}"`. Even if a `session` answer slipped through, it would
  only cover the identical call.

### 2.3 Outbound: tool approvals

Hermes names the prompt's `command` `<tool> (plugin approval rule)`. A prompt whose
command matches `^<linear_[a-z_]+> \(plugin approval rule\)$` and whose description ends
in a `digest: <hex>` line is a **tool approval**:

- Its choices are narrowed to `once` and `deny` (no `session`).
- Its grant key is `f"{command}#{digest}"`, used both when the answer stores the grant
  (`put_grant`) and when the re-run's prompt looks it up (`_use_grant`). The control row
  keeps the plain command; the key is derived from command + description, so no store
  migration is needed.
- The answer turn reads
  ``Answer to approval for `<tool>` call <digest>: approved (once). Re-run the same call with identical arguments.``
  (or `: denied`). Shell approvals keep ``Answer to approval for `<cmd>`: …``.
- Everything else about grants stays: one use, bound to the session key, the answering
  actor (checked against the asking-time authority), and the answer turn's mid; dropped
  when that turn is freed without reaching Hermes, and on restart.

A tool-approval prompt without a parseable digest is treated as a normal approval (no
grant on answer; the re-run asks again). Fail-closed.

### 2.4 Results

| Outcome | Tool result |
|---------|-------------|
| Approved (or `allow`) | The tool's normal result. |
| Denied by the human, or Hermes refused (e.g. a subagent: no human present) | `{"ok": false, "error": "APPROVAL_DENIED", "message": <Hermes's message>}` |
| Hermes timed out and the plugin kept the question pending (an `approval` control row in state `pending` for this turn's mid whose description carries this digest) | `{"ok": false, "error": "APPROVAL_PENDING", "message": "Not run: approval is pending in Linear. End this turn. If it is approved, the next message says so and re-running this same call with identical arguments is pre-approved once."}` |
| Timed out and no pending row (the question expired) | `APPROVAL_DENIED` with Hermes's timeout message. |
| The gate raised | `APPROVAL_FAILED` (logged by type). |

The pending check reads the store from the tool thread (thread-safe, no network), using
`HERMES_SESSION_MESSAGE_ID` and the session key as `hooks._turn_controls` does. Because
the handler writes this result itself, `transform_tool_result` needs no change for tool
approvals.

### 2.5 Bypasses and non-human contexts

These come from Hermes, and the plugin doesn't add any of its own:
- `yolo` and `approvals.mode: off` approve at once.
- Smart approval is not consulted for plugin rules.
- A context with no human (a subagent, cron) fails closed, which shows up as `APPROVAL_DENIED`.

## 3. `linear_query`

`linear_query(query, variables?)` runs a read-only GraphQL document as the app. It works
in any session (reads are already unscoped: `linear_issue_get` reads any issue), with no
scope rule and no approval.

### 3.1 Read-only guard (`hermes_linear/graphql_guard.py`)

A dependency-free check (about 80 lines) runs on the tool thread before any Linear call:

- Lexer: skips whitespace, commas, and `#` comments to end of line. `"…"` strings (with
  `\` escapes) and `"""…"""` block strings are single tokens, so a keyword inside a
  string or a comment never counts. Punctuators and names are tokens.
- It tracks `{}` and `()` depth.
- At depth 0 each definition must start with `{` (anonymous query), `query` or
  `fragment`.
  - `mutation` or `subscription` at depth 0 gives `READ_ONLY`.
  - Any other depth-0 token (SDL such as `type`, `schema`, `extend`; a stray name) gives
    `BAD_ARGS`.
- Also `BAD_ARGS`: unbalanced brackets, an unterminated string, an empty document, no
  definition, or a query over 20,000 chars.
- `variables` must be a JSON object or absent (`BAD_ARGS` otherwise).

### 3.2 Run and result

- The document runs through `LinearClient.gql` on the adapter loop with
  `TOOL_TIMEOUT_S` (30 s).
- Success: `{"ok": true, "data": <data>}`.
- If `len(json.dumps(data, ensure_ascii=False))` exceeds 50,000, the result is
  `{"ok": false, "error": "RESPONSE_TOO_LARGE", "size": N, "limit": 50000, "message": "Paginate (first:/after:) or select fewer fields."}`.

### 3.3 Errors

`LinearError` gains an optional `detail`, which `_first_error` fills from
`extensions.userPresentableMessage`. Existing tools are unchanged and still answer with
the code only. `linear_query` maps errors itself and adds `message`:

| Linear | Result |
|--------|--------|
| `Query too complex` (live probe: `INPUT_ERROR`, "The query is too complex. Complexity: 18937750. Maximum allowed complexity: 10000.") | `QUERY_TOO_COMPLEX`, `message` = the presentable text + "Lower first: or the nesting depth." |
| Rate limit, auth, unknown entity | Today's codes (`RATELIMITED`, `UNAUTHENTICATED`/auth codes, `NOT_FOUND`), plus `message`. |
| Any other GraphQL error (bad field, validation) | `GRAPHQL_ERROR`, `message` cut at 1,000 chars. |
| Timeout / not connected | `TIMEOUT` / `NOT_CONNECTED`. |

## 4. Skills and docs

- `hermes_linear/skills/manage-superpowers/SKILL.md` (it must stay at 150 lines or fewer, enforced by a test):
  - list `linear_query`;
  - say that out-of-scope writes from comment, update and create ask for approval;
  - explain that `APPROVAL_PENDING` means end the turn, and that on
    ``Answer to approval for `<tool>` call <digest>: approved`` the agent re-runs the
    same call with identical arguments in that turn.
- The neo `linear-agent-sessions` skill is updated after deploy and restart, never before.
- README:
  - the behaviour table and the configuration reference (`out_of_scope_writes`);
  - the tool list (`linear_query`);
  - the security notes: approvals go to a human, the bypasses are `yolo` and `mode: off`.
- The channel design spec `2026-11-21-hermes-linear-superpowers-design.md` §4: the scope
  rule is edited in place to point here, and `linear_query` is added under §4.1.

## 5. Testing

TDD, through the real tool registry (`plugin.tool_handler`) where a handler is involved.

- **Guard:**
  - top-level `mutation` and `subscription`;
  - `mutation` inside strings, block strings and comments (allowed);
  - fragments, and several query operations;
  - a mix of query and mutation (refused);
  - SDL, unbalanced brackets, unterminated strings, an empty document, the size limit.
- **Gate:**
  - each mode, with a fake client;
  - pass 1 makes no writes;
  - update checks both ends before `check()`;
  - pass 2 accepts approved UUIDs only, and a target that changes between the passes is refused;
  - `request_tool_approval` driven against the real `tools.approval` on a worker thread for approve, deny and timeout→pending;
  - `NOT_LINEAR_SESSION` is never gated;
  - a scope error code is never gated.
- **Digest:** stable for identical args; it changes with the body, the title or the target.
- **Outbound:**
  - tool approvals are narrowed to once and deny;
  - the grant key includes the digest;
  - the re-run with the same digest uses the grant once, and a different digest prompts again;
  - the answer-turn text;
  - a tool approval without a digest gets no grant;
  - shell approvals are unchanged (regression).
- **Settings:** the default is `approve`; values are case-insensitive; an invalid value is a `SettingsError`.
- **Skill:** the existing 150-line and tool-roster test covers `linear_query`.

Live (project "hermes linear test project", after `deploy-live` and a gateway restart):

1. From ticket X, run `linear_comment` on an unrelated ticket Y. A select appears with
   Approve once / Deny. Approve posts as the app; Deny posts nothing.
2. Let the select time out (pending), then answer later. The answer turn re-runs and posts
   without a second prompt. The same call with a different body prompts again.
3. A comment on X or on its child gets no prompt.
4. `linear_query`:
   - a read works;
   - a `mutation` returns `READ_ONLY`;
   - an over-nested query returns `QUERY_TOO_COMPLEX`;
   - a huge result returns `RESPONSE_TOO_LARGE`.

## 6. Verify during implementation

- The pending row lookup matches a tool approval by the digest in the row's stored
  `question` (the prompt text), not by `command`: confirm that `prompt.text` carries the
  description and therefore the `digest:` line. If it does not, store the digest on the
  row from `prompt.description`.
- Whether Hermes's generic tool deadline (`timeouts.tools.concurrent_batch`) can cut a
  handler that is waiting on an approval, compared with a pre-hook approval. If it can,
  declare a deadline floor or document the interaction.
- `request_tool_approval` called from a plugin handler sees the gateway context
  (`_presence`: `HERMES_SESSION_*` and the approval session contextvars are bound on the
  tool thread). Pin this with a test against the real module.
- Hermes's `ExecApprovalPrompt.choices` for a plugin rule. Narrowing keeps only
  offered choices, so a Deny-only card stays Deny-only.

## 7. Out of scope

- A session or per-target approval grain (the Q2 alternative b). It can be added later
  without breaking anything.
- Generic mutate and the destructive hard-deny list (sub-project D).
- Field parity, search and attachments (sub-projects B and C).
