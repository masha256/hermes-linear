# Projects and documents (`linear_project`, `linear_document`) Implementation Plan

Linear: RET-122

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The agent reads projects and documents, creates and edits documents (no prompt on in-scope issues), and posts project updates with health, creates/edits milestones and adds project links (always one Allow once), all as the app through two new typed tools. Spec and plan syncs write `Spec:` / `Plan:` documents on the ticket and an agent-written summary description (spec §6).

**Architecture:** Two new self-registering tool modules, `tools_document.py` and `tools_project.py`, on `agent_tools._tool(..., gated=...)` and A's `Gate`. New `LinearClient` calls for documents, project detail, project updates, milestones and project links. A new `created_documents` store table extends "made by this ticket" scope to documents. `linear_issue_get` gains `documents` through B's `QUERY_ISSUE_DETAILS`.

**Tech Stack:** Python 3.11+, the plugin's `LinearClient` (aiohttp), pytest, `tests/fake_linear.py` / `tests/fake_workspace.py`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-25-projects-and-documents-design.md`

## Global Constraints

- Public Hermes plugin API only (`~/.hermes/hermes-agent/hermes_cli/data/plugin_api.json`); no Hermes core changes; `hermes plugins validate .` must pass and its "private core imports" list must not grow. Tests may import Hermes internals.
- Tools never raise; failures are `{"ok": false, "error": CODE, ...extra}` (raise `agent_tools._Refusal(code, extra=...)` inside bodies). Every Linear call uses the app token through `LinearClient`. Linear failures keep `exc.code or "LINEAR_ERROR"` plus `message`, preferring `extensions.userPresentableMessage` when present.
- `QUERY_ISSUE_GET` and `client.issue_get` stay unchanged.
- Gated bodies: every read, resolution, duplicate check and scope check, then `_check_gate(ctx)`, then writes. A first pass that needs approval makes no write call. `get` actions never record a target.
- Never send `trashed`, `hiddenAt`, `issueId` or `projectId` in `documentUpdate`; never send `initiativeId` / `releaseId` in `entityExternalLinkCreate`.
- Constants (exact): `DOCUMENT_CONTENT_MAX = 50_000`; `PROJECT_UPDATE_BODY_MAX = 4_000`; `PROJECT_UPDATES_DEFAULT = 3`; `PROJECT_UPDATES_MAX = 20`. Health map: `on_track→onTrack`, `at_risk→atRisk`, `off_track→offTrack` (Linear's names also accepted). `target_date` grammar: issue_fields' `_YMD` (`YYYY-MM-DD`), same as `due_date`. Link URL: `^https?://\S+$` after `strip()`.
- New error codes (exact): `DOCUMENT_NOT_FOUND`, `ALREADY_EXISTS`. Reused: `PROJECT_NOT_FOUND`, `MILESTONE_NOT_FOUND` (directory misses with `candidates`), `ALREADY_LINKED`, `BAD_ARGS`, `APPROVAL_REQUIRED`, `OUT_OF_SCOPE`, `NOT_LINEAR_SESSION`.
- Arguments that don't belong to the chosen `action` → `BAD_ARGS`.
- Approval card targets: project `- project "<name>"` (C's `record_target(id, "project", name)`); new kind `document`: `record_target(doc_id, "document", title)` → `- document "<title>"`. Issue targets via `_writable` as today.
- Adding a tool updates in the same commit: its module's `SCHEMAS`/`HANDLERS` (self-registering at the module's end, like `tools_relation.py`), a placeholder key in `agent_tools.TOOL_SCHEMAS`/`TOOL_HANDLERS`, `agent_tools.__all__`, `plugin.yaml` `provides_tools` (order: the existing 13, then `linear_project`, `linear_document`), `tests/test_plugin_register.py` `TOOL_NAMES`, `tool_approval.GATED_TOOLS` + `FIELD_TYPES`, and one bullet in `hermes_linear/skills/manage-superpowers/SKILL.md`, which must stay ≤ 150 lines (pay for each line by tightening elsewhere; update its tool count wording).
- Schema types: nullable fields as `"type": ["string", "null"]` only (no `nullable`, no `anyOf`).
- Tests run from `.worktrees/ret122`: `.venv/bin/pytest -q` (baseline 1521 passed). Commit messages carry the task's sub-ticket ID.
- Edit files with `write_file` / `patch`, never shell heredocs, `python -c` or `sed -i`.
- Sync (spec §6): `SYNC_SUMMARY_MAX = 5_000`; `SYNC_DOCUMENT_MAX = 250_000` (Linear's document limit, probed); titles `Spec: <H1>` / `Plan: <H1>`; lookup among the app's own documents on the header ticket by prefix; two → `AMBIGUOUS`, nothing written; description markers `<!-- hermes-linear:spec -->` / `<!-- hermes-linear:plan -->`, plan part after a `---` line; no plan comment is created.
- `link` without `label` sends the URL as the label (Linear requires it; Task 1).
- Live probes as the app: `python3 ~/.hermes/profiles/neo/skills/software-development/hermes-linear-dev/scripts/linear-query.py '<graphql>'`. Write probes touch **only RET-161** and the project **hermes linear test project** (`d984ad14-969e-4d89-9538-14285b5e1743`); the task report lists everything created.

## Review Focus

1. **An approved re-call whose args the model sent as strings (`"updates": "5"`, `"target_date": "null"`)** runs once without a second prompt. Owned by Task 3 (`test_e2e_project_coerced_args_one_prompt`) and Task 2 (`test_e2e_document_one_prompt`).
2. **Document update in a later turn after a restart** (fresh `Store` on the same file) of a document this ticket created on a project: no prompt. Owned by Task 2 (`test_update_created_document_after_restart_no_prompt`).
3. **Re-run of `document create` after a timeout that actually succeeded**: `ALREADY_EXISTS` with the first id, no second document. Owned by Task 2 (`test_create_same_title_already_exists`).
4. **`link` with the same URL plus trailing whitespace or a different label**: `ALREADY_LINKED`, no card, no write. Owned by Task 4 (`test_link_duplicate_url_whitespace`).
5. **`get` on a project with long update bodies or a 60 KB document**: bounded result with truncation flags, never a `TOOL_RESULT_TOO_LARGE`-style failure. Owned by Task 3 (`test_get_cuts_update_bodies`) and Task 2 (`test_get_cuts_content`).

---

### Task 1: Client calls, `created_documents`, `documents` on `linear_issue_get` — RET-163

**Files:**
- Modify: `hermes_linear/linear_client.py` (new queries/mutations; `QUERY_ISSUE_DETAILS` gains `documents { nodes { id title url } }`)
- Modify: `hermes_linear/store.py` (table + two methods)
- Modify: `hermes_linear/tools_read.py` (`shape_details` adds `documents`)
- Modify: `tests/fake_linear.py` / `tests/fake_workspace.py` (documents, projects detail, project updates, milestones create/update with duplicate-name refusal, external links)
- Test: `tests/test_linear_client.py`, `tests/test_store.py`, `tests/test_tools_read.py`

**Interfaces:**
- Produces (all async on `LinearClient`, raising `LinearError` like the rest):
  - `document_get(ref: str) -> dict | None` — `document(id: ref)` (UUID or slugId); `{id, title, url, content, issue: {id, identifier, title} | None, project: {id, name} | None, creator: name | None, updated_at}`; `None` when Linear says not found / null.
  - `documents_on(issue_id: str | None = None, project_id: str | None = None) -> list[dict]` — `[{id, title, url}]` of the issue's or project's `documents(first: 250)`.
  - `document_create(title: str, content: str, *, issue_id: str | None, project_id: str | None) -> dict` — `{id, url}`.
  - `document_update(doc_id: str, fields: dict) -> dict` — `fields` ⊆ `{title, content}` (assert); `{id, url}`.
  - `project_detail(project_id: str, updates: int) -> dict` — `{id, name, url, state, health, lead, target_date, links: [{id, label, url}], milestones: [{id, name, target_date}], documents: [{id, title, url}], updates: [{id, created_at, author, health, body}]}` (updates newest first, `projectUpdates(first: updates, orderBy: createdAt)` sorted client-side).
  - `project_update_create(project_id: str, body: str, health: str | None) -> dict` — `{id, url}`; omits `health` from the input when `None`.
  - `milestone_create(project_id: str, name: str, description: str | None, target_date: str | None) -> dict` and `milestone_update(milestone_id: str, fields: dict) -> dict` — `{id, name, target_date}`; `fields` ⊆ `{name, description, targetDate}` with `None` meaning clear.
  - `project_link_create(project_id: str, url: str, label: str | None) -> dict` — `{id}`.
- Produces: `Store.record_document(owner_issue_id: str, document_id: str) -> None` (`INSERT OR IGNORE`) and `Store.documents_by(owner_issue_id: str) -> set[str]`; table `created_documents(document_id TEXT PRIMARY KEY, owner_issue_id TEXT NOT NULL, created_at TEXT NOT NULL)` + index `created_documents_owner`, created with `CREATE TABLE IF NOT EXISTS` (no `SCHEMA_VERSION` change).
- Produces: `linear_issue_get` result key `documents: [{id, title, url}]` (always present, `[]` when none).

- [ ] **Step 1: Write the failing tests.** Client: op name, variables and mapping for each call above; `test_document_update_rejects_other_fields` (`trashed` → `AssertionError`/`ValueError` before any request); `test_project_update_omits_null_health` (no `health` key in the sent input); `test_milestone_duplicate_name_error` (fake returns Linear's `INPUT_ERROR` with `userPresentableMessage`; the `LinearError` carries that message). Store: `test_record_document_roundtrip_and_restart` (new `Store` on the same path sees it), `test_documents_by_other_owner_empty`, `test_existing_db_gains_table` (open a DB created by the current schema, then the new code: table exists). Read: `test_issue_get_lists_documents` and `test_issue_get_documents_empty`.
- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_linear_client.py tests/test_store.py tests/test_tools_read.py` — expected: new tests FAIL.
- [ ] **Step 3: Implement.** Before writing the queries, run each one live once with `linear-query.py` (reads only; `document(id: "dd7c3ea51183")`, `project(id: "d984ad14-…")`) and paste the commands and results in the report.
- [ ] **Step 4: Run** the full suite — expected: all pass.
- [ ] **Step 5: Commit** `feat: client calls for documents and projects, created_documents store (<sub-ticket>)`.

### Task 2: `linear_document` — RET-164

**Files:**
- Create: `hermes_linear/tools_document.py`
- Modify: `hermes_linear/agent_tools.py` (placeholder keys, `__all__`, bottom `from . import tools_document`), `hermes_linear/tool_approval.py` (`GATED_TOOLS`, `FIELD_TYPES`), `plugin.yaml`, `tests/test_plugin_register.py`, `hermes_linear/skills/manage-superpowers/SKILL.md` (one bullet)
- Test: `tests/test_tools_document.py` (new), `tests/test_tool_approval_e2e.py`, `tests/test_plugin_register.py`

**Interfaces:**
- Consumes: Task 1 client calls and store methods; `agent_tools._tool`, `_writable`, `_check_gate`, `check_write_scope`, `_Refusal`, `_GATED_NOTE`, `_ISSUE_REF`; `Gate.record_target`.
- Produces: tool `linear_document` with schema properties `action` (enum `get|create|update`), `id`, `title`, `content`, `issue`, `project` (all `string`); `FIELD_TYPES["linear_document"]` = all `"string"`.
- Produces: `tools_document.doc_ref(raw: str) -> str` — a UUID or slugId passes through; a `https://linear.app/<ws>/document/<slug>-<slugId>` URL gives the last `-` segment (query and fragment dropped); anything else `BAD_ARGS`.
- Behaviour (spec §1.2, §2): `create` target = `issue` | `project` | session ticket (outside a Linear session with neither → `BAD_ARGS`); issue through `_writable`; project resolved through `directory.for_client(ctx.client).resolve("projects", ...)` and always `record_target(id, "project", name)`; same-title duplicate (stripped, case-sensitive) on the target → `ALREADY_EXISTS` with `document_id`, `url`, before the gate; after the write, `store.record_document(ctx.session_issue_id, id)` when `ctx.is_linear`. `update`: in scope if `doc.issue` passes `check_write_scope` or `doc.id in store.documents_by(session)`, else `record_target(doc.id, "document", doc.title)`; outside a Linear session every update records the target. `get`: `content` cut at `DOCUMENT_CONTENT_MAX` with `content_truncated: true`, `content_length`.

- [ ] **Step 1: Write the failing tests.** Per action, through the real registry: `test_get_by_uuid_slug_and_url`, `test_get_not_found`, `test_get_cuts_content` (Review Focus 5: 60,000 chars → 50,000 + flags), `test_create_on_session_ticket_no_prompt` (recorded in `created_documents`), `test_create_on_out_of_scope_issue_asks` (`APPROVAL_REQUIRED`, zero `documentCreate` calls in the fake), `test_create_on_project_always_asks` (session ticket's own project too), `test_create_same_title_already_exists` (Review Focus 3), `test_create_issue_and_project_bad_args`, `test_create_outside_session_without_target_bad_args`, `test_update_in_scope_no_prompt`, `test_update_created_document_after_restart_no_prompt` (Review Focus 2), `test_update_other_document_asks_with_document_target` (card line `- document "<title>"`), `test_update_sends_only_title_content`, `test_update_needs_a_field`, `test_foreign_arg_bad_args` (`project` with `get`). `tests/test_tool_approval_e2e.py`: `test_e2e_document_one_prompt` (real `_dispatch_pre_tool_call_hooks` on raw args, then the handler on `coerce_tool_args` output: one card, the approved re-call writes once). Roster/manifest/`FIELD_TYPES` sync tests include `linear_document`.
- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_tools_document.py tests/test_tool_approval_e2e.py tests/test_plugin_register.py tests/test_tool_approval.py` — expected: FAIL.
- [ ] **Step 3: Implement** `tools_document.py` (module docstring cites spec §1.2/§2) and the registration edits; the skill bullet: `` `linear_document` (`action`: `get|create|update`, `id?`, `title?`, `content?`, `issue?`|`project?`): docs; on an in-scope issue no prompt. ``
- [ ] **Step 4: Run** the full suite and `hermes plugins validate .` — expected: all pass, private-import list unchanged.
- [ ] **Step 5: Commit** `feat: linear_document tool (<sub-ticket>)`.

### Task 3: `linear_project` — `get` and `post_update` — RET-165

**Files:**
- Create: `hermes_linear/tools_project.py`
- Modify: `hermes_linear/agent_tools.py`, `hermes_linear/tool_approval.py`, `plugin.yaml`, `tests/test_plugin_register.py`, `hermes_linear/skills/manage-superpowers/SKILL.md` (one bullet), the fresh-interpreter import-order test (all three tool-module import orders)
- Test: `tests/test_tools_project.py` (new), `tests/test_tool_approval_e2e.py`

**Interfaces:**
- Consumes: Task 1 `project_detail`, `project_update_create`; `directory` `projects` resolve; `ctx.session_issue_id`'s project via `client.issue_get` (Linear session) or `settings.default_project` (outside).
- Produces: tool `linear_project`, schema properties `action` (enum `get|post_update|milestone|link`), `project`, `updates` (`integer`), `body`, `health` (enum `on_track|at_risk|off_track|onTrack|atRisk|offTrack`), `milestone`, `name`, `description` (`["string","null"]`), `target_date` (`["string","null"]`), `url`, `label`. `FIELD_TYPES["linear_project"]` accordingly (`updates: "integer"`, `description`/`target_date`: `"string|null"`, rest `"string"`). Task 4 fills `milestone`/`link`; until then those actions answer `BAD_ARGS`.
- Produces: `tools_project._project(ctx, ref: str | None) -> dict` (`{id, name}`; no project → `PROJECT_NOT_FOUND` with `candidates` from `directory.rows("projects")`) and `tools_project._ask_project(ctx, project: dict) -> None` (`record_target(id, "project", name)` on a first pass, `OUT_OF_SCOPE` if a different pass is approved — reuse `agent_tools._scope_target(ctx, row, "project")`).

- [ ] **Step 1: Write the failing tests.** `test_get_defaults_to_session_project`, `test_get_outside_uses_default_project`, `test_get_no_project_not_found_with_candidates`, `test_get_updates_default_3_max_20` (`21` → `BAD_ARGS`), `test_get_cuts_update_bodies` (Review Focus 5: 4,000 + `body_truncated`), `test_get_never_asks` (no stored request), `test_post_update_always_asks` (session ticket's own project too; zero `projectUpdateCreate` calls on the first pass), `test_post_update_health_map` (each agent name → Linear name; `"great"` → `BAD_ARGS`), `test_post_update_requires_body`, `test_post_update_outside_deny`. E2E: `test_e2e_project_coerced_args_one_prompt` (Review Focus 1: raw `"updates": "5"` on get is harmless; raw `post_update` with `health` string → one card, approved re-call posts once). Import-order test covers `tools_project`.
- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_tools_project.py tests/test_tool_approval_e2e.py tests/test_plugin_register.py` — expected: FAIL.
- [ ] **Step 3: Implement**; probe once live in the test project what a `projectUpdateCreate` without `health` does to `project.health` and record the answer in the report and in spec §6 (edit in place). Skill bullet: `` `linear_project` (`action`: `get|post_update|milestone|link`, `project?`, …): project read; writes always ask. ``
- [ ] **Step 4: Run** the full suite and `hermes plugins validate .` — expected: pass.
- [ ] **Step 5: Commit** `feat: linear_project get and post_update (<sub-ticket>)`.

### Task 4: `linear_project` — `milestone` and `link` — RET-166

**Files:**
- Modify: `hermes_linear/tools_project.py`
- Test: `tests/test_tools_project.py`

**Interfaces:**
- Consumes: Task 1 `milestone_create`, `milestone_update`, `project_link_create`, `project_detail` (links); Task 3 `_project`, `_ask_project`; directory `milestones` kind for `milestone` refs; `issue_fields` `_YMD`.
- Produces: `milestone` returns `{ok, milestone_id, name, target_date, created}`; Linear's "name not unique" `INPUT_ERROR` → `ALREADY_EXISTS` with `message`. `link` returns `{ok, link_id}`; duplicate URL → `ALREADY_LINKED` with `link_id`, before the gate.

- [ ] **Step 1: Write the failing tests.** `test_milestone_create_asks_then_creates`, `test_milestone_create_requires_name`, `test_milestone_update_by_name_and_uuid`, `test_milestone_update_needs_a_field`, `test_milestone_update_null_clears` (`description: null` / raw `"null"` → `None` sent), `test_milestone_not_found_candidates`, `test_milestone_bad_target_date` (`2026-W05-1` → `BAD_ARGS`), `test_milestone_duplicate_name_already_exists`, `test_link_asks_then_links`, `test_link_duplicate_url_whitespace` (Review Focus 4: no request stored, no write), `test_link_rejects_non_http` (`javascript:x`, `ftp://x`), `test_link_never_sends_initiative_or_release`.
- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_tools_project.py` — expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full suite — expected: pass.
- [ ] **Step 5: Commit** `feat: linear_project milestones and links (<sub-ticket>)`.

### Task 5: README, skill text, final checks — RET-167

**Files:**
- Modify: `README.md` (tool table rows; behaviour table: document scope, project writes always ask, `created_documents`), `hermes_linear/skills/manage-superpowers/SKILL.md` (tool count "fifteen"; out-of-scope sentence names `linear_document` and says `linear_project` writes always ask; re-run note: `post_update` posts again; setup step 3: "add it with `linear_project` `link` (asks once)" replaces "tools can't edit projects"), the spec's "verify during implementation" §6 (mark each item settled with its evidence)
- Test: `tests/test_plugin_register.py` (skill line cap and tool names), full suite

**Interfaces:**
- Consumes: Tasks 2–4 behaviour exactly as built; every sentence is checked against the code it describes.

- [ ] **Step 1: Update** the README and skill text; keep the skill ≤ 150 lines.
- [ ] **Step 2: Run** `.venv/bin/pytest -q` and `hermes plugins validate .` — expected: all pass; report the counts.
- [ ] **Step 3: Commit** `docs: projects and documents in README and skill (<sub-ticket>)`.

### Task 6: `linear_sync_spec` writes a `Spec:` document and the spec part of the description

**Files:**
- Modify: `hermes_linear/sync.py` (document and description rendering), `hermes_linear/agent_tools.py` (`_parse_sync_spec`, `_sync_spec`, the `linear_sync_spec` schema), `hermes_linear/linear_client.py` (`documents_on` rows gain `creator_id`; `QUERY_ISSUE_GET` untouched — read the description through the existing `issue_get` result)
- Test: `tests/test_sync_spec.py`, `tests/test_linear_client.py`

**Interfaces:**
- Consumes: Task 1 `documents_on`, `document_create`, `document_update`, `document_get` (for the stored content's hash), `Store.record_document`; existing `sync.footer`, `sync.with_desc_hash` / `read_desc_hash` / `desc_hash`, `sync.utf16_len` / `utf16_truncate`, `TRUNCATED`, `SPEC_INLINE_MAX`, `client.viewer_id()`, `_writable`.
- Produces in `sync.py` (Task 7 reuses all of them):
  - `SYNC_SUMMARY_MAX = 5_000`; `SYNC_DOCUMENT_MAX = 250_000`; `SPEC_PART_MARKER = "<!-- hermes-linear:spec -->"`; `PLAN_PART_MARKER = "<!-- hermes-linear:plan -->"`.
  - `doc_title(kind: str, text: str, path: str) -> str` — `kind` is `"Spec"` / `"Plan"`; `f"{kind}: {h1}"`, h1 = first `^# ` line stripped, else the file stem.
  - `document_content(text: str, info: SourceInfo) -> tuple[str, bool]` — file text + `\n\n---\n` + `footer(...)`, then the hash marker; cut to `SYNC_DOCUMENT_MAX` with `TRUNCATED` and `Full file (too long to show here): <link>` before the footer when needed; bool = inline.
  - `source_line(kind: str, title: str, doc_url: str, info: SourceInfo) -> str` — `**Spec:** [<title>](<doc_url>) · source [<path> @ <commit or UNCOMMITTED>](<link>)`.
  - `split_plan_part(description: str | None) -> tuple[str, str | None]` — `(before, plan_part)`; `plan_part` starts at the `---` line directly preceding `PLAN_PART_MARKER` (or at the marker if no bar precedes it) and runs to the end; `None` when there is no marker.
  - `spec_part(summary: str, source: str) -> str` — `f"{summary}\n\n{source}\n{SPEC_PART_MARKER}"`.
- Produces: `agent_tools._sync_document(ctx, issue: dict, kind: str, title: str, content: str) -> dict` — the lookup/create/update of §6.2: returns `{document_id, document_url, document_created, document_skipped}`; raises `_Refusal("AMBIGUOUS", extra={"documents": [...]})` before any write. Task 7 calls it with `kind="Plan"`.
- `linear_sync_spec` schema: `path` and `summary` both required; result keys per spec §6.5.

- [ ] **Step 1: Write the failing tests** (`tests/test_sync_spec.py`, rewriting the old full-description expectations): `test_summary_required` (missing, `"   "`, 5,001 chars → `BAD_ARGS`, nothing written), `test_first_sync_creates_doc_and_replaces_description` (description == `spec_part`, document title `Spec: <H1>`, content ends in footer + hash, `document_created: true`, recorded in `created_documents`), `test_resync_updates_same_doc` (one document after two syncs with different text; title retitled when H1 changed), `test_resync_unchanged_skips` (`document_skipped: true`, zero `documentUpdate` and zero `issueUpdate` calls), `test_human_spec_doc_ignored` (a human's `Spec: x` document is never touched; the app creates its own), `test_two_app_spec_docs_ambiguous` (nothing written), `test_keeps_plan_part` (an existing description with `---`+`PLAN_PART_MARKER` part keeps it byte for byte after the spec part), `test_out_of_tree_refused` (`OUT_OF_SCOPE`, no document), `test_oversize_file_cut` (260,000-char file → content ≤ 250,000 UTF-16 units, `inline: false`), `test_uncommitted_source_line`. Unit tests for `doc_title` (no H1 → stem), `split_plan_part` (no marker; marker with bar; marker without bar), `source_line`.
- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_sync_spec.py tests/test_linear_client.py` — expected: FAIL.
- [ ] **Step 3: Implement.** The spec-description helpers `spec_description` / `render_spec` / `spec_summary` go if nothing else uses them (grep). Order of writes: document, then description (only when it differs from the current one).
- [ ] **Step 4: Run** the full suite — expected: pass.
- [ ] **Step 5: Commit** `feat: linear_sync_spec writes a Spec document and a summary description (<sub-ticket>)`.

### Task 7: `linear_sync_plan` writes a `Plan:` document and the plan part; plan comment migrated; docs

**Files:**
- Modify: `hermes_linear/sync.py` (`plan_part`; `plan_comment` removed), `hermes_linear/agent_tools.py` (`_parse_sync_plan`, `_sync_plan`, schema), `README.md`, `hermes_linear/skills/manage-superpowers/SKILL.md` (Spec and Plan stages pass `summary`; review reads the documents; the Rules line), `docs/superpowers/specs/2026-11-25-projects-and-documents-design.md` §7 (mark settled items)
- Test: `tests/test_sync_plan.py`, `tests/test_plugin_register.py` (skill ≤ 150 lines)

**Interfaces:**
- Consumes: Task 6's `sync` helpers and `agent_tools._sync_document`; existing `_resolve_tasks`, task create/update/cancel loop, `_marker_tag(PLAN_MARKER)`, `client.app_comments`, `comment_update`, `write_ticket_ids`, `ANY_MARKER_RE`.
- Produces: `sync.plan_part(summary: str, source: str, tasks: list[PlanTask], tickets: dict[int, str]) -> str` — `"---\n" + PLAN_PART_MARKER + "\n" + summary + "\n\n" + source + "\n\n**Tasks:**\n\n" + "- T<n> · <name> — <ID>" lines`.
- The plan document's content is the plan text **with the IDs written back** and markers stripped (as the comment used to be), so a re-run on the written file is `document_skipped`.
- Description = `split_plan_part(current)[0].rstrip() + "\n\n" + plan_part(...)`, cut to `SPEC_INLINE_MAX` by trimming the task lines first, then the summary; written only when different.
- Old comment: the app's comment carrying the plan marker tag whose body is not already `Plan moved to [<title>](<url>).\n\n<tag>` is edited to exactly that; `comment_migrated: true`. No plan comment is ever created.
- Order: document, sub-tickets, description, old comment. Result keys per spec §6.5.

- [ ] **Step 1: Write the failing tests** (`tests/test_sync_plan.py`, rewriting the comment expectations): `test_summary_required`, `test_plan_doc_created_and_part_appended` (spec part kept above `---`; task lines carry the new IDs), `test_plan_resync_replaces_only_plan_part`, `test_plan_resync_unchanged_no_writes` (no document, issue or comment write on the second identical run over the written-back file), `test_no_plan_comment_created`, `test_old_plan_comment_migrated_once` (second run leaves it), `test_human_comment_with_marker_untouched`, `test_plan_without_spec_part_appends_under_brief`, `test_two_app_plan_docs_ambiguous` (no sub-ticket, description or comment write), and the existing sub-ticket tests unchanged. Skill: line cap and that the Spec/Plan stage text names `summary`.
- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_sync_plan.py tests/test_plugin_register.py` — expected: FAIL.
- [ ] **Step 3: Implement**, then the README rows and skill text (per spec §6.6; keep the skill ≤ 150 lines).
- [ ] **Step 4: Run** the full suite and `hermes plugins validate .` — expected: pass.
- [ ] **Step 5: Commit** `feat: linear_sync_plan writes a Plan document; plan comment retired (<sub-ticket>)`.
