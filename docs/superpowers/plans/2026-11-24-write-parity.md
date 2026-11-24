# Write parity (create/update fields, `linear_relation`, comment threads, `linear_lookup`) Implementation Plan

Linear: RET-121

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The agent assigns, delegates, prioritizes, estimates, schedules, files issues into projects and milestones, links relations, replies in comment threads and resolves them, and fixes bad names, all through the typed `linear_*` tools as the app, with A's Allow once for out-of-scope targets.

**Architecture:** A per-client `directory.py` cache (TTL 300 s, refetch once on a miss) serves every name resolution and the new `linear_lookup`. `issue_fields.py` parses and resolves the shared create/update fields and returns team/project approval targets, which A's `Gate` learns to record. `tools_relation.py` and `tools_lookup.py` follow B's self-registering module pattern. `tool_approval.normalize_args` mirrors Hermes's scalar coercion so hook and handler digests agree.

**Tech Stack:** Python 3.11+, the plugin's `LinearClient` (aiohttp), pytest, `tests/fake_linear.py`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-24-write-parity-design.md`

## Global Constraints

- Public Hermes plugin API only (`~/.hermes/hermes-agent/hermes_cli/data/plugin_api.json`); no Hermes core changes; `hermes plugins validate .` must pass and its "private core imports" list must not grow. Tests may import Hermes internals.
- Tools never raise; failures are `{"ok": false, "error": CODE, ...extra}`. Every Linear call uses the app token through `LinearClient`. Linear failures keep `exc.code or "LINEAR_ERROR"` plus `message`.
- `QUERY_ISSUE_GET` and `client.issue_get` stay unchanged.
- Gated bodies: every read, resolution and scope check, then `_check_gate(ctx)`, then writes. A first pass that needs approval writes nothing.
- Constants: `DIRECTORY_TTL_S = 300`; `CANDIDATES_MAX = 20`; `LABELS_AVAILABLE_MAX = 100`; `LOOKUP_MAX_ROWS = 250`. Priority map: `none 0, urgent 1, high 2, medium 3, low 4`.
- New error codes (exact): `STATE_NOT_FOUND`, `USER_NOT_FOUND`, `AMBIGUOUS`, `DELEGATE_NOT_AGENT`, `CYCLE_NOT_FOUND`, `MILESTONE_NOT_FOUND`, `PROJECT_NOT_FOUND`, `TEAM_NOT_FOUND`, `ALREADY_RELATED`, `COMMENT_NOT_ON_ISSUE`. Miss errors carry `candidates`; `LABEL_MISSING` carries `available`.
- Self-delegation warning (exact): `"Delegated to this app: no session starts until a human delegates the issue."`, with `starts_session: false`.
- Approval card target lines: issues `- RET-n "title"` (unchanged); `- project "<name>"`; `- team "<key>"`. CR/LF stripped. Digest line stays last.
- Adding a tool updates in the same commit: its module's `SCHEMAS`/`HANDLERS` (self-registering at the module's end, B's Ruling R3 pattern), a placeholder key in `agent_tools.TOOL_SCHEMAS`/`TOOL_HANDLERS`, `agent_tools.__all__`, `plugin.yaml` `provides_tools` (order: the existing 11, then `linear_relation`, `linear_lookup`), `tests/test_plugin_register.py` `TOOL_NAMES`, and one bullet in `hermes_linear/skills/manage-superpowers/SKILL.md`, which must stay ≤ 150 lines (pay for each line by tightening elsewhere).
- A gated tool's schema change updates `tool_approval.FIELD_TYPES` in the same commit (pinned by the sync test from Task 2).
- Tests run from `.worktrees/ret121`: `.venv/bin/pytest -q` (baseline 1038 passed). Commit messages carry the task's sub-ticket ID.
- Edit files with `write_file` / `patch`, never shell heredocs, `python -c` or `sed -i`.
- Live probes as the app: `python3 ~/.hermes/profiles/neo/skills/software-development/hermes-linear-dev/scripts/linear-query.py '<graphql>'`. Write probes touch **only RET-92** (hermes linear test project) and new children of it; the task report lists everything created.

## Review Focus

1. **An approved re-call whose args the model sent as strings (`"estimate": "2"`, `"resolve": "true"`, `"due_date": "null"`)** must run once without a second prompt. Owned by Task 2 (`test_normalize_scalar_mirrors_hermes`) and Task 4 (`test_e2e_coerced_estimate_one_prompt`).
2. **Create with a parent in another project (approved earlier, now in `created_issues`)**: the child follows the parent's project with no prompt; it is not compared to the session ticket's project. Owned by Task 4 (`test_create_follows_parent_project_no_target`).
3. **A label a human created after the cache was filled**: found on the first try via refetch-once, without waiting out the TTL. Owned by Task 1 (`test_miss_refetches_once`).
4. **`reply_to` naming a comment on a different issue that IS in scope**: `COMMENT_NOT_ON_ISSUE`, nothing posted. Owned by Task 6 (`test_reply_to_other_issue_refused`).
5. **`blocked_by` re-run after a crash between the relation and the question comment**: the existence check means no second relation is created; the question comment is posted again, as a re-run of today's `blocked` does. Owned by Task 5 (`test_blocked_by_rerun_no_duplicate_relation`).

---

### Task 1: Directory, list client calls, miss refusals with candidates

**Files:**
- Create: `hermes_linear/directory.py`
- Modify: `hermes_linear/linear_client.py` (add `users()`, `cycles(team_id)`, `projects()`, `milestones(project_id)`; `team_states` loses its `_team_states` cache)
- Modify: `hermes_linear/agent_tools.py` (`_Refusal` gains `extra`; `_tool` merges it; `_state_id` / `_label_ids` / `_blocked_label` / `_task_state` states go through the directory; state miss on create/update → `STATE_NOT_FOUND`; `LABEL_MISSING` + `available`)
- Modify: `hermes_linear/questions.py` (`_set_blocked` label lookup through the directory)
- Test: `tests/test_directory.py` (new), `tests/test_linear_client.py`, `tests/test_agent_tools_generic.py`, `tests/test_task_state.py`

**Interfaces:**
- Produces: `directory.DIRECTORY_TTL_S = 300`; `directory.for_client(client) -> Directory` (one per client, `WeakKeyDictionary`); `Directory.rows(kind: str, scope_id: str = "") -> list[dict]` (async; copies); `Directory.resolve(kind, scope_id, ref, match) -> dict` (async; `match(rows, ref) -> dict | list[dict] | None`; refetch once on `None`; raises `agent_tools._Refusal(code, extra={"candidates": [...]})`). Kinds and miss codes: `teams` → `TEAM_NOT_FOUND`, `states` → `STATE_NOT_FOUND`, `labels` → `LABEL_MISSING` (extra key `available`), `users` → `USER_NOT_FOUND`, `cycles` → `CYCLE_NOT_FOUND`, `projects` → `PROJECT_NOT_FOUND`, `milestones` → `MILESTONE_NOT_FOUND`; a list from `match` → `AMBIGUOUS`. Clock injectable: `Directory(client, clock=time.monotonic)`.
- Row shapes: teams `{id,key,name}`; states `{id,name,type}` (position order); labels `{id,name}` (groups excluded); users `{id,name,displayName,email,app,active}`; cycles `{id,number,name,startsAt,endsAt}` plus the team's `activeCycle` id marked `current: true` and the next upcoming one `next: true`; projects `{id,name,state}`; milestones `{id,name}`.
- Produces: `agent_tools._Refusal(code: str, extra: dict | None = None)`; `_tool` returns `{"ok": False, "error": code, **extra}`.
- Candidates: at most `CANDIDATES_MAX = 20` (substring matches on the ref first, then the rest in row order); `available` at most `LABELS_AVAILABLE_MAX = 100` names.

- [ ] **Step 1: Write the failing tests.** `tests/test_directory.py` with a stub client counting fetches and a fake clock: `test_hit_cached_within_ttl` (2 `rows` calls → 1 fetch), `test_expired_after_ttl` (clock + 301 → 2 fetches), `test_miss_refetches_once` (Review Focus 3: first fetch lacks "New", second has it → resolves, 2 fetches), `test_miss_after_refetch_refuses_with_candidates` (`STATE_NOT_FOUND`, `candidates` ≤ 20, substring matches first), `test_no_double_refetch_after_fresh_fetch` (first-ever fetch then miss → 1 fetch only), `test_ambiguous`, `test_per_client_isolation`, `test_labels_miss_has_available`. In `tests/test_linear_client.py`: op name + variables + row mapping for `users`, `cycles`, `projects`, `milestones` (paged like `teams`), and `test_team_states_not_cached` (two calls → two requests). Rewrite the existing tests pinning a create/update bad state as `BAD_ARGS` to expect `STATE_NOT_FOUND` with `candidates`, and `LABEL_MISSING` tests to expect `available`. `tests/test_task_state.py`: the existing `_target_state` `BAD_ARGS` cases stay unchanged.
- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_directory.py tests/test_linear_client.py tests/test_agent_tools_generic.py tests/test_task_state.py` — expected: the new tests fail (no module / wrong codes).
- [ ] **Step 3: Implement** `directory.py`, the client calls (GraphQL: `users(first:250, after:)` with `id name displayName email app active`; `team(id){ activeCycle{id} cycles(first:250, after:){ nodes{ id number name startsAt endsAt } } }`; `projects(first:250, after:){ id name state }`; `project(id){ projectMilestones(first:250, after:){ id name } }`), `_Refusal.extra`, and route the existing callers through `for_client(ctx.client)`. `questions._set_blocked` uses `for_client(client).rows("labels", team)`.
- [ ] **Step 4: Run** the full suite — expected: all pass.
- [ ] **Step 5: Commit** `feat: name directory with TTL cache and candidate-carrying misses (<sub-ticket>)`.

### Task 2: Scalar digest normalization (`FIELD_TYPES`)

**Files:**
- Modify: `hermes_linear/tool_approval.py` (`ARRAY_FIELDS` → `FIELD_TYPES`; `normalize_args` mirrors scalar coercion)
- Test: `tests/test_tool_approval.py`

**Interfaces:**
- Produces: `tool_approval.FIELD_TYPES: dict[str, dict[str, str]]`, per gated tool every schema property → one of `"array"`, `"integer"`, `"number"`, `"boolean"`, `"string"`, each optionally suffixed `"|null"` when the schema allows null (type list containing `"null"`). `normalize_args(tool, args)` signature unchanged.
- Coercion rules (mirror `tools/arg_coercion.py`): only `str` values are touched (arrays: today's `_as_array`); `"|null"` and `value.strip().lower() == "null"` → `None`; integer: `float(value)` finite and whole → `int`, else unchanged; number: finite float → `int` if whole else `float`; boolean: `"true"`/`"false"` case-insensitive; otherwise the original value.

- [ ] **Step 1: Write the failing tests.** `test_field_types_match_schemas`: derive the map from `agent_tools.TOOL_SCHEMAS` for every tool in `GATED_TOOLS` and assert equality with `FIELD_TYPES` (replaces the `ARRAY_FIELDS` sync test). `test_normalize_scalar_mirrors_hermes` (Review Focus 1): for a temporary schema registered in `tools.registry` (or the real `coerce_tool_args` on a dict mirroring a schema), assert `normalize_args(raw) == normalize_args(coerce_tool_args(raw))` for `"2"`, `"2.5"` (integer: unchanged), `"true"`, `"TRUE"`, `"null"` on a nullable and on a non-nullable string field, and an array given as a JSON string. `test_normalize_idempotent`.
- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_tool_approval.py` — expected: FAIL (`FIELD_TYPES` missing).
- [ ] **Step 3: Implement** in `tool_approval.py` (keep it import-free of `agent_tools`; the sync test guards drift). Keep `ARRAY_FIELDS` out (delete; grep `tests/` and `hermes_linear/` for users).
- [ ] **Step 4: Run** the full suite — expected: all pass.
- [ ] **Step 5: Commit** `feat: digest normalization mirrors Hermes scalar coercion (<sub-ticket>)`.

### Task 3: `issue_fields` — parse and resolve the shared fields

**Files:**
- Create: `hermes_linear/issue_fields.py`
- Test: `tests/test_issue_fields.py` (new)

**Interfaces:**
- Consumes: `directory.for_client` (Task 1), `agent_tools._Refusal`.
- Produces: `issue_fields.FIELDS = ("assignee", "delegate", "priority", "estimate", "due_date", "cycle", "milestone", "project")` (+ `"team"` on create); `parse(args: dict, *, update: bool) -> dict` (sync; key absent → not in the result; `None` / `""` → `None` meaning "clear" on update and "absent" on create; `BAD_ARGS` for a bad type, a priority outside the map, a negative or non-integer estimate, a non-calendar `due_date`, `team` on update); `async resolve(ctx, parsed: dict, *, team_id: str, project_id: str | None) -> Resolved`.
- `Resolved` (dataclass): `input: dict` (Linear `IssueCreateInput`/`IssueUpdateInput` keys: `assigneeId`, `delegateId`, `priority`, `estimate`, `dueDate`, `cycleId`, `projectMilestoneId`, `projectId`, `teamId`), `team_id: str` and `project_id: str | None` (the resulting ones), `warnings: list[str]`, `starts_session: bool | None` (False only for self-delegation), `team: dict | None`, `project: dict | None` (the resolved rows, for approval targets).
- Resolution rules (spec §1.1): users by UUID or case-insensitive exact `name`/`displayName`/`email`, `me` → `await ctx.client.viewer_id()`; delegate not `app` → `DELEGATE_NOT_AGENT`; cycle by UUID, `number` (int or digit string), name, `current`, `next` in `team_id`; milestone in the resulting project (no project → `MILESTONE_NOT_FOUND`, `candidates: []`); project by UUID or name; team by UUID, key or name.

- [ ] **Step 1: Write the failing tests** (stub client + `Directory` with canned rows): parse table — each field good/bad, `None`/`""` per mode, `team` on update `BAD_ARGS`, `due_date: "2026-02-30"` `BAD_ARGS`, `priority: "High"` → 2, `priority: 4` → 4, `priority: "5"` `BAD_ARGS`. Resolve — `test_me_is_app`, `test_user_by_email_case_insensitive`, `test_user_ambiguous`, `test_user_missing_candidates`, `test_delegate_human_refused`, `test_self_delegation_warning` (exact warning text, `starts_session is False`), `test_other_agent_delegate_no_warning`, `test_cycle_current_next_number_name`, `test_cycle_none_when_cycles_off` (`CYCLE_NOT_FOUND`, `candidates: []`), `test_milestone_in_resulting_project`, `test_milestone_without_project`, `test_clear_on_update` (`assigneeId: None` etc.), `test_project_resolution_sets_project_id`.
- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_issue_fields.py` — expected: FAIL (no module).
- [ ] **Step 3: Implement** `issue_fields.py` (no tool, no registration).
- [ ] **Step 4: Run** the full suite — expected: all pass.
- [ ] **Step 5: Commit** `feat: shared issue field parsing and resolution (<sub-ticket>)`.

### Task 4: Create/update field parity, team/project approval targets

**Files:**
- Modify: `hermes_linear/agent_tools.py` (`Gate.record_target`; `_parse_create` / `_parse_update` / `_create` / `_update` use `issue_fields`; schemas gain the fields; descriptions updated)
- Modify: `hermes_linear/tool_approval.py` (`approval_message` renders `kind` targets; `FIELD_TYPES` for the new schema fields)
- Test: `tests/test_agent_tools_generic.py`, `tests/test_agent_tools_gate.py`, `tests/test_tool_approval.py`, `tests/test_tool_approval_e2e.py`

**Interfaces:**
- Consumes: `issue_fields.parse` / `resolve` / `Resolved` (Task 3), `FIELD_TYPES` (Task 2).
- Produces: `Gate.record_target(id: str, kind: str, name: str) -> None` (appends `{"id", "kind", "identifier": name, "title": ""}`, deduped by id); on an approved pass, `agent_tools._target_ok(ctx, id) -> bool` (`id in gate.approved`); `approval_message` renders a target with `kind` as `- {kind} "{name}"`.
- Create (spec §1.2): base = parent's `(team.id, project.id)` if `parent`, else the session ticket's. A resolved `team` / `project` differing from the base → `record_target` on the first pass (before `_check_gate`), refused `OUT_OF_SCOPE` on an approved pass not covering it. `project: None` → base. State, labels, cycle resolve in the resulting team; milestone in the resulting project. Result `{ok, issue}` plus `starts_session` / `warnings` when set.
- Update (spec §1.3): new `project` allowed with no target if it equals the session ticket's project or the (new, if `parent` is in the call, else current) parent's project; clearing (`None`) is no target. Milestone resolves in the resulting project.
- Schemas: create gains `team`, `project`, `assignee`, `delegate`, `due_date`, `cycle`, `milestone` (`{"type": "string"}`), `priority` (`{"type": "string"}`, description lists `none|urgent|high|medium|low` or `0`–`4`; `parse` also accepts an integer, which coercion leaves alone, so both digests agree) and `estimate` (`{"type": "integer"}`). Update gains the same minus `team`, with `{"type": ["string", "null"]}` / `{"type": ["integer", "null"]}` so `null` clears.

- [ ] **Step 1: Probe (live, RET-92 only).** As the app: `issueUpdate(id:"RET-92", input:{delegateId:<app viewer id>})` then read `delegate`; `delegateId: <Mike's id>` (record the error code); create a child of RET-92 with `projectMilestoneId` of the test project's milestone if any (else skip and note), then `issueUpdate(projectId: <hermes-linear project id>)` and read `projectMilestone` (dropped or not); `issueUpdate(estimate: 2)` (record the error on estimates-off). Clear the delegate afterwards (`delegateId: null`). Write the findings into the report and into the update tool's `project` description line ("moving to another project clears / keeps the milestone").
- [ ] **Step 2: Write the failing tests:** `test_create_fields_input` (all fields → exact `IssueCreateInput`), `test_create_follows_parent_project_no_target` (Review Focus 2), `test_create_other_project_needs_approval` (first pass: `APPROVAL_REQUIRED`, zero `issue_create` calls; card has `- project "orion"`), `test_create_other_team_needs_approval`, `test_create_approved_writes_only_approved_target`, `test_update_project_session_or_parent_no_prompt`, `test_update_project_new_parent_same_call`, `test_update_other_project_needs_approval`, `test_update_clear_fields`, `test_self_delegation_result`, `test_update_team_bad_args`. In `tests/test_tool_approval.py`: `test_message_renders_kind_targets`. In `tests/test_tool_approval_e2e.py`: `test_e2e_coerced_estimate_one_prompt` (Review Focus 1: hook on raw `{"estimate": "2", ...}` via `_dispatch_pre_tool_call_hooks`, handler on `coerce_tool_args` output; one approval, re-call runs) and `test_e2e_project_target` (approve → re-call creates in the approved project only).
- [ ] **Step 3: Run** those files — expected: FAIL.
- [ ] **Step 4: Implement.**
- [ ] **Step 5: Run** the full suite + `hermes plugins validate .` — expected: all pass, validate passes.
- [ ] **Step 6: Commit** `feat: create/update field parity with team and project approval targets (<sub-ticket>)`.

### Task 5: `linear_relation` and `linear_task_state blocked_by`

**Files:**
- Create: `hermes_linear/tools_relation.py`
- Modify: `hermes_linear/linear_client.py` (`QUERY_ISSUE_RELATIONS`, `MUTATION_RELATION_CREATE`; `issue_relations(id)`, `relation_create(issue_id, related_id, type)`)
- Modify: `hermes_linear/agent_tools.py` (placeholder key, `__all__`, bottom import; `_parse_task_state` / `_task_state` gain `blocked_by`; schema)
- Modify: `hermes_linear/tool_approval.py` (`GATED_TOOLS` += `linear_relation`; `FIELD_TYPES`)
- Modify: `plugin.yaml`, `tests/test_plugin_register.py` `TOOL_NAMES`, `hermes_linear/skills/manage-superpowers/SKILL.md` (one `linear_relation` bullet; `blocked_by` in the SDD `blocked` line)
- Test: `tests/test_tools_relation.py` (new), `tests/test_task_state.py`, `tests/test_linear_client.py`, `tests/test_tool_approval_e2e.py`

**Interfaces:**
- Produces: `LinearClient.issue_relations(issue_id) -> list[dict]` (`[{id, type, issue_id, related_id}]`, outbound `relations` and inbound `inverseRelations`, first 250 each); `LinearClient.relation_create(issue_id, related_id, type) -> str` (relation id); `tools_relation.RELATION_TYPES = ("blocks", "blocked_by", "related", "duplicate_of")`; `async ensure_relation(ctx, issue: dict, other: dict, type: str) -> tuple[str, bool]` (`(relation_id, created)`; `blocked_by` swaps ends to `blocks`; `duplicate_of` → Linear `duplicate`; an existing match returns its id with `created=False`; match rules: same type and direction, `related` either direction).
- `linear_relation(id, type, other)`: both ends `_writable` (gated), self → `BAD_ARGS`, existing → `ALREADY_RELATED` (with `relation_id`), else `{ok, relation_id, issue, other, type}`.
- `linear_task_state`: `blocked_by` only with `blocked` (`BAD_ARGS` otherwise), blocker `_writable` ungated, blocker == task `BAD_ARGS`; order: scope checks → label → `ensure_relation(blocker, task, "blocks")` → question comment if `question`. Result adds `relation_id`, `relation_created`.

- [ ] **Step 1: Probe (live, RET-92 only):** create two children of RET-92; `issueRelationCreate(blocks)` twice between them; record whether the second duplicates, errors (code) or no-ops. Note the children's identifiers in the report.
- [ ] **Step 2: Write the failing tests:** each type's direction and Linear type, `test_already_related_blocks_direction` (reverse direction is not a match for `blocks`), `test_already_related_related_either_way`, `test_self_bad_args`, `test_out_of_scope_other_needs_approval_writes_nothing`, `test_unknown_type_bad_args`; task state: `test_blocked_by_creates_relation_no_question`, `test_blocked_by_with_question`, `test_blocked_by_out_of_tree_refused_before_writes` (zero writes), `test_blocked_by_rerun_no_duplicate_relation` (Review Focus 5), `test_blocked_by_with_done_bad_args`, `test_blocked_without_question_or_blocked_by_bad_args`, `test_label_missing_carries_relation_keys`; client op tests; `test_e2e_relation_out_of_scope` (one approval, re-call writes once).
- [ ] **Step 3: Run** — expected: FAIL. **Step 4: Implement.** **Step 5: Run** full suite + validate + skill cap test — expected: pass.
- [ ] **Step 6: Commit** `feat: linear_relation and task_state blocked_by (<sub-ticket>)`.

### Task 6: `linear_comment` `reply_to` and `resolve`

**Files:**
- Modify: `hermes_linear/linear_client.py` (`QUERY_COMMENT_GET`, `MUTATION_COMMENT_RESOLVE`; `comment_get(id)`, `comment_create(issue_id, body, parent_id=None)`, `comment_resolve(id, resolving_id)`)
- Modify: `hermes_linear/agent_tools.py` (`_parse_comment` / `_comment`; schema)
- Modify: `hermes_linear/tool_approval.py` (`FIELD_TYPES`: `reply_to` string, `resolve` boolean)
- Modify: `hermes_linear/skills/manage-superpowers/SKILL.md` (the `linear_comment` bullet)
- Test: `tests/test_agent_tools_generic.py`, `tests/test_linear_client.py`, `tests/test_agent_tools_gate.py`

**Interfaces:**
- Produces: `LinearClient.comment_get(id) -> dict | None` (`{id, issue_id, parent_id, resolved_at}`; `None` for an unknown comment); `comment_create(..., parent_id=None) -> str`; `comment_resolve(comment_id, resolving_comment_id) -> None`.
- Rules (spec §1.5): `resolve` without `reply_to` / `edit_marker` with `reply_to` → `BAD_ARGS` (parse); `id` `_writable` (gated) → `comment_get(reply_to)`; unknown or `issue_id != issue["id"]` → `COMMENT_NOT_ON_ISSUE`; thread = `parent_id or id` (re-fetched if the probe shows parents nest); `_check_gate`; create reply with `parentId=thread`; if `resolve` and the thread's `resolved_at` is null → `comment_resolve(thread, reply)`, `resolved: true`; already resolved → `already_resolved: true`, `resolved: true`. Result adds `thread_id`, `resolved`.

- [ ] **Step 1: Probe (live, RET-92 only):** a top-level comment, a reply, a `commentCreate(parentId: <the reply>)` (refused, flattened or nested?), `commentResolve(id: <top>, resolvingCommentId: <reply>)`, and a second `commentResolve` on the resolved thread. Record outcomes.
- [ ] **Step 2: Write the failing tests:** `test_reply_posts_with_parent`, `test_reply_to_reply_uses_thread`, `test_reply_to_other_issue_refused` (Review Focus 4: other issue in scope, zero posts), `test_reply_to_unknown_refused`, `test_resolve_posts_then_resolves`, `test_resolve_already_resolved`, `test_resolve_without_reply_to_bad_args`, `test_marker_with_reply_to_bad_args`, `test_out_of_scope_reply_needs_approval_writes_nothing`, client op tests.
- [ ] **Step 3: Run** — FAIL. **Step 4: Implement.** **Step 5: Run** full suite — pass.
- [ ] **Step 6: Commit** `feat: linear_comment thread replies and resolve (<sub-ticket>)`.

### Task 7: `linear_lookup`

**Files:**
- Create: `hermes_linear/tools_lookup.py`
- Modify: `hermes_linear/agent_tools.py` (placeholder key, `__all__`, bottom import)
- Modify: `plugin.yaml`, `tests/test_plugin_register.py` `TOOL_NAMES`, `hermes_linear/skills/manage-superpowers/SKILL.md` (one bullet)
- Test: `tests/test_tools_lookup.py` (new), `tests/test_plugin_register.py` (fresh-interpreter import order for `tools_relation` and `tools_lookup` first)

**Interfaces:**
- Consumes: `directory.for_client`, `Directory.rows` (Task 1).
- Produces: `linear_lookup(kind, team?, project?, query?)` per spec §1.7: `kind` in `("teams","states","labels","users","cycles","projects","milestones")` else `BAD_ARGS`; `team` resolved via the directory (`TEAM_NOT_FOUND`), default the session ticket's team; `project` likewise (`PROJECT_NOT_FOUND`), default the session ticket's project; no session and no arg → `NOT_LINEAR_SESSION`; `query` case-insensitive substring over name/key/displayName/email; ≤ `LOOKUP_MAX_ROWS = 250` rows with `truncated: true` past it; result `{ok, kind, rows}`. Never forces a refetch.

- [ ] **Step 1: Write the failing tests:** each kind's row shape, the defaults, `query`, the 250 cap, `test_no_session_needs_arg`, `test_unknown_team`, `test_lookup_uses_cache` (no extra fetch inside the TTL), `test_import_order_fresh_interpreter` (subprocess: `import hermes_linear.tools_lookup` first, then `agent_tools` roster complete; same for `tools_relation`).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** full suite + validate — pass.
- [ ] **Step 5: Commit** `feat: linear_lookup over the cached directory (<sub-ticket>)`.

### Task 8: README, skill text, final checks

**Files:**
- Modify: `README.md` (tool table: new args on create/update/comment/task_state, `linear_relation`, `linear_lookup`, new error codes; self-delegation note)
- Modify: `hermes_linear/skills/manage-superpowers/SKILL.md` (tool count wording "the thirteen tools"; create/update bullets list the new args; Large projects: "The tools can set a delegate, but a delegation by the app starts no session: ask the user to delegate"; ≤ 150 lines)
- Test: `tests/test_plugin_register.py` (skill cap and tool-name test)

- [ ] **Step 1:** Update README and skill; run `.venv/bin/pytest -q` and `hermes plugins validate .` — expected: all pass, skill ≤ 150 lines, validate passes with the same private-import list as `origin/main`.
- [ ] **Step 2:** `git push`; check branch CI via the GitHub runs API (`branch=ret-121-write-parity`) — expected: `conclusion: success` for the head sha (both Hermes matrix jobs).
- [ ] **Step 3: Commit** `docs: README and skill for write parity tools (<sub-ticket>)`.
