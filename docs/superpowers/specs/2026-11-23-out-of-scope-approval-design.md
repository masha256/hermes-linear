# Out-of-scope write approval and `linear_query` — design

Linear: RET-119

Status: approved in the RET-119 thread (Q1–Q4 and design sections 1–2), 2026-11-23;
§2 revised for Q1b (public API only), same day.
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
| Q1 | Where the scope decision runs | ~~In the handler, via `tools.approval.request_tool_approval`~~ (superseded: not in the published plugin API). |
| Q1b | Same, public API only | The handler walks the scope and records an out-of-scope verdict as an approval request (`APPROVAL_REQUIRED`); the agent's identical re-call makes `pre_tool_call` (store read only) return the `approve` directive; the handler writes only with an armed request. |
| Q2 | What one approval covers | **Allow once / Deny** only. `rule_key = <tool>:<digest>`; no session grain. A deferred answer grants one re-run bound to the argument digest. |
| Q3 | Default of `out_of_scope_writes` | `approve`. |
| Q4 | `linear_query` response cap | Refuse when the serialized `data` exceeds 50,000 chars (`RESPONSE_TOO_LARGE`). |

Coverage (proposed with Q3, accepted):

- **Gated:** `linear_comment` (`id`), `linear_issue_update` (`id` and a new `parent`),
  `linear_issue_create` (`parent`). An issue created under an approved out-of-scope
  parent is recorded as app-created (RET-110) and so is in scope afterwards.
- **Scope-only, refused without a prompt:** `linear_sync_spec`, `linear_sync_plan`,
  `linear_task_state`. They are workflow tools for the session's own tree.
- ~~**No Linear session** (e.g. a Slack session calling `linear_comment`): still
  `NOT_LINEAR_SESSION`, before any gate. A Slack approval must never authorize a Linear
  write ("answers only count in the ticket thread").~~ Superseded by RET-148
  (`2026-11-24-outside-session-writes-design.md`, Q2.3): outside a Linear session the
  gated tools follow `outside_session_writes`, and `approve` asks in the calling chat
  through Hermes's own gate.
- **No new hard-deny list.** The typed tools cannot reach destructive mutations; the
  deny list comes with D's generic mutate.

## 1. Configuration

`out_of_scope_writes: approve | deny | allow` in the plugin config
(`plugins.entries.linear.settings` / `ctx.get_config`), read into `Settings`:

| Value | Out-of-scope target in a Linear session |
|-------|-----------------------------------------|
| `approve` (default) | Ask a human (§2). |
| `deny` | Refuse `OUT_OF_SCOPE` (the behaviour before RET-119). |
| `allow` | Write without a prompt. Trusted single-user setups only. |

The value is case-insensitive and trimmed; anything else is a `SettingsError` naming
`out_of_scope_writes`. The handler reads the mode from the owning adapter's settings at
call time.

## 2. The approval path

Revised after spec approval (Q1b, RET-119 thread): `tools.approval.request_tool_approval`
is not in Hermes's published plugin API (`hermes_cli/data/plugin_api.json`). The gate is
therefore raised through the public `pre_tool_call` `approve` directive. The handler
records the scope verdict, and the hook turns it into the directive with no I/O.

### 2.1 Flow

The gated tools are `linear_comment`, `linear_issue_update` and `linear_issue_create`.
`_tool(..., gated=True)` wraps them.

1. **First call: the handler, on the adapter loop.** The body runs as today. In a
   Linear session, `_writable(ref)` resolves the issue and runs `check_write_scope`.
   - An out-of-scope target no longer raises. `_writable` records
     `{uuid, identifier, title}` on the call's `Gate` (a field on `ToolContext`) and
     returns the issue.
   - Each gated body calls `gate.check()` immediately before its first Linear write. If
     the gate recorded anything, `check()` raises `_NeedsApproval(targets)`, and nothing
     has been written. Every gated body already finishes its scope checks before any
     write. A test pins this for update: `id` and `parent` are both checked before
     `check()`.
   - A scope *error* (a `LinearError` code from the walk, or `NOT_LINEAR_SESSION`) still
     refuses at once. Only the plain `OUT_OF_SCOPE` verdict can be gated.
   - Non-gated tools (sync, task_state) keep raising `OUT_OF_SCOPE`.
2. **Mode (tool thread), on `_NeedsApproval`.**
   - `deny`: return `OUT_OF_SCOPE`.
   - `allow`: run the body again with `approved = {target uuids}` (step 5) and return its
     result.
   - `approve`: store an **approval request** (§2.3) holding the digest (§2.2), the
     targets and the prepared approval `message`. Then return:

     ```json
     {"ok": false, "error": "APPROVAL_REQUIRED",
      "message": "Not run: <RET-50> is outside this session's write scope. Call <tool> again with identical arguments to ask a human for approval in Linear. If that approval times out, the question stays pending in Linear: end this turn; an approval arrives as the next message, and re-running the identical call is then pre-approved once."}
     ```

     Nothing has been written.
3. **Identical re-call: `pre_tool_call`.**
   - For a gated tool in a Linear session, the hook computes the digest from the call's
     args. This is a pure function, the same one the handler uses.
   - It looks up an approval request for `(session key, digest)` in the owning adapter's
     store. That is a store read only, as `transform_tool_result` does today; there is no
     network call and no hop onto the loop.
   - If it finds one, it **arms** the request (stores `armed = 1` and `armed_at` = now)
     and returns
     `{"action": "approve", "message": <stored message>, "rule_key": "<tool>:<digest>"}`.
   - Anything else returns `None`, as today. That covers no request, a non-gated tool, a
     non-Linear session, and any error.
   - The hook never blocks the tool by itself.
4. **Hermes's gate.**
   - Hermes asks a human, renders it as the Linear select (§2.4), and waits as far as
     `approvals.timeout`.
   - `yolo` and `approvals.mode: off` approve at once. Smart approval is not used for
     plugin rules. Hermes's presence rules decide: a context with no human present (cron,
     a non-gateway run) is blocked; a subagent that inherits the gateway session asks in
     Linear like its parent (`tools/delegate_tool_child_run.py:1021`; not verified live).
   - Denial or timeout: the handler never runs; the agent sees Hermes's own BLOCKED text
     (§2.5).
5. **Approved: the handler again.**
   - It takes the request for `(session key, digest)` only if it is **armed**, was armed
     at most `ARMED_WINDOW_S` (3600 s) ago, and is younger than the 24 h TTL; it deletes
     it in the same store operation. The window bounds an arm left by an abandoned
     (timed-out) `pre_tool_call` worker; it must exceed Hermes's `approvals.timeout`
     (default 300 s, `tools/approval_context.py:327`).
   - It runs the body with `approved = {stored target uuids}`. `_writable` accepts an
     out-of-scope issue whose UUID is in `approved`.
   - A target that is out of scope and not approved is refused `OUT_OF_SCOPE`, with no
     new request. That happens when the issue moved since step 1, or a reference
     resolves to another issue.
   - Without an armed request, the handler treats the call as a first call (step 1). This
     covers a hook that did not run, for example `ctx.dispatch_tool`, which skips hooks.
     Such a hook-skipping call (only another in-process plugin's `ctx.dispatch_tool` can
     make one) with byte-identical args could use an approval while it is armed: between
     the human's answer and the handler run, bounded by `ARMED_WINDOW_S`. In-process
     plugins are trusted code.
6. **Disarm.** `post_tool_call` for a gated tool clears `armed` and `armed_at` on the
   request with that digest, so a later call that skips hooks can't use an approval that
   was denied or blocked. The request itself stays, so the deferred re-run (§2.4) still finds it.

In-scope calls are one handler pass, as today, plus one store operation: the
`take_armed_request` lookup that every gated call makes before its pass (Ruling R7). It
finds nothing and writes nothing. An out-of-scope write costs one extra tool call (the
identical re-call) and one extra round of reads.

### 2.2 Digest, message and rule key

- `digest` is the first 16 hex chars of `sha256` over the canonical JSON
  (`sort_keys=True, separators=(",", ":"), ensure_ascii=False`) of
  `{"tool": <name>, "args": <normalized args dict>}`.
  - The hook sees the raw args, the handler may see them after Hermes's
    `coerce_tool_args`. Normalization (ruling R10) coerces the gated tools' array fields
    (`labels`, `add_labels`, `remove_labels`) as `coerce_tool_args` does: a string that
    JSON-parses to a list becomes that list, any other non-list, non-None value `v`
    becomes `[v]`, lists and tuples stay lists, `None` stays `None`. It is idempotent, so
    both digests agree.
  - The target UUIDs are not part of the digest: they are stored on the request, and
    step 5 re-checks them.
  - Identical arguments give the identical digest. Any other argument (body, title,
    target reference) gives a new one.
- `message` becomes Hermes's `description`, which is shown in the Linear select. It holds:
  - the tool and what it will do, e.g. ``linear_comment on RET-50 "Title"``;
  - each target, as identifier and title (CR/LF replaced by spaces);
  - a preview of the args, with long strings (bodies, descriptions) cut at 1,500 chars
    and marked as cut;
  - a last line `digest: <hex>`.
- `rule_key = f"{tool}:{digest}"`. Even if a `session` answer slipped through, it would
  only cover the identical call.

### 2.3 Approval requests (store)

The new table is `approval_requests`:

| Column | Type | Notes |
|--------|------|-------|
| `session_key` | TEXT | Part of the primary key. |
| `digest` | TEXT | Part of the primary key. |
| `tool` | TEXT | |
| `targets` | TEXT | JSON list of UUIDs. |
| `message` | TEXT | |
| `armed` | INTEGER | 0 or 1. |
| `created_at` | REAL | |
| `armed_at` | REAL | When armed; NULL when not. Added by `ALTER TABLE` to older tables. |

Store methods:

- `put_request` replaces any existing row for the key and disarms it (clears `armed_at`).
- `arm_request` sets `armed = 1`, `armed_at` = now; returns the row, or None.
- `take_armed_request` is an atomic select-and-delete of a row with `armed = 1`,
  `armed_at >= now - ARMED_WINDOW_S` (`ARMED_WINDOW_S = 3600`, above Hermes's default
  300 s `approvals.timeout`) and `created_at >= now - REQUEST_TTL_S`.
- `disarm_request` clears `armed` and `armed_at`.
- `drop_all_requests` is the restart sweep, alongside `drop_all_grants`.

Rows older than 24 h are pruned on `put_request`. A stale row can only cause a prompt;
it never lets a write through unapproved.

### 2.4 Outbound: tool approvals and the deferred answer

Hermes names the prompt's `command` `<tool> (plugin approval rule)`. A prompt is a **tool
approval** when its command matches `^<linear_[a-z_]+> \(plugin approval rule\)$` and its
description ends in a `digest: <hex>` line. For a tool approval:

- The choices are narrowed to `once` and `deny`. There is no `session` choice.
- The grant key is `f"{command}#{digest}"`. It is used when the answer stores the grant
  (`put_grant`) and when the re-run's prompt looks it up (`_use_grant`). The control row
  stores this digest-bound key `command#digest`, taken from `prompt.description` when the
  prompt is posted (Hermes may cut `prompt.text`), so no migration is needed.
- The answer turn reads
  ``Answer to approval for `<tool>` call <digest>: approved (once). Re-run the same call with identical arguments.``
  (or `: denied`). Shell approvals keep ``Answer to approval for `<cmd>`: …``.
- The re-run in the answer turn follows §2.1 from step 3: the request row is still there,
  so the hook issues the directive, and `_use_grant` approves it without a new select.
- Everything else about grants is unchanged. A grant is used once and is bound to the
  session key, to the answering actor (checked against the asking-time authority) and to
  the answer turn's mid. It is dropped when that turn is freed without reaching Hermes,
  and on restart.

A tool approval without a parseable digest is treated as a normal approval: no grant on
the answer, and the re-run asks again. This fails closed.

### 2.5 Results

| Outcome | Tool result |
|---------|-------------|
| Out of scope, mode `approve`, first call | `APPROVAL_REQUIRED` (§2.1 step 2) |
| Approved, or mode `allow` | The tool's normal result |
| Denied by the human, timed out, or Hermes blocked it (no human present: cron, a non-gateway run) | Hermes's own block result, `{"error": "BLOCKED: …"}` |

Hermes does not run `transform_tool_result` for pre-hook blocks (`agent/tool_executor.py`,
`if not managed.blocked`), so the plugin cannot rewrite that result. The pending guidance
(end the turn; an approval arrives as the next message) is carried by the `APPROVAL_REQUIRED`
message instead (Ruling R9). The shell-approval rewrite already in `_approval_result` is
unchanged.

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
  - explain that `APPROVAL_REQUIRED` means call again with identical arguments (that asks the
    human); that an approval which times out (Hermes's `BLOCKED: …` result, §2.5; there is
    no `APPROVAL_PENDING` after Ruling R9) stays pending in Linear, so the agent ends the
    turn; and that on ``Answer to approval for `<tool>` call <digest>: approved`` the agent
    re-runs the same call with identical arguments in that turn.
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
- **Gate (handler):**
  - each mode, with a fake client;
  - the first call makes no writes and stores a request;
  - update checks both ends before `check()`;
  - an armed request is consumed exactly once;
  - an unarmed request (hooks skipped) never writes out of scope;
  - a target that changes between the calls is refused;
  - `NOT_LINEAR_SESSION` is never gated;
  - a scope error code is never gated.
- **Hook:**
  - a request returns the `approve` directive with the stored message and `<tool>:<digest>`, and arms it;
  - no request, or a non-gated tool, or a non-Linear session returns None;
  - `post_tool_call` disarms;
  - end to end through Hermes's real `_dispatch_pre_tool_call_hooks` / `request_tool_approval` on a worker thread: approve writes, deny does not, timeout→Hermes's BLOCKED result.
- **Store:** request put / arm / take-armed / disarm / 24 h prune / restart sweep.
- **Digest:** stable for identical args (key order irrelevant); it changes with the body, the title or the target.
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

1. From ticket X, run `linear_comment` on an unrelated ticket Y. The first call returns
   `APPROVAL_REQUIRED` and the agent re-calls. A select appears with Allow once / Deny.
   Approve posts as the app; Deny posts nothing.
2. Let the select time out (pending), then answer later. The answer turn re-runs and posts
   without a second prompt. The same call with a different body prompts again.
3. A comment on X or on its child gets no prompt.
4. `linear_query`:
   - a read works;
   - a `mutation` returns `READ_ONLY`;
   - an over-nested query returns `QUERY_TOO_COMPLEX`;
   - a huge result returns `RESPONSE_TOO_LARGE`.

## 6. Verify during implementation

Answered from Hermes at `caafe5389a` (`~/.hermes/hermes-agent`), Task 7 (RET-129).

- **Digest on the pending row.** Hermes may cut `prompt.text` (`_ea_fit`,
  `gateway/platforms/base.py:2693`), so the row does not rely on it: `approval_prompt`
  reads the digest from `prompt.description` and stores the digest-bound key
  `command#digest` as the row's `command` (`hermes_linear/outbound.py:578-581`, §2.4).
- **Generic tool deadline.** `timeouts.tools.concurrent_batch` does not cut the
  approval wait. The gateway wait runs inside `human_wait_window`
  (`tools/approval_gateway_wait.py:88`), and both executors add that time back to their
  deadline (`agent/tool_executor.py:475-547`, `_ConcurrentToolAuthorizationGate.excluded_seconds`;
  used at `:891` for the sequential poll and `:1464`/`:1474` for the batch). Only
  `approvals.timeout` ends the wait.
- **Hook args vs handler args.** No `modify` hook runs in between: Hermes merges
  `modify` directives and returns them as `modified_args` (`hermes_cli/plugins.py:1153-1167`),
  and this plugin never returns one (the e2e test asserts `modified_args is None`). The
  agent loop passes the hook's `ref.args` to the handler (`agent/tool_executor.py:708-709`
  and `:754`). One difference remains: `handle_function_call` runs `coerce_tool_args` on
  the args *after* the agent loop's hook (`model_tools.py:923`; the loop calls it with
  `skip_pre_tool_call_hook=True`, `agent/tool_executor.py:1738-1748`). For the gated tools
  coercion only changes an array field (`labels`, `add_labels`, `remove_labels`) sent as a
  string. That is handled by the R10 normalization in `tool_approval.digest` (§2.2): the
  hook's digest of the raw args equals the handler's digest of the coerced args (tested in
  `tests/test_tool_approval.py` and `tests/test_tool_approval_e2e.py`). On
  the `model_tools` path (`execute_code`, bridges) coercion runs before the hook
  (`model_tools.py:923` then `:984`), so the digests agree.
- **Tool Search bridge.** The agent loop unwraps `tool_call` before any hook runs
  (`agent/tool_executor.py:387-432`, `_unwrap_tool_search_call`), and `handle_function_call`
  re-dispatches the underlying call with its own hooks (`model_tools.py:945-970`). The
  wrapper is kept only for an unresolvable call, a connector batch, or an out-of-scope or
  invalid underlying call (blocked before the handler). `hooks._bridged_call` still unwraps
  a single-call wrapper.
- **`post_tool_call` on a gate block.** It fires: `_blocked_tool_result` emits it with
  `status="blocked"` (`agent/tool_executor.py:655`), as does `handle_function_call`
  (`model_tools.py:987`). `transform_tool_result` does not run for a blocked call
  (`agent/tool_executor.py:1845`, `if not managed.blocked`), hence Ruling R9. Disarm stays in
  `post_tool_call` only.
- **`execute_code`.** Its RPC dispatch calls `handle_function_call` without
  `skip_pre_tool_call_hook` (`tools/code_execution_rpc.py:28-35`), so `pre_tool_call` runs
  (`model_tools.py:984`, `_pre_dispatch_guards`). The RPC thread inherits the turn's
  approval context (`tools/code_execution_tool.py:628-629`, `propagate_context_to_thread`),
  so a sandboxed re-call asks the human like a direct one.
- **Choices of a plugin rule.** `request_tool_approval` is never one-shot for a plugin
  rule, so the prompt offers once / session / always / deny (`allow_permanent` and
  `allow_session` true, `tools/approval.py:1101`). `approval_prompt` narrows that to
  once / deny and keeps a Deny-only card Deny-only (`hermes_linear/outbound.py:576-582`).
- **End to end.** `tests/test_tool_approval_e2e.py` drives Hermes's real
  `_dispatch_pre_tool_call_hooks` → `request_tool_approval` → gateway wait with the
  plugin's hooks, handler, `approval_prompt` and answer path: approve writes, deny does
  not, a timeout leaves the row pending and the later answer's identical re-run resolves
  through the grant with no new select; yolo writes without a prompt.

## 7. Out of scope

- A session or per-target approval grain (the Q2 alternative b). It can be added later
  without breaking anything.
- Generic mutate and the destructive hard-deny list (sub-project D).
- Field parity, search and attachments (sub-projects B and C).
