"""linear_project ``get`` / ``post_update`` (RET-122 spec §1.1, §2, §3; RET-165) through the
real registry.

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
    # Task 4 implements these; until then they are BAD_ARGS
    {"action": "milestone", "name": "M1"},
    {"action": "link", "url": "https://example.test/repo"},
])
def test_foreign_arg_bad_args(pin_ctx, ws, adapter, args):
    assert linear_project(args) == {"ok": False, "error": "BAD_ARGS"}
    assert ws.calls == [] and _requests(adapter) == 0


def test_null_foreign_arg_counts_as_absent(pin_ctx, ws):
    """As ``linear_document``: a null argument carries no input, so it is not foreign."""
    assert linear_project({"action": "get", "body": None, "target_date": None})["ok"] is True


@pytest.mark.parametrize("args", [None, [], "get", 3])
def test_non_dict_args_never_raise(pin_ctx, ws, args):
    assert linear_project(args) == {"ok": False, "error": "BAD_ARGS"}
