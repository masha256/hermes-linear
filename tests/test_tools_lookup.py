"""linear_lookup (RET-121 spec §1.7; RET-143) through the real registry.

``FakeWorkspace`` (``ws``): RET-1 is the session issue SELF in team RET (``TEAM``) and
project Hermes (``PROJECT``); team OTH and project Other exist besides. The directory is
per client, so every test starts with an empty cache.
"""

from __future__ import annotations

import pytest

from test_agent_tools_generic import SELF, adapter, loop, pin_ctx  # noqa: F401
from fake_workspace import PROJECT, TEAM, FakeWorkspace

from hermes_linear import agent_tools, tool_approval, tools_lookup
from hermes_linear.agent_tools import TOOL_HANDLERS, TOOL_SCHEMAS
from hermes_linear.linear_client import LinearError

OTHER_TEAM, OTHER_PROJECT = "team-2", "proj-2"


@pytest.fixture
def ws():
    w = FakeWorkspace()
    w.add_issue("RET-1", title="Session issue", uuid=SELF)
    w.add_team(OTHER_TEAM, "OTH", "Other team")
    w.add_project(OTHER_PROJECT, "Other")
    w.states[OTHER_TEAM] = [{"id": "st-oth", "name": "Open", "type": "unstarted"}]
    w.cycles_rows[TEAM] = [
        {"id": "cy-1", "number": 1, "name": "Sprint 1", "startsAt": "2026-11-01T00:00:00.000Z",
         "endsAt": "2026-11-15T00:00:00.000Z", "current": True},
        {"id": "cy-2", "number": 2, "name": "", "startsAt": "2026-11-15T00:00:00.000Z",
         "endsAt": "2026-11-29T00:00:00.000Z", "next": True}]
    w.milestones_rows[PROJECT["id"]] = [{"id": "ms-1", "name": "Beta"}]
    w.milestones_rows[OTHER_PROJECT] = [{"id": "ms-9", "name": "GA"}]
    return w


def lookup(args):
    return TOOL_HANDLERS["linear_lookup"](args)


def _fetches(ws, kind):
    return [c[1] for c in ws.calls if c[0] == kind]


# -- registration


def test_schema_and_registration():
    s = TOOL_SCHEMAS["linear_lookup"]
    assert s["name"] == "linear_lookup" and "Linear app" in s["description"]
    assert "NOT_LINEAR_SESSION" in s["description"] and "truncated" in s["description"]
    props = s["parameters"]["properties"]
    assert list(props) == ["kind", "team", "project", "query"]
    assert props["kind"]["enum"] == list(tools_lookup.LOOKUP_KINDS)
    assert all(props[k]["type"] == "string" and props[k].get("description") for k in props)
    assert s["parameters"]["required"] == ["kind"]
    assert tools_lookup.LOOKUP_KINDS == ("teams", "states", "labels", "users", "cycles",
                                         "projects", "milestones")
    assert tools_lookup.LOOKUP_MAX_ROWS == 250
    assert agent_tools.linear_lookup is TOOL_HANDLERS["linear_lookup"]
    assert tools_lookup.HANDLERS == {"linear_lookup": TOOL_HANDLERS["linear_lookup"]}
    assert "linear_lookup" in agent_tools.__all__
    assert list(TOOL_SCHEMAS)[-1] == list(TOOL_HANDLERS)[-1] == "linear_lookup"


def test_not_gated():
    assert "linear_lookup" not in tool_approval.GATED_TOOLS
    assert "linear_lookup" not in tool_approval.FIELD_TYPES


# -- row shapes


def test_teams_rows(pin_ctx):
    out = lookup({"kind": "teams"})
    assert out == {"ok": True, "kind": "teams", "rows": [
        {"id": TEAM, "name": "Retail", "key": "RET"},
        {"id": OTHER_TEAM, "name": "Other team", "key": "OTH"}]}


def test_states_rows(pin_ctx, ws):
    out = lookup({"kind": "states"})
    assert out["ok"] is True and out["kind"] == "states"
    assert out["rows"][0] == {"id": "st-backlog", "name": "Backlog", "type": "backlog"}
    assert [r["name"] for r in out["rows"]] == ["Backlog", "Todo", "In Progress", "In Review",
                                               "Done", "Canceled"]
    assert _fetches(ws, "team_states") == [TEAM]


def test_labels_rows(pin_ctx):
    out = lookup({"kind": "labels"})
    assert out["rows"] == [{"id": "lb-bug", "name": "Bug"},
                           {"id": "lb-feature", "name": "Feature"},
                           {"id": "lb-plan", "name": "superpowers:plan"},
                           {"id": "lb-ws-blocked", "name": "Blocked"}]


def test_users_rows(pin_ctx):
    out = lookup({"kind": "users"})
    assert out["rows"] == [
        {"id": "app-user", "name": "Neo", "displayName": "neo", "app": True, "active": True},
        {"id": "human-1", "name": "Mike Smith", "displayName": "mike", "app": False,
         "active": True}]


def test_cycles_rows(pin_ctx, ws):
    out = lookup({"kind": "cycles"})
    assert out["rows"] == [
        {"id": "cy-1", "name": "Sprint 1", "number": 1, "startsAt": "2026-11-01T00:00:00.000Z",
         "endsAt": "2026-11-15T00:00:00.000Z"},
        {"id": "cy-2", "name": "", "number": 2, "startsAt": "2026-11-15T00:00:00.000Z",
         "endsAt": "2026-11-29T00:00:00.000Z"}]
    assert _fetches(ws, "cycles") == [TEAM]


def test_projects_rows(pin_ctx):
    out = lookup({"kind": "projects"})
    assert out["rows"] == [{"id": PROJECT["id"], "name": "Hermes", "state": "started"},
                           {"id": OTHER_PROJECT, "name": "Other", "state": "planned"}]


def test_milestones_rows(pin_ctx, ws):
    out = lookup({"kind": "milestones"})
    assert out == {"ok": True, "kind": "milestones", "rows": [{"id": "ms-1", "name": "Beta"}]}
    assert _fetches(ws, "milestones") == [PROJECT["id"]]


# -- team / project args and defaults


@pytest.mark.parametrize("ref", ["OTH", "oth", "Other team", OTHER_TEAM])
def test_explicit_team(pin_ctx, ws, ref):
    out = lookup({"kind": "states", "team": ref})
    assert out["rows"] == [{"id": "st-oth", "name": "Open", "type": "unstarted"}]
    assert _fetches(ws, "team_states") == [OTHER_TEAM]
    assert _fetches(ws, "issue_get") == []  # an explicit team needs no session ticket


@pytest.mark.parametrize("ref", ["Other", "other", OTHER_PROJECT])
def test_explicit_project(pin_ctx, ws, ref):
    out = lookup({"kind": "milestones", "project": ref})
    assert out["rows"] == [{"id": "ms-9", "name": "GA"}]
    assert _fetches(ws, "issue_get") == []


def test_blank_team_is_the_default(pin_ctx, ws):
    assert lookup({"kind": "labels", "team": "  "})["ok"] is True
    assert _fetches(ws, "team_labels") == [TEAM]


def test_session_ticket_without_project_has_no_milestones(pin_ctx, ws):
    ws.issues[SELF]["project"] = None
    assert lookup({"kind": "milestones"}) == {"ok": True, "kind": "milestones", "rows": []}
    assert _fetches(ws, "milestones") == []


def test_unknown_team(pin_ctx):
    out = lookup({"kind": "cycles", "team": "NOPE"})
    assert out["ok"] is False and out["error"] == "TEAM_NOT_FOUND"
    assert out["candidates"] == [{"id": TEAM, "key": "RET", "name": "Retail"},
                                 {"id": OTHER_TEAM, "key": "OTH", "name": "Other team"}]


def test_unknown_project(pin_ctx):
    out = lookup({"kind": "milestones", "project": "Nope"})
    assert out["ok"] is False and out["error"] == "PROJECT_NOT_FOUND"
    assert {"id": OTHER_PROJECT, "name": "Other"} in out["candidates"]


@pytest.mark.parametrize("kind", ["states", "labels", "cycles", "milestones"])
def test_no_session_needs_arg(pin_ctx, ws, kind):
    pin_ctx(linear=False)
    out = lookup({"kind": kind})
    assert out == {"ok": False, "error": "NOT_LINEAR_SESSION"}
    arg = {"project": "Hermes"} if kind == "milestones" else {"team": "RET"}
    assert lookup({"kind": kind, **arg})["ok"] is True


@pytest.mark.parametrize("kind", ["teams", "users", "projects"])
def test_unscoped_kinds_work_without_session(pin_ctx, kind):
    pin_ctx(linear=False)
    out = lookup({"kind": kind})
    assert out["ok"] is True and out["rows"]


# -- query and cap


@pytest.mark.parametrize("kind,query,names", [
    ("users", "MIKE", ["Mike Smith"]),          # name / displayName, case-insensitive
    ("users", "example.com", ["Mike Smith"]),   # email, although rows don't carry it
    ("users", "neo", ["Neo"]),
    ("teams", "ot", ["Other team"]),
    ("teams", "re", ["Retail"]),                # key RET and name Retail
    ("states", "prog", ["In Progress"]),
    ("labels", "BLOCK", ["Blocked"]),
    ("projects", "HERM", ["Hermes"]),
    ("users", "zzz", []),
])
def test_query(pin_ctx, kind, query, names):
    out = lookup({"kind": kind, "query": query})
    assert out["ok"] is True and [r["name"] for r in out["rows"]] == names


def test_cap(pin_ctx, ws):
    ws.users_rows = [{"id": f"u{i}", "name": f"User {i}", "displayName": f"u{i}",
                      "email": f"u{i}@x.test", "app": False, "active": True} for i in range(300)]
    out = lookup({"kind": "users"})
    assert len(out["rows"]) == tools_lookup.LOOKUP_MAX_ROWS == 250
    assert out["rows"][-1]["id"] == "u249" and out["truncated"] is True


def test_at_the_cap_not_truncated(pin_ctx, ws):
    ws.users_rows = [{"id": f"u{i}", "name": f"User {i}"} for i in range(250)]
    out = lookup({"kind": "users"})
    assert len(out["rows"]) == 250 and "truncated" not in out


def test_query_before_cap(pin_ctx, ws):
    ws.users_rows = [{"id": f"u{i}", "name": f"User {i}"} for i in range(300)] + [
        {"id": "late", "name": "Zed"}]
    assert [r["id"] for r in lookup({"kind": "users", "query": "zed"})["rows"]] == ["late"]


# -- cache


def test_lookup_uses_cache(pin_ctx, ws):
    """Within the TTL a lookup reads the directory's copy; a miss never refetches."""
    lookup({"kind": "users"})
    ws.users_rows.append({"id": "new", "name": "Newcomer"})
    assert lookup({"kind": "users", "query": "newcomer"})["rows"] == []
    assert lookup({"kind": "states"})["ok"] and lookup({"kind": "states"})["ok"]
    assert len(_fetches(ws, "users")) == 1 and len(_fetches(ws, "team_states")) == 1


def test_rows_are_copies(pin_ctx, ws):
    lookup({"kind": "teams"})["rows"][0]["name"] = "mutated"
    assert lookup({"kind": "teams"})["rows"][0]["name"] == "Retail"


# -- bad args and Linear errors


@pytest.mark.parametrize("args", [{}, {"kind": "issues"}, {"kind": None}, {"kind": 3},
                                  {"kind": "users", "query": 5}, {"kind": "states", "team": 1},
                                  {"kind": "milestones", "project": ["x"]}])
def test_bad_args(pin_ctx, ws, args):
    assert lookup(args) == {"ok": False, "error": "BAD_ARGS"}
    assert ws.calls == []


def test_linear_error(pin_ctx, ws):
    async def boom():
        raise LinearError("rate limited", code="RATELIMITED", detail="Too many requests")

    ws.users = boom
    out = lookup({"kind": "users"})
    assert out == {"ok": False, "error": "RATELIMITED", "message": "Too many requests"}


def test_linear_error_without_code(pin_ctx, ws):
    async def boom(team_id):
        raise LinearError("boom")

    ws.cycles = boom
    assert lookup({"kind": "cycles"}) == {"ok": False, "error": "LINEAR_ERROR",
                                          "message": "boom"}
