"""Generic Linear tools (RET-76) run against ``FakeWorkspace`` on a real background loop.

The handlers are called from the test thread, as a Hermes tool worker would call them; the
adapter's ``event_loop`` runs in a background thread (integration notes Q3). Context
resolution is Task 1's (tested there), so it is pinned here with ``pin_ctx``.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from test_agent_tools_runtime import new_adapter
from fake_workspace import PROJECT, TEAM, VIEWER, FakeWorkspace

from hermes_linear import agent_tools
from hermes_linear.agent_tools import (TOOL_HANDLERS, TOOL_SCHEMAS, linear_comment,
                                       linear_issue_create, linear_issue_get,
                                       linear_issue_update, resolve_state)

SELF = "uuid-self"


@pytest.fixture
def loop():
    lp = asyncio.new_event_loop()
    t = threading.Thread(target=lp.run_forever, name="GATEWAY-LOOP", daemon=True)
    t.start()
    yield lp
    lp.call_soon_threadsafe(lp.stop)
    t.join(5)
    lp.close()


@pytest.fixture
def ws():
    w = FakeWorkspace()
    w.add_issue("RET-1", title="Session issue", uuid=SELF)
    w.add_issue("RET-2", title="Child", uuid="uuid-child", parent=SELF)
    w.add_issue("OTH-9", title="Unrelated", uuid="uuid-other", project=None)
    return w


@pytest.fixture
def adapter(loop, ws):
    return new_adapter(loop, client=ws)


@pytest.fixture
def pin_ctx(monkeypatch, adapter):
    """Resolve every tool call to ``adapter`` in Linear session ``SELF`` (key ``sk``)."""
    def pin(*, linear=True, issue=SELF, key="sk", ad=adapter):
        ctx = agent_tools.ToolContext(adapter=ad, client=getattr(ad, "client", None),
                                      session_issue_id=issue if linear else None,
                                      session_key=key if linear else None, is_linear=linear)
        monkeypatch.setattr(agent_tools, "resolve_context", lambda session_id=None: ctx)
        return ctx
    pin()
    return pin


# ----------------------------------------------------------------- schemas / handlers


def test_schemas_and_handlers_pair_up():
    names = {"linear_issue_get", "linear_issue_create", "linear_issue_update", "linear_comment"}
    assert names <= set(TOOL_SCHEMAS) and names <= set(TOOL_HANDLERS)
    for name in names:
        s = TOOL_SCHEMAS[name]
        assert s["name"] == name
        assert "Linear app" in s["description"]
        assert s["parameters"]["type"] == "object"
        assert set(s["parameters"]["required"]) <= set(s["parameters"]["properties"])
        assert callable(TOOL_HANDLERS[name])


@pytest.mark.parametrize("handler", [linear_issue_get, linear_issue_create,
                                     linear_issue_update, linear_comment])
@pytest.mark.parametrize("args", [None, "x", {}, {"id": 5, "title": 3, "body": None}])
def test_bad_args_never_raise(pin_ctx, ws, handler, args):
    assert handler(args) == {"ok": False, "error": "BAD_ARGS"}
    assert ws.writes() == []


# ----------------------------------------------------------------- resolve_state


def test_resolve_state_name_then_type():
    states = [{"id": "a", "name": "Todo", "type": "unstarted"},
              {"id": "b", "name": "In Progress", "type": "started"},
              {"id": "c", "name": "Started", "type": "completed"},  # name beats type
              {"id": "d", "name": "In Review", "type": "started"}]
    assert resolve_state(states, "in progress") == "b"
    assert resolve_state(states, "STARTED") == "c"
    assert resolve_state(states, "unstarted") == "a"
    assert resolve_state(states, "nope") is None
    assert resolve_state(states, "") is None


# ----------------------------------------------------------------- linear_issue_get


def test_issue_get_shape(pin_ctx, ws):
    ws.add_issue("RET-3", title="Grandchild", parent="uuid-child", sort_order=2.0)
    ws.issues[SELF]["labels"] = ["lb-bug"]
    out = linear_issue_get({"id": "RET-1"})
    assert out["ok"] is True
    issue = out["issue"]
    assert issue["id"] == SELF and issue["identifier"] == "RET-1"
    assert issue["state"] == {"id": "st-todo", "name": "Todo", "type": "unstarted"}
    assert issue["team"] == {"id": TEAM}
    assert issue["project"]["externalLinks"] == PROJECT["externalLinks"]
    assert issue["labels"] == [{"id": "lb-bug", "name": "Bug"}]
    assert issue["parent"] is None
    assert [c["identifier"] for c in issue["children"]] == ["RET-2"]
    assert issue["children"][0]["state"] == {"type": "unstarted"}
    assert ws.writes() == []


def test_issue_get_needs_no_write_scope(pin_ctx, ws):
    pin_ctx(linear=False)
    out = linear_issue_get({"id": "OTH-9"})
    assert out["ok"] is True and out["issue"]["id"] == "uuid-other"


def test_issue_get_not_connected(pin_ctx, ws):
    pin_ctx(ad=new_adapter(None, client=ws))  # no running loop
    assert linear_issue_get({"id": "RET-1"}) == {"ok": False, "error": "NOT_CONNECTED"}
    pin_ctx(ad=None)
    assert linear_issue_get({"id": "RET-1"}) == {"ok": False, "error": "NOT_CONNECTED"}


def test_issue_get_unknown_is_linear_code(pin_ctx):
    assert linear_issue_get({"id": "NOPE-1"}) == {"ok": False, "error": "NOT_FOUND"}


# ----------------------------------------------------------------- linear_issue_create


def test_create_child_in_scope_records_created(pin_ctx, ws, adapter):
    out = linear_issue_create({"title": "Task A", "description": "do it", "parent": "RET-1",
                               "labels": ["bug"], "state": "In Progress"})
    assert out["ok"] is True
    new_id = out["issue"]["id"]
    stored = ws.issues[new_id]
    assert stored["parent"] == SELF  # identifier normalized to the UUID
    assert stored["team"] == TEAM and stored["project"] == PROJECT  # from the session issue
    assert stored["labels"] == ["lb-bug"] and stored["state"] == "st-progress"
    assert stored["description"] == "do it"
    assert adapter.tool_created["sk"] == {new_id}  # the UUID, never the identifier
    assert out["issue"]["identifier"] not in adapter.tool_created["sk"]


def test_create_without_parent_then_update_created(pin_ctx, ws, adapter):
    out = linear_issue_create({"title": "Loose"})
    assert out["ok"] is True
    new = ws.issues[out["issue"]["id"]]
    assert new["parent"] is None
    # created this session -> writable, by identifier too
    upd = linear_issue_update({"id": new["identifier"], "title": "Renamed"})
    assert upd["ok"] is True and new["title"] == "Renamed"


def test_create_parent_out_of_scope_refused(pin_ctx, ws, adapter):
    out = linear_issue_create({"title": "Sneaky", "parent": "OTH-9"})
    assert out == {"ok": False, "error": "OUT_OF_SCOPE"}
    assert ws.writes() == [] and adapter.tool_created == {}


def test_create_refused_outside_linear_session(pin_ctx, ws):
    pin_ctx(linear=False)
    assert linear_issue_create({"title": "x"}) == {"ok": False, "error": "NOT_LINEAR_SESSION"}
    assert ws.writes() == []


def test_create_unknown_state_is_bad_args(pin_ctx, ws):
    assert linear_issue_create({"title": "x", "state": "Limbo"}) == {"ok": False,
                                                                     "error": "BAD_ARGS"}
    assert ws.writes() == []


# ----------------------------------------------------------------- linear_issue_update


def test_update_state_by_name_and_type(pin_ctx, ws):
    assert linear_issue_update({"id": "RET-2", "state": "in review"})["ok"] is True
    assert ws.issues["uuid-child"]["state"] == "st-review"
    assert linear_issue_update({"id": "uuid-child", "state": "completed"})["ok"] is True
    assert ws.issues["uuid-child"]["state"] == "st-done"
    assert linear_issue_update({"id": "RET-2", "state": "Limbo"}) == {"ok": False,
                                                                      "error": "BAD_ARGS"}


def test_update_title_description(pin_ctx, ws):
    out = linear_issue_update({"id": "RET-1", "title": "T", "description": "D"})
    assert out["ok"] is True and out["issue"]["identifier"] == "RET-1"
    assert (ws.issues[SELF]["title"], ws.issues[SELF]["description"]) == ("T", "D")


def test_update_needs_a_change(pin_ctx, ws):
    assert linear_issue_update({"id": "RET-1"}) == {"ok": False, "error": "BAD_ARGS"}
    assert ws.writes() == []


def test_labels_add_remove_by_name(pin_ctx, ws):
    ws.issues["uuid-child"]["labels"] = ["lb-bug"]
    out = linear_issue_update({"id": "RET-2", "add_labels": ["FEATURE", "superpowers:plan"],
                               "remove_labels": ["bug"]})
    assert out["ok"] is True
    assert sorted(ws.issues["uuid-child"]["labels"]) == ["lb-feature", "lb-plan"]
    (_, (iid, inp)), = [c for c in ws.writes() if c[0] == "issue_update"]
    assert iid == "uuid-child"
    assert inp == {"addedLabelIds": ["lb-feature", "lb-plan"], "removedLabelIds": ["lb-bug"]}


def test_unknown_label_is_label_missing(pin_ctx, ws):
    out = linear_issue_update({"id": "RET-2", "add_labels": ["bug", "no-such-label"]})
    assert out == {"ok": False, "error": "LABEL_MISSING"}
    out = linear_issue_create({"title": "x", "labels": ["no-such-label"]})
    assert out == {"ok": False, "error": "LABEL_MISSING"}
    assert ws.writes() == []


def test_update_out_of_scope_refused(pin_ctx, ws):
    assert linear_issue_update({"id": "OTH-9", "title": "x"}) == {"ok": False,
                                                                  "error": "OUT_OF_SCOPE"}
    assert ws.writes() == []


# ----------------------------------------------------------------- linear_comment


def test_comment_plain_creates(pin_ctx, ws):
    out = linear_comment({"id": "RET-2", "body": "hello"})
    assert out["ok"] is True
    c = ws.comments[out["comment_id"]]
    assert (c["issue"], c["body"], c["user"]) == ("uuid-child", "hello", VIEWER)


def test_comment_out_of_scope_refused(pin_ctx, ws):
    assert linear_comment({"id": "OTH-9", "body": "x"}) == {"ok": False, "error": "OUT_OF_SCOPE"}
    assert ws.writes() == []


def test_comment_edit_marker_edits_existing(pin_ctx, ws):
    ws.add_comment(SELF, "unrelated app note")
    cid = ws.add_comment(SELF, "old status\n\n<!-- hermes-linear:status -->")
    out = linear_comment({"id": "RET-1", "body": "new status", "edit_marker": "status"})
    assert out == {"ok": True, "comment_id": cid, "edited": True}
    assert ws.comments[cid]["body"] == "new status\n\n<!-- hermes-linear:status -->"
    assert [c[0] for c in ws.writes()] == ["comment_update"]


def test_comment_edit_marker_creates_when_absent(pin_ctx, ws):
    ws.add_comment(SELF, "x <!-- hermes-linear:other -->")
    out = linear_comment({"id": SELF, "body": "first", "edit_marker": "status"})
    assert out["ok"] is True and out["edited"] is False
    assert ws.comments[out["comment_id"]]["body"] == "first\n\n<!-- hermes-linear:status -->"
    # a second call now edits the comment it created
    again = linear_comment({"id": SELF, "body": "second", "edit_marker": "status"})
    assert again == {"ok": True, "comment_id": out["comment_id"], "edited": True}


def test_human_comment_with_marker_text_not_edited(pin_ctx, ws):
    human = ws.add_comment(SELF, "copied <!-- hermes-linear:status -->", user="human-1")
    out = linear_comment({"id": SELF, "body": "mine", "edit_marker": "status"})
    assert out["ok"] is True and out["edited"] is False and out["comment_id"] != human
    assert ws.comments[human]["body"] == "copied <!-- hermes-linear:status -->"


def test_comment_marker_must_be_simple(pin_ctx, ws):
    assert linear_comment({"id": SELF, "body": "b", "edit_marker": "a -->x"}) == {
        "ok": False, "error": "BAD_ARGS"}
    assert ws.writes() == []
