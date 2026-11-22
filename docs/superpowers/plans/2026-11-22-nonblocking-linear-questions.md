# Non-blocking Linear Questions Implementation Plan

Linear: RET-94

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Linear `clarify` question or exec approval that times out becomes a stored *pending*
question: it stays answerable for hours and across restarts, and its answer starts the next turn.

**Architecture:** The existing `pending_controls` row gains a `live → pending` state, switched by
Hermes's own timeout callbacks (`retire_clarify_card`, approval `edit_message`). A
`transform_tool_result` hook tells the agent the question is pending. The outbound queue re-posts
the question after the turn's final response. The router's existing `ResolveControl` path turns
an answer to a pending row into a framed `StartTurn`. Approvals add a one-shot grant consumed by
the re-run's `approval_prompt`.

**Tech Stack:** Python 3.11, asyncio, sqlite3, pytest (`asyncio_mode=auto`), the Hermes plugin API.

**Spec:** `docs/superpowers/specs/2026-11-22-nonblocking-linear-questions-design.md`

## Global Constraints

- Plugin-only: no edits under `~/.hermes/hermes-agent`; Hermes public API only
  (`tools.clarify_gateway.resolve_gateway_clarify`, `tools.approval.resolve_gateway_approval_card`,
  `gateway.approval_actor.approval_refusal`, plugin hook `transform_tool_result`).
- Outside Linear sessions every new hook returns `None`; Slack and Telegram behaviour is unchanged.
- Hermes's timeouts drive the switch; the plugin adds no timer and no expiry.
- Every Linear write in the new paths is bounded by `constants.ACK_TIMEOUT_S`, logged on failure
  and never raises into the caller.
- Exact copy (verbatim):
  - clarify notice: `Not answered. The question stays open in the Linear ticket; the answer arrives as the next message. End this turn now; do not assume an answer.`
  - approval result: `Command not run: approval is pending in Linear. End this turn. If it is approved, the next message says so, and re-running this exact command is pre-approved once.`
  - answer turn (clarify): `Answer to "<question>": <value>`
  - answer turn (approval): ``Answer to approval for `<command>`: approved (<choice>)`` / ``Answer to approval for `<command>`: denied``
- The label name is `agent_tools.BLOCKED_LABEL` (`"Blocked"`), matched case-insensitively.
- Tests run from the worktree `.worktrees/ret94` (a valid identifier) with `.venv/bin/pytest -q`.
  The full suite stays green after every task, and `hermes plugins validate .` passes.

## Review Focus

1. A reply to a pending question that is not an option label (free prose) must start the answer
   turn verbatim, never be re-asked (clarify). For approvals an unparseable answer is re-asked
   (`REASK_TEXT`) and the row stays pending. Owner: Task 7.
2. Two Linear sessions on the same ticket (a delegation session plus a mention thread): a pending
   row belongs to its own `agent_session_id`; a reply in the other session must not answer it.
   Owner: Task 7.
3. A gateway restart while the asking turn is still in flight: the sweep marks the row pending, and
   the re-post after the response must still fire for the next response (state comes from the
   store, not from memory). Owner: Tasks 6 and 9.
4. An approval grant must never approve a different command, another session key, or outlive its
   answer turn. Owner: Task 8.
5. Hermes's cancel paths (`/stop`, a stale generation) also call `retire_clarify_card`; they must
   not create a pending question. Owner: Task 3.

---

### Task 1: Approval-grant feasibility gate — RET-96

**Files:**
- Test: `tests/test_approval_grant_feasibility.py`

**Interfaces:**
- Consumes: `tools.approval` (real module from the pinned hermes-agent the tests already import).
- Produces: a go/no-go for Task 8, recorded in the task's ledger entry and the sub-ticket summary.

- [ ] **Step 1: Write the test** `test_resolve_from_inside_notify_unblocks_waiter`. It registers
  a gateway notify callback for a session key, starts a real gated approval wait on a worker
  thread (the same entry point `tools.approval` uses for a gateway approval; follow
  `register_gateway_notify` / `submit_pending` / `resolve_gateway_approval_card` as the existing
  Hermes approval tests do). From inside the notify callback, on an asyncio loop, the test calls
  `resolve_gateway_approval_card(session_key, "once", request_id, actor_id=<actor>)`. Assert that
  the waiter returns an approved outcome within 2 s, and that the callback saw `request_id`.
- [ ] **Step 2: Run it.** `.venv/bin/pytest tests/test_approval_grant_feasibility.py -v`.
- [ ] **Step 3: Decide.**
  - PASS: commit the test; Task 8 implements the grant.
  - FAIL for a reason the plugin cannot change: stop. Report to the controller, who asks the user
    (`linear_task_state blocked`) whether to switch Task 8 to "ask again" (spec Q1 option 2).
- [ ] **Step 4: Commit** `test: approval resolved from inside the prompt callback (RET-94 T1)`.

### Task 2: Store — pending state, open tasks, approval grants — RET-97

**Files:**
- Modify: `hermes_linear/store.py` (schema, `Store.__init__` migration, `ControlRow`, control methods)
- Test: `tests/test_store.py`

**Interfaces:**
- Produces:
  - `ControlRow` new fields (all defaults `None`, except `state: str = "live"`): `state`,
    `question: str | None`, `command: str | None`, `ticket_id: str | None`, `turn_mid: str | None`.
  - `put_control(request_id, kind, agent_session_id, execution_generation, choices, *,
    session_key=None, question=None, command=None, turn_mid=None) -> None`. It always writes
    `state='live'`; existing callers keep working.
  - `mark_control_pending(request_id: str, ticket_id: str | None) -> bool` (False if the row is gone).
  - `live_controls() -> list[ControlRow]`
  - `controls_for_session_key(session_key: str) -> list[ControlRow]` (newest first)
  - `controls_for_issue(issue_id: str) -> list[ControlRow]` (joined through `sessions.issue_id`)
  - `set_open_task(issue_id: str, task_id: str, open: bool) -> None`; `open_tasks(issue_id: str) -> list[str]`
  - `put_grant(session_key: str, command: str, choice: str, actor_id: str, turn_mid: str) -> None`
    (replaces; `turn_mid` is the answer turn's message id, a column of `approval_grants`);
    `take_grant(session_key: str, command: str) -> tuple[str, str] | None` (choice, actor_id;
    deletes the row); `drop_grants_for_turn(turn_mid: str) -> None`

- [ ] **Step 1: Failing tests.**
  - `test_migrates_old_pending_controls`: a db created with the old `pending_controls` DDL (no
    new columns) plus one row opens. The row reads back with `state == "live"` and `question is None`.
  - `test_mark_pending_and_queries`: put two controls; `mark_control_pending(rid, "ISSUE")` →
    True; `control(rid).state == "pending"` and `.ticket_id == "ISSUE"`; `live_controls()` lists
    only the other one; `mark_control_pending("gone", None)` is False.
  - `test_controls_for_issue_and_key`.
  - `test_open_tasks_set_and_clear`: set A and B, clear A → `["B"]`; idempotent.
  - `test_grant_is_one_use`: `put_grant("K", "c", "once", "u1", "m1")` → `take_grant("K", "c")`
    returns `("once", "u1")` → a second `take_grant` is None; `drop_grants_for_turn("m1")` clears
    only that turn's grants.
- [ ] **Step 2:** run, expect FAIL.
- [ ] **Step 3: Implement.** Use `ALTER TABLE … ADD COLUMN` guarded by `PRAGMA table_info`, as
  for `session_key`. New tables go in `_SCHEMA` with `CREATE TABLE IF NOT EXISTS`. All writes go
  through `_write`, all reads take `_lock`.
- [ ] **Step 4:** run `tests/test_store.py`, then the full suite: PASS.
- [ ] **Step 5: Commit** `feat: store pending questions, open tasks and approval grants (RET-94 T2)`.

### Task 3: Switch to pending and the Blocked label — RET-98

**Files:**
- Create: `hermes_linear/questions.py` (pending-question helpers: target, label, copy)
- Modify: `hermes_linear/outbound.py` (`clarify`, `approval_prompt` store the question;
  `retire_clarify`, `edit_message`, `_live_controls`)
- Test: `tests/test_pending_questions.py` (new; reuse the `env` / `session_env` fixtures from
  `test_adapter_outbound` and the delegation helpers from `test_adapter_inbound`, as
  `test_elicitation_visibility.py` does)

**Interfaces:**
- Consumes: Task 2 store methods.
- Produces (`questions.py`):
  - constants `PENDING_NOTICE`, `APPROVAL_PENDING_TEXT` (exact copy from Global Constraints).
  - `blocked_target(store: Store, row: ControlRow) -> str | None`: the session ticket is
    `store.session(row.agent_session_id).issue_id`; `open_tasks(ticket)` of length 1 → that id;
    otherwise the ticket; None if the session row is unknown.
  - `async set_blocked(client, issue_id: str, on: bool) -> bool`. It does `issue_get`, finds the
    label via `client.team_labels(team)` (same match as `agent_tools._blocked_label`), and calls
    `issue_update` with `addedLabelIds` / `removedLabelIds` only when that changes something. It
    is bounded by `ACK_TIMEOUT_S` and returns False on a missing label or any failure. The first
    missing label per team logs a warning; later ones log at debug.
- Produces (`outbound.py`):
  - `clarify(...)` passes `question=str(question)` and `turn_mid=target.mid` to `put_control`.
  - `approval_prompt(...)` passes `question=prompt.text`, `command=prompt.command` and `turn_mid`.
  - `async defer(rid: str, kind: str) -> bool`: on the loop, if the row is `live` and not
    cancelled, marks it pending, sets `ticket_id`, and spawns `set_blocked(…, True)`; True if
    deferred. "Cancelled" means `rid`'s session has an entry in `turns.stop_message_ids` values,
    or the row's `execution_generation` differs from the ticket's current generation.
  - `retire_clarify(id)` → `defer(id, "clarify")`, falling back to today's `_expire` when not
    deferred. `edit_message("approval:<rid>")` → `defer(rid, "approval")`, falling back to
    `_expire(…, APPROVAL_EXPIRED_TEXT)`.
  - `_live_controls(sid)` keeps both states while the asking turn is in flight (unchanged rule:
    row present and turn in flight).
  - A new elicitation replaces the old one: `clarify` / `approval_prompt` drop the session's
    `pending` rows before `_put_control`. Their `Blocked` stays.

- [ ] **Step 1: Failing tests.**
  - `test_clarify_timeout_defers`: a delegated turn; the elicitation is posted; call
    `adapter.retire_clarify_card(cid, "expired")`. Assert the row's `state == "pending"`, no new
    activity was posted (no `This request expired.`), and the fake client got one `issue_update`
    with `addedLabelIds == [<Blocked id>]` on the session ticket.
  - `test_clarify_timeout_during_stop_expires`: same, but the session has a stop in flight
    (`a.turns.stop_message_ids["x"] = sid`). The row is dropped and the expiry thought is posted.
  - `test_approval_timeout_defers`: same as the first, via `edit_message("approval:<rid>")`; the row
    keeps `command`.
  - `test_blocked_target_open_task`: with `store.set_open_task(ticket, "T1", True)` the label goes
    to `T1`; with two open tasks it goes to the ticket.
  - `test_label_missing_still_defers`: the fake team has no `Blocked` label. The row is still
    pending, nothing is posted, and no exception is raised.
  - `test_new_question_replaces_pending`: a pending row, then a new `clarify` in the same session
    → only the new (live) row remains.
- [ ] **Step 2:** run, expect FAIL.
- [ ] **Step 3: Implement** as in Interfaces. The fake client in `tests/fake_linear.py` may need
  `team_labels` / `issue_get` with labels (check `test_task_state.py` for the existing fakes and
  reuse them).
- [ ] **Step 4:** the new tests, `test_elicitation_visibility.py` and the full suite: PASS.
- [ ] **Step 5: Commit** `feat: timed-out Linear questions become pending with Blocked (RET-94 T3)`.

### Task 4: Track open task sub-tickets — RET-99

**Files:**
- Modify: `hermes_linear/agent_tools.py` (`_task_state`)
- Test: `tests/test_task_state.py`

**Interfaces:**
- Consumes: `Store.set_open_task` (Task 2). The store is reached as `ctx.adapter._store`; skip
  when the adapter or store is None.
- Produces: open-task rows keyed by `ctx.session_issue_id`, with the task's UUID (`task["id"]`).

- [ ] **Step 1: Failing tests.** `test_task_state_tracks_open_tasks`: `in_progress` adds the id;
  `in_review` keeps it; `done` removes it; `blocked` and `unblocked` remove it. A failed call
  (refusal) changes nothing.
- [ ] **Step 2:** run, expect FAIL.
- [ ] **Step 3: Implement.** Record the change after the Linear writes succeed, at the end of each
  branch of `_task_state`.
- [ ] **Step 4:** `tests/test_task_state.py` and the full suite: PASS.
- [ ] **Step 5: Commit** `feat: linear_task_state records open tasks (RET-94 T4)`.

### Task 5: Tell the agent the question is pending (`transform_tool_result`) — RET-100

**Files:**
- Modify: `hermes_linear/hooks.py` (new `on_transform_tool_result`), `hermes_linear/plugin.py`
  (register it)
- Test: `tests/test_pending_questions.py`, `tests/test_plugin_register.py`

**Interfaces:**
- Consumes: `questions.PENDING_NOTICE`, `questions.APPROVAL_PENDING_TEXT`,
  `Store.controls_for_session_key`.
- Produces: `on_transform_tool_result(tool_name: str, args: Any = None, result: Any = None, **kw) -> str | None`.
  - Only when `get_session_env("HERMES_SESSION_PLATFORM") == "linear"` and
    `HERMES_SESSION_KEY` is bound.
  - It looks up rows via every adapter in `adapter_mod.ADAPTERS` (read-only, under the store lock).
  - **clarify** (direct or bridged, use `_silent`): `result` parses as JSON with
    `outcome == "timed_out"`, and a clarify row exists for the key. Return the JSON with
    `outcome` set to `"pending"` and `notice` set to `PENDING_NOTICE`; the responses are untouched.
  - **approval**: `result` is a string containing `timed out without user response`, and an
    approval row exists for the key whose `command` occurs in `json.dumps(args)`. Return
    `APPROVAL_PENDING_TEXT`.
  - Otherwise, or on any exception, return `None`.

- [ ] **Step 1: Failing tests.**
  - `test_transform_rewrites_timed_out_clarify`: a Linear session env; a clarify row for the key;
    a `timed_out` clarify result → returns JSON with `outcome == "pending"` and
    `notice == PENDING_NOTICE`.
  - `test_transform_ignores_answered_and_non_linear`: an `answered` result → None; the same
    timed-out result with platform `slack` → None; no row → None.
  - `test_transform_rewrites_approval_timeout`: an approval row with command `rm -rf build`; tool
    `terminal`, args `{"command": "rm -rf build"}`, a result containing
    `timed out without user response` → `APPROVAL_PENDING_TEXT`; a different command → None.
  - `test_plugin_registers_transform_hook` in `test_plugin_register.py`: `register(ctx)`
    registers `transform_tool_result`. Also, through the real `hermes_cli` hook dispatch (as the
    existing register test does for `pre_tool_call`), invoking it returns the rewritten string.
- [ ] **Step 2:** run, expect FAIL.
- [ ] **Step 3: Implement.** The hook does no I/O and no loop hops: it runs on the agent thread,
  and `Store` is thread-safe.
- [ ] **Step 4:** the new tests and the full suite: PASS; `hermes plugins validate .` OK.
- [ ] **Step 5: Commit** `feat: agent sees Linear questions as pending, not timed out (RET-94 T5)`.

### Task 6: Re-post the pending question after the final response — RET-101

**Files:**
- Modify: `hermes_linear/outbound.py` (`send`, new `_repost_pending`)
- Test: `tests/test_pending_questions.py`

**Interfaces:**
- Consumes: `Store.control_for_session` and the Task 2 fields.
- Produces: `_repost_pending(sid: str) -> None` (sync; it queues on `self.writes`). It is called
  right after a first `response` write is queued in `send`, and when `turn_finished` promotes the
  last thought to the response. If the newest row for `sid` is `pending`, it queues the
  elicitation: `renderer.elicitation(row.question)` with `signal="select"` and options, either
  `[{"label": v, "value": v}]` for clarify choices or `renderer.approval_options()` narrowed to
  `row.choices` for approvals. A clarify with no choices is re-posted with no signal.

- [ ] **Step 1: Failing tests.**
  - `test_final_response_then_repost`: defer a clarify (Task 3 path), then `a.send(...)` the final
    reply. The last two activities of the session are `response` and then `elicitation`, with the
    same body and options as the original.
  - `test_no_repost_without_pending`: an ordinary turn → the last activity is `response`.
  - `test_repost_after_promoted_thought`: the turn ends with only commentary
    (`turn_finished(SUCCESS)` promotes it) → `response`, then `elicitation`.
  - `test_repost_survives_memory_loss`: clear `a.outbound.elicited` (simulated restart state),
    then the response → the re-post still happens.
- [ ] **Step 2:** run, expect FAIL.
- [ ] **Step 3: Implement.** Order is guaranteed by `_SessionWrites` (queue at call time).
- [ ] **Step 4:** the new tests, `test_adapter_outbound.py` and the full suite: PASS.
- [ ] **Step 5: Commit** `feat: re-post a pending Linear question after the final response (RET-94 T6)`.

### Task 7: Answering a pending question starts the next turn — RET-102

**Files:**
- Modify: `hermes_linear/models.py` (`ResolveControl` gains `state: str = "live"`),
  `hermes_linear/router.py` (pass `control.state`), `hermes_linear/sessions_open.py`
  (`resolve_control`), `hermes_linear/adapter.py` (`Interrupt` and `SetMode` handling)
- Test: `tests/test_pending_questions.py`, `tests/test_router_agent.py`

**Interfaces:**
- Consumes: Task 2 store methods, `questions.set_blocked`, `router.turn_header`,
  `router._session_key`, `SessionOpener._enqueue`.
- Produces:
  - `SessionOpener.answer_pending(d: ResolveControl, row: ControlRow) -> None`: clarify →
    `StartTurn` with text `f"{turn_header(ident, root_comment_id, user_name)}\n\nAnswer to \"{row.question}\": {d.answer}"`.
    The approval branches are as in spec §6 (grant via `store.put_grant(…, turn_mid=<the answer turn's mid: router.message_id(sid, activity_id)>)`; refusal keeps the row;
    an unparseable answer re-asks). Each branch drops the row and spawns
    `set_blocked(row.ticket_id, False)` when `ticket_id` is set. `ResolveControl` needs
    `user_name`: add `user_name: str | None = None` to it and set it in the router.
  - `SessionOpener.drop_pending(rows: list[ControlRow]) -> None`: drop the rows and unlabel them.
  - adapter: `Interrupt` → first `drop_pending` of the session's pending rows. `SetMode` with
    mode `untouched`, or a generation change, → `drop_pending(store.controls_for_issue(issue_id))`.

- [ ] **Step 1: Failing tests.**
  - `test_click_answers_pending_clarify`: deferred clarify; deliver a `prompted` whose body is
    the option label. A new gateway event arrives whose text ends with
    `Answer to "Workflow framework?": superpowers`; the row is gone; one `issue_update` has
    `removedLabelIds`.
  - `test_prose_answers_pending_clarify` (Review Focus 1): body `use the other repo please` →
    the same framing, verbatim.
  - `test_live_answer_unchanged`: a live row → `resolve_gateway_clarify` is called (stub), and no
    new turn starts.
  - `test_other_session_does_not_answer` (Review Focus 2): a pending row on session A; a
    `prompted` on mention session B of the same ticket → an ordinary turn, and A's row stays.
  - `test_pending_approval_approve_and_deny`: Approve → grant stored, turn text
    ``Answer to approval for `rm -rf build`: approved (once)``; Deny → no grant, `: denied`;
    gibberish → `REASK_TEXT` elicitation and the row stays pending; refusal (stub
    `_approval_refusal` returns a name) → refusal thought, row kept.
  - `test_stop_while_pending_drops`: a pending row and a `stop` prompted → row dropped,
    unlabelled, stop handled (the `Stopped.` response as today).
  - `test_undelegation_drops_pending`: a pending row, then the issue is undelegated → row dropped
    and unlabelled.
- [ ] **Step 2:** run, expect FAIL.
- [ ] **Step 3: Implement.** The answer turn is a normal human turn (`human=True`), so the queue
  and ack behave as for any prompt.
- [ ] **Step 4:** the new tests, `test_router_agent.py`, `test_adapter_inbound.py` and the full
  suite: PASS.
- [ ] **Step 5: Commit** `feat: answers to pending Linear questions start the next turn (RET-94 T7)`.

### Task 8: One-shot approval grant on the re-run — RET-103

(Only if Task 1 passed. Otherwise: the grant is not stored, the approve turn text ends with
` — re-run the command; it will ask again`, and this task only tests that.)

**Files:**
- Modify: `hermes_linear/outbound.py` (`approval_prompt`, `turn_finished`)
- Test: `tests/test_pending_questions.py`

**Interfaces:**
- Consumes: `Store.take_grant`, `Store.drop_grants_for_turn`, `sessions_open._approval_refusal`,
  `sessions_open._resolve_approval_card` (module-level, stubbable).
- Produces: in `approval_prompt`, before `_put_control`:
  `grant = store.take_grant(prompt.session_key, prompt.command)`. With a grant, and
  `_approval_refusal(session_key, actor, request_id=rid)` returning None, and
  `_resolve_approval_card(session_key, choice, rid, actor_id=actor)` returning truthy: return
  `SendResult(success=True, message_id=f"approval:{rid}")` without posting. Otherwise continue
  with the normal prompt. `turn_finished` calls `store.drop_grants_for_turn(turn.mid)`: only the
  answer turn's own end drops its grant, never another turn of the shared ticket session that
  happened to be in flight.

- [ ] **Step 1: Failing tests** (Review Focus 4).
  - `test_grant_auto_approves_exact_rerun`: grant `("K", "rm -rf build", "once", "u1")`; an
    approval prompt for `K`, `rm -rf build` → the resolve stub is called with
    `("K", "once", rid, actor_id="u1")`, no elicitation is posted, and the grant is gone.
  - `test_grant_ignores_other_command_and_key`: `rm -rf build2`, or key `K2` → a normal
    elicitation, and the grant stays.
  - `test_grant_refused_falls_back`: the refusal stub returns a name → a normal elicitation.
  - `test_grant_dropped_at_turn_end`: `turn_finished` of the answer turn → `take_grant` is None;
    `turn_finished` of another turn on the same session key leaves the grant.
- [ ] **Step 2:** run, expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** the new tests, Task 1's test and the full suite: PASS.
- [ ] **Step 5: Commit** `feat: approved pending command runs once without a second prompt (RET-94 T8)`.

### Task 9: Restart sweep — RET-104

**Files:**
- Modify: `hermes_linear/adapter.py` (`connect`, after the store opens and before the worker starts)
- Test: `tests/test_pending_questions.py`

**Interfaces:**
- Consumes: `Store.live_controls`, `Store.mark_control_pending`, `questions.blocked_target`,
  `questions.set_blocked`.
- Produces: `LinearAdapter._sweep_live_controls() -> None`. Every live row becomes pending with
  its target; label writes are spawned (bounded); nothing is posted.

- [ ] **Step 1: Failing tests.**
  - `test_connect_marks_live_rows_pending`: a store with a live clarify row → after `connect`,
    the row is pending, one `addedLabelIds` write was made, and no activity was posted.
  - `test_restart_then_answer` (Review Focus 3): after the sweep, a `prompted` answer → the framed
    turn (Task 7 path), and a later response re-posts nothing (the row is gone).
- [ ] **Step 2:** run, expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** the new tests and the full suite: PASS.
- [ ] **Step 5: Commit** `feat: pending Linear questions survive a gateway restart (RET-94 T9)`.

### Task 10: Skill text — RET-105

**Files:**
- Modify: `hermes_linear/skills/manage-superpowers/SKILL.md` ("Questions and approvals")
- Modify (outside the repo, not committed): `~/.hermes/profiles/neo/skills/productivity/linear-agent-sessions/SKILL.md`
  (via `skill_manage` patch, rules 2–3)
- Test: `tests/test_plugin_register.py` (the skill still registers; frontmatter description ≤ 60 chars)

**Interfaces:** none (documentation). Content, per spec §9:
- one question per `clarify`, everything needed to answer inside the question text (Linear hides
  the message before it);
- `outcome: pending` → end the turn now and don't restate (the plugin re-posts it);
- a message `Answer to "…": …` is the answer to that question;
- `Command not run: approval is pending in Linear…` → end the turn;
  ``Answer to approval for `<cmd>`: approved (…)`` → re-run exactly `<cmd>`;
- the `linear_task_state blocked` flow is unchanged.

- [ ] **Step 1:** edit both texts; keep `manage-superpowers` within its current structure.
- [ ] **Step 2:** `.venv/bin/pytest -q` and `hermes plugins validate .`: PASS.
- [ ] **Step 3: Commit** `docs: skills describe pending Linear questions (RET-94 T10)`.

### Task 11: Deploy live and verify — RET-106

**Files:** none (deployment and live checks; findings go in the ledger).

- [ ] **Step 1:** push the branch; CI green on the push.
- [ ] **Step 2:** `scripts/deploy-live ret-94-nonblocking-questions`; `hermes -p neo linear status`
  shows the new commit on disk. **Stop point:** ask Mike to restart the gateway
  (`linear_task_state blocked`, question "deployed <sha>; please restart the gateway").
- [ ] **Step 3:** run the spec's four live cases in the **hermes linear test project**. Check each
  with `agentSession { activities }`: the order, the last activity being the elicitation while
  pending, `Blocked` on and off, and the answer turn text. Case 2 needs a second restart by Mike.
- [ ] **Step 4:** record the results (activity order excerpts) in the ledger and the sub-ticket's
  `done` summary. Then go to Finish (PR; never merge without Mike).
