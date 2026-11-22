# hermes-linear × superpowers — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

Linear: RET-73

**Goal:** Give the hermes-linear plugin agent-facing Linear tools, plus a
`linear:manage-superpowers` skill. Together they let a delegated Linear ticket run
the superpowers flow and mirror the spec, the plan and the task state onto tickets.

**Architecture:** Plugin tools are synchronous handlers. Each one bridges onto the
owning `LinearAdapter`'s event loop, the same way `hooks.py` does, and calls
`LinearClient` there. A pure module, `sync.py`, parses and renders the spec and plan.
The sync tools are idempotent and write ticket IDs back into the plan file. The
skill is a Markdown overlay that the plugin registers with `register_skill`.

**Tech Stack:** Python 3.11+, aiohttp, pytest (`asyncio_mode = "auto"`), Hermes
public plugin API.

**Spec:** `docs/superpowers/specs/2026-11-21-hermes-linear-superpowers-design.md`

**Branch:** `superpowers-integration`, rebased onto `m1`. The tools need the
channel plugin's adapter, client and hooks. Work in
`.worktrees/sp`.

## Global Constraints

- Public Hermes plugin API only. `hermes plugins validate .` must report no
  private imports. No Hermes core changes.
- Tools act as the app user, on the plugin's existing `LinearClient`. There are no
  new secrets and no new settings.
- Writes are allowed only from sessions whose `HERMES_SESSION_PLATFORM` is
  `linear`, and only to the session ticket, its descendants, or tickets created in
  this session (§4.3).
- No tool ever raises into the agent. Failure is `{"ok": false, "error": "<CODE>"}`,
  where `<CODE>` is a `LinearError.code` or one of `NOT_LINEAR_SESSION`,
  `OUT_OF_SCOPE`, `NOT_FOUND`, `LABEL_MISSING`, `NO_LINEAR_HEADER`, `TIMEOUT`,
  `BAD_ARGS`.
- `SPEC_INLINE_MAX = 200_000` chars (probe P1).
- Sub-ticket order uses `subIssueSortOrder`, never `sortOrder` (probe P2).
- The app cannot create labels (probe P3). `Blocked` must already exist; if it
  doesn't, return `LABEL_MISSING`.
- Task ticket title: `T<n> · <task name>` (U+00B7). Hidden comment marker:
  `<!-- hermes-linear:<marker> -->`.
- Spec footer, verbatim: `Source: <path> @ <short commit> · edit by asking in this thread`.
- Header line format in spec, plan and ledger files: `Linear: <IDENTIFIER>`.
- Task heading format: `### Task N: <name>`, optionally ending `— <IDENTIFIER>`.
- Commit identity: "Neo (Hermes Agent)" `<retrobitbot@users.noreply.github.com>`.

## Review Focus

1. **A crash between creating a ticket and writing its ID back.** A rerun must find
   the existing child by its `T<n> ·` title, not create a second one. Test:
   `test_sync_plan_crash_before_writeback_no_duplicate` (Task 4).
2. **A plan edited by hand, with headings renamed, reordered or renumbered.**
   Tickets follow the IDs in the headings, not the task numbers. Test:
   `test_sync_plan_renumber_keeps_ids` (Task 4).
3. **A tool call from a Slack or CLI session.** Every write tool returns
   `NOT_LINEAR_SESSION`, and reads still work. Test:
   `test_writes_refused_outside_linear_session` (Task 1).
4. **A Done task removed from the plan.** It is never canceled or edited. Test:
   `test_sync_plan_never_touches_done` (Task 4).
5. **The gateway loop is busy or Linear is slow.** The tool returns `TIMEOUT` after
   its deadline and never hangs the agent's turn. Test:
   `test_tool_times_out_cleanly` (Task 1).

---

### Task 0: Integration probe — RET-88

**Files:**
- Create: `docs/superpowers-integration-notes.md`

**Interfaces:**
- Produces: answers that Tasks 1 and 6 consume, each with a host `file:line`. Each
  question has a default and a fallback.

Answer each question from the Hermes source, read-only at
`/home/hermes/.hermes/hermes-agent`, and, where possible, from a scratch plugin run
under a throwaway `HERMES_HOME`:

- **Q1. Toolset enablement.** Is a plugin tool registered with
  `toolset="linear"` offered to a gateway session on the `linear` platform without
  extra config?
  - Default: yes, through the plugin's platform toolset.
  - Fallback: document the config key the README must tell users to set.
- **Q2. Session env.** Does `get_session_env("HERMES_SESSION_KEY")` /
  `("HERMES_SESSION_PLATFORM")` resolve inside a sync tool handler run by
  `registry.dispatch`?
  - Default: yes, because tools run in the turn's context.
  - Fallback: pass `session_id`/`task_id` kwargs and map them through the adapter's
    turns.
- **Q3. Threading.** On which thread and event loop do sync tool handlers run?
  Confirm that `asyncio.run_coroutine_threadsafe(coro, adapter.event_loop)` and
  waiting on its future with a timeout is safe from there.
- **Q4. Skills.** Confirm `register_skill(name, path, description)` exposes
  `linear:manage-superpowers` to `skill_view`, and that its index line appears in
  `<available_skills>` for the neo profile.
- **Q5. Manifest.** Which manifest keys must declare the tools
  (`provides_tools`) for `validate` to pass?

- [ ] Step 1: write the notes, one section per question: answer, `file:line`, chosen
  default or fallback.
- [ ] Step 2: commit `docs: superpowers integration probe notes`.

### Task 1: Tool runtime and guardrails — RET-80

**Files:**
- Create: `hermes_linear/agent_tools.py`
- Test: `tests/test_agent_tools_runtime.py`

**Interfaces:**
- Consumes: `adapter.ADAPTERS`; `LinearAdapter.event_loop`;
  `TurnEngine.turn_for_session_key(key) -> (Key, Turn) | None` (`turns.py:149`);
  `get_session_env`; the Task 0 answers.
- Produces:
  - `class ToolContext`: `adapter`, `client`, `session_issue_id: str | None`,
    `session_key: str | None`, `is_linear: bool`.
  - `def resolve_context() -> ToolContext`: finds the adapter by
    `HERMES_SESSION_KEY` (in-flight turn), else by message id, the same way
    `hooks._route` does. Reads that work outside Linear use the first adapter in
    `ADAPTERS`.
  - `def run_on_adapter(ctx, coro_factory, *, timeout_s: float = TOOL_TIMEOUT_S) -> dict`:
    calls `run_coroutine_threadsafe` on `ctx.adapter.event_loop` and waits.
    `TimeoutError` → `{"ok": false, "error": "TIMEOUT"}`, and the future is
    cancelled. `LinearError` → its `code`. Any other exception →
    `type(exc).__name__`.
  - `async def check_write_scope(ctx, issue_id: str) -> str | None`: returns `None`
    when allowed, else `"NOT_LINEAR_SESSION"` or `"OUT_OF_SCOPE"`. A target is
    allowed if it is the session issue, is in `ctx.adapter.tool_created` (a set
    per session key), or has the session issue in its parent chain (walk at most
    5 levels via `issue(id){parent{id}}`).
  - `TOOL_TIMEOUT_S = 30.0` in `constants.py`.

- [ ] **Step 1: Write the failing tests.**
  - `test_writes_refused_outside_linear_session`: platform env `slack`, so
    `check_write_scope` returns `"NOT_LINEAR_SESSION"`.
  - `test_scope_allows_self_descendant_created`.
  - `test_scope_refuses_unrelated_issue`: returns `"OUT_OF_SCOPE"`.
  - `test_tool_times_out_cleanly`: a coroutine sleeping 5 s with `timeout_s=0.1`
    gives `{"ok": False, "error": "TIMEOUT"}` within 1 s.
  - `test_linear_error_maps_to_code`: `LinearError(code="RATELIMITED")` gives
    `error == "RATELIMITED"`.
  - `test_resolve_context_by_session_key`: two adapters; the key belongs to the
    second.

  Use the existing fakes: `tests/fake_linear.py`, and the adapter builder from
  `tests/test_adapter_inbound.py` running on a real loop in a thread.
- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_agent_tools_runtime.py`.
  Expected: FAIL (module missing).
- [ ] **Step 3: Implement** the interfaces above. Add
  `tool_created: dict[str, set[str]]` on `LinearAdapter`, cleared when that session's
  turns finish and on disconnect.
- [ ] **Step 4: Run** the file and the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `feat: agent tool runtime and write-scope guard`.

### Task 2: Client operations and generic tools — RET-76

**Files:**
- Modify: `hermes_linear/linear_client.py`
- Modify: `hermes_linear/agent_tools.py`
- Test: `tests/test_agent_tools_generic.py`, `tests/test_linear_client.py`

**Interfaces:**
- Produces on `LinearClient`. Each method has one GraphQL document constant and
  raises `LinearError`:
  - `issue_get(id) -> dict`: `id identifier title url state{id name type} team{id} project{id name externalLinks{nodes{label url}}} parent{id identifier} labels{nodes{id name}} children{nodes{id identifier title subIssueSortOrder state{type}}}`.
  - `issue_create(input: dict) -> dict`.
  - `issue_update(id, input: dict) -> dict`.
  - `comment_create(issue_id, body) -> str`.
  - `comment_update(comment_id, body)`.
  - `app_comments(issue_id) -> list[{id, body}]`: only comments whose `user.id` is
    the viewer.
  - `team_states(team_id) -> list[{id,name,type}]`, cached per client.
  - `team_labels(team_id) -> list[{id,name}]`.
- Produces tools, in `agent_tools.py`. Each is sync, takes `args: dict` and returns
  a dict; JSON schemas go in `TOOL_SCHEMAS`:
  - `linear_issue_get(id)` (no write scope).
  - `linear_issue_create(title, description?, parent?, labels?, state?)`: team and
    project come from the session issue; `parent` must pass
    `check_write_scope`. The created ID is added to `tool_created`.
  - `linear_issue_update(id, title?, description?, state?, add_labels?, remove_labels?)`.
  - `linear_comment(id, body, edit_marker?)`. With a marker: find the app comment
    whose body contains `<!-- hermes-linear:<marker> -->`; edit it or create it,
    with the marker appended.
  - `resolve_state(states, name_or_type) -> str | None`: exact name
    (case-insensitive) first, then the first state of that `type`.

- [ ] **Step 1: Write the failing tests.**
  - `test_issue_get_shape`.
  - `test_create_child_in_scope_records_created`.
  - `test_update_state_by_name_and_type`.
  - `test_labels_add_remove_by_name`.
  - `test_unknown_label_is_label_missing`.
  - `test_comment_edit_marker_edits_existing`.
  - `test_comment_edit_marker_creates_when_absent`.
  - `test_human_comment_with_marker_text_not_edited`: only the app's own comments
    match.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** using `FakeLinear` extensions: issues, comments, labels,
  states, children.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: Linear issue and comment tools`.

### Task 3: `linear_sync_spec` — RET-77

**Files:**
- Create: `hermes_linear/sync.py`
- Modify: `hermes_linear/agent_tools.py`
- Test: `tests/test_sync_spec.py`

**Interfaces:**
- Produces in `sync.py` (pure, with no I/O except the injected `git`):
  - `read_linear_header(text) -> str | None`: the first `^Linear:\s*(\S+)` line.
  - `render_spec(text, path, short_commit) -> str`: the file text plus `\n\n---\n`
    plus the footer.
  - `spec_summary(text) -> str`: from the first `## 1.` heading up to the next
    `## `.
  - `git_short_commit(path) -> str | None`: `git log -1 --format=%h -- <path>` in
    the file's repo.
- Produces the tool `linear_sync_spec(path)`:
  - Reads the file and its header (no header → `NO_LINEAR_HEADER`).
  - Checks write scope.
  - Sets the ticket description to `render_spec`. If that exceeds
    `SPEC_INLINE_MAX`, it uses `spec_summary` plus the footer plus a link line
    instead.
  - Returns `{ok, issue, url, inline: bool}`.

- [ ] **Step 1: Write the failing tests.**
  - `test_header_parse`.
  - `test_render_has_exact_footer`.
  - `test_sync_overwrites_description`.
  - `test_oversize_falls_back_to_summary`: 250k chars gives `inline == False` and a
    description under `SPEC_INLINE_MAX`.
  - `test_missing_header_error`.
  - `test_uncommitted_file_footer_says_uncommitted`: `short_commit` None gives
    `@ uncommitted`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: linear_sync_spec`.

### Task 4: `linear_sync_plan` — RET-78

**Files:**
- Modify: `hermes_linear/sync.py`
- Modify: `hermes_linear/agent_tools.py`
- Test: `tests/test_sync_plan.py`
- Test fixture: `tests/fixtures/plans/channel-plan.md`, a copy of
  `docs/superpowers/plans/2026-11-21-hermes-linear-channel.md`.

**Interfaces:**
- Produces in `sync.py`:
  - `@dataclass PlanTask(number: int, name: str, ticket: str | None, summary: str, line: int)`.
    `summary` is the first non-empty paragraph after the heading, excluding
    `**Files:**` blocks.
  - `parse_plan(text) -> list[PlanTask]`. Heading regex:
    `^### Task (\d+): (.+?)(?:\s+—\s+([A-Z]+-\d+)\b.*)?\s*$`. The first `— <ID>`
    token wins, even when text follows it.
  - `write_ticket_ids(text, mapping: dict[int, str]) -> str`: adds or replaces the
    `— <ID>` suffix on each mapped heading. No other byte changes.
  - `task_title(t) -> str`: `T{n} · {name}`.
- Produces the tool `linear_sync_plan(path)`:
  1. Header → project ticket.
  2. `linear_comment(project, render_plan, edit_marker="plan")`, where
     `render_plan` uses the same footer as the spec.
  3. Fetch the project ticket's children.
  4. Per task:
     - If the heading has an ID: update the title and description. Skip any field
       that is unchanged.
     - Otherwise: find a child whose title starts with `T{n} ·` and adopt it, or
       create one.
  5. Every app-created child whose ID is no longer in the plan, and whose state
     type is not `completed`/`canceled`, is moved to `canceled` with a comment:
     "Removed from the plan in <short commit>."
  6. Set `subIssueSortOrder = 10 * position` on every task ticket.
  7. Write the IDs back to the file. Returns
     `{ok, issue, tasks: {n: id}, created: [...], canceled: [...], file_changed}`.

  Task ticket description: `[Plan](<repo blob url or path>) · Task <n>` plus a
  blank line plus `summary`.

- [ ] **Step 1: Write the failing tests.**
  - `test_parse_fixture_15_tasks`: the channel plan parses into 15 tasks with their
    `RET-` IDs.
  - `test_write_ids_only_changes_headings`.
  - `test_sync_creates_children_in_order`.
  - `test_sync_plan_idempotent`: the second run creates nothing, cancels nothing,
    and leaves the file unchanged.
  - `test_sync_plan_crash_before_writeback_no_duplicate`.
  - `test_heading_with_trailing_text_keeps_id`: `### Task 6: Skill — RET-81 (absorbs RET-82)`
    parses `ticket == "RET-81"` and never creates a new ticket. The manual sync of
    this very plan created a duplicate this way.
  - `test_sync_plan_renumber_keeps_ids`.
  - `test_removed_task_canceled_with_comment`.
  - `test_sync_plan_never_touches_done`.
  - `test_plan_comment_edited_in_place`.
  - `test_order_uses_sub_issue_sort_order`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: linear_sync_plan`.

### Task 5: `linear_task_state` — RET-79

**Files:**
- Modify: `hermes_linear/agent_tools.py`
- Test: `tests/test_task_state.py`

**Interfaces:**
- Produces the tool `linear_task_state(id, state, summary?, question?)`:
  - `in_progress`: state `In Progress`, else type `started`.
  - `in_review`: state named `In Review`, else stay or move to type `started`.
  - `done`: type `completed`. If `summary` is given, post it as a comment on the
    task ticket.
  - `blocked`:
    - Add the team label named `Blocked` (case-insensitive).
    - If there is none, return `LABEL_MISSING` but still post the question.
    - Post `question` as a comment on the project ticket (the session issue):
      `**<identifier> needs input:** <question>`.
  - `unblocked`: remove the label.
  - Returns `{ok, issue, state, label_missing: bool}`.

- [ ] **Step 1: Write the failing tests.**
  - `test_each_transition`.
  - `test_in_review_falls_back_to_started`.
  - `test_done_posts_summary`.
  - `test_blocked_labels_and_asks_on_project_ticket`.
  - `test_blocked_without_label_still_asks`.
  - `test_unblocked_removes_label`.
  - `test_task_state_out_of_scope`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: linear_task_state`.

### Task 6: Skill, registration, platform hint — RET-81

This task also covers the RET-82 scope (project setup), which was canceled into it.

**Files:**
- Create: `hermes_linear/skills/manage-superpowers/SKILL.md`
- Modify: `hermes_linear/plugin.py`, `plugin.yaml`
- Test: `tests/test_plugin_register.py`

**Interfaces:**
- Consumes: `TOOL_SCHEMAS` and the handlers from Tasks 2–5; the Task 0 Q1/Q4/Q5
  answers.
- Produces:
  - `register(ctx)` additionally calls `ctx.register_tool(name, toolset="linear", schema, handler, description, emoji="📐")`
    for each of the 7 tools, and
    `ctx.register_skill("manage-superpowers", <path>, description)`.
  - `plugin.yaml` gains `provides_tools`.
  - `PLATFORM_HINT` gains this sentence, verbatim: "If the repo has
    `.hermes-linear.yaml`, load `linear:manage-superpowers` first."

The SKILL.md frontmatter description, verbatim: "Use when a Linear ticket says
'manage with hermes-linear', or in a repo with .hermes-linear.yaml. Runs
superpowers through Linear." The body covers:
- **Setup** (spec §3 step 2 and §5.1, the RET-82 scope):
  - two selects via `clarify`: framework, then repo;
  - clone the repo if it is missing, into `~/git/<name>`;
  - create a branch `<identifier lowercase>-<slug>` in `.worktrees/<branch>`;
  - commit the `.hermes-linear.yaml` example from §5.1 with real values.
- **Per-stage differences** (§3 steps 3–7): the Linear header lines, when to call
  each tool, selects for every multiple-choice question and approval, and the
  ledger's line 2.
- **Rules:**
  - only the sync tools write to descriptions and plan comments;
  - answers count only when given in the ticket thread;
  - on sync failure: mark `unsynced` in the ledger, retry, and never ask for
    approval while unsynced (§6).

- [ ] **Step 1: Write the failing tests.**
  - `test_tools_registered_in_linear_toolset`: 7 names, all with schemas.
  - `test_skill_registered`.
  - `test_platform_hint_mentions_marker`.
  - `test_skill_frontmatter_valid`: name `manage-superpowers`, description of 60
    chars or fewer per house style, or the Hermes limit.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** Write SKILL.md as an overlay of at most about 150
  lines: it says "load `superpowers:<stage>`, then apply these differences" and
  never restates the superpowers instructions.
- [ ] **Step 4: Run** `pytest` and `hermes plugins validate .`. Expected: PASS, no
  private imports.
- [ ] **Step 5: Commit** `feat: manage-superpowers skill and tool registration`.

### Task 7: setup and status additions — RET-83

**Files:**
- Modify: `hermes_linear/setup_cli.py`, `hermes_linear/store.py`
- Test: `tests/test_plugin_register.py`

**Interfaces:**
- Produces:
  - `Store.last_real_event_at() -> float | None`: the max `at` over
    `seen_webhooks` rows whose `webhook_id` does not start with
    `hermes-linear-test-`.
  - `status` prints `last Linear event:` using it, alongside the existing
    `last event`.
  - `setup`, after its token and viewer check, lists the labels of the viewer's
    teams. If any team lacks `Blocked`, it prints: `Team <KEY> has no "Blocked"
    label — create it in Linear (Settings → Labels) so tasks can be marked
    blocked.`

- [ ] **Step 1: Write the failing tests.**
  - `test_last_real_event_ignores_test_probes`.
  - `test_status_prints_last_linear_event`.
  - `test_setup_warns_missing_blocked_label`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: status last Linear event; setup checks Blocked label`.

### Task 8: Install and M1 dogfood — RET-84

This is a stop point with Mike: a gateway restart, then a live run.

- [ ] **Step 1: Deploy.** Merge the branch into the plugin path used by neo (the
  symlink target). Mike restarts the gateway, then re-enables the app webhook if
  Linear paused it.
- [ ] **Step 2: Kickoff.** Mike creates a "Listener split" ticket in Valkyrie
  saying "manage with hermes-linear" and delegates it to the app.
- [ ] **Step 3: Exit checklist.** Run spec §8 "Milestone 1 exit", steps 1–7.
  Record each result on RET-84.
- [ ] **Step 4: Carry-forwards.** Copy the deferred minors from this plan's SDD
  ledger into spec §9 "Carry-forwards". Commit
  `docs: milestone 1 carry-forwards`.
