# Read and attach (`linear_issue_get` fields + comments, `linear_search`, `linear_attach`, `linear_session_link`) Implementation Plan

Linear: RET-120

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The agent reads everything about a ticket (fields, relations, attachments, comment threads), searches issues, and attaches files, URLs and inline images through the `linear_*` tools. It also reports its PR on the agent session. All of this works without going around the tools.

**Architecture:** Two new modules sit on `agent_tools`' `_tool` / `_gated` machinery. `tools_read.py` holds the `linear_issue_get` additions and `linear_search`. `tools_attach.py` holds `linear_attach` and `linear_session_link`. `agent_tools.py` imports both at its end and merges their `SCHEMAS` / `HANDLERS`. New GraphQL and the upload PUT live in `linear_client.py`. The file path policy is Hermes's public `gateway.platforms.base.validate_media_delivery_path`.

**Tech Stack:** Python 3.11, the plugin's `LinearClient` (aiohttp), pytest, and `tests/fake_linear.py` (an aiohttp fake server). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-24-read-and-attach-design.md`

## Global Constraints

- Public Hermes plugin API only (`~/.hermes/hermes-agent/hermes_cli/data/plugin_api.json`). Product code adds no private imports (tests may import Hermes internals). Hermes core is never changed. `hermes plugins validate .`: its "private core imports" list must not grow.
- Tools never raise. Failures are `{"ok": false, "error": CODE, ...}`. Every Linear call uses the app token through `LinearClient`.
- `client.issue_get` and every existing query stay unchanged (scope checks and syncs depend on them).
- Constants: `ISSUE_GET_MAX_CHARS = 50_000`; `COMMENTS_MAX = 50`; `REPLIES_PER_THREAD = 50`; `RELATIONS_MAX = ATTACHMENTS_MAX = 50`; `SEARCH_LIMIT_DEFAULT = 25`, `SEARCH_LIMIT_MAX = 50`; `ATTACH_MAX_BYTES = 50 * 1024 * 1024` (adjusted by the Task 3 probe); the attach timeout is `PLAN_SYNC_TIMEOUT_S` (120 s).
- State types: `("triage", "backlog", "unstarted", "started", "completed", "canceled")`.
- GitHub PR URL: `^https://github\.com/[^/]+/[^/]+/pull/\d+(?:[/?#].*)?$`.
- Upload PUT headers: `Content-Type: <content_type>`, `Cache-Control: public, max-age=31536000`, plus every `uploadFile.headers` pair. **No `Authorization` header.**
- Adding a tool updates, in the same commit: its module's `SCHEMAS`/`HANDLERS`, `agent_tools.__all__`, `plugin.yaml` `provides_tools` (order: the existing 8, then `linear_search`, `linear_attach`, `linear_session_link`), `tests/test_plugin_register.py` `TOOL_NAMES`, and one bullet in `hermes_linear/skills/manage-superpowers/SKILL.md`, which must stay ≤ 150 lines. Pay for each added line by tightening wording elsewhere.
- Tests run from `.worktrees/ret120`: `.venv/bin/pytest -q`. Commit messages carry the task's sub-ticket ID.
- Live probes run as the app: `python3 ~/.hermes/profiles/neo/skills/software-development/hermes-linear-dev/scripts/linear-query.py '<graphql>'`. Write probes only touch tickets in the **hermes linear test project**.

## Review Focus

1. **The newest thread alone is over 50,000 chars:** every thread is dropped, and the result is `comments: []` with `comments_truncated: true`. The issue still comes back. Owned by Task 1 (`test_single_huge_thread_drops_all_threads`).
2. **A symlink inside an allowed dir that points into `~/.ssh` (or to a `.env`):** refused with `PATH_DENIED`; the policy judges the resolved target. Owned by Task 3 (`test_symlink_to_denied_target_refused`).
3. **A file name with `]`, `)` or spaces in `embed` markdown:** must not break the link. The name is escaped (`\]`, `\)`) and the URL is used as Linear returns it. Owned by Task 4 (`test_embed_markdown_escapes_name`).
4. **An approved out-of-scope re-run whose file was deleted in between:** `FILE_NOT_FOUND`, and nothing is uploaded or attached. Owned by Task 4 (`test_approved_rerun_with_missing_file`).
5. **`linear_session_link` from a subagent or another session key while a Linear turn is in flight:** `NOT_LINEAR_SESSION`, never the in-flight turn's session. Owned by Task 5 (`test_other_session_key_is_not_linear_session`).

---

### Task 1: `linear_issue_get` fields + comment threads

**Files:**
- Create: `hermes_linear/tools_read.py`
- Modify: `hermes_linear/linear_client.py` (add `QUERY_ISSUE_DETAILS`, `QUERY_ISSUE_THREADS`, `issue_details`, `issue_threads`)
- Modify: `hermes_linear/agent_tools.py` (move the `linear_issue_get` body out; bottom import + merge; schema gains `comments`)
- Modify: `hermes_linear/skills/manage-superpowers/SKILL.md` (the `linear_issue_get` bullet mentions `comments`)
- Test: `tests/test_tools_read.py`, `tests/test_linear_client.py`

**Interfaces:**
- Produces: `LinearClient.issue_details(issue_id: str) -> dict` (raw `issue` node); `LinearClient.issue_threads(issue_id: str, first: int) -> list[dict]` (raw top-level comment nodes, each with `children.nodes`); `tools_read.SCHEMAS: dict[str, dict]`, `tools_read.HANDLERS: dict[str, Callable]`; `tools_read.shape_details(node: dict) -> dict` (spec §1.1 keys); `tools_read.shape_threads(nodes: list[dict], app_id: str) -> list[dict]`; `tools_read.trim(result: dict, limit: int = ISSUE_GET_MAX_CHARS) -> dict`.
- The merge mechanism at the end of `agent_tools.py` (later tasks reuse it): `from . import tools_read as _tools_read` (and later `tools_attach`), then `TOOL_SCHEMAS.update(_tools_read.SCHEMAS)` and `TOOL_HANDLERS.update(_tools_read.HANDLERS)`. Ordering key: `TOOL_SCHEMAS` keeps `linear_issue_get` in its current (first) slot.

- [ ] **Step 1: Write the failing tests** in `tests/test_tools_read.py`, using a `StubClient` and the `pin_ctx`/`loop`/`adapter` fixtures as in `tests/test_linear_query.py`:
  - `test_details_mapping`: the stub's `issue_details` returns assignee `{id:"u1",name:"Mike"}`, delegate null, priority 2 / `"High"`, estimate 3, dueDate `"2026-12-01"`, cycle `{id,number:4,name:"C4"}`, projectMilestone null, one `relations` node (`type:"blocks"`, `relatedIssue`) and one `inverseRelations` node (`type:"related"`, `issue`), and one attachment. The result has every §1.1 key with `relations == [{"type":"blocks","direction":"outbound","issue":{...}}, {"type":"related","direction":"inbound","issue":{...}}]`, and every existing `issue_get` key unchanged.
  - `test_comments_zero_runs_no_thread_query`: there is no `comments` key, `issue_threads` is never called, and there is no `comments_truncated`.
  - `test_thread_order`: 3 threads with shuffled `createdAt` and shuffled replies → threads newest first, replies oldest first. `author_is_app` is true only for the app viewer id. `resolved` is `resolvedAt is not None`.
  - `test_trim_drops_oldest_threads`: three threads of about 20,000 chars each → the two newest are kept, `comments_truncated is True`, and the serialized size is ≤ 50,000.
  - `test_single_huge_thread_drops_all_threads` (Review Focus 1).
  - `test_huge_description_not_trimmed`: a 60,000-char description with `comments: 0` comes back whole and has no `comments_truncated` key.
  - `test_comments_bounds`: `comments` of 51, -1, `"5"` or `true` is `BAD_ARGS`, before any client call.
  - `test_schema`: `properties.comments == {"type": "integer", "minimum": 0, "maximum": 50, "description": ...}`, `required == ["id"]`.
- In `tests/test_linear_client.py` (against `FakeLinear`): `test_issue_details_query` and `test_issue_threads_query`. Each checks the op name, the variables (`{"id": ..., "first": N}` for threads) and that the returned node is passed through.

- [ ] **Step 2: Run and confirm they fail**

Run: `.venv/bin/pytest -q tests/test_tools_read.py tests/test_linear_client.py`
Expected: FAIL (`tools_read` missing; `issue_details` / `issue_threads` missing).

- [ ] **Step 3: Implement**
  - `QUERY_ISSUE_DETAILS` (op `IssueDetails`): `assignee { id name } delegate { id name } priority priorityLabel estimate dueDate cycle { id number name } projectMilestone { id name } relations(first: 50) { nodes { type relatedIssue { identifier title state { name } } } } inverseRelations(first: 50) { nodes { type issue { identifier title state { name } } } } attachments(first: 50) { nodes { id title subtitle url sourceType } }`.
  - `QUERY_ISSUE_THREADS` (op `IssueThreads`, `$first: Int!`): `comments(first: $first, filter: { parent: { null: true } }) { nodes { id url body createdAt resolvedAt user { id name } children(first: 50) { nodes { id body createdAt resolvedAt user { id name } } } } }`.
  - In `tools_read._get`: `issue = await ctx.client.issue_get(id)`; merge `shape_details(await ctx.client.issue_details(issue["id"]))`. If `comments > 0`, add `comments = shape_threads(await ctx.client.issue_threads(issue["id"], n), await ctx.client.viewer_id())` and `comments_truncated = False`, then `trim`.
  - `trim`: measure with `json.dumps(result, ensure_ascii=False, default=str)`; pop from the end of `comments` (the oldest) while over the limit.
  - Re-check spec §4 item 1 live with the final `QUERY_ISSUE_THREADS` on RET-118 (`first: 50`). Expected: no `Query too complex`. Note the result in the commit message.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/pytest -q && hermes plugins validate .`
Expected: all pass, and the private-imports list is unchanged.

- [ ] **Step 5: Commit**

```bash
git add hermes_linear/tools_read.py hermes_linear/linear_client.py hermes_linear/agent_tools.py hermes_linear/skills/manage-superpowers/SKILL.md tests/test_tools_read.py tests/test_linear_client.py
git commit -m "feat: linear_issue_get fields, relations, attachments and comment threads (<T1 ID>)"
```

---

### Task 2: `linear_search`

**Files:**
- Modify: `hermes_linear/tools_read.py` (add `build_issue_filter`, the `linear_search` handler and schema)
- Modify: `hermes_linear/linear_client.py` (add `QUERY_SEARCH_ISSUES`, `QUERY_LIST_ISSUES`, `search_issues`, `list_issues`)
- Modify: `hermes_linear/agent_tools.py` (`__all__`), `plugin.yaml`, `tests/test_plugin_register.py` (`TOOL_NAMES`), `SKILL.md` (bullet)
- Test: `tests/test_tools_read.py`, `tests/test_linear_client.py`

**Interfaces:**
- Consumes: the Task 1 merge mechanism.
- Produces: `tools_read.build_issue_filter(args: dict, parent_id: str | None) -> dict`; `LinearClient.search_issues(term: str, filter: dict, include_comments: bool, first: int, after: str | None) -> dict` and `LinearClient.list_issues(filter: dict, first: int, after: str | None) -> dict`. Both return the raw connection `{nodes, pageInfo: {hasNextPage, endCursor}}`.

- [ ] **Step 1: Write the failing tests**
  - `test_build_issue_filter` (parametrized table, exact dicts from spec §2.3): `{"team":"ret"}` → `{"team":{"key":{"eq":"RET"}}}`. A project UUID → `{"project":{"id":{"eq":U}}}`; the name `"hermes-linear"` → `{"project":{"name":{"eqIgnoreCase":"hermes-linear"}}}`. `{"state":"Started"}` → `{"state":{"type":{"eq":"started"}}}` (a type word wins, case-insensitive); `{"state":"In Review"}` → `{"state":{"name":{"eqIgnoreCase":"In Review"}}}`. `{"assignee":"me"}` → `{"assignee":{"isMe":{"eq":True}}}`; `"a@b.c"` → `{"assignee":{"email":{"eqIgnoreCase":"a@b.c"}}}`; `"Mike"` → `{"assignee":{"or":[{"name":{"eqIgnoreCase":"Mike"}},{"displayName":{"eqIgnoreCase":"Mike"}}]}}`. The same three for `delegate`. `{"label":"bug"}` → `{"labels":{"some":{"name":{"eqIgnoreCase":"bug"}}}}`. Parent → `{"parent":{"id":{"eq":P}}}`. With several arguments, `{"and": [...]}` lists them in argument order; a single argument is returned bare; no arguments → `{}`.
  - `test_search_text_uses_search_issues` / `test_no_text_uses_list_issues`: the client method called, `first`, `after`, `include_comments` passed through; the row shape is spec §1.2 (`updated_at` from `updatedAt`, names or `None`).
  - `test_next_cursor`: `hasNextPage` true → `endCursor`; false → `None`.
  - `test_parent_resolved_by_issue_get`: `parent: "RET-5"` → one `issue_get("RET-5")`, its `id` used in the filter.
  - `test_bad_args`: `include_comments` without `text`; `limit` 0, 51 or `"5"`; a non-string filter value; an empty-string `text`. All are `BAD_ARGS` before any client call.
  - `tests/test_linear_client.py`: `test_search_issues_query`, `test_list_issues_query` (op names, variables, `orderBy: updatedAt` for the list).

- [ ] **Step 2: Run and confirm they fail**

Run: `.venv/bin/pytest -q tests/test_tools_read.py -k "filter or search or cursor or parent or bad_args" tests/test_linear_client.py`
Expected: FAIL.

- [ ] **Step 3: Implement.**
  - Node fields for both queries: `identifier title url updatedAt priority state { name } assignee { name } delegate { name } project { name }`.
  - Schema properties: `text`, `team`, `project`, `state`, `assignee`, `delegate`, `label`, `parent` (strings), `include_comments` (boolean), `limit` (integer, 1–50), `cursor` (string). No `required`.
  - The description says complex filters go through `linear_query`.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/pytest -q && hermes plugins validate .`
Expected: all pass (the roster, manifest and skill tests include `linear_search`).

- [ ] **Step 5: Commit**

```bash
git add hermes_linear/tools_read.py hermes_linear/linear_client.py hermes_linear/agent_tools.py plugin.yaml hermes_linear/skills/manage-superpowers/SKILL.md tests/test_tools_read.py tests/test_linear_client.py tests/test_plugin_register.py
git commit -m "feat: linear_search with typed filters (<T2 ID>)"
```

---

### Task 3: Path policy, upload and attachment client calls

**Files:**
- Create: `hermes_linear/tools_attach.py` (the path policy only in this task, plus empty `SCHEMAS`/`HANDLERS`)
- Modify: `hermes_linear/linear_client.py` (add `MUTATION_FILE_UPLOAD`, `MUTATION_ATTACHMENT_CREATE`, `MUTATION_ATTACHMENT_LINK_URL`, `MUTATION_ATTACHMENT_LINK_GITHUB_PR`, `UploadError`, `file_upload`, `attachment_create`, `attachment_link_url`, `attachment_link_github_pr`)
- Modify: `tests/fake_linear.py` (a `PUT /upload/{name}` route recording calls; status set by `fake.upload_status`, default 200)
- Test: `tests/test_tools_attach.py`, `tests/test_linear_client.py`

**Interfaces:**
- Produces:
  - `tools_attach.AttachFile` (frozen dataclass: `name: str, content_type: str, data: bytes`).
  - `tools_attach.load_attach_file(path: str, session_key: str | None) -> AttachFile`, which raises `agent_tools._Refusal("PATH_DENIED" | "FILE_NOT_FOUND" | "FILE_TOO_LARGE")`. The order is spec §2.4: not absolute → `PATH_DENIED`; `not os.path.isfile` → `FILE_NOT_FOUND`; `validate_media_delivery_path(path, session_key=session_key or "")` is `None` → `PATH_DENIED`; size over `ATTACH_MAX_BYTES` → `FILE_TOO_LARGE`; then read the **returned resolved** path. `name` is the basename of `path` as given.
  - `LinearClient.file_upload(name: str, content_type: str, data: bytes) -> str` (the asset URL). It raises `UploadError(status: int)` (a `LinearError` subclass with `code="UPLOAD_FAILED"`) on a non-2xx PUT.
  - `LinearClient.attachment_create(issue_id: str, title: str, subtitle: str | None, url: str) -> dict`, `LinearClient.attachment_link_url(issue_id: str, url: str, title: str | None) -> dict` and `LinearClient.attachment_link_github_pr(issue_id: str, url: str) -> dict`. Each returns `{"id": ..., "url": ...}` of the attachment and raises `LinearError` when `success` is false.

- [ ] **Step 0: Live probes (spec §4 items 2 and 4).**
  - Run `fileUpload(size: 60000000, filename: "probe.bin", contentType: "application/octet-stream")` and, if that is accepted, a few larger sizes, to find the limit the mutation refuses at. Set `ATTACH_MAX_BYTES` to the largest accepted power-of-ten MB at or below 50 MB. If nothing is refused up to 500 MB, keep 50 MB. Don't PUT any bytes.
  - On a ticket in the hermes linear test project, run `attachmentLinkURL` twice with the same URL, and `attachmentLinkGitHubPR` with `https://github.com/masha256/hermes-linear/pull/11`. Record the return shapes and whether the repeat updates or duplicates. Update the spec's §4 items 2 and 4 with ✅ and the findings, and commit the spec.

- [ ] **Step 1: Write the failing tests**
  - `tests/test_tools_attach.py`, using the real `validate_media_delivery_path` against tmp files under `tmp_path`. Monkeypatch `HOME` to `tmp_path / "home"` so that `$HOME/.ssh` is under the test's control. Cases:
    - `test_allowed_file_loads` (name, content type `image/png` for `x.png`, bytes);
    - `test_relative_path_denied`;
    - `test_missing_file_not_found`;
    - `test_directory_not_found`;
    - `test_env_file_denied` (`.env` anywhere);
    - `test_ssh_dir_denied`;
    - `test_symlink_to_denied_target_refused` (Review Focus 2: a link in an allowed dir to `$HOME/.ssh/id_ed25519`, and one to a `.env`);
    - `test_too_large` (monkeypatch `ATTACH_MAX_BYTES` to 10, with an 11-byte file);
    - `test_unknown_extension_is_octet_stream`.
  - `tests/test_linear_client.py`:
    - `test_file_upload_puts_bytes_without_bearer`: the `FileUpload` variables are `{size, filename, contentType}`. The PUT reaches the fake's `uploadUrl` with the exact bytes, `Content-Type`, `Cache-Control: public, max-age=31536000`, the returned extra header, and **no** `Authorization`. The method returns the `assetUrl`.
    - `test_file_upload_non_2xx_raises_upload_error`: `upload_status = 403` → `UploadError` with `.status == 403` and `.code == "UPLOAD_FAILED"`.
    - `test_attachment_create_input`, `test_attachment_link_url_input` and `test_attachment_link_github_pr_input`: the exact `input`/arguments, and `success: false` → `LinearError`.

- [ ] **Step 2: Run and confirm they fail**

Run: `.venv/bin/pytest -q tests/test_tools_attach.py tests/test_linear_client.py`
Expected: FAIL.

- [ ] **Step 3: Implement.**
  - The PUT uses the client's `_http()` session with `timeout=aiohttp.ClientTimeout(total=PLAN_SYNC_TIMEOUT_S)`.
  - `validate_media_delivery_path` is imported lazily inside `load_attach_file` (`from gateway.platforms.base import validate_media_delivery_path`). If the import fails, the path is refused with `PATH_DENIED` (fail closed).

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/pytest -q && hermes plugins validate .`
Expected: all pass; no new private imports (`gateway.platforms.base` is a public module).

- [ ] **Step 5: Commit**

```bash
git commit -m "feat: attach path policy, file upload and attachment client calls (<T3 ID>)"
```

---

### Task 4: `linear_attach` tool

**Files:**
- Modify: `hermes_linear/tools_attach.py` (the `linear_attach` parse, body, schema and handler)
- Modify: `hermes_linear/agent_tools.py` (bottom import of `tools_attach`, merge, `__all__`), `plugin.yaml`, `tests/test_plugin_register.py` (`TOOL_NAMES`), `SKILL.md` (bullet)
- Test: `tests/test_tools_attach.py`, `tests/test_tool_approval_e2e.py`

**Interfaces:**
- Consumes: `load_attach_file`, `AttachFile`, the Task 3 client methods and `UploadError`; `agent_tools._tool(parse, body, gated="linear_attach", blocking_parse=True, timeout_s=PLAN_SYNC_TIMEOUT_S)`, `_writable` and `_check_gate`.
- Produces: `tools_attach.GITHUB_PR_RE` (the pattern from the Global Constraints) and `tools_attach.embed_markdown(name: str, content_type: str, asset_url: str) -> str`, both used by Task 5.

Parse runs in the tool thread and needs the session key for the policy. It reads `HERMES_SESSION_KEY` through `gateway.session_context.get_session_env` (public), as `agent_tools._session()` does.

- [ ] **Step 1: Write the failing tests** (`StubClient` recording `file_upload` / `attachment_*` calls; `pin_ctx` with the session issue `SELF`; a tmp file the real policy allows):
  - `test_file_attach_in_scope`: `{id: SELF, path}` → `file_upload` then `attachment_create(SELF_UUID, "<basename>", None, asset)`. The result is `{ok, attachment_id, asset_url, issue, url}`. A `title`/`subtitle` given in the call is passed through.
  - `test_github_pr_url_uses_link_github_pr` and `test_plain_url_uses_link_url` (title passed). Neither makes a `file_upload` call.
  - `test_embed_uploads_only`: `{path, embed: true}` → `{ok, asset_url, markdown: "![x.png](<asset>)"}`; a `.txt` file → `"[notes.txt](<asset>)"`; no `attachment_*` call.
  - `test_embed_markdown_escapes_name` (Review Focus 3: `a]b).png` → `![a\]b\).png](<asset>)`).
  - `test_bad_combinations` (all `BAD_ARGS`, before any client call): both `path` and `url`; neither; `embed` with `id`; `embed` with `url`; `path` with no `id` and no `embed`; `url: "ftp://x"`; `url: "javascript:alert(1)"`.
  - `test_embed_needs_linear_session`: `pin_ctx(linear=False)` → `NOT_LINEAR_SESSION`, no upload.
  - `test_out_of_scope_first_pass_uploads_nothing`: the target is outside the scope (the stub's parent walk ends outside it) → `APPROVAL_REQUIRED`, zero `file_upload` calls.
  - `test_approved_rerun_with_missing_file` (Review Focus 4): an armed request exists, but the file was deleted before the re-call → `FILE_NOT_FOUND`, no upload, and the request is not consumed by a write.
  - `test_upload_failed`: `UploadError(403)` → `{ok: false, error: "UPLOAD_FAILED", status: 403}`.
  - `test_attach_failure_after_upload_returns_asset_url`: `attachment_create` raises `LinearError` → `{ok: false, error: "LINEAR_ERROR", asset_url}`.
  - `test_wrong_thread_on_loop`: called from the loop thread → `WRONG_THREAD`.
  - `test_registry_returns_str`: through `plugin.tool_handler("linear_attach")`.
  - `tests/test_tool_approval_e2e.py`: `test_out_of_scope_attach_end_to_end`, modelled on the existing gated-comment end-to-end test. The first call → `APPROVAL_REQUIRED`; the identical re-call through `_dispatch_pre_tool_call_hooks` → the approve directive → once → the upload and attach run exactly once.

- [ ] **Step 2: Run and confirm they fail**

Run: `.venv/bin/pytest -q tests/test_tools_attach.py tests/test_tool_approval_e2e.py`
Expected: FAIL.

- [ ] **Step 3: Implement.**
  - Body order (spec §2.4): `NOT_LINEAR_SESSION` check → `_writable` + `_check_gate` when there is an `id` → upload → attach.
  - Catch `UploadError` before `LinearError`.
  - In the schema description, state the path policy in one sentence ("files Hermes may deliver as media; `.env`, credential dirs and Hermes state are refused") and add `_GATED_NOTE`.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/pytest -q && hermes plugins validate .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git commit -m "feat: linear_attach (file, URL, embed) behind the scope gate (<T4 ID>)"
```

---

### Task 5: `linear_session_link`

**Files:**
- Modify: `hermes_linear/tools_attach.py` (the handler and schema)
- Modify: `hermes_linear/agent_tools.py` (`__all__`), `plugin.yaml`, `tests/test_plugin_register.py` (`TOOL_NAMES`), `SKILL.md` (bullet)
- Test: `tests/test_tools_attach.py`

**Interfaces:**
- Consumes: `GITHUB_PR_RE`; `adapter.turns.turn_for_session_key(session_key) -> tuple[Key, Turn] | None`; `LinearClient.update_session(agent_session_id, added_external_urls=[{label, url}])`; and the `put_turn` helper from `tests/test_agent_tools_runtime.py`.

- [ ] **Step 0: Live probe (spec §4 item 3).** On an agent session the app owns on a ticket in the hermes linear test project, run `agentSessionUpdate(addedExternalUrls: [{label, url}])` twice with the same URL, then read `agentSession(id) { externalUrls }`. Record whether the URL is de-duplicated. Update the spec's §4 item 3 with ✅ and the finding, then commit. The tool's description states the observed behaviour, for example "re-adding the same URL is a no-op" or "…adds a duplicate: call once per PR".

- [ ] **Step 1: Write the failing tests**
  - `test_links_current_turn_session`: `put_turn(adapter, "sk", SELF)` → `update_session("sid-m1", added_external_urls=[{"label": "Pull request", "url": PR}])`; the result is `{ok: true, agent_session_id: "sid-m1", label, url}`.
  - `test_label_defaults_to_host`: `https://docs.example.com/x` → the label is `docs.example.com`. An explicit `label` wins.
  - `test_no_turn_is_not_linear_session` and `test_other_session_key_is_not_linear_session` (Review Focus 5: a turn in flight under `"sk"`, a call pinned with `key="other"`). Neither calls `update_session`.
  - `test_non_http_url_bad_args` (`ftp://`, `javascript:` or an empty string).
  - `test_linear_error_passthrough`: `update_session` raises `LinearError(code="RATELIMITED")` → `{ok: false, error: "RATELIMITED"}`.
  - `test_registry_returns_str`.

- [ ] **Step 2: Run and confirm they fail**

Run: `.venv/bin/pytest -q tests/test_tools_attach.py -k session_link`
Expected: FAIL.

- [ ] **Step 3: Implement** with `_tool(parse, body)`. There is no gate and no `blocking_parse`.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/pytest -q && hermes plugins validate .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git commit -m "feat: linear_session_link adds a URL to the agent session (<T5 ID>)"
```

---

### Task 6: README, skill Finish step, final checks

**Files:**
- Modify: `README.md` (rows in the Agent tools table for `linear_search`, `linear_attach` and `linear_session_link`; the updated `linear_issue_get` row; one "How it behaves" row on attaching; and a short "Attachments" note on the path policy and `gateway.strict` / `HERMES_MEDIA_DELIVERY_STRICT`)
- Modify: `hermes_linear/skills/manage-superpowers/SKILL.md` ("The eight tools" → "The eleven tools"; the **Finish** step: after opening the PR, `linear_session_link` it and `linear_attach` its URL to the session ticket; still ≤ 150 lines)
- Modify: `docs/superpowers/specs/2026-11-24-read-and-attach-design.md` (only if an implementation fact changed a detail: the decision rows stay, and changes are marked)
- Test: `tests/test_plugin_register.py` (the existing skill test, plus `test_skill_finish_links_pr`: the Finish paragraph contains `linear_session_link` and `linear_attach`)

- [ ] **Step 1: Write `test_skill_finish_links_pr` and run it.** Expected: FAIL.
- [ ] **Step 2: Edit the skill and the README.** Cut wording elsewhere in the skill until `wc -l` ≤ 150.
- [ ] **Step 3: Run everything**

Run: `.venv/bin/pytest -q && hermes plugins validate . && wc -l hermes_linear/skills/manage-superpowers/SKILL.md`
Expected: all pass; ≤ 150 lines; the private-imports list matches `main`'s.

- [ ] **Step 4: Commit**

```bash
git commit -m "docs: README and skill for read & attach tools; Finish links the PR (<T6 ID>)"
```
