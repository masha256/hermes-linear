# Read and attach: `linear_issue_get` fields and comments, `linear_search`, `linear_attach`, `linear_session_link` — design

Linear: RET-120

Status: approved in the RET-120 thread (Q1–Q4 and design sections 1–3), 2026-11-24.
Sub-project B of the RET-118 umbrella. It builds on A's approval path (RET-119,
`docs/superpowers/specs/2026-11-23-out-of-scope-approval-design.md`). C and D come next.

## Goal

The agent reads everything it needs about a ticket and attaches artifacts without going
around the tools. The upload done by hand for RET-115 (`fileUpload` → PUT →
`attachmentCreate` through a script) should never be needed again.

- `linear_issue_get` adds assignee, delegate, priority, estimate, due date, cycle,
  milestone, relations and attachments, plus `comments: N` with thread structure.
- `linear_search` runs a text search or returns a filtered issue list.
- `linear_attach` uploads a local file and attaches it, links a URL (a PR or a doc), or,
  with `embed`, only uploads and returns the asset URL for inline images.
- `linear_session_link` adds a URL (usually the PR) to the current agent session's
  `externalUrls`, which is how Linear expects an agent to report a PR.

Constraints: public Hermes plugin API only, no core changes; tools never raise; every
write is attributed to the app.

## Decisions

| # | Question | Decision |
|---|----------|----------|
| Q1 | Which local files `linear_attach` may upload | Reuse Hermes's media-delivery policy as it is: `gateway.platforms.base.validate_media_delivery_path` (a public module), honouring the operator's strict setting. The plugin adds: the path must be absolute, a size cap, and the refusal `PATH_DENIED`. There is no approval path for a refused path. |
| Q2 | `linear_search` filter shape | Typed filters only. Anything more complex goes through `linear_query` (A). |
| Q3 | `comments: N` shape and size bound | The last N threads, newest first, each with all its replies (oldest first, at most 50). Over 50,000 serialized chars, the oldest threads are dropped and `comments_truncated: true` is set. |
| Q4 | Where the code lives | Two new modules, `hermes_linear/tools_read.py` and `hermes_linear/tools_attach.py`, built on the existing `_tool` / `_gated` machinery. `agent_tools.py` only registers them. |

Defaults stated in Q1 that you didn't object to:

- `comments` defaults to 0, so today's calls cost the same as before.
- A GitHub PR URL is linked with `attachmentLinkGitHubPR` (already verified as the app);
  any other URL with `attachmentLinkURL`.
- An out-of-scope `linear_attach` target goes through A's `APPROVAL_REQUIRED` path.
  `embed` targets no issue, so it has no gate.
- `linear_session_link` writes only to the current turn's own agent session, so it has
  no scope check.

Why Q1 lands there (facts from the brainstorm):

- Each platform plugin (Slack, Telegram, Discord, …) uploads a file named by a reply's
  `MEDIA:/path` only after the gateway has checked it with `validate_media_delivery_path`.
  That is the same exfiltration risk as `linear_attach`, so Linear is no looser than Slack,
  and the operator controls both with one setting.
- Default (non-strict) mode accepts any existing regular file, with symlinks resolved, outside a
  denylist. The denylist covers `/etc`, `/proc`, `/sys`, `/dev`, `/root`, `/var/log` and
  `/var/lib`; `$HOME/{.ssh,.aws,.gnupg,.kube,.docker,.config,.azure,.gcloud}`; in every
  Hermes home `.env*`, `auth.json`, `config.yaml`, `state.db` (and its WAL files),
  `sessions/`, `browser-profile/` and the credential stores; and `.env` files anywhere.
  Strict mode (`gateway.strict` / `HERMES_MEDIA_DELIVERY_STRICT=1`) allows only the Hermes
  cache dirs, `HERMES_MEDIA_ALLOW_DIRS` and freshly made files.
- Known gap, the same as Slack's: in default mode a credential file outside the
  denylisted dirs (`~/.netrc`, a key outside `~/.ssh`) passes. Strict mode closes it.
- The plugin's existing rule (Ruling 35) still stands: `MEDIA:` files in a final reply
  are named by basename and never uploaded. `linear_attach` is the only upload path, and
  the agent has to call it explicitly.

## 1. Tool surfaces

### 1.1 `linear_issue_get(id, comments?)`

All existing keys stay unchanged. New keys:

| Key | Shape |
|-----|-------|
| `assignee`, `delegate` | `{id, name}` or `null` |
| `priority`, `priority_label` | `0`–`4` and Linear's label (`No priority`, `Urgent`, …) |
| `estimate` | number or `null` |
| `due_date` | `YYYY-MM-DD` or `null` |
| `cycle` | `{id, number, name}` or `null` |
| `milestone` | `{id, name}` or `null` (`projectMilestone`) |
| `relations` | `[{type, direction, issue: {identifier, title, state}}]`: `direction` is `outbound` for `relations` and `inbound` for `inverseRelations`, up to 50 each |
| `attachments` | `[{id, title, subtitle, url, source_type}]`, up to 50 |
| `comments` | only when `comments` ≥ 1 (§2.2) |
| `comments_truncated` | only when `comments` ≥ 1: `true` if threads were dropped to fit |

`comments` is an integer from 0 to 50 and defaults to 0. Anything else is `BAD_ARGS`. A
thread is `{id, url, author, author_is_app, created_at, body, resolved, replies: [...]}`.
A reply has the same keys minus `url` and `replies`. Threads are listed newest first and
replies oldest first.

Reads stay unscoped and work from any session, as today.

### 1.2 `linear_search(text?, team?, project?, state?, assignee?, delegate?, label?, parent?, include_comments?, limit?, cursor?)`

Returns `{ok: true, issues: [row], next_cursor}`. `next_cursor` is `null` on the last page.

- With `text` it calls `searchIssues(term: text, filter, includeComments)`. Without `text`
  it calls `issues(filter, orderBy: updatedAt)`. With no arguments at all, it returns the
  most recently updated issues.
- `team`: a team key (`RET`). `project`: a UUID or a name (case-insensitive).
- `state`: a workflow type when it is one of `triage`, `backlog`, `unstarted`, `started`,
  `completed` or `canceled`; otherwise a state name (case-insensitive).
- `assignee`, `delegate`: `me` (the app), an email (contains `@`), or a name (matched
  against `name` or `displayName`, case-insensitive).
- `label`: one label name (case-insensitive). `parent`: an issue identifier or UUID.
- `include_comments` (boolean) needs `text`; without `text` it is `BAD_ARGS`.
- `limit`: 1–50, default 25. `cursor`: a previous `next_cursor`.
- A row is `{identifier, title, state, assignee, delegate, priority, project, url,
  updated_at}`, where `state`, `assignee`, `delegate` and `project` are names or `null`.

Typed filters are ANDed together. Anything more complex (OR, dates, nested filters) goes
through `linear_query`.

### 1.3 `linear_attach(id?, path?, url?, title?, subtitle?, embed?)`

Exactly one of `path` and `url` is required.

| Arguments | Action | Result |
|-----------|--------|--------|
| `path` + `id` | path policy (§2.4), `fileUpload` → PUT → `attachmentCreate` (title defaults to the basename) | `{ok, attachment_id, asset_url, issue, url}` |
| `url` + `id` | a GitHub PR URL: `attachmentLinkGitHubPR`; any other http(s) URL: `attachmentLinkURL` | `{ok, attachment_id, issue, url}` |
| `path` + `embed: true` | path policy, then the upload only | `{ok, asset_url, markdown}` |

- `markdown` is `![<name>](<asset_url>)` for an `image/*` content type and
  `[<name>](<asset_url>)` otherwise.
- `embed` combined with `id` or `url`, a call without `id` that is not an embed, or a
  non-http(s) `url` is `BAD_ARGS`.
- An `id` outside the session's write scope is gated by A's path: the first call returns
  `APPROVAL_REQUIRED`, and the identical re-call asks a human in Linear. The approval
  digest binds the arguments (the path), not the file's bytes, so an approved re-run reads
  the file again.
- `embed` needs a Linear session (`NOT_LINEAR_SESSION` otherwise), so every upload belongs
  to a ticket thread. It has no gate: the path policy is its only guard.

Errors (in addition to A's `NOT_LINEAR_SESSION`, `OUT_OF_SCOPE`, `APPROVAL_REQUIRED`,
`BAD_ARGS`, `LINEAR_ERROR` and `WRONG_THREAD`):

| Code | When |
|------|------|
| `FILE_NOT_FOUND` | the file is missing or not a regular file (checked before the policy) |
| `PATH_DENIED` | the path is not absolute, or `validate_media_delivery_path` returned `None` |
| `FILE_TOO_LARGE` | the file exceeds `ATTACH_MAX_BYTES` (50 MB; Linear accepts more, §4.2) |
| `UPLOAD_FAILED` | the PUT answered with a non-2xx status (`status` included) |
| `ALREADY_LINKED` | URL mode: Linear refused a re-link of a URL the issue already has (`INPUT_ERROR` whose message contains "already been linked" or "already exists", case-insensitive; `message` is Linear's). Any other `INPUT_ERROR` stays `LINEAR_ERROR` (Ruling R4) |

If the attachment step fails after a successful upload, the error also carries
`asset_url`, so the agent can embed the file instead or retry. The orphaned asset stays,
because the API has no way to delete an upload.

### 1.4 `linear_session_link(url, label?)`

Adds `{label, url}` to the current turn's agent session through
`agentSessionUpdate.addedExternalUrls`, which appends and keeps the existing links. The
label defaults to `Pull request` for a GitHub PR URL and to the URL's host otherwise.
Only http(s) URLs are accepted (`BAD_ARGS` otherwise). With no Linear turn in flight
(Slack, cron, a subagent) it returns `NOT_LINEAR_SESSION`. It targets no issue, so it
has no scope check. Result: `{ok: true, agent_session_id, label, url}`.

### 1.5 Common

Every tool returns `{ok: false, error, …}` instead of raising, and every Linear call
uses the app token. A GitHub PR URL is one matching
`^https://github\.com/[^/]+/[^/]+/pull/\d+(?:[/?#].*)?$`.

## 2. Internals and data flow

### 2.1 Modules and registration

- `hermes_linear/tools_read.py`: the `issue_get` additions, the thread assembly and
  trimming, the `IssueFilter` builder and `linear_search`.
- `hermes_linear/tools_attach.py`: the path policy, the upload, URL links, `embed` and
  `linear_session_link`.
- Both import the machinery from `agent_tools` (`_tool`, `_writable`, `_check_gate`,
  `_Refusal`, `_err`, `_req_str`/`_opt_str`). Each exports `SCHEMAS` and `HANDLERS`. At its
  end, `agent_tools.py` imports both modules and merges them into `TOOL_SCHEMAS`,
  `TOOL_HANDLERS` and `__all__`. The bottom import avoids an import cycle, and the
  roster stays in one place for `plugin.py` and the tests.
- `linear_issue_get` keeps its name and handler slot. Its body moves to `tools_read`.
- New GraphQL and client methods live in `linear_client.py`. Each has a fake in
  `tests/fake_linear.py`. No existing query changes:
  `issue_details`, `issue_threads`, `search_issues`, `list_issues`, `file_upload` (the
  mutation plus the PUT), `attachment_create`, `attachment_link_url` and
  `attachment_link_github_pr`.

### 2.2 `linear_issue_get`

1. `client.issue_get(id)`, unchanged. Every scope check and sync calls it, so it doesn't
   grow.
2. `client.issue_details(uuid)`: assignee, delegate, priority, priorityLabel, estimate,
   dueDate, cycle, projectMilestone, `relations(first: 50)`, `inverseRelations(first: 50)`
   and `attachments(first: 50)`. Merged into the result under the keys in §1.1.
3. With `comments: N`: `client.issue_threads(uuid, N)` runs
   `comments(first: N, filter: {parent: {null: true}})` with `children(first: 50)`, user,
   createdAt, body and resolvedAt. It is a separate query so the N × 50 nested nodes don't
   add to step 2's complexity. Threads are sorted newest first and replies oldest first on
   the client, without relying on Linear's default order. `author_is_app` compares the
   user id with `client.viewer_id()`.
4. Trim: serialize the result (`json.dumps`, `ensure_ascii=False`). While it is over
   `ISSUE_GET_MAX_CHARS` (50,000) and threads remain, drop the oldest thread and set
   `comments_truncated: true`. A result still too large with no threads left (a huge
   description) is returned as it is, as today.

### 2.3 `linear_search`

A pure function `build_issue_filter(args, parent_id)` returns the `IssueFilter`:

| Argument | Filter |
|----------|--------|
| `team` | `team: {key: {eq: KEY}}` (upper-cased) |
| `project` | `project: {id: {eq}}` for a UUID, else `project: {name: {eqIgnoreCase}}` |
| `state` | `state: {type: {eq}}` for a type, else `state: {name: {eqIgnoreCase}}` |
| `assignee` / `delegate` | `me` → `{isMe: {eq: true}}`; with `@` → `{email: {eqIgnoreCase}}`; else `{or: [{name: {eqIgnoreCase}}, {displayName: {eqIgnoreCase}}]}` |
| `label` | `labels: {some: {name: {eqIgnoreCase}}}` |
| `parent` | `parent: {id: {eq: parent_id}}` |

Only one step does I/O: `parent` is resolved to a UUID with `client.issue_get`. An
unknown parent is Linear's `NOT_FOUND`, which comes back as `LINEAR_ERROR` with the code.
The body then calls `search_issues` or `list_issues` with `first: limit, after: cursor`.
`next_cursor` is `pageInfo.endCursor` when `hasNextPage` is true, else `null`.

### 2.4 `linear_attach`

Parse runs in the tool thread (`blocking_parse=True`). On an event-loop thread the call
is refused with `WRONG_THREAD` before anything else runs.

1. Validate the argument combination (§1.3).
2. For `path`: not absolute → `PATH_DENIED`. Then `os.path.isfile` (follows symlinks) fails
   → `FILE_NOT_FOUND`. Then `validate_media_delivery_path(path, session_key=ctx session
   key)` returns `None` → `PATH_DENIED`. Its returned resolved path is the one that is
   read, so a symlink is judged and read by its target. `stat` over `ATTACH_MAX_BYTES` →
   `FILE_TOO_LARGE`. Read the bytes. `content_type` comes from
   `mimetypes.guess_type(name)` and defaults to `application/octet-stream`. `name` is the
   basename of the path as given.

The body runs on the adapter loop:

1. Not a Linear session → `NOT_LINEAR_SESSION`.
2. With `id`: `_writable(ctx, id)`, then `_check_gate(ctx)`, **before** any write. A
   first pass that needs approval uploads nothing.
3. For `path`: `client.file_upload(name, content_type, data)` (the size is `len(data)`):
   - `fileUpload(size, filename, contentType)` → `uploadFile {uploadUrl, assetUrl, headers}`;
   - an HTTP PUT of the bytes to `uploadUrl` with `Content-Type`,
     `Cache-Control: public, max-age=31536000` and every returned header, on the client's
     aiohttp session and **without** the bearer token (the URL is a signed storage URL);
   - a non-2xx answer → `UPLOAD_FAILED` with `status`; the result is `asset_url`.
4. Attach: `attachment_create(issue_uuid, title, subtitle, asset_url)`, or for `url`
   `attachment_link_github_pr(issue_uuid, url)` / `attachment_link_url(issue_uuid, url,
   title)`. A failure after the upload returns `LINEAR_ERROR` plus `asset_url`.

The tool is registered with `gated="linear_attach"` and `timeout_s=PLAN_SYNC_TIMEOUT_S`
(120 s), to allow for a 50 MB PUT. `_tool` currently rejects `finish` together with
`gated`, not `blocking_parse` together with `gated`. Both are needed here and are
compatible: parse runs once per handler call, before `_gated`'s passes.

### 2.5 `linear_session_link`

The body runs on the loop:
`ctx.adapter.turns.turn_for_session_key(ctx.session_key)` gives `(key, turn)`, or `None` →
`NOT_LINEAR_SESSION`. Then
`client.update_session(turn.decision.agent_session_id, added_external_urls=[{label, url}])`.
This is the same client method the renderer uses for the Hermes session link. It is
called directly (not through the activity write queue) because an external URL is not
an activity, so its order doesn't matter, and the tool can report success.

## 3. Testing, docs and skill

### 3.1 Tests (TDD)

- **`tools_read`:**
  - Fields: the mapping, with a null assignee, cycle and milestone, and relations in both
    directions.
  - Comments: the thread order (threads newest first, replies oldest first); the trim
    (oldest threads dropped, `comments_truncated`, a huge description left as it is); the
    bounds (0 runs no thread query; 51 and negative values are `BAD_ARGS`).
  - Search: a table test of `build_issue_filter` covering every argument, `me`, an email
    vs a name, a UUID vs a name project, a state type vs a name, and `include_comments`
    without `text`; plus `next_cursor` pagination.
- **`tools_attach`:**
  - Path policy, against the **real** `validate_media_delivery_path` (no stub): tmp
    files, a symlink into a denied dir, a relative path, a `.env`.
  - Arguments and routing: the size cap, `FILE_NOT_FOUND`, the `path`/`url`/`embed`
    combinations, a GitHub PR URL vs a plain URL.
  - Upload: the PUT headers (no bearer token), `UPLOAD_FAILED` on a non-2xx answer, and
    `asset_url` in the error after a failed attach.
  - Gate: an out-of-scope first pass makes no `file_upload` call.
- **`linear_session_link`:** an in-flight turn's `agent_session_id` reaches
  `update_session`; no turn gives `NOT_LINEAR_SESSION`; the label defaults; non-http
  URLs are refused.
- **Through the real registry:** handlers return `str`. One end-to-end out-of-scope
  `linear_attach` goes through A's approval path, modelled on
  `tests/test_tool_approval_e2e.py`.
- The roster, manifest and `hermes plugins validate` tests cover the three new tools.

### 3.2 Docs and skill

- README: the tool table and behaviour rows for the new tools and the new
  `linear_issue_get` keys, and the attach path policy with its strict-mode setting.
- The plugin skill `hermes_linear/skills/manage-superpowers/SKILL.md` stays within its
  150-line test cap; added lines are paid for by tightening wording elsewhere. It goes
  from eight to eleven tools, with one bullet each for `linear_search`, `linear_attach`
  and `linear_session_link`, and `linear_issue_get` mentions `comments`. **Finish**
  gains a step: after opening the PR, `linear_session_link` it and `linear_attach` its
  URL to the session ticket.
- The neo skill `hermes-linear-dev` (`references/linear-api-surface.md`) points its
  attachment recipe at `linear_attach`. This edit is made only after `deploy-live` and the
  gateway restart.

## 4. Verify during implementation

Live, as the app, before the plan's tasks rely on these:

1. ✅ (2026-11-24, brainstorm) `issue(RET-118) { comments(first: 50, filter: {parent: {null:
   true}}) { … children(first: 50) { … } } }` runs without a complexity error. Re-check
   with the final field list. If it fails, page the replies in a second query.
2. ✅ (2026-11-24) Linear's real upload size limit: adjust `ATTACH_MAX_BYTES` to match.
   `fileUpload(size:, filename: "probe.bin", contentType: "application/octet-stream")`
   returned `success: true` with an `uploadFile` for 60 MB, 100 MB, 200 MB, 500 MB and
   1 GB (no bytes were PUT). The only refusal is GraphQL's 32-bit `Int` (5 GB:
   `GRAPHQL_VALIDATION_FAILED`). The returned headers are `x-goog-content-length-range:
   "<size>,<size>"` (the PUT must carry exactly the declared size) and
   `Content-Disposition: attachment; filename="<name>"`. Nothing is refused up to 500 MB,
   so `ATTACH_MAX_BYTES` stays 50 MB, bounded by the 120 s budget rather than by Linear.
3. ✅ (2026-11-24, on RET-92) Whether `addedExternalUrls` de-duplicates a repeated URL.
   It does. On the app's completed agent session on RET-92, two identical
   `agentSessionUpdate(input: {addedExternalUrls: [{label: "Probe", url: …}]})` calls
   both returned `success: true` (a completed session accepts the update), and
   `agentSession { externalUrls }` then held one entry. `externalUrls` reads back as a
   JSON-encoded string. So the tool passes every call through, and its description says
   "Re-adding the same URL is a no-op" (pinned by `test_session_link_schema`).
4. ✅ (2026-11-24, on RET-92) `attachmentLinkURL` and `attachmentLinkGitHubPR` as the
   app: the return shape, and whether re-linking the same URL updates the attachment or
   adds a duplicate. Both return `{success, lastSyncId, attachment {…}}` and accept an
   identifier (`"RET-92"`) as `issueId`. The default titles are `owner/repo` for a GitHub
   file URL and `owner/repo#11` for the PR; `subtitle` is `""` and `sourceType` is `api`.
   A re-link of the same URL on the same issue neither updates nor duplicates: it fails
   with `INPUT_ERROR` ("Unable to create issue attachment" / "This URL has already been
   linked with RET-92." for `attachmentLinkURL`, also when a new `title` is passed;
   "Duplicate attachment for duplicate url" / "An attachment with the same URL already
   exists." for `attachmentLinkGitHubPR`). So a repeated `linear_attach` of a URL answers
   `ALREADY_LINKED` with Linear's message (§1.3; Ruling R4), and the issue keeps one
   attachment.
5. `validate_media_delivery_path(path, session_key=…)` called from a plugin tool thread
   gives the same verdicts as in the gateway. Already probed from the Hermes venv:
   `~/.ssh/id_ed25519`, `state.db`, a repo `.env`, `/etc/hostname` and a relative path
   were refused, and a repo `README.md` was allowed.
6. ✅ (2026-11-24) The exact signature of `validate_media_delivery_path` in the installed Hermes
   (`(path, session_key="")`), and that `gateway.platforms.base` is still a public module
   entry in `plugin_api.json`.

## 5. Live test

After `deploy-live` and a gateway restart, in the **hermes linear test project**:

1. `linear_issue_get` with `comments: 50` on a ticket with threads. The threads and
   replies are in order; a very long thread history is trimmed with `comments_truncated`.
2. `linear_search`: text; each typed filter; `me`; paging with `cursor`.
3. `linear_attach`: a file from scratch onto the session ticket; `embed` of an image,
   pasted into a comment; a GitHub PR URL; a plain doc URL; `~/.ssh/...` refused with
   `PATH_DENIED`.
4. `linear_attach` on an out-of-scope ticket: `APPROVAL_REQUIRED`, then the re-call shows
   the select. Allow attaches; Deny does not.
5. `linear_session_link` with the PR URL. The link shows on the agent session, and a
   repeat behaves as recorded in §4.3.

Then the PR.
