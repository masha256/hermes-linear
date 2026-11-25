# Projects and documents: `linear_project`, `linear_document` — design

Linear: RET-122

Status: approved in the RET-122 thread (Q1–Q4 and design sections 1–2), 2026-11-25.
Sub-project D, the last one, of the RET-118 umbrella. It builds on A's approval path (RET-119,
`docs/superpowers/specs/2026-11-23-out-of-scope-approval-design.md`), B's tool modules
(RET-120), C's directory and destination targets (RET-121,
`docs/superpowers/specs/2026-11-24-write-parity-design.md`) and RET-148's outside-session
writes (`docs/superpowers/specs/2026-11-24-outside-session-writes-design.md`).

## Goal

The agent reads projects and documents and makes the routine project and document writes
through typed tools, as the app. It can post a project update with health, create and
edit milestones, add a project link, and create and edit documents. A document on an
in-scope issue runs with no prompt. Every project-level write asks through A's Allow once.

Success: none of these needs `linear_query` plus a raw API call any more, and the
manage-superpowers setup can add the project's `repo` link itself, with one approval click.

Constraints: public Hermes plugin API only, no core changes; tools never raise; every
write is attributed to the app.

## Decisions

| # | Question | Decision |
|---|----------|----------|
| Q1 | Ship `linear_mutate`? | **No.** D ships typed tools; `linear_query` stays the generic read. A typed tool is added when a real gap shows up. |
| Q2 | Can `linear_sync_spec` write to a Linear document? | **No.** The spec stays in the ticket description. `linear_document` is a plain tool for documents the agent writes on purpose. The sync tools do not change. |
| Q3 | Which project and document writes run without asking | A **document follows issue scope**: it runs with no prompt when it is attached to an in-scope issue or the session ticket's tools created it. **Project updates, milestones, project links and project documents always ask**, the session ticket's own project included. |
| Q4 | Can `linear_project` add project links? | **Yes, add only**, always behind approval. No link edit or removal. |

Facts from the brainstorm that shape these (all probed as the app):

- Linear has 327 mutations. The brief's name denylist (`*Delete`, `organization*`,
  `team*`, `workflowState*`, `webhook*`, `apiKey*`, `oauth*`, `user*`, `integration*`)
  leaves 203 of them, among them `issueArchive`, `issueBatchUpdate`, the issue imports,
  `notificationArchiveAll`, `customerMerge`, `logout*` and `saml*`. `issueUpdate`,
  `documentUpdate` and `projectUpdate` also take `trashed: true`, a soft delete inside the
  input, usually in `variables`. Hence Q1.
- `documentCreate(input: {issueId})` attaches the document to the issue
  (`Issue.documents`), and the Linear app shows it on the issue (checked by Mike). The
  creator is the app. Linear normalizes document markdown the way it does descriptions
  (`- a` → `* a`) and keeps HTML comments.
- **A second `documentCreate` with the same title creates a second document.** Linear
  de-duplicates nothing.
- `documentUpdate` edits `title` and `content` in place; it also takes `issueId`,
  `projectId`, `trashed` and `hiddenAt`, which the tool never sends.
- The router handles only issue comments and agent-session events, so comments on a
  document never reach a session (part of Q2).
- Specs so far are 16–25 KB and the description limit is 200,000 characters, so every
  spec has synced inline.
- `projectUpdateCreate(input: {projectId, body, health})`, where `health` is the enum
  `ProjectUpdateHealthType` = `onTrack | atRisk | offTrack`. A project update is visible
  to the whole project, notifies its subscribers and sets the project's health.
- `projectMilestoneCreate(input: {projectId, name, description, targetDate})`. **A
  duplicate name is refused by Linear** (`INPUT_ERROR`, "name not unique"; the message
  is in `extensions.userPresentableMessage`). `projectMilestoneUpdate(id, input: {name,
  description, targetDate, sortOrder, projectId})`.
- `entityExternalLinkCreate(input: {projectId, url, label})` works as the app. **It does
  not refuse a duplicate URL**: two identical calls made two links. The same input also
  takes `initiativeId` and `releaseId`, which the tool never sends.

## 1. Tools

Both tools are new modules registered the way `tools_relation.py` is: `tools_project.py`
and `tools_document.py`, built on `agent_tools`' `_tool` machinery. Each registers itself
at its own end, `agent_tools.TOOL_SCHEMAS` / `TOOL_HANDLERS` get a placeholder key for
each in `plugin.yaml` order, and `agent_tools.py` ends with bare imports of both modules.

### 1.1 `linear_project`

Arguments: `action` (required: `get | post_update | milestone | link`) and `project`
(optional: a project name or UUID). Without `project`, the session ticket's project is
used; outside a Linear session, the `default_project` setting. When neither exists, the
tool answers `PROJECT_NOT_FOUND` with `candidates` (the workspace's projects), never an
empty success. Names resolve through the `directory` cache (`projects` kind), so a miss
refetches once and then answers `PROJECT_NOT_FOUND` with `candidates`.

- **`get`** (`updates?`: integer 0–20, default 3). Returns `project`: `id`, `name`,
  `url`, `state` (Linear's `status.name`), `health`, `lead` (name), `target_date`,
  `links` (`label`, `url`), `milestones` (`id`, `name`, `target_date`), `documents`
  (`id`, `title`, `url`), and `updates`: the newest `updates` project updates (`id`,
  `created_at`, `author`, `health`, `body`), newest first, from a tool-only query. Each
  update `body` is cut at 4,000 characters with `body_truncated: true`. The whole result
  is capped the way `linear_issue_get` caps its own (`tools_read.trim`). A read: it never
  records a target and never asks.
- **`post_update`** (`body`: required, non-empty string; `health?`). `health` takes
  `on_track | at_risk | off_track` or Linear's own `onTrack | atRisk | offTrack`; anything
  else is `BAD_ARGS`. Without `health`, the input omits it (what Linear then shows is checked live, §6).
  Returns `update_id` and `url`. **Not idempotent**: a re-run posts a second update.
- **`milestone`** (`milestone?`, `name?`, `description?`, `target_date?`). Without
  `milestone` it creates one: `name` is required (else `BAD_ARGS`). With `milestone` (a
  name or UUID, resolved in the project through the directory `milestones` kind; a miss is
  `MILESTONE_NOT_FOUND` with `candidates`) it updates that milestone, and at least one of
  `name`, `description` and `target_date` is required. `description: null` and
  `target_date: null` clear the field on update. `target_date` is `YYYY-MM-DD` (the same
  grammar C uses for `due_date`). Linear's duplicate-name refusal becomes
  `ALREADY_EXISTS` with Linear's message. Returns `milestone_id`, `name`,
  `target_date`, and `created: true | false`.
- **`link`** (`url`: required, `http(s)://` only, else `BAD_ARGS`; `label?`). First reads
  the project's links: a link with the same URL (compared exactly, after stripping
  surrounding whitespace) answers `ALREADY_LINKED` with its `link_id` and writes nothing.
  Otherwise `entityExternalLinkCreate` with `projectId` only. Returns `link_id`.

Arguments that don't belong to the chosen action (for example `body` with `get`) are
`BAD_ARGS`, so a mistyped action never silently drops input.

### 1.2 `linear_document`

Arguments: `action` (required: `get | create | update`).

- **`get`** (`id`: a document UUID, a `slugId`, or a document URL
  `https://linear.app/<ws>/document/<slug>-<slugId>`; the slug id is the last `-`
  segment). Returns `document`: `id`, `title`, `url`, `content`, `issue` (`identifier`,
  `title`) or `project` (`name`), `creator`, `updated_at`. `content` over 50,000 characters
  is cut there, with `content_truncated: true` and `content_length`. Unknown is
  `DOCUMENT_NOT_FOUND`.
- **`create`** (`title`: required; `content`: required string, may be empty; `issue?` |
  `project?`, at most one, else `BAD_ARGS`). With neither, it attaches to the session
  ticket; outside a Linear session that is `BAD_ARGS` (no session ticket). First reads
  the target's documents: one with the same title (compared after stripping, case-sensitive)
  answers `ALREADY_EXISTS` with its `document_id` and `url`, and writes nothing. Returns
  `document_id`, `url`, and the target (`issue` identifier or `project` name).
- **`update`** (`id` as for `get`; `title?`, `content?`, at least one, else `BAD_ARGS`).
  Sends only `title` and `content`: never `issueId`, `projectId`, `trashed` or `hiddenAt`,
  so the tool can't move, trash or hide a document. Returns `document_id`, `url`.

### 1.3 `linear_issue_get` gains `documents`

B's tool-only detail query adds `documents { nodes { id title url } }`, shown as
`documents: [{id, title, url}]` (empty list when there are none). `QUERY_ISSUE_GET`, which
every scope check and sync uses, is not widened.

## 2. Scope and approval

Both tools join `tool_approval.GATED_TOOLS`, and their fields join `FIELD_TYPES`
(`updates` is `integer`; `description` and `target_date` are `string|null`; the rest are
`string`), so the hook's digest of the raw args equals the handler's digest of Hermes's
coerced args. Nothing changes in the hooks: the gate, the stored request, the Linear
select and the outside-session path are A's and RET-148's as they stand. A `get` never
records a target, so it never stores a request and never asks.

Every write comes after all scope checks and `_check_gate`, so a first pass that needs
approval writes nothing; the duplicate reads (`ALREADY_EXISTS`, `ALREADY_LINKED`) come
before the gate, since a refusal needs no approval.

### 2.1 Targets

- **Project writes** (`post_update`, `milestone`, `link`) always record the project as a
  target, shown on the card as `- project "<name>"` (C's `record_target` kind). The
  session ticket's own project is not exempt (Q3).
- **Document create on an issue:** the issue goes through `_writable`. In scope runs with
  no prompt; out of scope records the issue (`- RET-n "title"`).
- **Document create on a project:** the project is always a target (`- project "<name>"`).
- **Document update:** no prompt when the document's issue is in scope
  (`check_write_scope` on its UUID) or the document id is in `created_documents` for the
  session ticket. Otherwise a new target kind `document`, shown as `- document "<title>"`
  (`record_target(id, "document", title)`).

### 2.2 `created_documents`

A new store table, created like `created_issues` (`CREATE TABLE IF NOT EXISTS`, no
`SCHEMA_VERSION` change): `created_documents(document_id TEXT PRIMARY KEY,
owner_issue_id TEXT NOT NULL, created_at TEXT NOT NULL)`, with an index on
`owner_issue_id`, and `Store.record_document(owner, id)` / `Store.documents_by(owner)`.
A document created from a Linear session is recorded against the session ticket's UUID,
whatever it was attached to (an approved project document included), so later edits from
that ticket's sessions need no prompt, across restarts. Documents created outside a Linear
session are not recorded: there is no session ticket.

### 2.3 Outside a Linear session

Writes follow `outside_session_writes` exactly as the other gated tools do (RET-148):
`deny` answers `NOT_LINEAR_SESSION` before anything runs, and `approve` asks in the
calling chat for every write, since nothing is in scope there. Reads work everywhere.

## 3. Errors

Tools never raise; failures are `{"ok": false, "error": <code>, ...}`.

- `BAD_ARGS`: missing or unknown `action`, an argument that doesn't belong to the action,
  a missing required argument, a bad `health`, `target_date` or `url`, both `issue` and
  `project`, `updates` outside 0–20.
- `PROJECT_NOT_FOUND` / `MILESTONE_NOT_FOUND` with `candidates` (the directory's miss).
  A bad `issue` on document create is Linear's not-found code, as `linear_comment` gives.
- `DOCUMENT_NOT_FOUND` for `get` / `update`.
- `ALREADY_EXISTS` (milestone name, document title) and `ALREADY_LINKED` (project link
  URL), each with the existing id.
- `APPROVAL_REQUIRED`, `OUT_OF_SCOPE`, `NOT_LINEAR_SESSION` as in A and RET-148.
- Anything else from Linear: its own code (`exc.code or "LINEAR_ERROR"`) plus `message`,
  with `extensions.userPresentableMessage` preferred when present.

## 4. Docs and skill

- `plugin.yaml` `provides_tools`, `TOOL_SCHEMAS`, `TOOL_HANDLERS` and `__all__` gain
  both tools in one commit with the skill bullets.
- `hermes_linear/skills/manage-superpowers/SKILL.md`: two tool bullets, the tool count
  becomes fourteen, the out-of-scope list names both tools, and the re-run note gains
  "`post_update` posts again". The file stays at or under its 150-line cap.
- The setup step 3 note "tools can't edit projects" becomes "add it with
  `linear_project` `link` (asks once)".
- README tool table and behaviour table rows for both tools.

## 5. Testing

- Unit tests through the real registry, against the fake client, for every action and
  every error in §3, including: a first pass that needs approval makes no write call (reads only); `ALREADY_EXISTS`
  / `ALREADY_LINKED` write nothing; document update never sends `trashed`, `hiddenAt`,
  `issueId` or `projectId`.
- `created_documents`: an update of a recorded document runs with no prompt in a later
  turn (fresh store object on the same file); one recorded for another ticket asks.
- One end-to-end approval test per tool: the real `_dispatch_pre_tool_call_hooks` on raw
  args (strings for `updates`, `"null"` for `target_date`), the handler on
  `coerce_tool_args` output, with the gateway seams named in the docstring as in
  `tests/test_approval_grant_feasibility.py`.
- The roster test, the manifest test, the `FIELD_TYPES` sync test and the
  fresh-interpreter import-order test cover both new modules.
- `hermes plugins validate .` shows no new private core imports.

### 5.1 Live test (before any PR)

In project **hermes linear test project**, after `deploy-live` and a gateway restart:

1. From a session on a fresh test ticket: `linear_document create` with no target, no
   prompt; the document shows on the ticket. The same call again: `ALREADY_EXISTS`, still
   one document.
2. `linear_document update` on it: no prompt; content changed.
3. `linear_document create` on another, unrelated ticket: `APPROVAL_REQUIRED`, then one
   Allow once / Deny card; Deny writes nothing.
4. `linear_project get` with no `project`: the test project, with its links, milestones
   and updates.
5. `post_update` (`health: at_risk`), `milestone` (create), `link`: one card each;
   approved writes appear in the project, and the project's health is At risk.
6. `link` with the same URL again: `ALREADY_LINKED`, no card, no second link.
7. Should NOT happen: any prompt in steps 1–2; any write after a Deny; any second link
   or document.

Afterwards, as the app, remove the brainstorm probe leftovers: the two "RET-122 probe doc"
documents on RET-161, the two `ret-122 probe` links and the "ret-122 probe" milestone in
the test project, plus the live test's own objects.

## 6. Verify during implementation

- What a `projectUpdateCreate` without `health` does to the project's health (probe once
  in the test project; record the answer here).
- The `Project` fields the `get` query reads (`status { name }`, `lead`, `targetDate`,
  `externalLinks`, `documents`, `projectMilestones`, `projectUpdates`): one live
  `linear_query` of the real `QUERY_*` constant before the task is marked done.
- Settled in the brainstorm: `document(id:)` accepts a slug id as well as a UUID (probed:
  `document(id: "dd7c3ea51183")` returns the probe document), so `get` / `update` pass
  the UUID or the URL's last `-` segment straight to it. `Project.status { name }`,
  `lead`, `targetDate`, `health` and `documents` read fine as the app.

## Out of scope

- `linear_mutate` (Q1), document sync for specs (Q2).
- Editing a project itself (name, description, status, dates, lead), and editing or
  removing project links.
- Archiving, trashing, hiding or deleting anything; moving documents.
- Reactions, subscribing, favorites, initiatives (need the `initiative:*` scopes).
