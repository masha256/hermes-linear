# Writes from outside a Linear session — design

Linear: RET-148

Status: approved in the RET-148 thread (Q1, Q2), 2026-11-24. Q2.1, Q2.7, §2 item 2, §3, §5
and §6 amended at the rebase onto RET-121 (controller rulings), 2026-11-24.

## Goal

A Hermes session that is not a Linear session (Slack, Telegram, the CLI, cron) can open a
Linear ticket and, under the same rule, comment on, update or attach to one, as the
profile's Linear app. Permission is the toolset: a profile whose platform toolsets list
`hermes_linear` is granted the write tools, and a new setting decides whether each such
write asks a human first. Today every one of those writes refuses with
`NOT_LINEAR_SESSION`, whatever the configuration.

Constraints: public Hermes plugin API only, no core changes; tools never raise; every
write is attributed to the app; behaviour inside Linear sessions does not change.

## Decisions

| # | Question | Decision |
|---|----------|----------|
| Q1 | Who writes, as whom | Each profile writes with its **own** Linear credentials and settings (its own adapter). No cross-profile bridge: Hermes imports a separate module copy per profile scope, so a profile without credentials (raymond today) still answers `NOT_CONNECTED`. A credentials-only "tools-only" mode is a separate ticket. |
| Q2 | Policy for writes outside a session | A new setting `outside_session_writes: deny \| approve \| allow`, default `approve`, separate from `out_of_scope_writes` (Linear sessions are driven by workspace text; other sessions by an allowlisted user, so the two risks get separate knobs). `approve` asks a human in the calling chat through Hermes's own approval gate, the same `pre_tool_call` → `approve` path RET-119 built. |
| Q2.1 | Team / project of a new issue outside a session | **Amended at the rebase onto RET-121** (controller rulings 1–3). `linear_issue_create` takes RET-121's `team` / `project` and every other RET-121 field outside a session too, resolved through RET-121's directory and `issue_fields` (misses refuse with `candidates`; `SELF_DELEGATION` / `ASSIGNEE_IS_APP` before any write). The base is RET-121's: the `parent`'s team and project; without a parent, settings `default_team` / `default_project`. No parent, no `team` and no `default_team` → `BAD_ARGS`. RET-121's Ruling R8 applies to that base. Inside a session create is exactly RET-121's. (Originally: RET-148's own `team` / `project` resolution, and inside a session a differing `team` / `project` was `BAD_ARGS`.) |
| Q2.2 | Prompt-injection carry-over | Issues and comments written outside a session get an origin footer `Filed by <profile> via <platform>` (both values from the session, never from args). |
| Q2.3 | RET-119's "A Slack approval must never authorize a Linear write" | **Reversed** (RET-119 spec, Coverage list, struck there). Its reason was Linear's *deferred* answers ("answers only count in the ticket thread"). Off Linear, core's gate is answered synchronously inside the turn by the chat's own authorized user, as for any dangerous shell command. |
| Q2.4 | Approval grain | Inside a Linear session unchanged: `rule_key = <tool>:<digest>`. The plugin can't narrow another platform's card, so Slack shows Hermes's full choice set; *session* and *always* then cover only identical calls (same tool and arguments). Outside rule keys are `<tool>:outside:<digest>` (final review), so permanent off-Linear grants never apply in Linear sessions (which still ask once/deny in the ticket thread), nor in-Linear keys off Linear. "Outside" is `tool_approval.is_linear_platform` false, the one predicate the tool context and the hook share; an unbound `HERMES_SESSION_PLATFORM` counts as outside. |
| Q2.5 | No human present | Under `approve`, Hermes's presence rules block (cron follows `approvals.cron_mode`). Under `allow`, it writes. |
| Q2.6 | What stays session-only | `linear_sync_spec`, `linear_sync_plan`, `linear_task_state`, `linear_session_link`, `linear_attach` with `embed`: `NOT_LINEAR_SESSION` as today. Issues created outside a session are not remembered for follow-ups (v2): every later write to them is an outside write again. |
| Q2.7 | RET-121's new tools outside a session (rebase) | `linear_relation` and `linear_comment` `reply_to` / `resolve` are gated writes on issues: they follow `outside_session_writes` like `linear_comment` (every end is a target; a reply carries the footer). `linear_lookup` is a read and works anywhere (outside it needs `team` / `project` where the kind uses one, as RET-121 has it). `linear_task_state` `blocked_by` stays session-only with the rest of that tool. |

## 1. Configuration

New plugin settings (`plugins.entries.linear.settings.<key>`; `settings.py`, `Settings`
dataclass, validated in `load_settings` like `out_of_scope_writes`):

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `outside_session_writes` | string | `approve` | `approve`: every write from a non-Linear session asks a human in that chat first. `deny`: refuse (`NOT_LINEAR_SESSION`, today's behaviour). `allow`: write without asking. Case-insensitive; any other value is a `SettingsError`. |
| `default_team` | string | `""` | Base team for `linear_issue_create` outside a session without a `parent`: UUID, key (`RET`) or name, resolved like RET-121's `team`. Empty: the call must pass `team`. |
| `default_project` | string | `""` | Base project for the same case: UUID or name. Used only with `default_team` (it is that team's project; RET-121's R8 drops it when `team` names another team). Empty = no project. |

`out_of_scope_writes` keeps its meaning and only ever applies inside Linear sessions.

"Outside a session" means `HERMES_SESSION_PLATFORM != "linear"`. A Linear-platform call
whose session ticket can't be resolved (no turn found) is NOT outside: it keeps refusing
`NOT_LINEAR_SESSION` (fail-closed, as today).

## 2. The gate outside a session

`agent_tools.ToolContext` gains `outside: bool` (platform is not Linear) and keeps the
caller's `session_key` for outside calls too (today it is `None` off Linear). The gated
path (`_gated`, `_writable`, `_check_gate`, store requests, hook arm/disarm) is reused as is,
with these changes:

1. **Every target is out of scope.** Outside a session there is no tree, so `_writable`
   fetches the issue and, on a gated pass, records it on the gate without a scope walk.
   Not gated (an approved pass that names a different issue) → `OUT_OF_SCOPE`.
2. **A create always has a destination target** (RET-121's team/project targets record
   nothing when team and project equal the base; outside, the base itself must be
   approved). `_create` records ONE target per create, `{"id":
   "destination:<team uuid>/<project uuid or ->", "kind": "destination", "identifier":
   "<team key> / <project name>" or "<team key> (no project)"}`, rendered by RET-121's
   kind line as `- destination "RET / <project>"`, so the card names where the issue
   lands. It covers the resulting team AND project, so an approved re-run whose
   destination changed (a default moved, a project dropped) is `OUT_OF_SCOPE` with no
   write. With a `parent`, the parent issue and the destination are both targets.
3. **Mode:** `_gated` reads `outside_session_writes` when `ctx.outside`, else
   `out_of_scope_writes`. `deny` → `NOT_LINEAR_SESSION`; `allow` → run with the recorded
   targets approved; `approve` → store the request and return `APPROVAL_REQUIRED` with the
   outside wording (§2.1).
4. **`can_request`** requires a Linear session OR an outside session (`ctx.is_linear or
   ctx.outside`), plus a session key, a store and a digest. No session key (some cron or
   script contexts) under `approve` → `OUT_OF_SCOPE` (nobody can be asked), as RET-119
   does for the same case.
5. **Hook:** `hooks._gated_request` no longer requires `HERMES_SESSION_PLATFORM ==
   "linear"`; it requires a bound `HERMES_SESSION_KEY` and finds the request in the
   profile's adapter store by `(session key, digest)`. Hermes session keys of different
   platforms never collide, so a Slack request can't be armed from a Linear session.
   `_session` (activity rendering) stays Linear-only: tool calls from Slack post no Linear
   `action` activities.
6. **Rendering** is core's: `tools.approval` → gateway → the calling adapter's
   `_send_exec_approval_prompt` (Slack and Telegram implement it with buttons; a platform
   without it gets Hermes's text fallback). The plugin's Linear renderer is not involved.

### 2.1 Texts (`tool_approval.py`)

- `REQUIRED_TEXT_OUTSIDE`: "Not run: writing to Linear from outside a Linear session needs a
  human's approval ({targets}). Call {tool} again with identical arguments to ask in this
  chat. If it is denied or times out, do not retry." (No "pending in Linear" wording: an
  off-Linear approval is never deferred.)
- `approval_message(..., outside=True)` opens with "Approve `{tool}` from outside a Linear
  session?" and lists the targets as today; the digest line stays last.

## 3. `linear_issue_create` outside a session

Amended at the rebase onto RET-121 (controller rulings 1–3; supersedes RET-148's own
`team` / `project` arguments, its `client.teams()` / `projects_named` / `project(id:)`
resolution and the inside-session `BAD_ARGS` rule). The schema is RET-121's (`team`,
`project` and the shared fields); its description adds the outside base.

Inside a Linear session create is exactly RET-121's (its spec §1.2): base = the parent's or
the session ticket's team and project; another `team` / `project` is an approval target;
the `default_*` settings are ignored; no footer.

Outside, in order (one body, `agent_tools._create`):
1. `parent` (optional) → `_writable` (a gate target). With it, the base is the parent's
   team and project (RET-121 Q1).
2. Without a parent, the base is `default_team` / `default_project`, resolved through
   RET-121's directory (`teams` by UUID, key or name; `projects` by UUID or name; misses
   are `TEAM_NOT_FOUND` / `PROJECT_NOT_FOUND` with `candidates`, several matches
   `AMBIGUOUS`). No `default_team` and no `team` arg → `BAD_ARGS` before any I/O. No
   `default_team` with a `team` arg: no base (so no project unless `project` is given;
   `default_project` belongs to the default team and is not used).
3. `issue_fields.resolve` over the args against that base: `team` / `project` and every
   other RET-121 field (`assignee`, `delegate`, `priority`, `estimate`, `due_date`,
   `cycle`, `milestone`), with RET-121's refusals (`SELF_DELEGATION`, `ASSIGNEE_IS_APP`,
   `DELEGATE_NOT_AGENT`, misses with candidates) before any write or request. Ruling R8
   applies relative to the base: a `team` other than the base without `project` → no
   project. `state`, `labels`, `cycle` resolve in the resulting team, `milestone` in the
   resulting project.
4. The destination target (§2 item 2), then the footer appended to `description` (§4),
   then `_check_gate`, then `issueCreate`.
5. Nothing is recorded in `created_issues` (no session ticket to key it by).

RET-121 checks no project-team membership, so neither does this path: a project of
another team is passed to Linear, which refuses it (`LINEAR_ERROR` family) after approval.

## 4. Origin footer

`agent_tools._origin_footer()` → `"\n\n---\n_Filed by <profile> via <platform>_"`, where
`<profile>` = `HERMES_SESSION_PROFILE` (`""` → `default`) and `<platform>` =
`HERMES_SESSION_PLATFORM` (unset → `unknown`). Appended to the body of
`linear_issue_create` and `linear_comment` (both create and `edit_marker` edits, and thread
replies) on outside calls only. It is part of what gets written, not part of the digest (the digest is over
the model's args), so the approval card shows the args and the written body carries the
footer. Not added to `linear_issue_update` descriptions (an update replaces a body a human
or session wrote; a footer there would stack on every edit).

## 5. Other gated tools outside a session

- `linear_comment` (`id`, also with RET-121's `reply_to` / `resolve`): the issue is a gate
  target; the comment-on-issue check comes before the gate; footer per §4 (replies too).
- `linear_issue_update` (`id`, `parent`, RET-121's fields): both ends are gate targets;
  cycle check as today. RET-121's project rule applies unchanged (outside there is no
  session ticket, so a project other than the issue's own or its parent's is a project
  target too); no footer.
- `linear_relation` (RET-121): both `id` and `other` are gate targets; its read checks
  (`ALREADY_RELATED`, `RELATION_CONFLICT`) run before the gate as in a session.
- `linear_attach` (`id` + `url` or `path`): the issue is a gate target. The path policy
  (`validate_media_delivery_path`) is unchanged and runs before the gate, as today.
  `embed: true` stays `NOT_LINEAR_SESSION`.
- `linear_lookup` (RET-121) is a read: no gate, works anywhere.

## 6. Results

| Case (outside a session) | Result |
|--------------------------|--------|
| `deny` | `NOT_LINEAR_SESSION` |
| `approve`, first call | `APPROVAL_REQUIRED` + outside text |
| `approve`, identical re-call, human approves / `allow` | the tool's normal result |
| Human denies, times out, or nobody can be asked | Hermes's own `BLOCKED: …` result |
| `approve`, no session key | `OUT_OF_SCOPE` |
| Approved create re-run whose destination changed | `OUT_OF_SCOPE`, nothing written |
| Create: no parent, no `team`, no `default_team` | `BAD_ARGS` |
| Create: unknown team / project (arg or default) | `TEAM_NOT_FOUND` / `PROJECT_NOT_FOUND` with `candidates` (RET-121) |
| Create / update: `delegate` = this app, app `assignee` | `SELF_DELEGATION` / `ASSIGNEE_IS_APP`, no write, no request |
| Session-only tool | `NOT_LINEAR_SESSION` |
| Profile has no adapter | `NOT_CONNECTED` (unchanged) |

## 7. Skills and docs

- README: the write-scope paragraph (drop "an approval given anywhere but the ticket
  thread never authorizes a Linear write"; describe `outside_session_writes`), the
  settings table (three new rows), the "How it behaves" row, the approvals note (presence
  rules, `cron_mode`, Slack card shows Hermes's full choice set).
- RET-119 spec: strike the Coverage bullet and point to this spec.
- Plugin skill `manage-superpowers`: one sentence in "Calling the tools" (outside a
  session the write tools ask in that chat per `outside_session_writes`; create's base
  there is the parent's or `default_team` / `default_project`). It sits at the 150-line
  cap: reflow to make room.
- Neo `linear-agent-sessions` / `hermes-linear-dev`: update "What the app can't do" and
  "Tracking tickets from Slack" (the raw GraphQL recipe becomes the fallback) after the
  deploy, not before.

## 8. Testing

Unit (through the real registry and handlers, fake client):
- Settings: defaults, case-insensitivity, bad value → `SettingsError`, three new keys.
- Outside create (rebased on RET-121): base from parent / defaults; no parent, no team, no
  `default_team` → `BAD_ARGS`; `default_project` alone is no base; team by key, name or
  UUID (any case) through the directory; unknown / ambiguous team or project (arg or
  default) → RET-121's codes with `candidates`; Ruling R8 against the default base; RET-121
  fields set; `SELF_DELEGATION` / `ASSIGNEE_IS_APP` with no write and no request; footer
  present with profile `""` → `default`; nothing recorded in `created_issues`.
- Inside create: exactly RET-121's (another team / project asks; `default_*` ignored; no
  footer).
- Outside destination target: base team/project equal to the args still a target; one
  `destination:` target names team and project; changed destination (team, project,
  project dropped) between approval and re-run → `OUT_OF_SCOPE`, zero writes.
- RET-121 tools outside: `linear_relation` and comment `reply_to` / `resolve` follow
  `outside_session_writes` (reply carries the footer); update's project rule without a
  session ticket; `linear_lookup` reads with explicit `team` / `project`.
- Modes outside: `deny` → `NOT_LINEAR_SESSION`; `allow` writes once; `approve` →
  `APPROVAL_REQUIRED` with the outside text and a stored request whose targets include the
  destination target; no session key → `OUT_OF_SCOPE`.
- `out_of_scope_writes: allow` + `outside_session_writes: deny` (and the reverse): each
  knob governs only its own side.
- Comment / update / attach outside: gate targets, footer only on comment.
- Session-only tools outside still `NOT_LINEAR_SESSION`.
- E2E through Hermes's real dispatch (pattern of `tests/test_approval_grant_feasibility.py`
  per `hermes-linear-dev` skill): a non-Linear platform session key, raw args with a
  string-typed `labels` on the hook side → `_dispatch_pre_tool_call_hooks` → real
  `tools.approval` gate approved → handler on `coerce_tool_args` output writes exactly
  once; denied → no write, `BLOCKED`.
- Hook: `_gated_request` arms for a non-Linear key; `_session` still returns None off
  Linear (no activities posted).

Live (neo, Slack DM, test project **hermes linear test project**):
1. `outside_session_writes` unset (approve), `default_team: RET`, `default_project: hermes
   linear test project`: ask Neo in Slack to file a ticket. Expect `APPROVAL_REQUIRED`, then
   a Slack approval card naming the destination (`- destination "RET / hermes linear test
   project"`); approve → ticket in the test project with the footer `Filed by neo via
   slack`. A `priority` / `assignee` (human) on the same create is set too.
2. Same, deny → no ticket, Neo reports it was denied and does not retry.
3. Comment from Slack on that ticket → card again (no follow-up memory, Q2.6); approve →
   comment with footer.
4. `outside_session_writes: deny` → `NOT_LINEAR_SESSION`, nothing written.
5. Inside a Linear session, an out-of-scope comment still asks in Linear (no regression).

## 9. Verify during implementation

- That the agent loop's `pre_tool_call` path (`agent/tool_executor.py` →
  `_dispatch_pre_tool_call_hooks`) runs for Slack sessions exactly as for Linear ones, and
  that `request_tool_approval`'s presence check finds the Slack requester (live step 1).
- Which choices Slack's card offers for a plugin-rule approval (`allow_permanent` /
  `allow_session`) and that *always* writes `plugin_rule:<tool>:<digest>` only.
- The CI-pinned Hermes commit (`.github/workflows/*.yml`): same `pre_tool_call` directive
  handling and `_send_exec_approval_prompt` in the Slack adapter.
- ~~`projects(filter: {name: {eqIgnoreCase}})` and `project { teams { nodes { id } } }` as
  the app~~ — probed 2026-11-24: works as the app. No longer used: since the rebase onto
  RET-121 projects resolve through RET-121's directory (`projects()` list).

## 10. Out of scope

- Cross-profile use of another profile's Linear identity; a credentials-only tools mode.
- Remembering issues created outside a session for follow-up writes.
- State allowlists for outside creates (the approval card shows the state; `allow` is the
  operator's explicit grant).
- Any change to Linear-session scope or `out_of_scope_writes`.
