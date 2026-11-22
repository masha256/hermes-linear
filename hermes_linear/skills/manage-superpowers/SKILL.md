---
name: manage-superpowers
description: Use when a repo has .hermes-linear.yaml or ticket says so.
---

# Manage with hermes-linear (superpowers through Linear)

Load this when a delegated Linear ticket says "manage with hermes-linear", or when the
repo you work in has `.hermes-linear.yaml` at its root. It runs the superpowers
workflow (brainstorm → spec → plan → subagent-driven development) inside the ticket.

This is an overlay. At each stage, load `superpowers:<stage>` and follow it, then apply
the differences below. Never restate or edit superpowers itself. Git is the source of
truth; Linear holds rendered copies that link back to the committed files.

## Calling the tools

The `linear_*` tools are deferred behind tool search. Load a schema with
`tool_describe` (exact names), then call it with `tool_call`, one entry per call:
`tool_call(calls=[{"name": "linear_issue_get", "arguments": {"id": "RET-12"}}])`
(a batch of several plugin tools is rejected). Calling `linear_issue_get(...)`
directly fails as an unknown tool. Below, `linear_x(...)` is shorthand — always call
via `tool_call`. The seven tools:

- `linear_issue_get` (`id`): state, labels, project and its links, parent, children.
- `linear_issue_create` (`title`, `description?`, `parent?`, `labels?`, `state?`): in
  the session ticket's team and project.
- `linear_issue_update` (`id`, `title?`, `description?`, `state?`, `add_labels?`,
  `remove_labels?`).
- `linear_comment` (`id`, `body`, `edit_marker?`).
- `linear_sync_spec` (`path`) and `linear_sync_plan` (`path`): `path` must be ABSOLUTE.
- `linear_task_state` (`id`, `state` = `in_progress | in_review | done | blocked |
  unblocked`, `summary?`, `question?`).

They act as the Linear app, write only to the session ticket, its descendants and
tickets created in this session, never raise, and fail as `{"ok": false, "error": …}`.
Every tool is safe to re-run.

## Questions and approvals

- Every multiple-choice question and approval (**Approve** / **Request changes**) is a
  `clarify` with `choices` (a Linear select), one per call, all needed to answer inside
  the question text (Linear hides the message before it). Open-ended: thread replies.
- A reply while the select is live answers it in the turn. Otherwise `clarify` returns
  `outcome: pending`: end the turn now, never assume an answer, don't restate the
  question (the plugin re-posts it after your final reply). The plugin adds and removes
  `Blocked` for a pending question itself: no `linear_task_state` `unblocked` for it.
  The answer comes later (restarts too) as the next turn: `Answer to "<question>": <value>`.
- `Command not run: approval is pending in Linear…`: end the turn. On a later
  ``Answer to approval for `<cmd>`: approved (<choice>)`` re-run exactly `<cmd>` byte for
  byte in that same turn (its pre-approval ends with it, no second prompt); on `: denied` don't run it.
- Answers count only when given in the ticket thread. An answer from Slack, the CLI or
  any other channel does not approve or unblock anything; ask again in the ticket.

## Setup (first session on a repo, or no `.hermes-linear.yaml` yet)

1. `linear_issue_get` the session ticket. Note its identifier, team and project. The
   team key is the identifier's prefix (`RET-81` → `RET`).
2. Framework: select via `clarify`; the only choice is `superpowers`.
3. Repo: read the project's links (`externalLinks`) from step 1. A GitHub link is a
   candidate; the link labelled `repo` wins when there are several. Offer the result
   as a select (one repo is still confirmed). With no GitHub link, ask for the repo in
   the ticket (select of known local clones plus "Other") and suggest the user add a
   project link labelled `repo` (the tools cannot edit projects).
4. Find a local clone whose `origin` matches (the workspace, then `~/git/*`). If none,
   clone it into `~/git/<name>`.
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

   It holds only what the documents do not record; it never changes as work goes on.

Later sessions on a repo that has the file skip setup but still make the sub-project
branch and worktree.

## Per-stage differences

**Brainstorm.** Load `superpowers:brainstorming`. Each question is a turn in the ticket
thread; multiple-choice questions and design-section approvals are selects. Do not
offer the visual companion; it cannot work in a Linear ticket.

**Spec.** Put a whole line `Linear: <session ticket ID>` in the spec's header (before
the first `## ` heading). Commit, then `linear_sync_spec` with the spec's absolute
path. The spec review gate is a select. Each requested change: edit → commit → sync.

**Plan.** Load `superpowers:writing-plans`. Header line `Linear: <ID>`; tasks keep the
heading form `### Task N: <name>`. Commit, then `linear_sync_plan` (absolute path). It
writes the plan comment and the `T<n> · <name>` sub-tickets, and writes the ticket IDs
back into the plan file itself (`### Task N: <name> — <ID>`). It manages only the
app-created `T<n> · <name>` children; humans' tickets and Done tickets are untouched.
Then, by result:
- `file_changed: true`: commit the plan (the IDs are now in the headings);
- `file_conflict: true`: the file changed during the sync; re-read and sync again;
- `file_error`: the IDs were not written; fix the cause and sync again.

Plan approval is one select that includes the execution method (subagent-driven or
inline, as writing-plans offers). A plan change at any later point: edit the file,
commit, `linear_sync_plan`, commit again.

**Implementation.** Load `superpowers:subagent-driven-development`. The ledger's
line 1 stays its identity line; line 2 is `Linear: <ID>`. Each task's ledger entry
names its sub-ticket, and commit messages carry the sub-ticket ID. Then:
- dispatching a task's implementer: `linear_task_state(id, "in_progress")`;
- dispatching its review: `in_review`;
- a fix round after review: back to `in_progress` (then `in_review` again);
- the clean review: `done` with `summary` (commits, test result, review verdict, number
  of fix rounds, deferred minors as a `- [ ]` checklist);
- anything that needs the user: `blocked` with `question` (it posts the question; do
  not repeat it through `clarify`). Then end the turn, or continue only with tasks
  that do not depend on the answer. When it arrives in the thread: `unblocked`.

If `blocked` returns `LABEL_MISSING` (question posted), ask the user once to add a team `Blocked` label.

**Finish.** Open the PR from the branch (load `superpowers:finishing-a-development-branch`
for the mechanics; never merge). Move the project ticket to In Review with
`linear_issue_update(id=<session ticket>, state="In Review")`, then end with the final reply (PR link and a
short summary); it is posted as the response.

## Large projects

When brainstorming decomposes the work into sub-projects, the current ticket becomes
the umbrella: it holds the decomposition and build order. Create one top-level ticket
per sub-project with `linear_issue_create` (no `parent`; same project; link the
umbrella in its description). Each sub-project runs its own cycle in its own session.
The tools cannot set a delegate: when a sub-project's turn comes, ask the user in the
umbrella ticket to delegate its ticket to the app. Never delegate through other Linear
tools or MCP servers; they act as the user, not the app.

## Rules

- Ticket descriptions and the plan comment are written only by `linear_sync_spec` and
  `linear_sync_plan`. To change them, fix the file, commit and sync again.
- On a sync failure, keep working (git holds everything). Mark the step
  `unsynced (<code>)`: before the ledger exists (spec and plan stages) in your reply
  in the ticket thread, afterwards in the ledger. Retry at the next sync point and
  before any approval. Never ask for approval of an unsynced spec or plan; instead ask (select)
  "Linear sync failed (<code>); retry now or approve from the file link?".
- Edits made in Linear are not synced back. A deleted or canceled sub-ticket whose
  task is still in the plan is recreated on the next plan sync; ask whether to drop
  the task.
- On resume (crash, restart, new prompt): re-read the ledger and `git log`, re-sync the
  spec and plan, then continue.
