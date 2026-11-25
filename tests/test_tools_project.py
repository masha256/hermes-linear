"""linear_project (RET-122 spec §1.1, §2, §3; RET-165 ``get`` / ``post_update``, RET-166
``milestone`` / ``link``) through the real registry.

``FakeWorkspace`` (``ws``): RET-1 is the session issue SELF (project ``Hermes``,
``PROJECT``), RET-2 (``CHILD``) its child, OTH-9 (``OTHER``) unrelated and without a
project. ``ws.add_project`` adds more projects; ``ws.project_updates`` holds each project's
updates oldest first.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from fake_workspace import PROJECT, FakeWorkspace
from test_agent_tools_generic import SELF, adapter, deny_mode, loop, pin_ctx  # noqa: F401

from hermes_linear import agent_tools, directory, tool_approval, tools_project, tools_read
from hermes_linear.agent_tools import TOOL_HANDLERS, TOOL_SCHEMAS
from hermes_linear.linear_client import LinearError
from hermes_linear.tool_approval import digest

CHILD, OTHER = "uuid-child", "uuid-other"
TOOL = "linear_project"
PID = PROJECT["id"]


@pytest.fixture
def ws():
    w = FakeWorkspace()
    w.add_issue("RET-1", title="Session issue", uuid=SELF)
    w.add_issue("RET-2", title="Child", uuid=CHILD, parent=SELF)
    w.add_issue("OTH-9", title="Unrelated", uuid=OTHER, project=None)
    return w


def linear_project(args):
    return TOOL_HANDLERS[TOOL](args)


def _calls(ws, name):
    return [c[1] for c in ws.calls if c[0] == name]


def _requests(adapter) -> int:
    return adapter._store.conn.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0]


def _request(adapter, args):
    return adapter._store.request("sk", digest(TOOL, args))


def _approve_and_rerun(adapter, args):
    """The human's "Allow once": arm the stored request, then the identical re-call."""
    assert adapter._store.arm_request("sk", digest(TOOL, args)) is not None
    return linear_project(dict(args))


def _settings(adapter, **kw):
    adapter._settings = dataclasses.replace(adapter._settings, **kw)


def _seed_updates(ws, n, project=PID, body=lambda i: f"update {i}"):
    ws.project_updates[project] = [
        {"id": f"pu-seed-{i}", "created_at": f"2026-11-{1 + i // 60:02d}T00:{i % 60:02d}:00.000Z",
         "author": "Neo", "health": "onTrack", "body": body(i)} for i in range(n)]


# -- registration


def test_schema_and_registration():
    s = TOOL_SCHEMAS[TOOL]
    assert s["name"] == TOOL and "Linear app" in s["description"]
    assert "APPROVAL_REQUIRED" in s["description"] and "PROJECT_NOT_FOUND" in s["description"]
    props = s["parameters"]["properties"]
    assert list(props) == ["action", "project", "updates", "body", "health", "milestone",
                           "name", "description", "target_date", "url", "label"]
    assert props["action"]["enum"] == ["get", "post_update", "milestone", "link"]
    assert props["health"]["enum"] == ["on_track", "at_risk", "off_track",
                                       "onTrack", "atRisk", "offTrack"]
    assert props["updates"]["type"] == "integer"
    assert props["description"]["type"] == ["string", "null"]
    assert props["target_date"]["type"] == ["string", "null"]
    assert all(props[k]["type"] == "string" for k in props
               if k not in {"updates", "description", "target_date"})
    assert all(props[k].get("description") for k in props)
    assert s["parameters"]["required"] == ["action"]
    assert agent_tools.linear_project is TOOL_HANDLERS[TOOL]
    assert tools_project.HANDLERS == {TOOL: TOOL_HANDLERS[TOOL]}
    assert TOOL in agent_tools.__all__ and TOOL in tool_approval.GATED_TOOLS
    assert tool_approval.FIELD_TYPES[TOOL] == {
        "action": "string", "project": "string", "updates": "integer", "body": "string",
        "health": "string", "milestone": "string", "name": "string",
        "description": "string|null", "target_date": "string|null", "url": "string",
        "label": "string"}
    assert tools_project.PROJECT_UPDATE_BODY_MAX == 4_000
    assert tools_project.PROJECT_UPDATES_DEFAULT == 3
    assert tools_project.PROJECT_UPDATES_MAX == 20
    assert list(TOOL_SCHEMAS)[-2:] == [TOOL, "linear_document"]


# -- get


def test_get_defaults_to_session_project(pin_ctx, ws, adapter):
    _seed_updates(ws, 5)
    out = linear_project({"action": "get"})
    assert out["ok"] is True, out
    p = out["project"]
    assert (p["id"], p["name"], p["state"]) == (PID, "Hermes", "In Progress")
    assert p["links"] == [{"id": "link-0", "label": "Spec", "url": "https://example.test/spec"}]
    assert [u["id"] for u in p["updates"]] == ["pu-seed-4", "pu-seed-3", "pu-seed-2"]
    assert _calls(ws, "issue_get") == [SELF]  # the session ticket's project
    assert _calls(ws, "project_detail") == [(PID, 3)]
    # an explicit project (name or UUID) wins over the session's
    ws.add_project("proj-2", "Orion")
    for ref in ("Orion", "orion", "proj-2"):
        got = linear_project({"action": "get", "project": ref})
        assert got["ok"] is True and got["project"]["id"] == "proj-2", ref
    assert ws.writes() == [] and _requests(adapter) == 0


def test_get_outside_uses_default_project(pin_ctx, ws, adapter):
    ws.add_project("proj-2", "Orion")
    pin_ctx(linear=False)
    _settings(adapter, default_team="RET", default_project="Orion",
              outside_session_writes="deny")  # reads work even when writes are denied
    out = linear_project({"action": "get"})
    assert out["ok"] is True and out["project"]["name"] == "Orion", out
    assert _calls(ws, "issue_get") == []
    assert linear_project({"action": "get", "project": "Hermes"})["project"]["id"] == PID
    # a configured default that misses keeps the directory's code and names the setting
    _settings(adapter, default_project="Nope")
    miss = linear_project({"action": "get"})
    assert miss["error"] == "PROJECT_NOT_FOUND" and miss["candidates"], miss
    assert miss["message"] == 'default_project "Nope" not found'
    assert ws.writes() == [] and _requests(adapter) == 0


def test_get_no_project_not_found_with_candidates(pin_ctx, ws, adapter):
    for n in range(25):
        ws.add_project(f"proj-x{n}", f"Extra {n}")
    # a session ticket without a project
    pin_ctx(issue=OTHER)
    out = linear_project({"action": "get"})
    assert out["ok"] is False and out["error"] == "PROJECT_NOT_FOUND", out
    assert len(out["candidates"]) == directory.CANDIDATES_MAX
    assert out["candidates"][0] == {"id": PID, "name": "Hermes"}
    # outside a session, no default_project
    pin_ctx(linear=False)
    out = linear_project({"action": "get"})
    assert out["error"] == "PROJECT_NOT_FOUND" and len(out["candidates"]) == 20
    # a Linear call whose ticket was not found: no session project either
    pin_ctx(issue=None)
    assert linear_project({"action": "get"})["error"] == "PROJECT_NOT_FOUND"
    # an unknown name
    pin_ctx()
    out = linear_project({"action": "get", "project": "Extra 7x"})
    assert out["error"] == "PROJECT_NOT_FOUND" and out["candidates"]
    assert _calls(ws, "project_detail") == [] and ws.writes() == [] and _requests(adapter) == 0


def test_get_updates_default_3_max_20(pin_ctx, ws, adapter):
    _seed_updates(ws, 25)
    assert len(linear_project({"action": "get"})["project"]["updates"]) == 3
    got = linear_project({"action": "get", "updates": 20})["project"]["updates"]
    assert [u["id"] for u in got] == [f"pu-seed-{i}" for i in range(24, 4, -1)]
    none = linear_project({"action": "get", "updates": 0})["project"]
    assert none["updates"] == []
    assert linear_project({"action": "get", "updates": None})["ok"] is True  # default
    assert _calls(ws, "project_detail") == [(PID, 3), (PID, 20), (PID, 0), (PID, 3)]
    before = len(ws.calls)
    for bad in (21, -1, True, "5", 2.5, [3]):
        assert linear_project({"action": "get", "updates": bad}) == {
            "ok": False, "error": "BAD_ARGS"}, bad
    assert len(ws.calls) == before


def test_get_cuts_update_bodies(pin_ctx, ws):
    """Review Focus 5: each update body is cut at 4,000 chars, flagged ``body_truncated``."""
    _seed_updates(ws, 3, body=lambda i: ["x" * 4_001, "y" * 4_000, "short"][i])
    ups = linear_project({"action": "get"})["project"]["updates"]
    assert [u["body"] for u in ups] == ["short", "y" * 4_000, "x" * 4_000]
    assert [u.get("body_truncated") for u in ups] == [None, None, True]


def test_get_result_capped_like_issue_get(pin_ctx, ws):
    """The whole result is capped as ``linear_issue_get``'s: the oldest updates go first."""
    _seed_updates(ws, 20, body=lambda i: f"{i:02d}" + "z" * 3_998)
    out = linear_project({"action": "get", "updates": 20})
    p = out["project"]
    assert p["updates_truncated"] is True
    assert len(json.dumps(out, ensure_ascii=False)) <= tools_read.ISSUE_GET_MAX_CHARS
    ids = [u["id"] for u in p["updates"]]
    assert ids == [f"pu-seed-{i}" for i in range(19, 19 - len(ids), -1)] and 0 < len(ids) < 20
    small = linear_project({"action": "get"})["project"]
    assert "updates_truncated" not in small


def test_get_never_asks(pin_ctx, ws, adapter):
    ws.add_project("proj-2", "Orion")
    for args in ({"action": "get"}, {"action": "get", "project": "Orion"},
                 {"action": "get", "project": "Hermes", "updates": 5}):
        assert linear_project(dict(args))["ok"] is True
        assert _request(adapter, args) is None
    pin_ctx(linear=False)
    _settings(adapter, outside_session_writes="approve")
    assert linear_project({"action": "get", "project": "Orion"})["ok"] is True
    assert _requests(adapter) == 0 and ws.writes() == []


def test_get_linear_failure_keeps_code_and_message(pin_ctx, ws, monkeypatch):
    async def boom(*a, **k):
        raise LinearError("Entity not found: Project", code="INPUT_ERROR",
                          detail="Could not find referenced Project.")

    monkeypatch.setattr(ws, "project_detail", boom)
    assert linear_project({"action": "get"}) == {
        "ok": False, "error": "INPUT_ERROR", "message": "Could not find referenced Project."}


# -- post_update


def test_post_update_always_asks(pin_ctx, ws, adapter):
    """Q3: a project update always asks, the session ticket's own project included."""
    ws.add_project("proj-2", "Orion")
    for args, pid, name in (
            ({"action": "post_update", "body": "Weekly: on track"}, PID, "Hermes"),
            ({"action": "post_update", "body": "Orion news", "project": "Orion"},
             "proj-2", "Orion")):
        n = len(_calls(ws, "project_update_create"))
        out = linear_project(dict(args))
        assert out["ok"] is False and out["error"] == "APPROVAL_REQUIRED", out
        assert name in out["message"] and TOOL in out["message"]
        assert len(_calls(ws, "project_update_create")) == n  # the first pass wrote nothing
        req = _request(adapter, args)
        assert req.targets == [pid] and req.tool == TOOL
        assert f'\n- project "{name}"\n' in req.message
        out = _approve_and_rerun(adapter, args)
        made = _calls(ws, "project_update_create")[-1]
        assert made == (pid, args["body"], None)
        uid = ws.project_updates[pid][-1]["id"]
        assert out == {"ok": True, "update_id": uid,
                       "url": f"https://linear.app/x/project/{pid}/updates#{uid}"}
        assert _request(adapter, args) is None
    assert len(_calls(ws, "project_update_create")) == 2
    # deny mode: refused, nothing stored or written
    deny_mode(adapter)
    out = linear_project({"action": "post_update", "body": "again"})
    assert out == {"ok": False, "error": "OUT_OF_SCOPE"}
    assert len(ws.writes()) == 2


@pytest.mark.parametrize("given, sent", [
    ("on_track", "onTrack"), ("at_risk", "atRisk"), ("off_track", "offTrack"),
    ("onTrack", "onTrack"), ("atRisk", "atRisk"), ("offTrack", "offTrack"), (None, None)])
def test_post_update_health_map(pin_ctx, ws, adapter, given, sent):
    _settings(adapter, out_of_scope_writes="allow")
    out = linear_project({"action": "post_update", "body": "b", "health": given})
    assert out["ok"] is True, out
    assert _calls(ws, "project_update_create") == [(PID, "b", sent)]


@pytest.mark.parametrize("health", ["great", "", "ON_TRACK", "on track", 1, True])
def test_post_update_bad_health(pin_ctx, ws, adapter, health):
    out = linear_project({"action": "post_update", "body": "b", "health": health})
    assert out == {"ok": False, "error": "BAD_ARGS"}
    assert ws.calls == [] and _requests(adapter) == 0


@pytest.mark.parametrize("extra", [{}, {"body": ""}, {"body": "  \n"}, {"body": None},
                                   {"body": 5}, {"health": "on_track"}])
def test_post_update_requires_body(pin_ctx, ws, adapter, extra):
    out = linear_project({"action": "post_update", **extra})
    assert out == {"ok": False, "error": "BAD_ARGS"}
    assert ws.calls == [] and _requests(adapter) == 0


def test_post_update_outside_deny(pin_ctx, ws, adapter):
    pin_ctx(linear=False)
    _settings(adapter, default_team="RET", default_project="Hermes",
              outside_session_writes="deny")
    out = linear_project({"action": "post_update", "body": "from Slack"})
    assert out == {"ok": False, "error": "NOT_LINEAR_SESSION"}
    assert ws.calls == [] and _requests(adapter) == 0
    # approve: asks in the calling chat for the default project, approved re-call posts once
    _settings(adapter, outside_session_writes="approve")
    args = {"action": "post_update", "body": "from Slack", "health": "at_risk"}
    out = linear_project(dict(args))
    assert out["error"] == "APPROVAL_REQUIRED" and ws.writes() == []
    assert _request(adapter, args).targets == [PID]
    out = _approve_and_rerun(adapter, args)
    assert out["ok"] is True
    assert ws.writes() == [("project_update_create", (PID, "from Slack", "atRisk"))]


def test_post_update_linear_call_without_ticket_refused(pin_ctx, ws, adapter):
    pin_ctx(issue=None)
    out = linear_project({"action": "post_update", "body": "b", "project": "Hermes"})
    assert out == {"ok": False, "error": "NOT_LINEAR_SESSION"}
    assert ws.writes() == [] and _requests(adapter) == 0


def test_post_update_no_project_not_found(pin_ctx, ws, adapter):
    pin_ctx(issue=OTHER)
    out = linear_project({"action": "post_update", "body": "b"})
    assert out["error"] == "PROJECT_NOT_FOUND" and out["candidates"]
    assert ws.writes() == [] and _requests(adapter) == 0


def test_post_update_linear_failure_keeps_code_and_message(pin_ctx, ws, adapter, monkeypatch):
    async def boom(*a, **k):
        raise LinearError("body too long", code="INVALID_INPUT")

    monkeypatch.setattr(ws, "project_update_create", boom)
    _settings(adapter, out_of_scope_writes="allow")
    out = linear_project({"action": "post_update", "body": "b"})
    assert out == {"ok": False, "error": "INVALID_INPUT", "message": "body too long"}


# -- milestone


def _seed_milestones(ws, *names, project=PID):
    rows = ws.milestones_rows.setdefault(project, [])
    for n in names:
        mid = f"ms-{n.lower()}"
        rows.append({"id": mid, "name": n})
        ws.milestone_meta[mid] = {"project": project, "description": None,
                                  "targetDate": "2026-12-31"}


def test_milestone_create_asks_then_creates(pin_ctx, ws, adapter):
    """Q3: a milestone always asks, the session ticket's own project included; the first
    pass writes nothing; the approved re-call creates once."""
    args = {"action": "milestone", "name": " Beta ", "description": "Second cut",
            "target_date": "2026-12-01"}
    out = linear_project(dict(args))
    assert out["ok"] is False and out["error"] == "APPROVAL_REQUIRED", out
    assert _calls(ws, "milestone_create") == [] and ws.writes() == []
    req = _request(adapter, args)
    assert req.targets == [PID] and '\n- project "Hermes"\n' in req.message
    out = _approve_and_rerun(adapter, args)
    mid = ws.milestones_rows[PID][-1]["id"]
    assert out == {"ok": True, "milestone_id": mid, "name": "Beta",
                   "target_date": "2026-12-01", "created": True}
    assert ws.writes() == [("milestone_create", (PID, "Beta", "Second cut", "2026-12-01"))]
    # on create a null (or blank) description / target_date is simply left out
    _settings(adapter, out_of_scope_writes="allow")
    out = linear_project({"action": "milestone", "name": "Gamma", "description": None,
                          "target_date": None})
    assert out["ok"] is True and out["target_date"] is None, out
    assert _calls(ws, "milestone_create")[-1] == (PID, "Gamma", None, None)
    assert linear_project({"action": "milestone", "name": "Delta", "description": "  ",
                           "target_date": ""})["ok"] is True
    assert _calls(ws, "milestone_create")[-1] == (PID, "Delta", None, None)


def test_milestone_new_name_resolves_after_create(pin_ctx, ws, adapter):
    """The directory has no invalidate API: a name made after the project's milestones were
    cached resolves through the directory's refetch-once on a miss."""
    _seed_milestones(ws, "Alpha")
    _settings(adapter, out_of_scope_writes="allow")
    assert linear_project({"action": "milestone", "milestone": "Alpha",
                           "name": "Alpha"})["ok"] is True  # caches [Alpha]
    assert linear_project({"action": "milestone", "name": "Beta"})["created"] is True
    out = linear_project({"action": "milestone", "milestone": "Beta",
                          "target_date": "2027-01-15"})
    assert out == {"ok": True, "milestone_id": ws.milestones_rows[PID][-1]["id"],
                   "name": "Beta", "target_date": "2027-01-15", "created": False}
    assert _calls(ws, "milestones") == [PID, PID]  # the miss refetched once
    # a rename resolves by its new name the same way
    assert linear_project({"action": "milestone", "milestone": "Alpha",
                           "name": "Alpha 2"})["ok"] is True
    assert linear_project({"action": "milestone", "milestone": "Alpha 2",
                           "description": "d"})["ok"] is True


@pytest.mark.parametrize("extra", [{}, {"name": ""}, {"name": "  "}, {"name": None},
                                   {"name": 5}, {"description": "only a description"},
                                   {"target_date": "2026-12-01"}])
def test_milestone_create_requires_name(pin_ctx, ws, adapter, extra):
    out = linear_project({"action": "milestone", **extra})
    assert out == {"ok": False, "error": "BAD_ARGS"}
    assert ws.calls == [] and _requests(adapter) == 0


def test_milestone_update_by_name_and_uuid(pin_ctx, ws, adapter):
    _seed_milestones(ws, "Alpha", "Beta")
    args = {"action": "milestone", "milestone": "alpha", "name": "Alpha v2"}
    out = linear_project(dict(args))
    assert out["error"] == "APPROVAL_REQUIRED" and ws.writes() == []
    assert _request(adapter, args).targets == [PID]
    out = _approve_and_rerun(adapter, args)
    assert out == {"ok": True, "milestone_id": "ms-alpha", "name": "Alpha v2",
                   "target_date": "2026-12-31", "created": False}
    _settings(adapter, out_of_scope_writes="allow")
    out = linear_project({"action": "milestone", "milestone": " ms-beta ",
                          "description": "Beta notes", "target_date": "2027-02-01"})
    assert out["ok"] is True and out["milestone_id"] == "ms-beta", out
    assert ws.writes() == [
        ("milestone_update", ("ms-alpha", {"name": "Alpha v2"})),
        ("milestone_update", ("ms-beta", {"description": "Beta notes",
                                          "targetDate": "2027-02-01"}))]
    # in another project, given by name
    ws.add_project("proj-2", "Orion")
    ws.milestones_rows["proj-2"] = [{"id": "ms-orion-alpha", "name": "Alpha"}]
    ws.milestone_meta["ms-orion-alpha"] = {"project": "proj-2", "description": None,
                                           "targetDate": None}
    out = linear_project({"action": "milestone", "project": "Orion", "milestone": "Alpha",
                          "name": "Orion Alpha"})
    assert out["milestone_id"] == "ms-orion-alpha", out


@pytest.mark.parametrize("extra", [{}, {"name": None}, {"name": ""}, {"name": "  "},
                                   {"name": 3}, {"description": 3}, {"target_date": 3}])
def test_milestone_update_needs_a_field(pin_ctx, ws, adapter, extra):
    _seed_milestones(ws, "Alpha")
    out = linear_project({"action": "milestone", "milestone": "Alpha", **extra})
    assert out == {"ok": False, "error": "BAD_ARGS"}
    assert ws.calls == [] and _requests(adapter) == 0


@pytest.mark.parametrize("ref", ["", "  ", 5])
def test_milestone_bad_ref(pin_ctx, ws, adapter, ref):
    out = linear_project({"action": "milestone", "milestone": ref, "name": "x"})
    assert out == {"ok": False, "error": "BAD_ARGS"}
    assert ws.calls == []


def test_milestone_update_null_clears(pin_ctx, ws, adapter, monkeypatch):
    """``target_date: null`` (or the raw ``"null"`` Hermes coerces to ``None``) clears the
    date: ``targetDate: None`` is sent. ``description: null`` can't clear: Linear leaves a
    description unchanged on null or "" (RET-166 probe, spec §7), so it is ``BAD_ARGS``
    with a message instead of a success that changed nothing."""
    import copy

    from tools.arg_coercion import coerce_tool_args
    from tools.registry import registry

    _seed_milestones(ws, "Alpha")
    _settings(adapter, out_of_scope_writes="allow")
    real_get_schema = registry.get_schema
    monkeypatch.setattr(registry, "get_schema", lambda name: copy.deepcopy(
        TOOL_SCHEMAS[name]) if name == TOOL else real_get_schema(name))
    raw = {"action": "milestone", "milestone": "Alpha", "target_date": "null"}
    coerced = coerce_tool_args(TOOL, copy.deepcopy(raw))
    assert coerced["target_date"] is None
    assert digest(TOOL, raw) == digest(TOOL, coerced)
    out = linear_project(coerced)
    assert out == {"ok": True, "milestone_id": "ms-alpha", "name": "Alpha",
                   "target_date": None, "created": False}
    assert ws.writes() == [("milestone_update", ("ms-alpha", {"targetDate": None}))]
    assert ws.milestone_meta["ms-alpha"]["targetDate"] is None
    for desc in (None, "", "  "):
        out = linear_project({"action": "milestone", "milestone": "Alpha",
                              "description": desc, "name": "Alpha"})
        assert out["ok"] is False and out["error"] == "BAD_ARGS", desc
        assert "description" in out["message"]
    assert len(ws.writes()) == 1


def test_milestone_not_found_candidates(pin_ctx, ws, adapter):
    _seed_milestones(ws, "Alpha", "Beta")
    out = linear_project({"action": "milestone", "milestone": "Gamma", "name": "x"})
    assert out == {"ok": False, "error": "MILESTONE_NOT_FOUND",
                   "candidates": [{"id": "ms-alpha", "name": "Alpha"},
                                  {"id": "ms-beta", "name": "Beta"}]}
    assert _calls(ws, "milestones") == [PID]  # fetched by this call, so no second fetch
    # scoped to the project: Orion's milestones are not Hermes's
    ws.add_project("proj-2", "Orion")
    out = linear_project({"action": "milestone", "project": "Orion", "milestone": "Alpha",
                          "name": "x"})
    assert out == {"ok": False, "error": "MILESTONE_NOT_FOUND", "candidates": []}
    assert ws.writes() == [] and _requests(adapter) == 0


@pytest.mark.parametrize("date", ["2026-W05-1", "2026-13-01", "2026-02-30", "20261201",
                                  "2026-1-1", "2026-12-01T00:00", 20261201, True])
def test_milestone_bad_target_date(pin_ctx, ws, adapter, date):
    _seed_milestones(ws, "Alpha")
    for args in ({"action": "milestone", "name": "New", "target_date": date},
                 {"action": "milestone", "milestone": "Alpha", "target_date": date}):
        assert linear_project(args) == {"ok": False, "error": "BAD_ARGS"}, args
    assert ws.calls == [] and _requests(adapter) == 0


def test_milestone_duplicate_name_already_exists(pin_ctx, ws, adapter, monkeypatch):
    """Linear refuses a duplicate name only on write (after the gate): ``ALREADY_EXISTS``
    with Linear's userPresentableMessage and the existing milestone's id."""
    _seed_milestones(ws, "Alpha")
    args = {"action": "milestone", "name": "Alpha"}
    assert linear_project(dict(args))["error"] == "APPROVAL_REQUIRED"
    out = _approve_and_rerun(adapter, args)
    assert out == {"ok": False, "error": "ALREADY_EXISTS", "milestone_id": "ms-alpha",
                   "message": "A project milestone called 'Alpha' already exists on this "
                              "project."}
    assert len(ws.milestones_rows[PID]) == 1
    # a rename onto a taken name answers the same; without a detail, Linear's message
    _settings(adapter, out_of_scope_writes="allow")
    _seed_milestones(ws, "Beta")

    async def taken(*a, **k):
        raise LinearError("name not unique", code="INPUT_ERROR")

    monkeypatch.setattr(ws, "milestone_update", taken)
    out = linear_project({"action": "milestone", "milestone": "Beta", "name": "Alpha"})
    assert out == {"ok": False, "error": "ALREADY_EXISTS", "milestone_id": "ms-alpha",
                   "message": "name not unique"}

    # any other Linear failure keeps its code and message
    async def other(*a, **k):
        raise LinearError("bad date", code="INPUT_ERROR", detail="Target date is invalid.")

    monkeypatch.setattr(ws, "milestone_update", other)
    out = linear_project({"action": "milestone", "milestone": "Beta", "name": "B"})
    assert out == {"ok": False, "error": "INPUT_ERROR", "message": "Target date is invalid."}


def test_milestone_and_link_outside(pin_ctx, ws, adapter):
    pin_ctx(linear=False)
    _settings(adapter, default_team="RET", default_project="Hermes",
              outside_session_writes="deny")
    for args in ({"action": "milestone", "name": "M"},
                 {"action": "link", "url": "https://example.test/repo"}):
        assert linear_project(dict(args)) == {"ok": False, "error": "NOT_LINEAR_SESSION"}
    assert ws.calls == [] and _requests(adapter) == 0
    _settings(adapter, outside_session_writes="approve")
    for args in ({"action": "milestone", "name": "M"},
                 {"action": "link", "url": "https://example.test/repo"}):
        assert linear_project(dict(args))["error"] == "APPROVAL_REQUIRED"
        assert _request(adapter, args).targets == [PID]
        assert _approve_and_rerun(adapter, args)["ok"] is True
    assert [w[0] for w in ws.writes()] == ["milestone_create", "project_link_create"]
    # a Linear call without its ticket is refused as post_update is
    pin_ctx(issue=None)
    for args in ({"action": "milestone", "name": "N", "project": "Hermes"},
                 {"action": "link", "url": "https://x.test", "project": "Hermes"}):
        assert linear_project(args) == {"ok": False, "error": "NOT_LINEAR_SESSION"}


# -- link


def test_link_asks_then_links(pin_ctx, ws, adapter):
    args = {"action": "link", "url": " https://github.com/masha256/hermes-linear ",
            "label": "repo"}
    out = linear_project(dict(args))
    assert out["ok"] is False and out["error"] == "APPROVAL_REQUIRED", out
    assert _calls(ws, "project_link_create") == [] and ws.writes() == []
    req = _request(adapter, args)
    assert req.targets == [PID] and '\n- project "Hermes"\n' in req.message
    out = _approve_and_rerun(adapter, args)
    lid = ws.project_links[PID][-1]["id"]
    assert out == {"ok": True, "link_id": lid}
    assert ws.writes() == [("project_link_create",
                            (PID, "https://github.com/masha256/hermes-linear", "repo"))]
    # without a label (or a blank one) the client sends the URL as the label
    _settings(adapter, out_of_scope_writes="allow")
    for i, label in enumerate((None, "  ")):
        url = f"http://example.test/other/{i}"
        assert linear_project({"action": "link", "url": url, "label": label})["ok"] is True
        assert _calls(ws, "project_link_create")[-1] == (PID, url, None)
        assert ws.project_links[PID][-1]["label"] == url


def test_link_duplicate_url_whitespace(pin_ctx, ws, adapter):
    """Review Focus 4: a link with the same URL (both sides stripped, compared exactly) is
    ``ALREADY_LINKED`` with its ``link_id`` before the gate: no request, no card, no write."""
    ws.project_links[PID].append({"id": "link-ws", "label": "padded",
                                  "url": "  https://example.test/padded\n"})
    for url, lid in (("https://example.test/spec", "link-0"),
                     ("\t https://example.test/spec \n", "link-0"),
                     ("https://example.test/padded", "link-ws")):
        out = linear_project({"action": "link", "url": url, "label": "again"})
        assert out == {"ok": False, "error": "ALREADY_LINKED", "link_id": lid}, url
    assert _requests(adapter) == 0 and ws.writes() == []
    # outside, approve mode: still no card for a duplicate
    pin_ctx(linear=False)
    _settings(adapter, default_team="RET", default_project="Hermes",
              outside_session_writes="approve")
    out = linear_project({"action": "link", "url": "https://example.test/spec"})
    assert out["error"] == "ALREADY_LINKED" and _requests(adapter) == 0
    # exact: another case or a trailing slash is a different URL, which asks
    pin_ctx()
    for url in ("https://example.test/Spec", "https://example.test/spec/"):
        assert linear_project({"action": "link", "url": url})["error"] == "APPROVAL_REQUIRED"
    assert ws.writes() == []


@pytest.mark.parametrize("url", ["javascript:x", "ftp://x", "http://", "https://",
                                 "https://a b", "example.com", "mailto:a@b.test",
                                 "", "  ", None, 5, "HTTPS://x.test"])
def test_link_rejects_non_http(pin_ctx, ws, adapter, url):
    out = linear_project({"action": "link", "url": url})
    assert out == {"ok": False, "error": "BAD_ARGS"}
    assert ws.calls == [] and _requests(adapter) == 0


@pytest.mark.parametrize("label", [5, True, ["a"]])
def test_link_bad_label(pin_ctx, ws, adapter, label):
    out = linear_project({"action": "link", "url": "https://x.test", "label": label})
    assert out == {"ok": False, "error": "BAD_ARGS"} and ws.calls == []


def test_link_never_sends_initiative_or_release(pin_ctx, ws, adapter):
    from hermes_linear import linear_client

    _settings(adapter, out_of_scope_writes="allow")
    assert linear_project({"action": "link", "url": "https://x.test/a",
                           "label": "a"})["ok"] is True
    assert _calls(ws, "project_link_create") == [(PID, "https://x.test/a", "a")]
    q = linear_client.MUTATION_PROJECT_LINK_CREATE
    assert "initiativeId" not in q and "releaseId" not in q
    # neither is an argument of the tool, so neither can reach the client
    props = TOOL_SCHEMAS[TOOL]["parameters"]["properties"]
    assert not {"initiative", "initiativeId", "release", "releaseId"} & set(props)
    for extra in ({"initiativeId": "i-1"}, {"release": "r-1"}):
        out = linear_project({"action": "link", "url": "https://x.test/b", **extra})
        assert out == {"ok": False, "error": "BAD_ARGS"}
    assert len(_calls(ws, "project_link_create")) == 1


def test_link_linear_failure_keeps_code_and_message(pin_ctx, ws, adapter, monkeypatch):
    async def boom(*a, **k):
        raise LinearError("url invalid", code="INVALID_INPUT", detail="URL is invalid.")

    monkeypatch.setattr(ws, "project_link_create", boom)
    _settings(adapter, out_of_scope_writes="allow")
    out = linear_project({"action": "link", "url": "https://x.test/c"})
    assert out == {"ok": False, "error": "INVALID_INPUT", "message": "URL is invalid."}


def test_schema_describes_milestone_and_link():
    """Task 3 review: the "not available yet" sentence is gone; milestone and link are
    described with their refusals and the label default."""
    s = TOOL_SCHEMAS[TOOL]["description"]
    assert "not available yet" not in s
    for want in ("milestone", "ALREADY_EXISTS", "MILESTONE_NOT_FOUND", "link",
                 "ALREADY_LINKED", "label defaults to the URL", "YYYY-MM-DD"):
        assert want in s, want
    assert not hasattr(tools_project, "_NOT_YET")


# -- argument shape


@pytest.mark.parametrize("args", [
    {}, {"action": "nope"}, {"action": None}, {"action": 1},
    {"action": "get", "body": "x"},
    {"action": "get", "health": "on_track"},
    {"action": "get", "url": "https://x.test"},
    {"action": "get", "unknown": 1},
    {"action": "get", "project": ""},
    {"action": "get", "project": "  "},
    {"action": "get", "project": 5},
    {"action": "post_update", "body": "b", "updates": 3},
    {"action": "post_update", "body": "b", "name": "x"},
    {"action": "post_update", "body": "b", "target_date": "2026-12-01"},
    {"action": "milestone", "name": "M1", "body": "x"},
    {"action": "milestone", "name": "M1", "url": "https://example.test/repo"},
    {"action": "milestone", "name": "M1", "updates": 3},
    {"action": "milestone", "name": "M1", "health": "on_track"},
    {"action": "link", "url": "https://example.test/repo", "name": "repo"},
    {"action": "link", "url": "https://example.test/repo", "target_date": "2026-12-01"},
    {"action": "link", "url": "https://example.test/repo", "milestone": "Alpha"},
    {"action": "link", "url": "https://example.test/repo", "initiative": "i-1"},
    {"action": "link", "url": "https://example.test/repo", "releaseId": "r-1"},
])
def test_foreign_arg_bad_args(pin_ctx, ws, adapter, args):
    assert linear_project(args) == {"ok": False, "error": "BAD_ARGS"}
    assert ws.calls == [] and _requests(adapter) == 0


def test_null_foreign_arg_counts_as_absent(pin_ctx, ws):
    """As ``linear_document``: a null argument carries no input, so it is not foreign."""
    assert linear_project({"action": "get", "body": None, "target_date": None})["ok"] is True
    assert linear_project({"action": "link", "url": "https://example.test/spec",
                           "name": None, "target_date": None})["error"] == "ALREADY_LINKED"


@pytest.mark.parametrize("args", [None, [], "get", 3])
def test_non_dict_args_never_raise(pin_ctx, ws, args):
    assert linear_project(args) == {"ok": False, "error": "BAD_ARGS"}
