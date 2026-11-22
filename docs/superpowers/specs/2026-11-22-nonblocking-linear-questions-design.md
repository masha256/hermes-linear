# Non-blocking Linear questions — design

Linear: RET-94

Status: approved in the RET-94 thread (Q1–Q4 and design sections 1–3), 2026-11-22.

## Goal

A `clarify` question or exec approval asked in a Linear session stays answerable for hours
and across gateway restarts, without any turn sitting blocked on it. An attended ticket keeps
today's fast path. The work is done in the plugin only (public Hermes API, no core changes);
Slack and Telegram are unchanged.

## Decisions

| # | Question | Decision |
|---|----------|----------|
| — | Hybrid vs fully non-blocking | Hybrid. Ask as today (blocking select, Hermes's clarify / approval timeout). Only on timeout does the question become **pending**. |
| — | What drives the switch | Hermes's own timeout. No plugin timer. |
| Q1 | Exec approvals | Deny-and-defer with a one-shot pre-approval of the exact re-run. |
| Q2 | Final reply after a pending select | Post the reply, then re-post the pending question as the last activity. |
| Q3 | Which ticket gets `Blocked` | The session's single open task sub-ticket (tracked from `linear_task_state`), else the session ticket. |
| Q4 | Lifetime | No timer. A question lives until it is answered, replaced by a newer question in the same session, or the ticket is undelegated / re-delegated. |

## Background (facts this design relies on)

- On timeout, the Hermes gateway's clarify wait returns `[user did not respond within Nm]`
  with `answered=False`; the tool result has `"outcome": "timed_out"`. The gateway then calls
  the adapter's `retire_clarify_card(clarify_id, notice)` on the loop
  (`gateway/run_turn_runner.py`). It calls the same method on cancel, `/new` and run end.
- On approval timeout Hermes denies the command (fail closed, "Silence is not consent") and
  calls `edit_message("approval:<rid>")` (integration notes Q10). The approval timeout is
  Hermes's `approvals.timeout`.
- `transform_tool_result` is a public plugin hook. It runs on the agent thread after
  `post_tool_call`, before the result enters context. The first string returned replaces the
  result; it is fail-open.
- Public control APIs used: `tools.clarify_gateway.resolve_gateway_clarify`,
  `tools.approval.resolve_gateway_approval_card`, `gateway.approval_actor.approval_refusal`.
- Linear shows a select only while its elicitation is the agent session's latest activity
  (fixed in `fix-elicitation-visibility`, which this branch builds on).
- Linear shows only the elicitation at question time. A thought posted just before it is not
  seen by the user. Everything needed to answer must be inside the question text.
- Linear appears to hold back a user's `prompted` webhook while the session is `active`. On
  RET-93 (2026-11-22 16:16) a reply typed during a live select was delivered only after a stop.
  An action posted after the elicitation (the pre-fix bug) had made the session active again.
  On the fixed build this is confirmed: in the RET-94 thread (2026-11-22 17:16:37) a reply
  typed during a live select arrived at once and resolved the clarify as its answer.

## States

A row in `pending_controls` (kind `clarify` or `approval`) is in one of two states:

- **live**: the select is posted and a Hermes waiter is blocked on it. This is today's row.
- **pending**: the Hermes waiter is gone (timeout or restart). The question still stands in
  Linear and in the store.

Transitions:

| From | Event | To / effect |
|------|-------|-------------|
| — | `clarify` / `approval_prompt` posts the elicitation | live (as today) |
| live | answer (click or reply) | resolved via Hermes (as today); row dropped |
| live | Hermes timeout → `retire_clarify` / `edit_message("approval:…")` | pending; `Blocked` added; nothing posted |
| live | the plugin's own stop for that session, or a stale generation | row dropped (as today) |
| live | gateway restart (startup sweep) | pending (no posts, no relabel) |
| pending | answer (click or reply) | row dropped, `Blocked` removed, next turn started |
| pending | stop signal on the session | row dropped, `Blocked` removed; stop handled as today |
| pending | a new elicitation in the same session | old row dropped (its `Blocked` stays, the new question re-adds it when it goes pending) |
| pending | undelegation / re-delegation (generation change) | row dropped, `Blocked` removed |

## Components

### 1. Store (`store.py`)

`pending_controls` gains columns, added by an additive migration (`ALTER TABLE … ADD COLUMN`,
the same pattern as `session_key`):

- `state TEXT NOT NULL DEFAULT 'live'` (`live` | `pending`)
- `question TEXT` (the elicitation body; the approval prompt text for approvals)
- `command TEXT` (approvals: `prompt.command`)
- `ticket_id TEXT` (the issue that carries `Blocked`; set at the switch to pending)
- `turn_mid TEXT` (the turn that asked; used by the transform hook and the re-post)

New methods:

- `mark_control_pending(rid, ticket_id)`
- `pending_controls_live()` (startup sweep)
- `controls_for_session_key(key)`

`ControlRow` gains the fields.

New table `open_tasks(issue_id TEXT, task_id TEXT, PRIMARY KEY (issue_id, task_id))`,
keyed by the **session ticket** (the agent tools know `session_issue_id`, not the agent
session id; a row's ticket comes from `sessions.issue_id`), with
`set_open_task(issue_id, task_id, open: bool)` and `open_tasks(issue_id) -> list[str]`.

New table `approval_grants(session_key TEXT, command TEXT, choice TEXT, actor_id TEXT,
turn_mid TEXT, created_at REAL, PRIMARY KEY (session_key, command))`. One use per row;
`turn_mid` is the answer turn, whose end drops the grant.

### 2. Switch to pending (`outbound.py`)

- `retire_clarify(clarify_id)`: if the row is gone or is not `live`, nothing happens. If the
  session has a plugin-issued stop in flight (`turns.stop_message_ids` maps to the session) or
  the row's generation is stale, keep today's behaviour (drop the row and post the expiry
  thought). Otherwise: `mark_control_pending`, add `Blocked` to the target ticket (component
  4), post nothing.
- `edit_message("approval:<rid>")`: the same rule for approvals.
- `_live_controls` (the thought/action suppression from `fix-elicitation-visibility`) treats
  `pending` rows like live ones while the asking turn is in flight, so the switch does not
  reopen the gap that hides the select.

### 3. What the agent sees (`hooks.py`: `on_transform_tool_result`)

Registered with `ctx.register_hook("transform_tool_result", …)`. It runs only in a Linear
session (`HERMES_SESSION_PLATFORM == "linear"`). Outside Linear, or on any error, it returns
`None` and leaves the result untouched.

- **clarify**: when the result's `outcome` is `timed_out` and the store holds a row for this
  session key that is not answered (live or pending), return the same JSON with
  `"outcome": "pending"` and `"notice": "Not answered. The question stays open in the Linear
  ticket; the answer arrives as the next message. End this turn now; do not assume an
  answer."`. The decision comes from the store row and the result, not from the order of the
  loop-side switch.
- **approval**: when a tool result is an approval-timeout denial and an approval row for this
  session key exists (asked during this call), return: `"Command not run: approval is pending
  in Linear. End this turn. If it is approved, the next message says so, and re-running this
  exact command is pre-approved once."`.

The store is read under its lock from the agent thread (it is thread-safe today). The hook
does no I/O.

### 4. `Blocked` target

- `agent_tools` `linear_task_state` records open tasks under the session ticket:
  `in_progress` / `in_review` / `unblocked` → `set_open_task(session_issue_id, task_id, True)`;
  `done` / `blocked` → `False`.
- At the switch to pending: exactly one open task → that issue; otherwise the session ticket.
  The id is stored as `row.ticket_id`.
- Add and remove reuse `agent_tools._blocked_label` and the client's `issue_update`, bounded
  by `ACK_TIMEOUT_S` and spawned so they never block the switch or the answer. A missing label
  is logged at warning once per team, and the question still stands.

### 5. Final reply: re-post (`outbound.send`)

When `send` posts a turn's **first response** (kind `response`) for an agent session that has
a pending row, it queues a re-post of that row's elicitation (same body and options, same
`request_id`) right after the response, on the session's `_SessionWrites` queue. The re-post
is therefore the session's last activity. The re-post uses the stored `question` and
`choices`. Approvals use `renderer.approval_options()` narrowed to the stored choices.

### 6. Answer path (`router.py`, `sessions_open.py`)

The router is unchanged: a `prompted` event for a session with a row still becomes
`ResolveControl` (the allowlist is checked before this). `ResolveControl` carries the row
state. `SessionOpener.resolve_control`:

- **live**: as today.
- **pending, clarify**: drop the row, remove `Blocked` from `row.ticket_id`, enqueue a
  `StartTurn` on the session's Hermes key (`router._session_key`) with
  `turn_header(…)` + `Answer to "<question>": <value>`. `<value>` is the raw reply text (an
  option click arrives as its label).
- **pending, approval**: parse the answer with `renderer.parse_approval_answer`. An
  unrecognised answer is re-asked as today (`REASK_TEXT`). For `once` / `session`: run the
  answer check against the approval's authority captured when it was asked
  (`tools.approval.list_gateway_approval_authority`, stored on the row; checked with
  `gateway.approval_actor.may_answer`). `approval_refusal` cannot be used here: once Hermes
  timed the approval out it is no longer pending, and nothing pending is never refused. On
  refusal post the refusal text and keep the row; a row with no stored authority is refused. Otherwise store a grant `(session_key, command, choice, actor_id)`, drop
  the row, remove `Blocked`, and start the turn `Answer to approval for \`<command>\`: approved
  (<choice>)`. For `deny`: drop the row, remove `Blocked`, and start the turn `Answer to approval
  for \`<command>\`: denied`.
- A `stop` signal on a session with a pending row (`Interrupt`): drop the row, remove
  `Blocked`, then stop as today.
- `SetMode` to `untouched`, or a generation change: drop every row of the issue's sessions
  and remove `Blocked`.

### 7. Approval grant on the re-run (`outbound.approval_prompt`)

Before posting, look up a grant for `(prompt.session_key, prompt.command)`. If one exists:
consume it (delete), run `approval_refusal(session_key, grant.actor_id, request_id=rid)`, and
on no refusal call `resolve_gateway_approval_card(session_key, grant.choice, rid,
actor_id=grant.actor_id)`. No select is posted. If the resolve fails, fall through to a
normal prompt. A command that differs in any byte gets a normal prompt. Grants left unused are
deleted when the answer turn ends (`turn_finished` for that turn's message id, not for any other
turn of the shared ticket session), so a grant never
outlives the turn it was made for.

**Feasibility gate (plan task 1):** a unit test against the real `tools.approval` must show
that resolving from inside `approval_prompt` unblocks the waiting agent thread. If it does
not, approvals fall back to Q1 option 2 (the re-run is asked again; no grant) and the user is
told.

### 8. Restart

At `connect`, after the store opens: `pending_controls_live()` → `mark_control_pending(rid,
ticket_id)` with the target computed as in component 4. No posts: the select is either still
the latest activity, or a later activity hid it, and then the next reply still answers it. No
relabel calls beyond adding `Blocked` once.

### 9. Skill text

- Plugin `hermes_linear/skills/manage-superpowers/SKILL.md`, "Questions and approvals":
  - one question per `clarify`, everything needed to answer inside the question text;
  - `outcome: pending` → end the turn immediately, do not restate (the plugin re-posts it);
  - an `Answer to "…"` message is the answer to that question;
  - approval pending → end the turn; `Answer to approval for … : approved` → re-run the exact
    same command.
- Neo skill `linear-agent-sessions`: the same rules; rules 2–3 (short-lived selects, restate on
  timeout) are rewritten for pending questions.

## Error handling

- Every Linear write in the new paths is bounded (`ACK_TIMEOUT_S`) and logged on failure. A
  failed label write never blocks the switch or the answer.
- A failed re-post leaves the row pending. The next reply still answers it, because the
  answer path is driven by the store, not by the UI.
- A row whose ticket no longer resolves (deleted ticket) is dropped on the answer attempt,
  with an info log.
- The transform hook is fail-open: any exception returns `None`.

## Testing

Unit (pytest, through the real registry and hook dispatch where a contract is involved):

- store migration on an old database; the new methods;
- `retire_clarify` → pending (no post) and stop → drop + expiry; the same for approvals;
- the transform hook rewrites the clarify and approval results only in Linear sessions with a
  row, and is a no-op elsewhere;
- re-post ordering: response then elicitation, last on the queue;
- pending answer → `StartTurn` text and `Blocked` removal; live answer unchanged; stop while
  pending; undelegation;
- `Blocked` target: zero, one or several open tasks;
- startup sweep;
- approval grant: match → auto-resolve, mismatch → normal prompt, refusal → normal prompt,
  expiry at turn end;
- the feasibility test against real `tools.approval` (component 7).

Live, in the **hermes linear test project**, after `scripts/deploy-live` and Mike's gateway
restart:

1. A clarify left unanswered past 90 s: the session shows the select as the last activity,
   `Blocked` is set; answered minutes later, the next turn carries `Answer to "…"`, and
   `Blocked` is gone.
2. A gateway restart between asking and answering: same result.
3. A reply typed during a live select resolves it inside the turn (no "Stopped.").
4. An approval left unanswered: the command is not run and the select is re-posted; Approve
   later → the turn re-runs the command without a second prompt.

Then a PR. The activity order is checked with `agentSession { activities }` for each case.

## Out of scope

- Several questions in one `clarify` call: the skill already asks one per call. If several
  are asked, only the posted one is tracked.
- Approvals with `always`: Linear never offers it (Q8 narrowing stays).
- Any change to the Hermes clarify / approval timeouts or their defaults.
