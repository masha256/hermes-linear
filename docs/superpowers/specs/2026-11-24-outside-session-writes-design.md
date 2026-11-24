# Writes from outside a Linear session — design

Linear: RET-148

Status: approved in the RET-148 thread (Q1, Q2), 2026-11-24.

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
| Q2.1 | Team / project of a new issue outside a session | New optional `team` (key, name or UUID) and `project` (name or UUID) arguments on `linear_issue_create`, falling back to settings `default_team` / `default_project`. No team at all → `BAD_ARGS`. Inside a session nothing changes. |
| Q2.2 | Prompt-injection carry-over | Issues and comments written outside a session get an origin footer `Filed by <profile> via <platform>` (both values from the session, never from args). |
| Q2.3 | RET-119's "A Slack approval must never authorize a Linear write" | **Reversed** (RET-119 spec, Coverage list, struck there). Its reason was Linear's *deferred* answers ("answers only count in the ticket thread"). Off Linear, core's gate is answered synchronously inside the turn by the chat's own authorized user, as for any dangerous shell command. |
| Q2.4 | Approval grain | Unchanged: `rule_key = <tool>:<digest>`. The plugin can't narrow another platform's card, so Slack shows Hermes's full choice set; *session* and *always* then cover only byte-identical calls. |
| Q2.5 | No human present | Under `approve`, Hermes's presence rules block (cron follows `approvals.cron_mode`). Under `allow`, it writes. |
| Q2.6 | What stays session-only | `linear_sync_spec`, `linear_sync_plan`, `linear_task_state`, `linear_session_link`, `linear_attach` with `embed`: `NOT_LINEAR_SESSION` as today. Issues created outside a session are not remembered for follow-ups (v2): every later write to them is an outside write again. |

## 1. Configuration

New plugin settings (`plugins.entries.linear.settings.<key>`; `settings.py`, `Settings`
dataclass, validated in `load_settings` like `out_of_scope_writes`):

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `outside_session_writes` | string | `approve` | `approve`: every write from a non-Linear session asks a human in that chat first. `deny`: refuse (`NOT_LINEAR_SESSION`, today's behaviour). `allow`: write without asking. Case-insensitive; any other value is a `SettingsError`. |
| `default_team` | string | `""` | Team for `linear_issue_create` outside a session when `team` is not given: key (`RET`), name or UUID. |
| `default_project` | string | `""` | Project for the same case when `project` is not given: name or UUID. Empty = no project. |

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
2. **A create with no `parent` still has a target:** the destination. `_create` records a
   pseudo-target `{"id": "team:<team uuid>[/project:<project uuid>]", "identifier": "<team
   key>", "title": "new issue in <team key>[ / <project name>]"}` so the approval card
   names where the issue lands. With a `parent`, both the parent and the destination are
   targets.
3. **Mode:** `_gated` reads `outside_session_writes` when `ctx.outside`, else
   `out_of_scope_writes`. `deny` → `NOT_LINEAR_SESSION`; `allow` → run with the recorded
   targets approved; `approve` → store the request and return `APPROVAL_REQUIRED` with the
   outside wording (§2.1).
4. **`can_request`** drops the `ctx.is_linear` requirement: a session key, a store and a
   digest are enough. No session key (some cron or script contexts) under `approve` →
   `OUT_OF_SCOPE` (nobody can be asked), as RET-119 does for the same case.
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

Schema: add `team` ("Team key, name or UUID; only outside a Linear session") and
`project` ("Project name or UUID; only outside a Linear session"), both plain strings
(non-array, so `FIELD_TYPES` / `ARRAY_FIELDS` sync: `ARRAY_FIELDS` unchanged). Description:
"Create a Linear issue as the Linear app. In a Linear session: in the session issue's team
and project. Elsewhere: in `team`/`project` or the configured defaults, after approval
(`outside_session_writes`)."

Inside a session: `team`/`project` given and different from the session ticket's →
`BAD_ARGS` (the in-session create never leaves the session's team/project silently).
Equal values are accepted.

Outside, in order:
1. Team = `team` arg, else `default_team`, else `BAD_ARGS`. Resolve via the existing
   `client.teams()` (match UUID, key case-insensitive, then name case-insensitive); no
   match → `TEAM_NOT_FOUND`.
2. Project = `project` arg, else `default_project`, else none. Resolve with a new
   `client.projects_by_name(name)` (`projects(filter: {name: {eqIgnoreCase: $name}})`, also
   accepting a UUID via `project(id:)`), and require the team among the project's
   `teams`; no match or wrong team → `PROJECT_NOT_FOUND`.
3. `parent` (optional) → `_writable` (a gate target). `state`, `labels` resolve against
   the destination team as today.
4. Footer appended to `description` (§4); then `_check_gate`, then `issueCreate`.
5. Nothing is recorded in `created_issues` (no session ticket to key it by).

## 4. Origin footer

`agent_tools._origin_footer()` → `"\n\n---\n_Filed by <profile> via <platform>_"`, where
`<profile>` = `HERMES_SESSION_PROFILE` (`""` → `default`) and `<platform>` =
`HERMES_SESSION_PLATFORM` (unset → `unknown`). Appended to the body of
`linear_issue_create` and `linear_comment` (both create and `edit_marker` edits) on outside
calls only. It is part of what gets written, not part of the digest (the digest is over
the model's args), so the approval card shows the args and the written body carries the
footer. Not added to `linear_issue_update` descriptions (an update replaces a body a human
or session wrote; a footer there would stack on every edit).

## 5. Other gated tools outside a session

- `linear_comment` (`id`): the issue is a gate target; footer per §4.
- `linear_issue_update` (`id`, `parent`): both ends are gate targets; cycle check as today.
- `linear_attach` (`id` + `url` or `path`): the issue is a gate target. The path policy
  (`validate_media_delivery_path`) is unchanged and runs before the gate, as today.
  `embed: true` stays `NOT_LINEAR_SESSION`.

## 6. Results

| Case (outside a session) | Result |
|--------------------------|--------|
| `deny` | `NOT_LINEAR_SESSION` |
| `approve`, first call | `APPROVAL_REQUIRED` + outside text |
| `approve`, identical re-call, human approves / `allow` | the tool's normal result |
| Human denies, times out, or nobody can be asked | Hermes's own `BLOCKED: …` result |
| `approve`, no session key | `OUT_OF_SCOPE` |
| No team | `BAD_ARGS`; unknown team / project → `TEAM_NOT_FOUND` / `PROJECT_NOT_FOUND` |
| Session-only tool | `NOT_LINEAR_SESSION` |
| Profile has no adapter | `NOT_CONNECTED` (unchanged) |

## 7. Skills and docs

- README: the write-scope paragraph (drop "an approval given anywhere but the ticket
  thread never authorizes a Linear write"; describe `outside_session_writes`), the
  settings table (three new rows), the "How it behaves" row, the approvals note (presence
  rules, `cron_mode`, Slack card shows Hermes's full choice set).
- RET-119 spec: strike the Coverage bullet and point to this spec.
- Plugin skill `manage-superpowers`: one sentence in "Calling the tools" (outside a
  session the write tools ask in that chat per `outside_session_writes`; create takes
  `team`/`project`). It sits at the 150-line cap: reflow to make room.
- Neo `linear-agent-sessions` / `hermes-linear-dev`: update "What the app can't do" and
  "Tracking tickets from Slack" (the raw GraphQL recipe becomes the fallback) after the
  deploy, not before.

## 8. Testing

Unit (through the real registry and handlers, fake client):
- Settings: defaults, case-insensitivity, bad value → `SettingsError`, three new keys.
- Outside create: team from arg / default / missing (`BAD_ARGS`) / unknown; project
  name, UUID, wrong team; footer present with profile `""` → `default`; nothing recorded in
  `created_issues`.
- Inside create: `team`/`project` differing → `BAD_ARGS`; equal → normal create; no footer.
- Modes outside: `deny` → `NOT_LINEAR_SESSION`; `allow` writes once; `approve` →
  `APPROVAL_REQUIRED` with the outside text and a stored request whose targets include the
  destination pseudo-target; no session key → `OUT_OF_SCOPE`.
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
   a Slack approval card naming the destination; approve → ticket in the test project with
   the footer `Filed by neo via slack`.
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
  the app~~ — probed 2026-11-24: works as the app (`"HERMES LINEAR TEST PROJECT"` →
  `d984ad14-…`, teams `[RET]`).

## 10. Out of scope

- Cross-profile use of another profile's Linear identity; a credentials-only tools mode.
- Remembering issues created outside a session for follow-up writes.
- State allowlists for outside creates (the approval card shows the state; `allow` is the
  operator's explicit grant).
- Any change to Linear-session scope or `out_of_scope_writes`.
