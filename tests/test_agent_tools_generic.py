"""Generic Linear tools (RET-76) run against ``FakeWorkspace`` on a real background loop.

The handlers are called from the test thread, as a Hermes tool worker would call them; the
adapter's ``event_loop`` runs in a background thread (integration notes Q3). Context
resolution is Task 1's (tested there), so it is pinned here with ``pin_ctx``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading

import pytest

from test_agent_tools_runtime import new_adapter
from fake_workspace import PROJECT, TEAM, VIEWER, FakeWorkspace

from hermes_linear.linear_client import LinearError

from hermes_linear import agent_tools
from hermes_linear.store import Store
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
    a = new_adapter(loop, client=ws)
    a._store = Store.open(":memory:")
    return a


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


def deny_mode(adapter) -> None:
    """Out-of-scope writes refused outright (no approval request; RET-119)."""
    adapter._settings = dataclasses.replace(adapter._settings, out_of_scope_writes="deny")


def needs_approval(out: dict) -> bool:
    return out.get("ok") is False and out.get("error") == "APPROVAL_REQUIRED"


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
    issue["project"]["externalLinks"].clear()  # a copy: the workspace is untouched
    assert ws.issues[SELF]["project"]["externalLinks"] == PROJECT["externalLinks"]
    assert PROJECT["externalLinks"]
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


async def test_fake_not_found_matches_client(ws):
    """The fake raises what LinearClient raises for Linear's 'Entity not found'."""
    for call in (lambda: ws.issue_get("NOPE-1"),
                 lambda: ws.gql(agent_tools.QUERY_ISSUE_PARENT, {"id": "NOPE-1"})):
        with pytest.raises(LinearError) as ei:
            await call()
        assert (ei.value.code, ei.value.status, str(ei.value)) == (
            "NOT_FOUND", 200, "Entity not found: Issue")


def test_fake_children_are_unsorted(ws):
    ws.add_issue("RET-5", parent=SELF, sort_order=-1.0)
    kids = asyncio.run(ws.issue_get(SELF))["children"]
    assert [c["identifier"] for c in kids] == ["RET-2", "RET-5"]  # insertion order


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
    # recorded for the session ticket, as the UUID (never the identifier)
    assert adapter._store.created_by(SELF) == {new_id}


def test_create_without_parent_then_update_created(pin_ctx, ws, adapter):
    out = linear_issue_create({"title": "Loose"})
    assert out["ok"] is True
    new = ws.issues[out["issue"]["id"]]
    assert new["parent"] is None
    assert new["state"] == "st-backlog"  # the fake's default: first backlog/unstarted state
    # created this session -> writable, by identifier too
    upd = linear_issue_update({"id": new["identifier"], "title": "Renamed"})
    assert upd["ok"] is True and new["title"] == "Renamed"


def test_create_parent_out_of_scope_refused(pin_ctx, ws, adapter):
    assert needs_approval(linear_issue_create({"title": "Sneaky", "parent": "OTH-9"}))
    assert ws.writes() == [] and adapter._store.created_by(SELF) == set()
    deny_mode(adapter)
    out = linear_issue_create({"title": "Sneaky", "parent": "OTH-9"})
    assert out == {"ok": False, "error": "OUT_OF_SCOPE"}
    assert ws.writes() == [] and adapter._store.created_by(SELF) == set()


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


def test_workspace_label_applies(pin_ctx, ws):
    assert linear_issue_update({"id": "RET-2", "add_labels": ["blocked"]})["ok"] is True
    assert ws.issues["uuid-child"]["labels"] == ["lb-ws-blocked"]


def test_remove_unknown_label_is_noop(pin_ctx, ws):
    ws.issues["uuid-child"]["labels"] = ["lb-bug"]
    out = linear_issue_update({"id": "RET-2", "remove_labels": ["no-such-label", "Bug"]})
    assert out["ok"] is True and ws.issues["uuid-child"]["labels"] == []
    (_, (_, inp)), = [c for c in ws.writes() if c[0] == "issue_update"]
    assert inp == {"removedLabelIds": ["lb-bug"]}
    # only unknown names to remove, with another change: no label field at all
    out = linear_issue_update({"id": "RET-2", "title": "T", "remove_labels": ["no-such"]})
    assert out["ok"] is True
    assert ws.writes()[-1][1][1] == {"title": "T"}


def test_unknown_label_is_label_missing(pin_ctx, ws):
    out = linear_issue_update({"id": "RET-2", "add_labels": ["bug", "no-such-label"]})
    assert out == {"ok": False, "error": "LABEL_MISSING"}
    out = linear_issue_create({"title": "x", "labels": ["no-such-label"]})
    assert out == {"ok": False, "error": "LABEL_MISSING"}
    assert ws.writes() == []


def test_update_out_of_scope_refused(pin_ctx, ws, adapter):
    assert needs_approval(linear_issue_update({"id": "OTH-9", "title": "x"}))
    deny_mode(adapter)
    assert linear_issue_update({"id": "OTH-9", "title": "x"}) == {"ok": False,
                                                                  "error": "OUT_OF_SCOPE"}
    assert ws.writes() == []


# ----------------------------------------------------------------- reparent (RET-110)


def test_reparent_within_scope(pin_ctx, ws):
    ws.add_issue("RET-4", title="Sibling", uuid="uuid-sib", parent=SELF)
    out = linear_issue_update({"id": "RET-4", "parent": "RET-2"})  # parent alone is a change
    assert out["ok"] is True and out["issue"]["identifier"] == "RET-4"
    assert ws.issues["uuid-sib"]["parent"] == "uuid-child"
    assert ws.writes() == [("issue_update", ("uuid-sib", {"parentId": "uuid-child"}))]


@pytest.mark.parametrize("detach", [None, "", "  "])
def test_reparent_detach(pin_ctx, ws, detach):
    out = linear_issue_update({"id": "RET-2", "parent": detach})
    assert out["ok"] is True
    assert ws.issues["uuid-child"]["parent"] is None
    assert ws.writes() == [("issue_update", ("uuid-child", {"parentId": None}))]


def test_reparent_with_other_fields(pin_ctx, ws):
    ws.add_issue("RET-4", uuid="uuid-sib", parent=SELF)
    out = linear_issue_update({"id": "RET-4", "parent": "uuid-child", "title": "Moved"})
    assert out["ok"] is True
    assert ws.writes()[-1][1] == ("uuid-sib", {"title": "Moved", "parentId": "uuid-child"})


def test_reparent_out_of_scope_source(pin_ctx, ws, adapter):
    assert needs_approval(linear_issue_update({"id": "OTH-9", "parent": "RET-1"}))
    assert needs_approval(linear_issue_update({"id": "OTH-9", "parent": None}))
    assert ws.writes() == []
    deny_mode(adapter)
    assert linear_issue_update({"id": "OTH-9", "parent": "RET-1"}) == {
        "ok": False, "error": "OUT_OF_SCOPE"}
    assert linear_issue_update({"id": "OTH-9", "parent": None}) == {
        "ok": False, "error": "OUT_OF_SCOPE"}
    assert ws.writes() == []


def test_reparent_out_of_scope_target(pin_ctx, ws, adapter):
    assert needs_approval(linear_issue_update({"id": "RET-2", "parent": "OTH-9"}))
    assert ws.writes() == []
    deny_mode(adapter)
    assert linear_issue_update({"id": "RET-2", "parent": "OTH-9"}) == {
        "ok": False, "error": "OUT_OF_SCOPE"}
    assert ws.issues["uuid-child"]["parent"] == SELF
    assert ws.writes() == []


def test_reparent_unknown_target(pin_ctx, ws):
    assert linear_issue_update({"id": "RET-2", "parent": "NOPE-1"}) == {
        "ok": False, "error": "NOT_FOUND"}
    assert ws.writes() == []


def test_reparent_cycle_refused(pin_ctx, ws):
    ws.add_issue("RET-3", title="Grandchild", uuid="uuid-gc", parent="uuid-child")
    for issue, parent in (("RET-2", "RET-2"),        # itself
                          ("RET-2", "uuid-child"),   # itself, by UUID
                          ("RET-1", "RET-2"),        # its child
                          ("RET-1", "RET-3"),        # its grandchild
                          ("RET-2", "RET-3")):       # its child
        assert linear_issue_update({"id": issue, "parent": parent}) == {
            "ok": False, "error": "PARENT_CYCLE"}, (issue, parent)
    assert ws.writes() == []


def test_reparent_cycle_walk_bounded_fails_closed(pin_ctx, ws, monkeypatch):
    monkeypatch.setattr(agent_tools, "CYCLE_WALK_DEPTH", 2)
    ws.add_issue("RET-3", uuid="uuid-gc", parent="uuid-child")
    ws.add_issue("RET-4", uuid="uuid-ggc", parent="uuid-gc")
    # RET-4's ancestry (gc, child, SELF) is deeper than the walk: can't prove no cycle
    assert linear_issue_update({"id": "RET-1", "parent": "RET-4"}) == {
        "ok": False, "error": "PARENT_CYCLE"}
    assert linear_issue_update({"id": "RET-3", "parent": "RET-2"})["ok"] is True  # 1 level up
    assert ws.writes() == [("issue_update", ("uuid-gc", {"parentId": "uuid-child"}))]


def test_reparent_bad_parent_type(pin_ctx, ws):
    for bad in (5, ["RET-1"], {"id": "RET-1"}):
        assert linear_issue_update({"id": "RET-2", "parent": bad}) == {
            "ok": False, "error": "BAD_ARGS"}
    assert ws.writes() == []


def test_update_schema_has_parent():
    props = TOOL_SCHEMAS["linear_issue_update"]["parameters"]["properties"]
    assert props["parent"]["type"] == "string"
    assert "detach" in props["parent"]["description"].lower()


# ------------------------------------------------- app-created scope persists (RET-110)


def test_created_issue_writable_in_a_later_turn(pin_ctx, ws, adapter):
    top = linear_issue_create({"title": "Umbrella"})["issue"]
    # a later turn: another gateway session key, same session ticket, nothing in memory
    pin_ctx(key="sk-later")
    kid = linear_issue_create({"title": "Sub", "parent": top["identifier"]})
    assert kid["ok"] is True and ws.issues[kid["issue"]["id"]]["parent"] == top["id"]
    assert linear_issue_update({"id": top["identifier"], "title": "Umbrella 2"})["ok"] is True
    # and it can adopt an existing child of the session ticket
    assert linear_issue_update({"id": "RET-2", "parent": top["id"]})["ok"] is True
    assert ws.issues["uuid-child"]["parent"] == top["id"]


def test_created_issue_survives_adapter_restart(pin_ctx, ws, adapter, loop):
    top = linear_issue_create({"title": "Umbrella"})["issue"]
    fresh = new_adapter(loop, client=ws)
    fresh._store = adapter._store  # same plugin store, new adapter (reconnect / restart)
    pin_ctx(ad=fresh, key="sk-after-restart")
    assert linear_issue_update({"id": top["id"], "title": "Still mine"})["ok"] is True


def test_created_issue_scoped_to_its_session_ticket(pin_ctx, ws, adapter):
    top = linear_issue_create({"title": "Umbrella"})["issue"]
    pin_ctx(issue="uuid-other", key="sk-other")  # a session on another ticket
    deny_mode(adapter)  # the scope verdict itself, not the approval path (test_agent_tools_gate)
    assert linear_issue_update({"id": top["id"], "title": "x"}) == {
        "ok": False, "error": "OUT_OF_SCOPE"}
    assert linear_issue_create({"title": "x", "parent": top["id"]}) == {
        "ok": False, "error": "OUT_OF_SCOPE"}


def test_create_without_store_still_creates(pin_ctx, ws, adapter):
    adapter._store = None  # disconnect race: the write lands, scope just isn't extended
    out = linear_issue_create({"title": "Loose"})
    assert out["ok"] is True and out["issue"]["id"] in ws.issues


# ----------------------------------------------------------------- linear_comment


def test_comment_plain_creates(pin_ctx, ws):
    out = linear_comment({"id": "RET-2", "body": "hello"})
    assert out["ok"] is True and out["edited"] is False
    assert out["issue"] == "RET-2" and out["url"] == "https://linear.app/x/issue/RET-2"
    c = ws.comments[out["comment_id"]]
    assert (c["issue"], c["body"], c["user"]) == ("uuid-child", "hello", VIEWER)


def test_comment_out_of_scope_refused(pin_ctx, ws, adapter):
    assert needs_approval(linear_comment({"id": "OTH-9", "body": "x"}))
    assert ws.writes() == []
    deny_mode(adapter)
    assert linear_comment({"id": "OTH-9", "body": "x"}) == {"ok": False, "error": "OUT_OF_SCOPE"}
    assert ws.writes() == []


def test_comment_edit_marker_edits_existing(pin_ctx, ws):
    ws.add_comment(SELF, "unrelated app note")
    cid = ws.add_comment(SELF, "old status\n\n<!-- hermes-linear:status -->")
    out = linear_comment({"id": "RET-1", "body": "new status", "edit_marker": "status"})
    assert out == {"ok": True, "comment_id": cid, "edited": True, "issue": "RET-1",
                   "url": "https://linear.app/x/issue/RET-1"}
    assert ws.comments[cid]["body"] == "new status\n\n<!-- hermes-linear:status -->"
    assert [c[0] for c in ws.writes()] == ["comment_update"]


def test_comment_edit_marker_creates_when_absent(pin_ctx, ws):
    ws.add_comment(SELF, "x <!-- hermes-linear:other -->")
    out = linear_comment({"id": SELF, "body": "first", "edit_marker": "status"})
    assert out["ok"] is True and out["edited"] is False
    assert ws.comments[out["comment_id"]]["body"] == "first\n\n<!-- hermes-linear:status -->"
    # a second call now edits the comment it created
    again = linear_comment({"id": SELF, "body": "second", "edit_marker": "status"})
    assert again["comment_id"] == out["comment_id"] and again["edited"] is True
    assert again["issue"] == "RET-1" and again["url"].endswith("/RET-1")


def test_comment_marker_strips_other_markers(pin_ctx, ws):
    other = ws.add_comment(SELF, "x <!-- hermes-linear:other -->")
    body = ("status <!-- hermes-linear:other -->\n<!--hermes-linear:status-->\n"
            "<!-- hermes-linear:a\nb -->tail\n\n<!-- hermes-linear:status -->")
    out = linear_comment({"id": SELF, "body": body, "edit_marker": "status"})
    assert out["ok"] is True and out["edited"] is False
    text = ws.comments[out["comment_id"]]["body"]
    assert text.count("hermes-linear:") == 1
    assert text.endswith("\n\n<!-- hermes-linear:status -->")
    assert text.startswith("status\n") and "tail" in text
    assert ws.comments[other]["body"] == "x <!-- hermes-linear:other -->"  # not edited
    # a later edit matches the one marker it kept
    again = linear_comment({"id": SELF, "body": "v2", "edit_marker": "status"})
    assert again["comment_id"] == out["comment_id"] and again["edited"] is True


def test_human_comment_with_marker_text_not_edited(pin_ctx, ws):
    human = ws.add_comment(SELF, "copied <!-- hermes-linear:status -->", user="human-1")
    out = linear_comment({"id": SELF, "body": "mine", "edit_marker": "status"})
    assert out["ok"] is True and out["edited"] is False and out["comment_id"] != human
    assert ws.comments[human]["body"] == "copied <!-- hermes-linear:status -->"


def test_comment_marker_must_be_simple(pin_ctx, ws):
    assert linear_comment({"id": SELF, "body": "b", "edit_marker": "a -->x"}) == {
        "ok": False, "error": "BAD_ARGS"}
    assert ws.writes() == []
