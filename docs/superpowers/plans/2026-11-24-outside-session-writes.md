# Writes from outside a Linear session — Implementation Plan

Linear: RET-148

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let non-Linear Hermes sessions (Slack, Telegram, CLI, cron) use the gated Linear write tools under a new `outside_session_writes` policy, including creating issues in a chosen team/project.

**Architecture:** Reuse RET-119's gate unchanged in shape: `ToolContext` learns `outside`, `_writable` treats every outside target as out of scope, `_gated` picks the mode from `outside_session_writes` off Linear, and the `pre_tool_call` hook arms requests for any session key. `linear_issue_create` gains `team`/`project` resolution and a destination pseudo-target; outside writes carry an origin footer.

**Tech Stack:** Python 3, stdlib only; pytest; Hermes plugin API (public only).

**Spec:** `docs/superpowers/specs/2026-11-24-outside-session-writes-design.md`

## Global Constraints

- Public Hermes plugin API only; no core changes. `hermes plugins validate .` must pass with only the 3 pre-existing private imports (all in `hermes_linear/sessions_open.py`).
- Tools never raise; failures are `{"ok": false, "error": CODE}`.
- Baseline: `.venv/bin/pytest -q` → 1042 passed (worktree `.worktrees/ret148`, branch `ret-148-outside-session-writes`).
- "Outside" = `HERMES_SESSION_PLATFORM != "linear"`. A Linear-platform call with no resolved session ticket keeps refusing `NOT_LINEAR_SESSION`.
- `outside_session_writes: deny | approve | allow`, default `approve`, case-insensitive; invalid → `SettingsError`. `out_of_scope_writes` only ever applies inside Linear sessions.
- Outside `deny` → `NOT_LINEAR_SESSION`. Outside `approve` with no session key → `OUT_OF_SCOPE`.
- Session-only tools stay `NOT_LINEAR_SESSION` outside: `linear_sync_spec`, `linear_sync_plan`, `linear_task_state`, `linear_session_link`, `linear_attach` with `embed`.
- Approval grain unchanged: `rule_key = <tool>:<digest>`; the digest is over the model's args (never the footer).
- Footer text exactly: `"\n\n---\n_Filed by <profile> via <platform>_"`; profile from `HERMES_SESSION_PROFILE` (`""` → `default`), platform from `HERMES_SESSION_PLATFORM` (unset → `unknown`). Outside calls only; on `linear_issue_create` and `linear_comment` (create and `edit_marker` edit); never on `linear_issue_update`.
- Edit files with `write_file`/`patch`; never shell heredocs, `python -c` or `sed -i` (command approval stalls subagents).
- Every gated tool's array fields stay in `tool_approval.ARRAY_FIELDS` (sync test `test_array_fields_match_tool_schemas`). New `team`/`project` are plain `"type": "string"`.

## Review Focus

- A Slack request armed by key must never authorize a Linear-session call (different keys) — and a Linear request never a Slack call: pin both directions in the hook test (Task 1).
- `edit_marker` comment edited from outside twice must not stack two footers: `_with_marker` strips markers only, so the footer is appended after stripping any existing footer line (Task 2 test `test_outside_comment_edit_does_not_stack_footer`).
- `team` given as lower-case key (`ret`) or as name (`retrograde capital`) resolves; an ambiguous name never picks silently — first exact UUID, then key, then name; two teams with the same name → `TEAM_NOT_FOUND` (Task 2).
- A project in another team than the chosen one → `PROJECT_NOT_FOUND`, nothing written, no request stored (Task 2).
- An approved outside pass whose call now resolves to a different destination (default_team changed between calls) is refused `OUT_OF_SCOPE`, since the pseudo-target id differs (Task 2 test `test_outside_create_destination_changed_refused`).

---

### Task 1: Outside-session gate for comment, update and attach

**Files:**
- Modify: `hermes_linear/settings.py` (new `OUTSIDE_MODES = OUT_OF_SCOPE_MODES`; `Settings.outside_session_writes: str = "approve"`, `default_team: str = ""`, `default_project: str = ""`; parse in `load_settings`)
- Modify: `hermes_linear/agent_tools.py` (`ToolContext.outside: bool = False`; `_resolve`; `_writable`; `_gated`)
- Modify: `hermes_linear/tools_attach.py:122-127` (`_attach`: only `embed` needs a Linear session)
- Modify: `hermes_linear/tool_approval.py` (`REQUIRED_TEXT_OUTSIDE`; `approval_message(..., outside: bool = False)`)
- Modify: `hermes_linear/hooks.py` (`_gated_request` without the platform check)
- Test: `tests/test_settings.py`, `tests/test_agent_tools_generic.py` (`pin_ctx`), `tests/test_agent_tools_gate.py`, `tests/test_tools_attach.py`, `tests/test_tool_approval.py`, `tests/test_hooks_approval.py`, `tests/test_tool_approval_e2e.py`

**Interfaces:**
- Produces: `Settings.outside_session_writes`, `Settings.default_team`, `Settings.default_project` (strings, stripped; mode lower-cased). `ToolContext.outside` (True iff platform is not linear; then `session_issue_id is None` and `session_key` = the bound `HERMES_SESSION_KEY` or None). `tool_approval.REQUIRED_TEXT_OUTSIDE` (format keys `tool`, `targets`). `approval_message(tool, targets, args, digest, outside=False)`. Gate targets keep the shape `{"id", "identifier", "title"}` (Task 2 adds a pseudo-target of that shape).
- `pin_ctx` fixture becomes `pin(*, linear=True, outside=None, issue=SELF, key="sk", ad=adapter)`: `outside` defaults to `not linear`; `session_key = key if (linear or outside) else None`; passes `outside=` to `ToolContext`.

- [ ] **Step 1: Write the failing tests**

`tests/test_settings.py`:
```python
def test_outside_session_writes_default_and_values(): ...
    # unset -> "approve"; "ALLOW" -> "allow"; "deny" -> "deny"
def test_outside_session_writes_invalid_raises():
    # "maybe" -> SettingsError mentioning 'outside_session_writes'
def test_default_team_and_project_stripped_default_empty(): ...
    # unset -> ""; " RET " -> "RET"; "hermes linear test project" kept
```

`tests/test_agent_tools_gate.py` — replace `test_not_linear_session_never_gated` with:
```python
def _outside(adapter, mode): adapter._settings = dataclasses.replace(adapter._settings, outside_session_writes=mode)

def test_outside_comment_first_call_requires_approval(pin_ctx, ws, adapter):
    pin_ctx(linear=False)
    out = linear_comment({"id": "RET-2", "body": "hi"})   # RET-2 is IN scope inside; outside every target is out
    assert out["error"] == "APPROVAL_REQUIRED" and "outside a Linear session" in out["message"]
    assert ws.writes() == [] and _requests(adapter) == 1
def test_outside_armed_request_writes_once(pin_ctx, ws, adapter): ...   # arm via store.arm_request("sk", digest(...)); 1 comment, second call -> APPROVAL_REQUIRED again
def test_outside_deny_refuses_not_linear_session(pin_ctx, ws, adapter): ...  # _outside(adapter,"deny") -> NOT_LINEAR_SESSION, 0 requests
def test_outside_allow_writes_without_request(pin_ctx, ws, adapter): ...     # 1 comment, 0 requests
def test_outside_no_session_key_refused(pin_ctx, ws, adapter):
    pin_ctx(linear=False, key=None)  # approve mode -> {"ok": False, "error": "OUT_OF_SCOPE"}
def test_knobs_are_independent(pin_ctx, ws, adapter):
    # out_of_scope_writes="allow", outside_session_writes="deny": outside -> NOT_LINEAR_SESSION;
    # inside OTH-9 comment -> written. Then reverse: outside allow writes, inside OTH-9 -> OUT_OF_SCOPE with deny.
def test_outside_update_both_ends_are_targets(pin_ctx, ws, adapter): ...    # update RET-2 parent=RET-3: one request, targets == {RET-2 uuid, RET-3 uuid}
def test_linear_platform_without_ticket_still_refused(pin_ctx, ws, adapter):
    pin_ctx(linear=True, issue=None)  # -> NOT_LINEAR_SESSION, 0 requests
def test_session_only_tools_refuse_outside(pin_ctx, ws, adapter):
    pin_ctx(linear=False)  # linear_task_state -> NOT_LINEAR_SESSION (key bound this time)
```

`tests/test_tools_attach.py`: rewrite the test at line 358 to `test_outside_attach_url_is_gated` (`linear_attach({"id": "RET-1", "url": "https://example.com/x"})` → `APPROVAL_REQUIRED`, no upload/attach call); keep line 351 (embed → `NOT_LINEAR_SESSION`) and the `linear_session_link` tests unchanged.

`tests/test_tool_approval.py`:
```python
def test_outside_message_first_line_and_digest_last():
    m = approval_message("linear_comment", [{"id": "u", "identifier": "RET-2", "title": "t"}], {"id": "RET-2"}, "0123456789abcdef", outside=True)
    assert m.splitlines()[0] == "Approve `linear_comment` from outside a Linear session?"
    assert m.splitlines()[-1] == "digest: 0123456789abcdef"
def test_required_text_outside_verbatim(): ...  # equals the spec §2.1 sentence with {targets}/{tool}
```

`tests/test_hooks_approval.py`:
```python
def test_outside_request_armed_by_slack_key(...):  # platform "slack", key "slack:k": stored request -> directive with rule_key "linear_comment:<digest>"
def test_slack_request_not_armed_from_linear_key(...)  # request under "slack:k", call bound to linear key "lk" -> None
def test_linear_request_not_armed_from_slack_key(...)
def test_outside_no_key_no_directive(...)
def test_outside_posts_no_activities(...)            # _session(kw) is None off Linear; no _schedule call
```

`tests/test_tool_approval_e2e.py`:
```python
async def test_e2e_outside_comment_approve_writes(e2e):   # platform bound "slack"; real _dispatch_pre_tool_call_hooks + tools.approval approved -> exactly 1 comment
async def test_e2e_outside_comment_deny_never_writes(e2e): # denied -> BLOCKED, 0 comments
```
Follow the existing `e2e` fixture; bind `HERMES_SESSION_PLATFORM="slack"` and a Slack-shaped key; name the stand-in seams in the docstring.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest -q tests/test_settings.py tests/test_agent_tools_gate.py tests/test_tools_attach.py tests/test_tool_approval.py tests/test_hooks_approval.py tests/test_tool_approval_e2e.py`
Expected: FAIL (AttributeError on `outside_session_writes` / `outside`; `NOT_LINEAR_SESSION` where `APPROVAL_REQUIRED` is expected).

- [ ] **Step 3: Implement**

- `settings.py`: parse the three keys; mode validated against `("approve", "deny", "allow")` with the same error shape as `out_of_scope_writes`.
- `agent_tools._resolve`: `ctx()` sets `outside=not is_linear` and `session_key=env["session_key"]` for outside calls too (inside unchanged).
- `_writable`: `if ctx.outside:` fetch the issue; if `gate` and `issue["id"] in gate.approved` → return; if `gate` and not `gate.approved` → `gate.record(issue)`, return; else `_Refusal("OUT_OF_SCOPE")`. Without a gate (non-gated caller) → `_Refusal("NOT_LINEAR_SESSION")`. Inside path unchanged.
- `_gated`: `can_request = bool(ctx.session_key) and store is not None and dg is not None and (ctx.is_linear or ctx.outside)`; mode = `outside_session_writes` if `ctx.outside` else `out_of_scope_writes` (default `approve` when settings missing). Outside `deny` → `_err("NOT_LINEAR_SESSION")`. Outside messages use `REQUIRED_TEXT_OUTSIDE` and `approval_message(..., outside=True)`.
- `tools_attach._attach`: refuse `NOT_LINEAR_SESSION` only when `a["id"] is None` (embed) and not a Linear session; the id path goes through `_writable`.
- `hooks._gated_request`: drop the `HERMES_SESSION_PLATFORM` check; keep the session-key requirement. `_session` unchanged.
- `linear_issue_create` outside still refuses `NOT_LINEAR_SESSION` in this task (Task 2 lifts it); keep `tests/test_agent_tools_generic.py:211` passing as is.

- [ ] **Step 4: Run the suite and validate**

Run: `.venv/bin/pytest -q` → all pass (baseline 1042 + new). `hermes plugins validate .` → "Validation passed", same 3 private imports.

- [ ] **Step 5: Commit**

```bash
git add hermes_linear tests
git commit -m "feat: outside-session writes for comment/update/attach via outside_session_writes (RET-148)"
```

### Task 2: `linear_issue_create` outside a session, and the origin footer

**Files:**
- Modify: `hermes_linear/linear_client.py` (new `QUERY_PROJECTS_BY_NAME`, `QUERY_PROJECT`, `async def projects_named(name: str) -> list[dict]`, `async def project(ref: str) -> dict | None`; each project dict `{"id", "name", "team_ids": [..]}`)
- Modify: `hermes_linear/agent_tools.py` (`_parse_create` + `team`/`project`; `_create`; `_destination`; `_origin_footer`; `_comment` footer; `linear_issue_create` schema)
- Modify: `tests/fake_workspace.py` (`teams()`, `projects_named()`, `project()` over a `self.projects` dict; second team `OTH` and a project in it)
- Test: `tests/test_agent_tools_generic.py`, `tests/test_agent_tools_gate.py`, `tests/test_linear_client.py`, `tests/test_tool_approval_e2e.py`

**Interfaces:**
- Consumes: Task 1's `ToolContext.outside`, `Settings.default_team/default_project`, gate target shape, `REQUIRED_TEXT_OUTSIDE`.
- Produces: `agent_tools._origin_footer() -> str`; `async _destination(ctx, team_ref: str, project_ref: str | None) -> tuple[dict, dict | None]` (team `{id,key,name}`, project `{id,name,team_ids}` or None), raising `_Refusal("BAD_ARGS" | "TEAM_NOT_FOUND" | "PROJECT_NOT_FOUND")`. Pseudo-target `{"id": "team:<team id>" + ("/project:<project id>" if project else ""), "identifier": <team key>, "title": "new issue in <key>" + (" / <project name>" if project else "")}`.

- [ ] **Step 1: Write the failing tests**

`tests/test_linear_client.py`:
```python
async def test_projects_named_query_and_shape(...)   # FakeLinear op "ProjectsByName": variables {"name": "hermes linear test project"}; returns [{"id","name","team_ids":["T1"]}]
async def test_project_by_id_missing_returns_none(...)
```

`tests/test_agent_tools_generic.py` (replace the line-211 test):
```python
def test_outside_create_without_team_bad_args(pin_ctx, ws, adapter):   # no arg, default_team "" -> BAD_ARGS, 0 writes, 0 requests
def test_outside_create_allow_uses_defaults_and_footer(pin_ctx, ws, adapter, monkeypatch):
    # outside allow; default_team "RET", default_project = PROJECT name; HERMES_SESSION_PROFILE="" , PLATFORM "slack"
    # -> created in TEAM/PROJECT; description endswith "\n\n---\n_Filed by default via slack_"; created_issues unchanged
def test_outside_create_team_by_key_case_insensitive_and_name(...)
def test_outside_create_duplicate_team_name_not_found(...)            # two teams named "Same" -> TEAM_NOT_FOUND
def test_outside_create_project_other_team_not_found(...)             # PROJECT_NOT_FOUND, 0 writes, 0 requests
def test_outside_create_state_and_labels_resolve_on_destination_team(...)
def test_inside_create_team_project_must_match(pin_ctx, ws, adapter):
    # inside: team "OTH" -> BAD_ARGS; team "RET" + project PROJECT name -> created, no footer
def test_outside_comment_footer_and_edit_does_not_stack_footer(...)    # allow; edit_marker "m" twice -> body has exactly one "_Filed by" line
def test_outside_update_has_no_footer(...)
```

`tests/test_agent_tools_gate.py`:
```python
def test_outside_create_request_names_destination(pin_ctx, ws, adapter):
    # approve: APPROVAL_REQUIRED; stored request targets == ["team:<RET id>/project:<PROJECT id>"]; message lists "new issue in RET / <project>"
def test_outside_create_with_parent_two_targets(...)
def test_outside_create_destination_changed_refused(...)
    # arm request for default_team RET; change settings default_team to OTH; re-call -> OUT_OF_SCOPE, 0 writes
```

`tests/test_tool_approval_e2e.py`:
```python
async def test_e2e_outside_create_approve_writes_once(e2e):  # raw args labels as string "bug" on the hook side; handler on coerce_tool_args output; 1 issue
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest -q tests/test_linear_client.py tests/test_agent_tools_generic.py tests/test_agent_tools_gate.py tests/test_tool_approval_e2e.py`
Expected: FAIL (`NOT_LINEAR_SESSION` from `_create`; missing client methods).

- [ ] **Step 3: Implement**

- Queries: `projects(filter: {name: {eqIgnoreCase: $name}}, first: 10) { nodes { id name teams { nodes { id } } } }` and `project(id: $id) { id name teams { nodes { id } } }` (a `NOT_FOUND`/entity error → None).
- `_destination`: team match order UUID, key (casefold), name (casefold); >1 name match → `TEAM_NOT_FOUND`. Project: if the ref is a UUID-shaped string try `project(ref)` first, else `projects_named`; keep those whose `team_ids` contain the team; exactly one → it; else `PROJECT_NOT_FOUND`.
- `_create` outside: `team_ref = a["team"] or settings.default_team` (blank → `BAD_ARGS`), `project_ref = a["project"] or settings.default_project or None`; record the pseudo-target on the gate if `ctx.gate` and its id not in `gate.approved` (on an approved pass, a pseudo-target id not in `approved` → `_Refusal("OUT_OF_SCOPE")`); parent via `_writable`; state/labels on the destination team; description = `(a["description"] or "") + _origin_footer()`; `_check_gate`; `issue_create`; no `_record_created`.
- `_create` inside: if `team`/`project` given, resolve with `_destination` and `BAD_ARGS` unless ids equal the session ticket's team/project; rest unchanged.
- `_comment` outside: body = `_FOOTER_RE.sub("", body).rstrip() + _origin_footer()` before the marker is added (so `_with_marker` keeps the marker last and a re-edit replaces, not stacks, the footer). `_FOOTER_RE = re.compile(r"\n*---\n_Filed by [^\n]* via [^\n]*_\s*$")`.
- Schema: `team` and `project` properties and the description from spec §3.

- [ ] **Step 4: Run the suite and validate**

Run: `.venv/bin/pytest -q` → all pass. `hermes plugins validate .` → passed, same 3 private imports.

- [ ] **Step 5: Commit**

```bash
git add hermes_linear tests
git commit -m "feat: linear_issue_create outside a session (team/project, defaults) and origin footer (RET-148)"
```

### Task 3: Docs and skill text

**Files:**
- Modify: `README.md` (write-scope paragraph at ~line 73; "How it behaves" table row ~line 50; settings table ~line 310: rows for `outside_session_writes`, `default_team`, `default_project`; approvals note ~line 369: presence rules, `approvals.cron_mode`, Slack/Telegram card shows Hermes's full choice set, *session*/*always* only cover identical calls)
- Modify: `hermes_linear/skills/manage-superpowers/SKILL.md` ("Calling the tools": `linear_issue_create` line gains `team?`, `project?`; one sentence: outside a Linear session the write tools follow `outside_session_writes` and ask in that chat)
- Test: `tests/test_plugin_register.py` (existing cap test), new `test_readme_documents_outside_session_settings`

**Interfaces:**
- Consumes: setting names and error codes from Tasks 1–2.

- [ ] **Step 1: Write the failing test**

```python
def test_readme_documents_outside_session_settings():
    text = README.read_text()
    for key in ("outside_session_writes", "default_team", "default_project", "TEAM_NOT_FOUND", "PROJECT_NOT_FOUND"):
        assert key in text
    assert "never authorizes a Linear write" not in text
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest -q tests/test_plugin_register.py -k readme` → FAIL.

- [ ] **Step 3: Edit README and skill**

The skill must stay ≤ 150 lines (`test_skill_body_is_a_short_overlay_naming_every_tool`): reflow an existing paragraph to make room.

- [ ] **Step 4: Run the suite and validate**

Run: `.venv/bin/pytest -q` and `hermes plugins validate .` → pass.

- [ ] **Step 5: Commit**

```bash
git add README.md hermes_linear/skills tests
git commit -m "docs: outside-session writes in README and skill (RET-148)"
```
