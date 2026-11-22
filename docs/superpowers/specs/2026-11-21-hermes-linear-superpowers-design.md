# hermes-linear × superpowers — managed projects

Status: draft for review · 2026-11-21
Linear: RET-73
Depends on: the hermes-linear channel plugin (`2026-11-21-hermes-linear-channel-design.md`)

This is its own sub-project of hermes-linear: same repo and plugin, but its own
spec → plan → implementation cycle and its own top-level ticket. Work items are
tracked in §11.

## 1. Purpose

Let a team run the superpowers workflow (brainstorm → spec → plan →
subagent-driven development) inside Linear. The work starts in a Linear ticket and
the conversation happens there. Linear also carries the artifacts: the spec on a
ticket, the plan with one sub-ticket per task, and live task status. Git stays the
source of truth for every document.

The first framework is superpowers. The seam stays framework-neutral, so BMAD can
become a second overlay on the same tools later (§9).

### Success looks like

You delegate one ticket that says "manage with hermes-linear" and everything else
happens in Linear:
- The brainstorm questions and approvals happen in that ticket.
- The spec appears as that ticket's description.
- The plan appears as a comment, with `T<n>` sub-tickets.
- Sub-tickets move to Done as subagent-driven development finishes each task.
- The spec, plan and ledger files carry the ticket IDs.
- The run ends with a PR.

## 2. Decisions

| # | Decision |
|---|---|
| D1 | The whole flow runs inside a delegated Linear ticket: one Hermes session for brainstorm, spec, plan and implementation. |
| D2 | Git is the source of truth. Linear holds rendered copies that link back to the committed file. Edits made in Linear are not synced back. |
| D3 | The plugin provides its own Linear tools. They run on the app token, act as the app user, and need no extra setup. |
| D4 | One ticket per (sub-)project. The spec goes in its description and the plan in one comment that is edited in place. Tasks are direct sub-tickets titled `T<n> · <task>`, in plan order. |
| D5 | IDs link both ways. Spec and plan headers carry `Linear: <ID>`; each `### Task N` heading carries `— <sub-ticket ID>`; the SDD ledger names both. |
| D6 | Approval gates are Linear select prompts (Approve / Request changes; execution method). |
| D7 | Activation: the phrase "manage with hermes-linear" loads the skill. The skill commits `.hermes-linear.yaml`, and later sessions on that repo load the skill from the file. |
| D8 | Git: one branch per sub-project, named after its ticket (`ret-72-listener-split`), worked in a worktree. One PR at the end; commit messages carry the sub-ticket ID. |
| D9 | Each sub-ticket moves In Progress → In Review → Done and gets a summary comment when Done. A task waiting on the user gets the **Blocked** label; the question is asked in the project ticket's thread. |
| D10 | Build approach: a skill decides *when*, and tested plugin tools do the Linear writes. Re-running a tool is safe. |
| D11 | Milestone 1 covers a single sub-project. Large projects, repo lookup from the project link, and BMAD are milestone 2 (§9). |

## 3. End-to-end flow (milestone 1)

1. **Kickoff.** The user creates a ticket in a Linear project, e.g. "Listener split".
   The ticket contains "manage with hermes-linear" and is delegated to the app. The
   plugin opens one Hermes session for the ticket.
2. **Setup.** The agent loads `linear:manage-superpowers`. If the repo has no
   `.hermes-linear.yaml`, the agent asks two select questions in the ticket: which
   framework (only `superpowers` for now), and which repo. It clones the repo if it
   is missing, creates the sub-project branch and worktree, and commits the marker
   file.
3. **Brainstorm.** `superpowers:brainstorming` runs unchanged. Each question is a
   turn in the ticket thread. Multiple-choice questions go through `clarify` with
   choices, which the plugin renders as a Linear `select` elicitation. Design-section
   approvals are selects: **Approve** / **Request changes**.
4. **Spec.** The spec is written in superpowers' usual location with `Linear: <ID>`
   in its header. The agent commits it, then calls `linear_sync_spec`. The spec
   review gate is a select. Each requested change loops through edit, commit, sync.
5. **Plan.** `superpowers:writing-plans` writes the plan with `Linear: <ID>` in its
   header. `linear_sync_plan` then creates or updates the plan comment and the `T<n>`
   sub-tickets, and writes the IDs into the task headings. The agent commits. Plan
   approval includes the choice of execution method (select).
6. **Implementation.** `superpowers:subagent-driven-development` runs unchanged,
   with these additions:
   - The ledger's line 2 is `Linear: <ID>`, and each task's ledger entry names its
     sub-ticket.
   - Dispatching a task calls `linear_task_state(…, in_progress)`.
   - Dispatching its review calls `in_review`.
   - A clean review calls `done`, with a summary covering commits, test result,
     verdict, number of fix rounds, and deferred minors as a checklist.
   - Anything that needs the user calls `blocked` with the question; the answer calls
     `unblocked`.
7. **Finish.** The agent opens the PR from the branch. Linear's GitHub integration,
   if installed, links it. The agent posts a final `response` and moves the project
   ticket to In Review. Merging stays with the user.

## 4. Plugin tools

All tools run on the plugin's app token and act as the app user. They return JSON
that includes the identifier and URL of each ticket they touch.

### 4.1 Generic

- `linear_issue_get(id)`: title, state, labels, project (including its resource
  links), parent, children (identifier, title, state), URL. `id` can be an
  identifier or a UUID.
- `linear_issue_create(title, description?, parent?, labels?, state?)`: team and
  project default to those of the session's ticket.
- `linear_issue_update(id, title?, description?, state?, add_labels?, remove_labels?)`:
  `state` can be a state name or a state type (`started`, `completed`, …), resolved
  against the team's workflow states.
- `linear_comment(id, body, edit_marker?)`: with `edit_marker`, the tool edits the
  app's existing comment that carries the hidden marker `<!-- hermes-linear:<marker> -->`.
  If no such comment exists, it creates one.

### 4.2 Sync

- `linear_sync_spec(path)`
  - Reads `Linear: <ID>` from the file header and renders the file into that
    ticket's description.
  - Appends a footer: `Source: <path> @ <short commit> · edit by asking in this thread`.
  - Overwrites the description on every call.
  - If the rendered text exceeds `SPEC_INLINE_MAX` (200,000 chars), the description
    becomes the spec's §1 plus a link to the file.
- `linear_sync_plan(path)`
  - Parses the `Linear:` header and the `### Task N: <name>` headings, including any
    trailing `— <ID>`.
  - Writes the rendered plan to a single comment on the project ticket, located by
    `edit_marker="plan"`.
  - For each task:
    - If the heading has an ID: update that ticket (title `T<n> · <name>`,
      description = link to the plan plus the task's first paragraph).
    - If the heading has no ID: first match an existing child titled `T<n> ·`, so a
      crash between create and write-back cannot cause duplicates. If nothing
      matches, create the child.
  - Cancels any child created by the app whose task is no longer in the plan, with a
    comment naming the change. Tickets already Done are never touched.
  - Sets `subIssueSortOrder` on every child, in plan order.
  - Writes the IDs back into the headings. It returns the task → ID map and
    `file_changed`; the agent commits the file.
- `linear_task_state(id, state, summary?, question?)`. `state` is one of
  `in_progress | in_review | done | blocked | unblocked`.
  - `in_review` uses the team's state named "In Review". If the team has none, it
    stays in a `started` state.
  - `done` posts `summary` as a comment.
  - `blocked` adds the team's `Blocked` label and posts `question` in the project
    ticket's thread. `unblocked` removes the label.

### 4.3 Guardrails

- Writes are limited to the session's own ticket, its descendants, and tickets these
  tools created in the session. Reads work across the workspace.
- In a session that did not start on the Linear platform there is no session ticket,
  so writes are refused.
- The tools reuse the plugin's `LinearClient`: token renewal, bounded 429 retries and
  sanitized errors. They never raise into the agent; failures come back as
  `{"ok": false, "error": "<code>"}`.

## 5. The `linear:manage-superpowers` skill

The plugin registers the skill with `register_skill`, so it exists only in profiles
where hermes-linear is enabled. It is an overlay: it tells the agent to load the
superpowers skill for each stage and lists only the differences in §3. It never
edits superpowers itself.

- **Description:** "Use when a Linear ticket says 'manage with hermes-linear', or
  when working in a repo that has `.hermes-linear.yaml`."
- **Platform hint:** the plugin's Linear platform hint gains one line: "If the repo
  has `.hermes-linear.yaml`, load `linear:manage-superpowers` first."
- **Rule:** Linear descriptions and plan comments are written only through the sync
  tools. To fix something in them, fix the file and sync again.

### 5.1 `.hermes-linear.yaml`

Committed at the repo root during first setup:

```yaml
framework: superpowers        # bmad in milestone 2
linear:
  team: RET
  project: Valkyrie
repo: https://github.com/masha256/hermes-linear   # M1 stand-in; M2 reads the project link
```

The file holds only what the documents themselves do not record. Ticket IDs live in
the spec, plan and ledger headers, so the marker never changes as work progresses.

## 6. Failure handling

- **Sync fails** (Linear down, 429, or auth). The tool returns an error. The work
  continues, because git holds everything. The agent marks the step `unsynced` in the
  ledger and retries at the next sync point and before any approval question. It
  never presents an unsynced spec or plan for approval; instead it asks "Linear sync
  failed (<code>); retry now or approve from the file link?"
- **Webhooks stop arriving.** The agent cannot detect this; the user sees no reply.
  Two mitigations: the listener split (§8 dogfood), and `hermes linear status`
  showing the time of the last real event. Nothing is corrupted. Work resumes on the
  next delivered prompt.
- **Manual edits in Linear.**
  - *Description:* overwritten on the next sync; the footer says so.
  - *Sub-ticket state:* respected until the agent's next transition for that task.
  - *Deleted or canceled sub-ticket whose task is still in the plan:* recreated on
    the next plan sync, and the agent asks whether to drop the task from the plan.
  - *Human comments:* never overwritten. On the project ticket, a human comment
    wakes the agent. On a sub-ticket, it is held as context.
- **Plan changes mid-implementation.** Edit the plan file, then run
  `linear_sync_plan`. New tasks get tickets, removed tasks are canceled with a
  reason, and the order is rewritten. Done tasks are untouched. Tickets are matched
  by the ID in the heading, so renumbering is safe.
- **Crash or gateway restart mid-step.** Every sync tool can safely be re-run. On
  resume, the agent re-reads the ledger and re-syncs. Ticket sessions survive
  restarts (plugin fix `a55c0e9`).
- **An answer given outside Linear** (e.g. in Slack) does not unblock a task.
  Answers count only in the ticket thread, which keeps approvals traceable.
- **The team has no `Blocked` label.** The app cannot create labels (probe P3), so
  `blocked` returns `LABEL_MISSING`. The agent still posts the question and tells the
  user once to create the label. `hermes linear setup` also checks for the label and
  prints how to add it.

## 7. Probe results (Linear API, app actor, 2026-11-21)

| # | Question | Result | Effect on the design |
|---|---|---|---|
| P1 | Maximum issue description length | 200,000 chars accepted; 400,000 rejected (`INVALID_INPUT`) | `SPEC_INLINE_MAX = 200,000`; above that, summary plus link |
| P2 | Ordering of children | Sub-issues order by `subIssueSortOrder`, not `sortOrder` | The plan sync sets `subIssueSortOrder` |
| P3 | Can the app create labels? | No (`FORBIDDEN`) | `Blocked` must already exist; setup checks for it (§6) |
| P4 | A team without "In Review" | Not testable here (one team, which has In Review) | The fallback to `started` stays; unit-tested |
| P5 | Can the app delegate a ticket to itself? | Yes: `issueUpdate(delegateId: <app user>)` succeeds | Unblocks milestone 2's per-sub-project sessions |

The probe tickets (RET-70…72) were canceled.

## 8. Testing and milestone 1 exit

### Unit tests (fake Linear, no network)

- `linear_sync_plan`:
  - running it twice leaves the second run with no changes;
  - a crash between create and write-back produces no duplicate;
  - renumbering keeps the ticket mapping;
  - a removed task's ticket is canceled with a reason;
  - `subIssueSortOrder` follows plan order;
  - Done tickets are untouched.
- Plan parsing, using real superpowers plans as fixtures, including this repo's
  15-task plan.
- `linear_sync_spec`: the footer, the size fallback, and overwriting the description.
- `linear_task_state`:
  - every state transition;
  - the `started` fallback;
  - `LABEL_MISSING`;
  - the question posted to the project ticket.
- Guardrails: writes outside the ticket tree are refused, and writes from sessions
  that are not Linear sessions are refused.
- The skill: `hermes plugins validate` and a frontmatter check. Its behaviour is
  tested by the dogfood run.

### Milestone 1 exit: dogfood the listener split end to end

1. A "Listener split" ticket in Valkyrie, containing "manage with hermes-linear", is
   delegated to the app. The setup questions are answered and
   `.hermes-linear.yaml` is committed.
2. The brainstorm happens in the ticket thread. Every multiple-choice question and
   approval is a select.
3. The spec appears in the ticket description and the plan as a comment. The `T<n>`
   sub-tickets are in plan order, and the IDs are in the plan headings.
4. SDD moves every sub-ticket In Progress → In Review → Done, and each gets a summary
   comment.
5. At least one task goes Blocked and is answered in the ticket. The
   gateway-restart stop point will provide this.
6. A PR is opened and the ticket is In Review.
7. Running `linear_sync_plan` again afterwards changes nothing.

Until the listener split ships, gateway restarts may pause Linear's webhook
deliveries. The dogfood accepts this and re-enables the webhook by hand.

## 9. Milestone 2 (outline, not designed)

### Scope

- **Large projects.** An umbrella ticket holds the decomposition and the build order.
  Each sub-project becomes its own top-level ticket in the same Linear project,
  linked to the umbrella. The agent delegates each one to itself when its turn comes
  (P5), so each sub-project gets its own session and its own brainstorm → spec →
  plan → SDD cycle.
- **Repo from the Linear project.** The repo comes from the project's GitHub resource
  link (`externalLinks`):
  - The link labelled `repo` wins when there are several.
  - An existing local clone is matched by its `origin`; otherwise the repo is cloned
    under the profile workspace.
  - If there is no link, the agent asks and offers to add one.
  - This replaces the `repo:` key in `.hermes-linear.yaml`.
- **BMAD** as a second overlay skill on the same tools. Check BMAD's current docs
  before mapping its artifacts.

### Open questions for the M2 brainstorm

- How the umbrella ticket shows progress across sub-projects.
- Whether large projects should use **Linear project milestones**, for example one
  milestone per sub-project, possibly instead of or alongside umbrella tickets.
- How BMAD's artifacts (PRD, architecture, epics and stories) map onto tickets and
  tools.

### Carry-forwards

Items deferred during the milestone 1 build are logged in its SDD ledger. When
milestone 1 closes, they are copied here and become the starting list for the M2
brainstorm.

## 10. Out of scope

- Two-way sync (editing in Linear and syncing back to git).
- Linear Documents.
- Merging PRs.
- Frameworks other than superpowers in milestone 1.

## 11. Tracking

Top-level ticket: **RET-73** (Linear project Valkyrie). Its sub-tickets, in build
order:

| Ticket | Work item | Spec |
|---|---|---|
| RET-74 | Spec review and approval (Done) | this document |
| RET-75 | Implementation plan | `docs/superpowers/plans/2026-11-21-hermes-linear-superpowers.md` |
| RET-88 | T0 · Integration probe | — |
| RET-80 | T1 · Tool runtime and guardrails | §4.3 |
| RET-76 | T2 · Client operations and generic tools | §4.1 |
| RET-77 | T3 · `linear_sync_spec` | §4.2 |
| RET-78 | T4 · `linear_sync_plan` | §4.2 |
| RET-79 | T5 · `linear_task_state` | §4.2, §6 |
| RET-81 | T6 · Skill, registration, platform hint (includes project setup) | §3, §5 |
| RET-83 | T7 · `setup`/`status` additions | §6 |
| RET-84 | T8 · Install and M1 dogfood | §8 |
| RET-82 | Canceled: absorbed by T6 | — |
| RET-85 | M2 · Large projects | §9 |
| RET-86 | M2 · Repo from the project link | §9 |
| RET-87 | M2 · BMAD overlay | §9 |

The plan's task tickets are the work-item tickets above, retitled `T<n> · <task>`.
Each task heading in the plan carries its ticket ID.
