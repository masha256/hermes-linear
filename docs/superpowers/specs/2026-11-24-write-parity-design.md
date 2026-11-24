# Write parity: create/update fields, `linear_relation`, comment threads, `linear_lookup` — design

Linear: RET-121

Status: approved in the RET-121 thread (Q1–Q4 and design sections 1–2), 2026-11-24.
Sub-project C of the RET-118 umbrella. It builds on A's approval path (RET-119,
`docs/superpowers/specs/2026-11-23-out-of-scope-approval-design.md`) and B's tool modules
(RET-120, `docs/superpowers/specs/2026-11-24-read-and-attach-design.md`). D comes next.

## Goal

The agent makes routine ticket writes through the typed tools, as the app: assign,
delegate, prioritize, estimate, schedule, file an issue into a project or milestone, link
blockers, reply in a thread and resolve it, and fix a bad label or state name. Inside the
session tree they run with no prompt; outside it they use A's Allow once.

Success: none of these needs `linear_query` or a raw API call any more, and a
`LABEL_MISSING` or a bad state name can be fixed from inside the tools.

- `linear_issue_create` / `linear_issue_update` gain assignee, delegate, priority,
  estimate, due date, cycle, project and milestone (create also `team`).
- `linear_relation`: blocks, blocked-by, related and duplicate-of, both ends scope-checked.
- `linear_task_state blocked` can set a real `blocks` relation (`blocked_by`).
- `linear_comment` gains `reply_to` (thread replies) and `resolve`.
- `linear_lookup`: teams, states, labels, users, cycles, projects and milestones from a
  cached directory that every name resolution shares.

Constraints: public Hermes plugin API only, no core changes; tools never raise; every
write is attributed to the app.

## Decisions

| # | Question | Decision |
|---|----------|----------|
| Q1 | Where an issue's team and project come from, and what needs approval | Create's base is the **parent's** team and project when `parent` is given, else the session ticket's. Explicit `team` / `project` equal to the base run with no prompt; a different one is an **approval target**. Update gets `project` (no prompt for the session ticket's project or the issue's parent's project, else approval) but **no `team`**. |
| Q2 | How `blocked` sets a relation; relation removal | `linear_task_state` gains `blocked_by: <issue>`: it also creates `<issue> blocks <task>`, and `question` becomes optional with it. Tree-only like the rest of the tool. **C deletes no relations**: `unblocked` removes only the label. |
| Q3 | What `delegate` accepts | Any agent (app) user, **this app included**. Self-delegation runs and the result carries `starts_session: false` plus a warning that a human must delegate for a session to start. `""` / `null` clears. |
| Q4 | Lookup and name-resolution caching | One shared directory (`hermes_linear/directory.py`): a per-client cache entry per kind and team/project with a **300 s TTL**; a resolution miss **refetches once** before failing. `team_states` moves into it. |

Facts from the brainstorm that shape these:

- The router ignores a delegation the app sets (fail-closed actor check: an app-set
  `delegateId` is a "denied delegator"), so self-delegation marks the ticket delegated in
  Linear but starts no session (Q3). Making it start work needs a human authorization the
  plugin records; that is a separate design, not C.
- A delegate must be an app user. This workspace has one human (Mike) and one app (Neo).
- Linear relation types are `blocks`, `duplicate`, `related` and `similar`. Blocked-by is
  `blocks` with the ends swapped. Linear clears the "blocked" flag when the blocker
  completes; the relation stays. `similar` is set by Linear's AI and is left out.
- Moving an issue to another team changes its identifier and remaps its state (why update
  has no `team`).
- This workspace has one team (RET) with cycles and estimates off: unit tests cover
  cycles and estimates, the live test cannot.
- **Hermes coerces tool args after `pre_tool_call`.** The agent loop fires the hook on the
  model's raw args (`agent/tool_executor.py:663`), then calls `handle_function_call` with
  `skip_pre_tool_call_hook=True` (`:1748`), which runs `tools.arg_coercion.coerce_tool_args`
  (`model_tools.py:923`; not in `plugin_api.json`, so the plugin can't call it). On the
  `model_tools` path (`execute_code`, bridges) coercion runs before the hook. Coercion
  turns `"2"` into `2` for integer fields, `"true"` into `True` for booleans and `"null"`
  into `None` for nullable fields, the same at the CI pin 3b42157. A's digest normalization
  mirrors only array fields, so without §2.3 the hook's digest and the handler's would
  differ for the new fields and an approved re-call would prompt again.

## 1. Tool surfaces

### 1.1 Shared field rules (create and update)

- A key that is absent leaves the field untouched. On update, `null` clears a field, and so
  does `""` for a string field. On create, `null` / `""` is the same as absent.
- `assignee`, `delegate`: a user UUID, or a case-insensitive exact match on `name`,
  `displayName` or email; `me` is the app. No match: `USER_NOT_FOUND`; several:
  `AMBIGUOUS`. Both carry `candidates` (`[{id, name, displayName, app}]`, at most 20,
  closest substring matches first).
  - A `delegate` that is not an app user: `DELEGATE_NOT_AGENT`.
  - A `delegate` equal to the app: the write runs and the result adds
    `starts_session: false` and
    `warnings: ["Delegated to this app: no session starts until a human delegates the issue."]`.
- `priority`: `none|urgent|high|medium|low` (case-insensitive) or `0`–`4` (Linear's
  numbers: 0 none, 1 urgent, 2 high, 3 medium, 4 low). Anything else: `BAD_ARGS`.
- `estimate`: an integer ≥ 0. A team that doesn't use estimates answers with Linear's own
  error code and message (see §4).
- `due_date`: `YYYY-MM-DD`, a real calendar date (else `BAD_ARGS`).
- `cycle`: a UUID, a number, a name (case-insensitive), `current` or `next`, in the issue's
  team. No match: `CYCLE_NOT_FOUND` with `candidates` (`[{id, number, name}]`).
- `milestone`: a UUID or a name (case-insensitive) in the issue's **resulting** project
  (after a `project` change in the same call). No match, or no project:
  `MILESTONE_NOT_FOUND` with `candidates` (`[]` without a project).
- `project`: a UUID or a name (case-insensitive). No match: `PROJECT_NOT_FOUND` with
  `candidates`.
- `team` (create only): a UUID, a key (`RET`) or a name. No match: `TEAM_NOT_FOUND` with
  `candidates`.
- `state`: a name or a type, as today (`resolve_state`). A miss is now `STATE_NOT_FOUND`
  with `candidates` (the team's states, `[{id, name, type}]`) instead of `BAD_ARGS`.
- Labels: an unknown label to add is `LABEL_MISSING`, as today, and now carries `available`
  (the team's and workspace's label names, at most 100).
- Every miss code goes through the directory's refetch-once rule (§2.2) first.
- Errors stay `{"ok": false, "error": CODE, ...}`; the extra keys sit next to `error`.

### 1.2 `linear_issue_create`

New args: `team`, `project`, `assignee`, `delegate`, `priority`, `estimate`, `due_date`,
`cycle`, `milestone` (existing: `title`, `description`, `parent`, `labels`, `state`).

- **Base:** with `parent`, the parent's team and project (the parent is `_writable`, as
  today); without, the session ticket's.
- `team` / `project` equal to the base (compared by UUID after resolution) add nothing.
  A different one is an approval target (§2.4): `team "<key>"` / `project "<name>"`.
- `project: null` / `""` on create means "the base", not "no project" (to create without a
  project, use a parent without one or update afterwards).
- State, labels and cycle resolve in the resulting team; the milestone in the resulting
  project.
- Result as today (`{ok, issue}`) plus `starts_session` / `warnings` for self-delegation.

### 1.3 `linear_issue_update`

New args: `project`, `assignee`, `delegate`, `priority`, `estimate`, `due_date`, `cycle`,
`milestone` (no `team`).

- The issue itself is `_writable`, as today.
- A `project` change to the session ticket's project, or to the issue's parent's project
  (after a `parent` change in the same call, the new parent's), runs with no prompt. Any
  other project (`null` / `""` clears it, which counts as no prompt) is an approval target.
- `milestone` resolves in the resulting project. A `project` change without `milestone`
  sends no `projectMilestoneId`; what Linear then does with the old milestone is probed
  in §4 and documented in the tool description.
- At least one field must be given (as today, `BAD_ARGS` otherwise).

### 1.4 `linear_relation(id, type, other)`

- `type`: `blocks` (`id` blocks `other`), `blocked_by` (`other` blocks `id`), `related`,
  `duplicate_of` (`id` is a duplicate of `other`).
- Both `id` and `other` go through `_writable`; an out-of-scope end is an Allow once target
  (the tool is gated). `id == other` (by UUID): `BAD_ARGS`.
- Before the create, the existing relations of `id` (both directions) are read. A match is
  `ALREADY_RELATED` with no write: same type and direction for `blocks` / `duplicate`,
  either direction for `related`.
- Result: `{ok: true, relation_id, issue, other, type}` (identifiers for `issue`/`other`).

### 1.5 `linear_comment` additions

New args: `reply_to` (a comment UUID, as returned by `linear_issue_get` `comments`),
`resolve` (boolean).

- `reply_to` must be a comment on issue `id` (`COMMENT_NOT_ON_ISSUE` otherwise, also for
  an unknown comment). A reply's id resolves to its thread's top-level comment, and the
  new comment is posted with that `parentId`.
- `resolve: true` needs `reply_to` (`BAD_ARGS` otherwise). It posts the reply, then calls
  `commentResolve(id: <thread>, resolvingCommentId: <new reply>)`. A thread that is
  already resolved gets the reply and no resolve call; the result says
  `already_resolved: true`.
- `edit_marker` together with `reply_to`: `BAD_ARGS`.
- Result as today plus `thread_id` and `resolved` (when `reply_to` is given).
- `id` is scope-checked (gated) as today; the comment check comes after it, before
  `_check_gate`.

### 1.6 `linear_task_state` addition

- `blocked_by` (an issue ref) is accepted only with `state: blocked` (`BAD_ARGS`
  otherwise). With it, `question` is optional.
- The blocker goes through `_writable` without a gate (the tool stays tree-only): an
  out-of-tree blocker is `OUT_OF_SCOPE`, and the agent uses `linear_relation`, which asks.
  The blocker equal to the task: `BAD_ARGS`.
- Order: both scope checks; then the label (as today); then `<blocked_by> blocks <task>`
  through `ensure_relation` (skipped if it exists, so a re-run is safe); then the question
  comment, only if `question` was given.
- Result adds `relation_id` and `relation_created` (false when it existed).
  `LABEL_MISSING` keeps its current shape (Ruling S18) plus the relation keys.

### 1.7 `linear_lookup(kind, team?, project?, query?)`

- `kind`: `teams | states | labels | users | cycles | projects | milestones`.
- `team` (a UUID, key or name; for `states`, `labels`, `cycles`) defaults to the session
  ticket's team; `project` (for `milestones`) defaults to the session ticket's project.
  Outside a Linear session without these args: `NOT_LINEAR_SESSION`. Unknown team or
  project: `TEAM_NOT_FOUND` / `PROJECT_NOT_FOUND` with candidates.
- `query`: a case-insensitive substring filter on the name (and on key, displayName and
  email where the row has them).
- Rows: `id`, `name`, plus `type` (state), `key` (team), `displayName` / `app` / `active`
  (user), `number` / `startsAt` / `endsAt` (cycle), `state` (project).
- At most 250 rows, with `truncated: true` past that. A read: no scope check and no gate.
- Served from the directory (§2.2); a lookup never forces a refetch.

## 2. Internals

### 2.1 Modules and registration

Following B (RET-120 spec §2.1): each new tool module imports `agent_tools`' machinery and
registers its schemas and handlers at its own end; `agent_tools.py` ends with bare
`from . import ...` lines and keeps placeholder keys so the roster order stays fixed.

- `hermes_linear/directory.py`: the cache and resolution (§2.2). No tool of its own.
- `hermes_linear/issue_fields.py`: `parse(args, *, update: bool) -> dict` (sync, raises
  `_Refusal("BAD_ARGS")` before any I/O) and `async resolve(ctx, parsed, *, team, project)`
  → `(input_fields: dict, warnings: list[str], targets: list[dict], starts_session)`
  for the shared fields of §1.1. Create and update (still in `agent_tools.py`) call it.
- `hermes_linear/tools_relation.py`: `linear_relation` and
  `async ensure_relation(ctx, issue, other, type) -> (relation_id, created)`, reused by
  `linear_task_state`.
- `hermes_linear/tools_lookup.py`: `linear_lookup`.
- `linear_comment` and `linear_task_state` stay in `agent_tools.py`.
- `LinearClient` gains: `comment_get(id)` (`{id, issue{id}, parent{id}, resolvedAt}`),
  `comment_create(issue_id, body, parent_id=None)`, `comment_resolve(id, resolving_id)`,
  `issue_relations(id)` (both directions, with relation ids and issue ids),
  `relation_create(issue_id, related_id, type)`, `users()`, `cycles(team_id)`,
  `projects()`, `milestones(project_id)`. Lists are paged like `teams()` (`_paged`,
  `PAGE_CAP`). `team_states` loses its own cache (the directory caches it).
- `QUERY_ISSUE_GET` is not widened (B's rule); the create base and project checks use the
  fields it already has (`team.id`, `project.id`, `parent.id`). The parent's project for
  update's no-prompt rule comes from one `issue_get` of the parent (already fetched when
  `parent` is in the call).

### 2.2 Directory

- One `Directory` per `LinearClient`, held in a module-level `weakref.WeakKeyDictionary`.
  Entries are keyed `(kind, scope_id)` (`scope_id` is the team or project UUID, or `""`),
  each with its fetch time; an entry older than `DIRECTORY_TTL_S = 300` is refetched on use.
- `async rows(kind, scope_id) -> list[dict]`: cached rows (copies).
- `async resolve(kind, scope_id, ref, match) -> dict`: `match(rows, ref)` returns one row,
  `None` (miss) or a list (ambiguous). On a miss, the entry is refetched once (skipped if
  it was fetched in this call already) and matched again. Still a miss → the kind's
  `NOT_FOUND` refusal with candidates; ambiguous → `AMBIGUOUS` with candidates.
- Concurrency: everything runs on the adapter loop; two concurrent misses may fetch twice,
  which is harmless. No lock.
- Callers: `issue_fields.resolve`, `_state_id` / `_label_ids` (create/update),
  `linear_task_state` (states, the Blocked label), `questions.py`'s Blocked label lookup,
  `linear_lookup`. `setup_cli` keeps calling the client directly (one-shot, no cache).
- The refusal carrying candidates is a `_Refusal` subclass with an `extra` dict that `_tool`
  merges into the error result (`{"ok": false, "error": code, **extra}`).

### 2.3 Digest normalization for scalar fields

- `tool_approval.ARRAY_FIELDS` becomes `FIELD_TYPES: dict[str, dict[str, str]]`, the JSON
  type of every property of every gated tool's schema, with `"nullable"` marked where the
  schema allows null (e.g. `{"estimate": "integer|null", "resolve": "boolean",
  "labels": "array", ...}`). A test derives it from `TOOL_SCHEMAS` and asserts equality
  (the existing `test_constants_verbatim` grows into this).
- `normalize_args` mirrors `coerce_tool_args` for those types: array (as today), integer
  (`_coerce_number(integer_only=True)` semantics: a string parsing to a whole float →
  `int`, else unchanged), number, boolean (`"true"`/`"false"`, case-insensitive), and
  `"null"` → `None` for a nullable field. Originals are kept whenever the repair is not
  exact, as Hermes does.
- Idempotent on coerced args, so the hook (raw args) and the handler (coerced args) compute
  the same digest. Pinned by an e2e test: the real `_dispatch_pre_tool_call_hooks` on the raw
  args (hook sees `"2"`), then the handler on the real `coerce_tool_args` output (sees `2`):
  one approval, and the re-call runs.
- `GATED_TOOLS` gains `linear_relation`.

### 2.4 Approval targets beyond issues

- `Gate.record_target(id, kind, name)` records a team or project (`kind` is `team` or
  `project`) next to the issue targets. `Gate.record(issue)` is unchanged.
- The stored request keeps a list of UUIDs as today. On an approved pass, a team or project
  check passes when its UUID is in `gate.approved`; any other out-of-scope team or project
  is `OUT_OF_SCOPE` (`_sealed`).
- `tool_approval.approval_message` renders a target with `kind` as `- project "orion"` /
  `- team "RET"` (one line, CR/LF stripped like issue lines); issues stay
  `- RET-n "title"`. The digest line stays last.
- `mode: allow` approves every recorded target as today; `mode: deny` refuses.
- Ordering rule (unchanged from A, now for every new body): all reads, resolutions and
  scope checks, then `_check_gate`, then writes. A first pass that needs approval writes
  nothing.

### 2.5 Error and result conventions

- Linear failures keep Linear's code (`exc.code or "LINEAR_ERROR"`) plus `message`, as B's
  tools do.
- New plugin codes: `STATE_NOT_FOUND`, `USER_NOT_FOUND`, `AMBIGUOUS`, `DELEGATE_NOT_AGENT`,
  `CYCLE_NOT_FOUND`, `MILESTONE_NOT_FOUND`, `PROJECT_NOT_FOUND`, `TEAM_NOT_FOUND`,
  `ALREADY_RELATED`, `COMMENT_NOT_ON_ISSUE`.
- `linear_task_state`'s own "the team has no such state" (`_target_state`) stays `BAD_ARGS`:
  that is not a name the agent supplied.

## 3. Testing, docs and skill

### 3.1 Tests (TDD)

Rewrite first the tests that pin old behaviour (a bad state's `BAD_ARGS` on create/update;
`ARRAY_FIELDS` in `test_constants_verbatim`; `team_states`' client cache), watch them fail
for the right reason, then implement. All tool tests go through the real registry.

- `issue_fields`: each field's parse (good and bad values), every miss code with
  candidates, `me`, self-delegation's `starts_session` / warning, `DELEGATE_NOT_AGENT`,
  `AMBIGUOUS`, clearing on update, `null` on create.
- Create base: with and without a parent; `team` / `project` equal to the base (no
  target) and different (target; `APPROVAL_REQUIRED`; approved re-call writes with the
  approved UUIDs only).
- Update `project`: session project, parent's project, new parent's project in the same
  call, another project (target), clear.
- `linear_relation`: each type's direction, `ALREADY_RELATED` (direction rules), self,
  out-of-scope `id` and `other` (targets), first pass writes nothing.
- `linear_comment`: reply, a reply's id → top-level thread, `COMMENT_NOT_ON_ISSUE`,
  resolve, already resolved, `resolve` without `reply_to`, `edit_marker` + `reply_to`.
- `linear_task_state blocked_by`: relation created / existing, optional `question`,
  out-of-tree blocker refused before any write, blocker = task, `blocked_by` with another
  state, `LABEL_MISSING` with the relation keys.
- Directory: TTL expiry (injected clock), refetch-once on a miss, no refetch on a hit,
  per-client isolation, ambiguity.
- `linear_lookup`: each kind, defaults from the session ticket, `query`, the 250 cap.
- Digest parity under coercion: the hook through the real `_dispatch_pre_tool_call_hooks`
  (the `invoke_hook` patch of `tests/test_tool_approval_e2e.py`) on raw args, the handler
  on the real `coerce_tool_args` output, for `estimate: "2"`,
  `resolve: "true"` and a nullable `"null"`; plus a relation and a project target e2e.
- Roster / manifest / `GATED_TOOLS` / `FIELD_TYPES` sync; a fresh-interpreter test of both
  import orders for the new modules; the skill's 150-line cap.

### 3.2 Docs and skill

- README tool table: the new args, `linear_relation`, `linear_lookup`, the new codes.
- Plugin skill (`hermes_linear/skills/manage-superpowers/SKILL.md`): the tool list gains
  `linear_relation` and `linear_lookup` and the new args (tool count wording updated),
  staying at or under the 150-line cap by reflowing existing text. The SDD `blocked` bullet
  mentions `blocked_by`. The Large-projects line "The tools cannot set a delegate" becomes:
  the tools can set one, but a delegation by the app starts no session, so ask the user.
- The neo `linear-agent-sessions` skill ("What the app can't do", rule 4b's list of gated
  tools) is edited after `deploy-live` and Mike's restart.

## 4. Verify during implementation

Live probes as the app on one ticket in `hermes linear test project`, named in the plan;
the report lists what each probe created.

- `commentCreate` with `parentId` set to a **reply's** id: refused, or flattened? (§1.5
  resolves to the top-level comment either way; the probe decides whether that is needed
  or only defensive.)
- `commentResolve(id, resolvingCommentId)` as the app; `commentResolve` on an already
  resolved thread.
- `issueRelationCreate` for a relation that already exists: duplicate, error or no-op?
  (§1.4 checks first either way.)
- `issueUpdate(delegateId: <the app>)` and `delegateId: <a human>` (the error code).
- `issueUpdate(projectId: <other>)` on an issue with a milestone: is the milestone dropped?
  Documented in the tool description.
- `estimate` on a team with estimates off; `cycles` on a team with cycles off (empty
  list → `CYCLE_NOT_FOUND` with `candidates: []`).
- `users` connection as the app: includes app users, the `app` flag, deactivated users.
- Hermes: the agent loop's hook-then-`handle_function_call(skip_pre_tool_call_hook=True)`
  order and the coercion rules, on the installed version and at the pin 3b42157 (read at
  both; the e2e test pins the outcome, not private symbols).

## 5. Live test

In `hermes linear test project`, from a delegated test ticket, after `deploy-live` and a
restart:

1. Create a sub-issue with assignee `me`, priority `high`, due date, milestone: runs with
   no prompt, fields set.
2. Create with `project` = another project: Allow once card shows `project "<name>"`;
   approve; the re-call creates it there. A second, identical call prompts again.
3. Update the sub-issue's assignee, priority and due date; then `delegate: me`: the result
   says `starts_session: false` with the warning, and no session starts in Linear.
4. A bad state name and an unknown label: `STATE_NOT_FOUND` / `LABEL_MISSING` with
   candidates; `linear_lookup states` / `labels` shows the same.
5. `linear_relation` between two in-tree issues (`blocks`), repeat → `ALREADY_RELATED`;
   with an out-of-tree `other` → Allow once.
6. `linear_task_state blocked` with `blocked_by` (no question): label + relation, no
   question comment.
7. Reply to a human's comment thread with `resolve: true`: reply posted in the thread, the
   thread resolved, attributed to the app.

What should NOT happen: any write on a first pass that answered `APPROVAL_REQUIRED`; a
second prompt for an approved identical re-call; a relation or comment duplicated by a
re-run of `blocked_by`.

## 6. Out of scope

- Moving issues between teams (`team` on update).
- Deleting or editing relations; `similar` relations.
- Self-delegation starting a session (needs a recorded human authorization).
- Unresolving comment threads; reactions; subscribing.
- `linear_mutate`, projects, documents (D).
