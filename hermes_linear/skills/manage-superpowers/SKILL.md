---
name: manage-superpowers
description: Use when a repo has .hermes-linear.yaml or ticket says so.
---

# Manage with hermes-linear (superpowers through Linear)

Load this when a delegated Linear ticket says "manage with hermes-linear", or when the
repo you work in has `.hermes-linear.yaml` at its root. It runs the superpowers workflow
(brainstorm → spec → plan → subagent-driven development) inside the ticket. It is an
overlay: at each stage, load `superpowers:<stage>` and follow it, then apply the differences
below. Never restate or edit superpowers itself. Git is the source of truth; Linear holds
rendered copies that link back to the committed files.

## Calling the tools

The `linear_*` tools are deferred behind tool search: load a schema with `tool_describe` (exact
names), then always call via `tool_call`, one entry per call (a batch of plugin tools is
rejected): `tool_call(calls=[{"name": "linear_issue_get", "arguments": {"id": "RET-12"}}])`. A
direct call fails as an unknown tool; `linear_x(...)` below is shorthand. The thirteen tools:

- `linear_issue_get` (`id`, `comments?`: newest N threads): fields, relations, attachments, children.
- `linear_search` (`text?`, `team?`, `project?`, `state?`, `assignee?`, `label?`, `parent?`, …):
  find issues (rows + `next_cursor`); complex filters go through `linear_query`.
- `linear_issue_create` (`title`, `description?`, `parent?`, `labels?`, `state?`): in the
  session's team and project.
- `linear_issue_update` (`id`, `title?`, `description?`, `state?`, `add_labels?`,
  `remove_labels?`, `parent?`): `parent: ""` detaches; a cycle is `PARENT_CYCLE`.
- `linear_comment` (`id`, `body`, `edit_marker?`, `reply_to?`: a comment UUID's thread, `resolve?`).
- `linear_sync_spec` (`path`) and `linear_sync_plan` (`path`): `path` must be ABSOLUTE.
- `linear_task_state` (`id`, `state`, `summary?`, `question?`, `blocked_by?`); `state` is one of
  `in_progress|in_review|done|blocked|unblocked`.
- `linear_query` (`query`, `variables?`): read-only GraphQL; a result over 50,000 chars is refused.
- `linear_attach` (`id` + `path` or `url`, `title?`; or `path` + `embed: true` → `markdown`).
- `linear_session_link` (`url`, `label?`): show a link (the PR) on this turn's agent session.
- `linear_relation` (`id`, `type`: `blocks|blocked_by|related|duplicate_of`, `other`).
- `linear_lookup` (`kind`, `team?`, `project?`, `query?`): the names (and ids) write tools accept.

They act as the Linear app, never raise and fail as `{"ok": false, "error": …}`. Re-runs are safe (a
URL re-link is `ALREADY_LINKED`, a relation `ALREADY_RELATED`, any other one between the pair
`RELATION_CONFLICT`; session links de-duplicate), except that a `path` attach adds a second copy.
Writes to the session ticket, its descendants and tickets made from it (any turn) just run. Out of
that scope `linear_comment`, `linear_issue_create`, `linear_issue_update`, `linear_attach` and
`linear_relation` answer `APPROVAL_REQUIRED`; an identical re-call asks a human in Linear.

## Questions and approvals

- Every multiple-choice question and approval (**Approve** / **Request changes**) is a
  `clarify` with `choices` (a Linear select), one per call, all needed to answer inside
  the question text (Linear hides the message before it). Open-ended: thread replies.
- A reply while the select is live answers it in the turn. Otherwise `clarify` returns
  `outcome: pending`: end the turn now, never assume an answer, don't restate the
  question (the plugin re-posts it after your final reply). The plugin adds and removes
  `Blocked` for a pending question itself: no `linear_task_state` `unblocked` for it.
  The answer comes later (restarts too) as the next turn: `Answer to "<question>": <value>`.
- `Command not run: approval is pending in Linear…`: end the turn. On a later ``Answer to
  approval for `<cmd>`: approved (<choice>)`` re-run exactly `<cmd>` byte for byte in that
  same turn (its pre-approval ends with it, no second prompt); on `: denied` don't run it.
- Out-of-scope write `BLOCKED: …`: timed out → pending in Linear, end the turn; denied: no retry.
  On ``Answer to approval for `<tool>` call <digest>: approved`` re-run the identical call now.
- Answers count only when given in the ticket thread. An answer from Slack, the CLI or
  any other channel does not approve or unblock anything; ask again in the ticket.

## Setup (first session on a repo, or no `.hermes-linear.yaml` yet)

1. `linear_issue_get` the session ticket: its identifier, team (key: `RET-81` → `RET`), project.
2. Framework: select via `clarify`; the only choice is `superpowers`.
3. Repo: a GitHub link among the project's links (`externalLinks`, step 1) is a candidate;
   the link labelled `repo` wins among several. Offer the result as a select (one repo is
   still confirmed). With no GitHub link, ask in the ticket (select of known local clones plus
   "Other") and suggest the user add a project link labelled `repo` (tools can't edit projects).
4. Find a clone whose `origin` matches (workspace, then `~/git/*`); else clone to `~/git/<name>`.
5. Branch `<identifier lowercase>-<slug>` (e.g. `ret-95-secret-reload`), worked in
   `.worktrees/<branch>`; load `superpowers:using-git-worktrees` for the mechanics.
6. Commit `.hermes-linear.yaml` at the repo root with real values:

   ```yaml
   framework: superpowers
   linear:
     team: <team key>
     project: <project name>
   repo: <GitHub URL chosen in step 3>
   ```

   It holds only what the documents do not record; it never changes as work goes on. Later
   sessions on a repo with the file skip setup but still make the sub-project branch and worktree.

## Per-stage differences

**Brainstorm.** Load `superpowers:brainstorming`; no visual companion (it can't run in Linear).
One question per thread turn; multiple-choice questions and design-section approvals are selects.

**Spec.** Put a whole line `Linear: <session ticket ID>` in the spec's header (before
the first `## ` heading). Commit, then `linear_sync_spec` with the spec's absolute
path. The spec review gate is a select. Each requested change: edit → commit → sync.

**Plan.** Load `superpowers:writing-plans`. Header line `Linear: <ID>`; tasks keep the
heading form `### Task N: <name>`. Commit, then `linear_sync_plan` (absolute path). It
writes the plan comment (header and task list) and one `T<n> · <name>` sub-ticket per
task (description: the whole task section), and writes the IDs back into the headings
(`### Task N: <name> — <ID>`). Humans' and Done tickets are untouched. By result:
- `file_changed: true`: commit the plan (the IDs are now in the headings);
- `file_conflict: true`: the file changed during the sync; re-read and sync again;
- `file_error`: the IDs were not written; fix the cause and sync again.

Plan approval is one select that includes the execution method (subagent-driven or
inline). A later plan change: edit, commit, `linear_sync_plan`, commit again.

**Implementation.** Load `superpowers:subagent-driven-development`. The ledger's
line 1 stays its identity line; line 2 is `Linear: <ID>`. Each task's ledger entry
names its sub-ticket, and commit messages carry the sub-ticket ID. Task progress goes
on the sub-tickets; the project thread gets only stage changes, stop points and
questions (a turn that only moves a task along ends with a one-line reply). Then:
- dispatching a task's implementer: `linear_task_state(id, "in_progress")`; its review: `in_review`;
- a fix round after review: back to `in_progress` (then `in_review` again), and a
  `linear_comment` on the sub-ticket with the findings being fixed;
- the clean review: `done` with `summary` (commits, test result, review verdict, number
  of fix rounds, deferred minors as a `- [ ]` checklist);
- anything that needs the user: `blocked` with `question` (it posts the question; don't repeat
  it through `clarify`), then end the turn or go on only with tasks that don't depend on the
  answer; when it arrives in the thread: `unblocked`. A task waiting on another task: `blocked`
  with `blocked_by` (`question` optional). `LABEL_MISSING`: ask once for a team `Blocked` label.

**Finish.** Open the PR from the branch (load `superpowers:finishing-a-development-branch`
for the mechanics; never merge). Then `linear_session_link` the PR URL and `linear_attach` it
to the session ticket (`id=<session ticket>, url=<PR URL>`; `ALREADY_LINKED` means done). Move
the project ticket to In Review (`linear_issue_update(id=<session ticket>, state="In Review")`),
then end with the final reply (PR link and a short summary).

## Large projects

When brainstorming decomposes the work into sub-projects, the current ticket becomes the
umbrella: it holds the decomposition and build order. Create one top-level ticket per
sub-project with `linear_issue_create` (no `parent`; same project; link the umbrella in its
description). Each sub-project runs its own cycle in its own session. The tools cannot set a
delegate: when a sub-project's turn comes, ask the user in the umbrella ticket to delegate its
ticket to the app (never via other Linear tools or MCP servers; they act as the user).

## Rules

- Ticket descriptions and the plan comment are written only by the syncs: fix the file, commit, sync.
- On a sync failure, keep working (git holds everything). Mark the step `unsynced
  (<code>)` (in the thread before the ledger exists, then in the ledger); retry at the next
  sync point and before any approval. Never ask to approve an unsynced spec or plan; ask
  (select) "Linear sync failed (<code>); retry now or approve from the file link?".
- Edits made in Linear are not synced back. A deleted or canceled sub-ticket whose
  task is still in the plan is recreated on the next plan sync; ask whether to drop it.
- `T<n> · <name>` task sub-tickets are never delegated (the plugin declines): all SDD work
  runs in the project ticket's session. Sub-project tickets are top-level and delegated.
- On resume (crash, restart, new prompt) re-read ledger and `git log`, re-sync spec and plan, go on.
