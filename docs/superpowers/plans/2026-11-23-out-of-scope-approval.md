# Out-of-scope write approval + `linear_query` Implementation Plan

Linear: RET-119

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Out-of-scope writes from `linear_comment` / `linear_issue_update` / `linear_issue_create` ask a human in Linear (through the public `pre_tool_call` `approve` directive) instead of being refused. A new read-only `linear_query` tool is added.

**Architecture:** The handler walks the scope. An out-of-scope target is stored as an approval request, and the call answers `APPROVAL_REQUIRED`. The agent's identical re-call makes `pre_tool_call`, which only reads the store, arm the request and return `approve`. The handler writes only after taking an armed request. Outbound renders tool approvals as Approve once / Deny, and keys deferred grants by an argument digest. `linear_query` is a tokenizer guard plus `LinearClient.gql`.

**Tech Stack:** Python 3.11, the plugin's own `LinearClient` (aiohttp), sqlite `Store`, and pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-23-out-of-scope-approval-design.md`

## Global Constraints

- Public Hermes plugin API only (`hermes_cli/data/plugin_api.json`). Product code adds no new private imports; tests may import Hermes internals. Hermes core is never changed.
- Tools never raise. Failures are `{"ok": false, "error": CODE, ...}`.
- Hooks never do network I/O and never hop onto the loop. Store reads and writes are allowed, since the store is thread-safe.
- Every write is made as the app, through the plugin's `LinearClient`.
- `out_of_scope_writes: approve | deny | allow`, default `approve`.
- Tool approvals offer `once` and `deny` only. `rule_key = f"{tool}:{digest}"`.
- `digest` is the first 16 hex chars of `sha256(json.dumps({"tool": t, "args": raw_args}, sort_keys=True, separators=(",", ":"), ensure_ascii=False))`.
- `linear_query`: refuse at more than 50,000 chars of `json.dumps(data, ensure_ascii=False)`. A query over 20,000 chars is `BAD_ARGS`.
- Tests run from a worktree whose dir name is a valid identifier (`.worktrees/ret119`): `.venv/bin/pytest -q`. Also run `hermes plugins validate .`: its "private core imports" list must not grow.
- Commit messages carry the task's sub-ticket ID.

## Review Focus

1. **A re-call with the same args in a different key order** must hit the same request, because the agent may reorder JSON keys. Owned by the Task 3 digest test (`sort_keys`).
2. **A call that skips hooks** (`ctx.dispatch_tool`), made while a request exists, must never write out of scope. Owned by Task 4 (unarmed request → `APPROVAL_REQUIRED` again, no write).
3. **The re-run in the deferred answer turn** arrives after `post_tool_call` disarmed the request. The hook must re-arm it, and the grant must match `command#digest`. Owned by the Task 7 end-to-end test.
4. **A `mutation` hidden in a comment, string or block string** is allowed. A real one after a valid `query` is refused. Owned by the Task 1 guard tests.
5. **A transform rewrite must never touch the tool's own JSON**, including the tool's own `APPROVAL_REQUIRED`. Owned by the Task 5 transform test.

---

### Task 1: `linear_query` (read-only guard + tool)

**Files:**
- Create: `hermes_linear/graphql_guard.py`
- Modify: `hermes_linear/linear_client.py` (`LinearError.detail`, `_first_error`)
- Modify: `hermes_linear/agent_tools.py` (handler, schema, `TOOL_HANDLERS`)
- Test: `tests/test_graphql_guard.py`, `tests/test_linear_query.py`

**Interfaces:**
- Produces: `graphql_guard.check(query: Any) -> str | None`, which returns `None` (read-only, OK), `"READ_ONLY"` or `"BAD_ARGS"`. `MAX_QUERY_CHARS = 20_000`.
- Produces: `LinearError(message, *, code=None, status=None, detail=None)`. `_first_error` returns `(code, message, detail)`, with `detail` taken from `errors[0].extensions.userPresentableMessage`. Update its one caller in `gql`.
- Produces: tool `linear_query` with args `query: str` and `variables?: object`. The result is `{"ok": true, "data": …}`, or an error with `message`. `QUERY_MAX_RESULT_CHARS = 50_000`.

- [ ] **Step 1: Write the guard tests** (`tests/test_graphql_guard.py`, parametrized):
  - `None` for:
    - `"{ viewer { id } }"`
    - `"query Q($id: String!) { issue(id: $id) { id } }"`
    - `"fragment F on Issue { id } query { issue(id:\"x\") { ...F } }"`
    - `"# mutation\n{ viewer { id } }"`
    - `'{ issues(filter:{title:{contains:"mutation { x }"}}) { nodes { id } } }'`
    - `'query { a(x: """mutation\n{""") { id } }'`
    - `"query A { a } query B { b }"`
  - `"READ_ONLY"` for:
    - `"mutation { issueDelete(id:\"x\") { success } }"`
    - `"subscription { a }"`
    - `"query { a } mutation { b }"`
    - `" \n, mutation M { b }"`
  - `"BAD_ARGS"` for:
    - `""`
    - `"   "`
    - `None`
    - `42`
    - `"type Query { a: Int }"`
    - `"extend type X { a: Int }"`
    - `"{ a "`
    - `"{ a } }"`
    - `'{ a(x: "unterminated) }'`
    - `"query"` (no selection set)
    - `"x" * 20_001`
- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_graphql_guard.py`. Expected: fails with ImportError.
- [ ] **Step 3: Implement `check(query)`.** It is a single-pass lexer:
  - Skip whitespace, commas, `\ufeff`, and `#` up to end of line.
  - Read `"""…"""` (with `\"""` escapes) and `"…"` (with `\` escapes, no newline) as single tokens.
  - Names are `[_A-Za-z][_0-9A-Za-z]*`. Every other character is a one-char punctuator (handle `...` as one token).
  - Track the depth of `{}` and `()` together.
  - At depth 0, a definition starts at `{`, or at a name in `{query, fragment}` that must be followed (before depth returns to 0) by a `{`. `mutation` or `subscription` at depth 0 is `READ_ONLY`. Any other depth-0 token is `BAD_ARGS`.
  - A negative depth, a non-zero depth at end of input, an unterminated string, or no definition at all is `BAD_ARGS`.
  - The `MAX_QUERY_CHARS` check comes first.
- [ ] **Step 4: Run** the guard tests. Expected: PASS.
- [ ] **Step 5: Write the tool tests** (`tests/test_linear_query.py`). Reuse the `loop` / `adapter` / `pin_ctx` fixtures pattern from `tests/test_agent_tools_generic.py` (import `new_adapter` from `test_agent_tools_runtime`), with a client stub whose `gql` records calls:
  - `test_query_returns_data`: `{"ok": True, "data": {...}}`, and the variables are passed through.
  - `test_mutation_refused_before_any_call`: `READ_ONLY`, and the stub saw 0 calls.
  - `test_variables_must_be_object`: `variables=[1]` gives `BAD_ARGS`.
  - `test_works_outside_linear_session`: `pin_ctx(linear=False)` still returns data.
  - `test_result_too_large`: data whose dump is 50,001 chars gives `error == "RESPONSE_TOO_LARGE"`, `size == 50001`, `limit == 50000`, and `"Paginate"` in `message`.
  - `test_query_too_complex`: the stub raises `LinearError("Query too complex", code="INPUT_ERROR", detail="The query is too complex. Complexity: 18937750. Maximum allowed complexity: 10000.")`. The result is `QUERY_TOO_COMPLEX`, with `"Maximum allowed complexity: 10000"` and `"first:"` in `message`.
  - `test_graphql_error_has_message`: `LinearError("Cannot query field \"x\"", code="GRAPHQL_VALIDATION_FAILED")` gives `GRAPHQL_ERROR`, with the message cut at 1,000 chars.
  - `test_known_codes_keep_code`: `RATELIMITED` and `NOT_FOUND` keep their codes and add `message`.
  - `test_first_error_reads_user_presentable_message` (in `tests/test_linear_client.py`): `_first_error` on the live-probe body shape returns `detail`.
  - `test_registered_handler_returns_str`, through `plugin.tool_handler("linear_query")`.
- [ ] **Step 6: Run** them. Expected: they fail.
- [ ] **Step 7: Implement.**
  - The `linear_query` handler uses `_tool(parse, body)`. `parse` runs `graphql_guard.check` and validates `variables`.
  - `body` calls `ctx.client.gql(query, variables)` and catches `LinearError` itself, mapping it per spec §3.3: `message.startswith("Query too complex")` → `QUERY_TOO_COMPLEX`; the codes `RATELIMITED`, `NOT_FOUND`, `UNAUTHENTICATED`, `FORBIDDEN` are kept; anything else → `GRAPHQL_ERROR`.
  - The size cap is applied in `body`.
  - Schema description: "Run a read-only GraphQL query against Linear's API as the Linear app (no mutations or subscriptions). Results over 50,000 chars are refused: paginate with first:/after: or select fewer fields."
- [ ] **Step 8: Run** the full suite and `hermes plugins validate .`. Expected: PASS. Update the skill tool-roster test only if it asserts an exact tool set; the skill text itself changes in Task 7.
- [ ] **Step 9: Commit** with `feat: linear_query read-only GraphQL tool (<T1 ID>)`.

### Task 2: `out_of_scope_writes` setting + approval-request store

**Files:**
- Modify: `hermes_linear/settings.py`, `hermes_linear/store.py`, `hermes_linear/adapter.py:287` (restart sweep)
- Test: `tests/test_settings.py`, `tests/test_store.py`, and the restart-sweep test file that covers `drop_all_grants` (grep for it)

**Interfaces:**
- Produces: `Settings.out_of_scope_writes: str` (one of `"approve" | "deny" | "allow"`), default `"approve"`, read from `get_config("out_of_scope_writes", "approve")`. It is case-insensitive and trimmed. Any other value raises `SettingsError` with a message containing `'out_of_scope_writes'`. Give the field a dataclass default so the existing `Settings(...)` constructions in tests keep working. `OUT_OF_SCOPE_MODES = ("approve", "deny", "allow")`.
- Produces: `store.ApprovalRequest` (a frozen dataclass) with `session_key: str`, `digest: str`, `tool: str`, `targets: list[str]`, `message: str`, `armed: bool`, `created_at: float`.
- Produces these `Store` methods:
  - `put_request(session_key, digest, tool, targets: list[str], message) -> None` (replace; `armed = 0`; prunes rows older than `REQUEST_TTL_S = 86_400`)
  - `request(session_key, digest) -> ApprovalRequest | None`
  - `arm_request(session_key, digest) -> ApprovalRequest | None` (sets `armed = 1`, returns the row)
  - `take_armed_request(session_key, digest) -> ApprovalRequest | None` (atomic select and delete, armed rows only)
  - `disarm_request(session_key, digest) -> None`
  - `drop_all_requests() -> None`
- Table `approval_requests`, as in spec §2.3, added to the schema with `CREATE TABLE IF NOT EXISTS` (no column migration needed).

- [ ] **Step 1: Write the tests.**
  - `test_out_of_scope_writes_default_approve`
  - `test_out_of_scope_writes_case_insensitive` (`" Deny "` → `"deny"`)
  - `test_out_of_scope_writes_invalid_raises` (`"ask"` → `SettingsError` naming the key)
  - `test_request_put_arm_take`: `take_armed` is None before `arm`, returns the row after it, and is None on a second take.
  - `test_request_disarm_blocks_take`
  - `test_request_put_replaces_and_disarms`
  - `test_request_ttl_prune`: monkeypatch `time.time`, so a 25 h old row is gone after the next `put_request`.
  - `test_restart_sweep_drops_requests`, next to the existing `drop_all_grants` sweep test.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** the setting, the table, the methods and the sweep call. Store `targets` as JSON.
- [ ] **Step 4: Run** the full suite. Expected: PASS.
- [ ] **Step 5: Commit** with `feat: out_of_scope_writes setting and approval-request store (<T2 ID>)`.

### Task 3: Tool-approval helpers (digest, message, command keys)

**Files:**
- Create: `hermes_linear/tool_approval.py`
- Test: `tests/test_tool_approval.py`

**Interfaces:**
- Produces, all pure with no I/O:
  - `GATED_TOOLS = frozenset({"linear_comment", "linear_issue_update", "linear_issue_create"})`
  - `digest(tool: str, args: Any) -> str`, as defined in the Global Constraints. A non-dict `args` is digested as `{}`.
  - `rule_key(tool: str, digest: str) -> str` returns `f"{tool}:{digest}"`.
  - `approval_message(tool: str, targets: list[dict], args: dict, digest: str) -> str`.
    - First line: ``Approve `<tool>` outside this session's tickets?``
    - Then one line per target: ``- <identifier> "<title>"``.
    - Then `Arguments:` followed by a JSON preview (`indent=1`, `sort_keys`), where any string over 1,500 chars is cut to 1,500 + `… [cut, N chars]`.
    - Last line: `digest: <hex>`.
  - `parse_rule(command: str, description: str | None) -> tuple[str, str] | None` returns `(tool, digest)` when `command` matches `^<(linear_[a-z_]+)> \(plugin approval rule\)$` and the description's last non-empty line matches `^digest: ([0-9a-f]{16})$`. Otherwise it returns `None`.
  - `grant_command(command: str, digest: str) -> str` returns `f"{command}#{digest}"`.
  - `split_grant_command(command: str | None) -> tuple[str, str] | None` returns `(tool, digest)` for a `"<tool> (plugin approval rule)#<hex>"` string, else `None`.
  - Copy constants `REQUIRED_TEXT = "Not run: {targets} is outside this session's write scope. Call {tool} again with identical arguments to ask a human for approval in Linear."` and `PENDING_TEXT` (the spec §2.5 string, verbatim).

- [ ] **Step 1: Write the tests.**
  - `test_digest_stable_across_key_order`
  - `test_digest_changes_with_body_title_target` (parametrized)
  - `test_digest_is_16_hex`
  - `test_message_cuts_long_strings_and_ends_with_digest`
  - `test_parse_rule_matches_only_linear_tools_with_digest`: `<terminal> (plugin approval rule)` gives None, a missing digest line gives None, and a digest line that is not the last line gives None.
  - `test_grant_command_round_trip`
- [ ] **Step 2: Run** them. Expected: FAIL (ImportError).
- [ ] **Step 3: Implement** `tool_approval.py`.
- [ ] **Step 4: Run** them. Expected: PASS.
- [ ] **Step 5: Commit** with `feat: tool-approval digest and message helpers (<T3 ID>)`.

### Task 4: Gated write handlers

**Files:**
- Modify: `hermes_linear/agent_tools.py` (`ToolContext`, `_writable`, `_tool`, `_create`, `_update`, `_comment`, schema descriptions of the three gated tools)
- Test: `tests/test_agent_tools_gate.py`, plus updates to `tests/test_agent_tools_generic.py` for the old `OUT_OF_SCOPE` expectations (`test_create_parent_out_of_scope_refused`, `test_update_out_of_scope_refused`, `test_reparent_out_of_scope_*`). Those now expect `APPROVAL_REQUIRED` under the default mode, or `OUT_OF_SCOPE` with mode `deny`.

**Interfaces:**
- Consumes: `tool_approval.digest`, `approval_message`, `REQUIRED_TEXT`, `GATED_TOOLS` (Task 3); the `Store` request methods and `Settings.out_of_scope_writes` (Task 2).
- Produces:
  - `class Gate`, with `approved: frozenset[str]`, `targets: list[dict]` (`{"id", "identifier", "title"}`), `record(issue: dict) -> None` (deduplicated by id) and `check() -> None` (raises `_NeedsApproval(list(targets))` when non-empty).
  - `ToolContext.gate: Gate | None = None` (a default, so existing constructions keep working).
  - `_tool(parse, body, *, gated: str | None = None, ...)`, where `gated` is the tool name.
  - The session key used for requests is `ctx.session_key`.

- [ ] **Step 1: Write the tests** (`tests/test_agent_tools_gate.py`). Use the `ws` / `adapter` / `pin_ctx` fixtures pattern, and set the mode through `adapter._settings = Settings(... out_of_scope_writes=...)` or a small helper.
  - `test_in_scope_comment_unchanged_no_request`: posted, and no request row.
  - `test_out_of_scope_comment_first_call_requires_approval`: `error == "APPROVAL_REQUIRED"`, `"OTH-9"` in `message`, no comment on OTH-9, and `store.request("sk", digest)` holds `targets == ["uuid-other"]` with the message ending in the digest.
  - `test_armed_request_writes_once`: after `store.arm_request`, the call posts the comment, and the row is gone. A second identical call gives `APPROVAL_REQUIRED` again.
  - `test_unarmed_request_never_writes` (Review Focus 2): two identical calls with no arm in between both give `APPROVAL_REQUIRED`, and nothing is posted.
  - `test_update_checks_both_ends_before_writing`: `id` in scope and `parent` out of scope gives `APPROVAL_REQUIRED` with zero writes, and `targets` holds the parent. Both out of scope gives both targets in one request.
  - `test_create_with_out_of_scope_parent_then_in_scope`: after arm, the issue is created under the parent and recorded via `record_created`, and a later `linear_comment` on it needs no approval.
  - `test_target_moved_between_calls_refused`: arm a request for `uuid-other`, then a call resolving to a different out-of-scope issue (same args digest, with ws remapped so `OTH-9` resolves elsewhere) gives `OUT_OF_SCOPE` and no write.
  - `test_mode_deny_refuses`: `OUT_OF_SCOPE`, no request.
  - `test_mode_allow_writes_without_request`
  - `test_not_linear_session_never_gated`: `NOT_LINEAR_SESSION`, no request.
  - `test_scope_error_code_not_gated`: the walk raises `LinearError(code="RATELIMITED")`, which gives `RATELIMITED` and no request.
  - `test_sync_and_task_state_still_out_of_scope`: `linear_task_state` on OTH-9 gives `OUT_OF_SCOPE`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - `_writable(ctx, ref)`: after `issue_get`, if `ctx.gate` is set and `issue["id"] in ctx.gate.approved`, return the issue. Otherwise run `check_write_scope`. A code of `"OUT_OF_SCOPE"` with `ctx.gate` set and `not ctx.gate.approved` records the issue and returns it. Any other code, or `OUT_OF_SCOPE` on the approved pass, raises `_Refusal(code)`.
  - Each gated body calls `ctx.gate.check()` (when the gate is set) right before its first write. `_update` must do both `_writable` calls and `_refuse_cycle` before `check()`.
  - `_tool(gated=name)` handler, after `resolve_context`:
    - If `ctx.is_linear`, take the armed request (`ctx.adapter._store.take_armed_request(ctx.session_key, digest(name, args))`).
    - With a request, run with `Gate(approved=frozenset(req.targets))`.
    - Otherwise run with `Gate()`. On `_NeedsApproval`, read the mode from `getattr(ctx.adapter, "_settings", None)` (None → `"approve"`):
      - `deny` → `OUT_OF_SCOPE`;
      - `allow` → run once more with `Gate(approved=…)`;
      - `approve` → `put_request(...)` and return `APPROVAL_REQUIRED` with `REQUIRED_TEXT`.
    - With no store, `approve` returns `OUT_OF_SCOPE`.
    - `_NeedsApproval` must cross `run_on_adapter`: return a sentinel dict `{"ok": False, "error": "_NEEDS_APPROVAL", "targets": [...]}` from `run()` and handle it in the handler. It must never leak to the caller.
  - Schema descriptions for the three gated tools: replace "Only the session issue, …" with "Outside the session issue's tree (issues you did not create) the first call returns APPROVAL_REQUIRED; call again with identical arguments to ask a human."
- [ ] **Step 4: Run** the full suite. Expected: PASS. Old out-of-scope tests are updated as listed under Files.
- [ ] **Step 5: Commit** with `feat: gated out-of-scope writes record an approval request (<T4 ID>)`.

### Task 5: Hooks: approve directive, disarm, result rewrite

**Files:**
- Modify: `hermes_linear/hooks.py`
- Test: `tests/test_hooks_approval.py`

**Interfaces:**
- Consumes: `tool_approval.GATED_TOOLS`, `digest`, `rule_key`, `PENDING_TEXT`, `parse_rule`/`split_grant_command` (Task 3); `Store.arm_request`, `request`, `disarm_request` (Task 2); control rows whose `command` is `grant_command(...)` for tool approvals (Task 6 writes them that way; for this task, test with rows built that way).
- Produces:
  - `on_pre_tool_call` returns `{"action": "approve", "message": req.message, "rule_key": rule_key(tool, d)}` when a gated tool in a Linear session has a request for `(HERMES_SESSION_KEY, digest)` in any adapter's store. It arms the request first. Otherwise it returns None, and the existing action-scheduling behaviour is kept in both cases.
  - `on_post_tool_call` disarms that request.
  - `on_transform_tool_result` rewrites per spec §2.5.

- [ ] **Step 1: Write the tests.** Bind session vars the way existing hook tests do (grep `tests/` for `HERMES_SESSION_PLATFORM` binding).
  - `test_pre_tool_call_approves_and_arms`: the directive's `message` and `rule_key` are as stored, and `take_armed_request` then succeeds.
  - `test_pre_tool_call_none_without_request` / `_non_gated_tool` / `_non_linear_session` / `_store_error` (store raises, giving None).
  - `test_pre_tool_call_still_schedules_action_event`: the existing start event is still scheduled.
  - `test_post_tool_call_disarms`
  - `test_transform_pending`: the result is Hermes's timeout text (containing `timed out without user response`), with a pending approval control row for this turn's mid and `command == "<linear_comment> (plugin approval rule)#<d>"`. The output is JSON with `error == "APPROVAL_PENDING"` and `message == PENDING_TEXT`.
  - `test_transform_denied`: a `BLOCKED…` text with a request present gives `APPROVAL_DENIED`, with that text as `message`.
  - `test_transform_never_rewrites_tool_json` (Review Focus 5): `{"ok": false, "error": "APPROVAL_REQUIRED", …}` and `{"ok": true}` both give None.
  - `test_transform_no_request_untouched`
  - The existing shell-approval transform tests stay green.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - Unwrap the `tool_call` bridge, as `_silent` parses `calls`, so the real name and args are used. A wrapped batch of more than one call is never approved; it returns None.
  - Store access goes through `adapter_mod.ADAPTERS` stores, as `_turn_controls` does. Wrap all of it in `try`, so any exception returns None (fail-open: no approval directive means the handler refuses with `APPROVAL_REQUIRED`).
- [ ] **Step 4: Run** the full suite. Expected: PASS.
- [ ] **Step 5: Commit** with `feat: pre_tool_call approves recorded out-of-scope writes (<T5 ID>)`.

### Task 6: Outbound and answers for tool approvals

**Files:**
- Modify: `hermes_linear/outbound.py` (`approval_prompt`, `_use_grant`), `hermes_linear/sessions_open.py` (`answer_pending` head text)
- Test: `tests/test_pending_questions.py` (new cases next to the existing approval-grant tests)

**Interfaces:**
- Consumes: `tool_approval.parse_rule`, `grant_command`, `split_grant_command` (Task 3).
- Produces: for a tool approval (`parse_rule(prompt.command, prompt.description)` is not None):
  - choices are narrowed to `[c for c in ("once", "deny") if c in prompt.choices]`;
  - the control row is stored with `command = grant_command(prompt.command, digest)`;
  - `_use_grant` looks up `grant_command(prompt.command, digest)`;
  - `answer_pending` builds its head with `split_grant_command(row.command)`:
    - approved: ``Answer to approval for `<tool>` call <digest>: approved (<choice>). Re-run the same call with identical arguments.``
    - denied: ``Answer to approval for `<tool>` call <digest>: denied``
  - `put_grant(row.session_key, row.command, …)` is unchanged, because `row.command` is already the grant key.
- This deviates from spec §2.4 ("the control row keeps the plain command"): the row holds the digest-bound key. Update that sentence of the spec in this task. The reason is that `prompt.text` may cut the description (`_ea_fit`), so the digest must be taken from `prompt.description` when the prompt is posted.

- [ ] **Step 1: Write the tests.**
  - `test_tool_approval_offers_once_and_deny`: the elicitation options are exactly once and deny.
  - `test_tool_approval_row_command_has_digest`
  - `test_tool_approval_answer_text_and_grant`: answering a pending tool approval starts a turn whose text contains `` `linear_comment` call <d>: approved (once). Re-run the same call with identical arguments.``, and `take_grant(sk, "<linear_comment> (plugin approval rule)#<d>")` returns it.
  - `test_tool_grant_matches_only_same_digest`: a prompt with another digest posts a select and leaves the grant unused. The same digest resolves through `_resolve_approval_card` (stubbed as in the existing grant tests).
  - `test_tool_approval_without_digest_is_plain`: no narrowing, the plain command is stored, and the answer text uses the shell form.
  - `test_shell_approval_unchanged`: the existing tests stay green.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.** Make the spec sentence edit as well.
- [ ] **Step 4: Run** the full suite. Expected: PASS.
- [ ] **Step 5: Commit** with `feat: tool approvals render once/deny with digest-bound grants (<T6 ID>)`.

### Task 7: End-to-end through Hermes + skill and docs

**Files:**
- Test: `tests/test_tool_approval_e2e.py`
- Modify: `hermes_linear/skills/manage-superpowers/SKILL.md`, `README.md`, `docs/superpowers/specs/2026-11-21-hermes-linear-superpowers-design.md` §4, and spec §6 (record the answers to its verify list)

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Answer spec §6 by reading Hermes** (`~/.hermes/hermes-agent`). Record each answer, with file:line, in spec §6:
  - Do the hook's args equal the handler's args (no `modify` in between)?
  - Is the `tool_call` bridge unwrapped before hooks run?
  - Does `post_tool_call` fire for a gate-blocked call (`emit_post(..., status="blocked")`)?
  - Does `execute_code`'s RPC run `pre_tool_call`?
  - Does `timeouts.tools.concurrent_batch` cover the pre-hook approval wait?
  If `post_tool_call` does **not** fire on a block, disarm in `transform_tool_result` as well, and add a test for it.
- [ ] **Step 2: Write the e2e tests.** Model them on `tests/test_approval_grant_feasibility.py`: real `tools.approval`, `register_gateway_notify`, the session key contextvar, and the approval mode patched to `manual`. Drive Hermes's real `hermes_cli.plugins._dispatch_pre_tool_call_hooks("linear_comment", args, ...)` with the plugin's hooks registered (or `invoke_hook` patched to call `hooks.on_pre_tool_call`), on a worker thread, then the registered handler.
  - `test_e2e_approve_writes`: first call `APPROVAL_REQUIRED`; re-call, notify, resolve `once`, posted.
  - `test_e2e_deny_never_writes`
  - `test_e2e_deferred_rerun_uses_grant` (Review Focus 3): first call; re-call; the approval times out (short `approvals.timeout`); the pending row is rewritten to `APPROVAL_PENDING`; `answer_pending` approves; the identical re-call in the answer turn resolves through `_use_grant` with no new elicitation and posts. A re-call with a different body posts a new select.
  - `test_e2e_yolo_bypasses`: with yolo active, the re-call writes without a prompt.
- [ ] **Step 3: Run** them. Expected: PASS. If a test fails on a real Hermes behaviour, fix the plugin, not the test, and note it in the ledger.
- [ ] **Step 4: Skill text** (the 150-line cap is enforced by test):
  - Add `linear_query` to the tool list.
  - Replace "write only to the session ticket…" with the gated rule: out-of-scope comment, update and create return `APPROVAL_REQUIRED`, so call again with identical arguments to ask. `APPROVAL_PENDING` means end the turn. On ``Answer to approval for `<tool>` call <digest>: approved``, re-run the same call with identical arguments in that turn.
- [ ] **Step 5: README:** the behaviour table row, the configuration reference row for `out_of_scope_writes`, the tool list, and the security notes (approvals go to a human; the bypasses are `yolo` and `approvals.mode: off`; subagents are blocked). Edit the channel design spec §4 in place: the scope rule points to the RET-119 spec, and `linear_query` goes under §4.1.
- [ ] **Step 6: Run** the full suite and `hermes plugins validate .`. Expected: PASS, with no new private imports listed.
- [ ] **Step 7: Commit** with `test+docs: tool approval end to end; skill, README, spec (<T7 ID>)`.
