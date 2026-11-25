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
from fake_workspace import HUMAN, PROJECT, TEAM, VIEWER, FakeWorkspace

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


def test_refusal_extra_merges_into_error(pin_ctx):
    """RET-121 Ruling R3: one ``_Refusal`` type; its ``extra`` sits next to ``error``."""
    from hermes_linear.agent_tools import _Refusal, _tool

    def bad_parse(args):
        raise _Refusal("X_PARSE", extra={"candidates": [1]})

    async def bad_body(ctx, a):
        raise _Refusal("X_BODY", extra={"available": ["a"]})

    assert _tool(bad_parse, bad_body)({}) == {"ok": False, "error": "X_PARSE",
                                              "candidates": [1]}
    assert _tool(lambda a: a, bad_body)({}) == {"ok": False, "error": "X_BODY",
                                                "available": ["a"]}
    assert _Refusal("BAD_ARGS").extra == {}


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
    assert linear_issue_get({"id": "NOPE-1"}) == {"ok": False, "error": "NOT_FOUND",
                                                   "message": "Entity not found: Issue"}


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


#: ``STATE_NOT_FOUND`` candidates for FakeWorkspace's default states (no substring match).
STATE_CANDIDATES = [
    {"id": "st-backlog", "name": "Backlog", "type": "backlog"},
    {"id": "st-todo", "name": "Todo", "type": "unstarted"},
    {"id": "st-progress", "name": "In Progress", "type": "started"},
    {"id": "st-review", "name": "In Review", "type": "started"},
    {"id": "st-done", "name": "Done", "type": "completed"},
    {"id": "st-cancel", "name": "Canceled", "type": "canceled"}]
LABELS_AVAILABLE = ["Bug", "Feature", "superpowers:plan", "Blocked"]


def test_create_unknown_state_is_state_not_found(pin_ctx, ws):
    assert linear_issue_create({"title": "x", "state": "Limbo"}) == {
        "ok": False, "error": "STATE_NOT_FOUND", "candidates": STATE_CANDIDATES}
    assert ws.writes() == []


def test_unknown_state_candidates_substring_first(pin_ctx, ws):
    out = linear_issue_create({"title": "x", "state": "review"})  # no state named "review"
    assert out["error"] == "STATE_NOT_FOUND"
    assert out["candidates"][0] == {"id": "st-review", "name": "In Review", "type": "started"}
    assert len(out["candidates"]) == len(STATE_CANDIDATES)


def test_state_added_meanwhile_resolves_after_refetch(pin_ctx, ws):
    assert linear_issue_update({"id": "RET-2", "state": "Todo"})["ok"] is True  # cached
    ws.states[TEAM].append({"id": "st-qa", "name": "QA", "type": "started"})
    assert linear_issue_update({"id": "RET-2", "state": "qa"})["ok"] is True
    assert ws.issues["uuid-child"]["state"] == "st-qa"
    assert [c for c in ws.calls if c[0] == "team_states"] == [("team_states", TEAM)] * 2


def test_states_cached_across_calls(pin_ctx, ws):
    for name in ("Todo", "In Progress", "Done"):
        assert linear_issue_update({"id": "RET-2", "state": name})["ok"] is True
    assert [c for c in ws.calls if c[0] == "team_states"] == [("team_states", TEAM)]


# ----------------------------------------------------------------- linear_issue_update


def test_update_state_by_name_and_type(pin_ctx, ws):
    assert linear_issue_update({"id": "RET-2", "state": "in review"})["ok"] is True
    assert ws.issues["uuid-child"]["state"] == "st-review"
    assert linear_issue_update({"id": "uuid-child", "state": "completed"})["ok"] is True
    assert ws.issues["uuid-child"]["state"] == "st-done"
    assert linear_issue_update({"id": "RET-2", "state": "Limbo"}) == {
        "ok": False, "error": "STATE_NOT_FOUND", "candidates": STATE_CANDIDATES}


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


def test_remove_label_newer_than_the_cache(pin_ctx, ws):
    """A label a human created and applied after the directory cached the team's labels
    is still removed: removals match the issue's own labels, not the cache."""
    assert linear_issue_update({"id": "RET-2", "add_labels": ["bug"]})["ok"] is True
    ws.labels[TEAM].append({"id": "lb-fresh", "name": "Fresh"})  # cache now stale
    ws.issues["uuid-child"]["labels"].append("lb-fresh")
    n_label_fetches = sum(c[0] == "team_labels" for c in ws.calls)
    out = linear_issue_update({"id": "RET-2", "remove_labels": ["FRESH"]})
    assert out["ok"] is True and ws.issues["uuid-child"]["labels"] == ["lb-bug"]
    assert ws.writes()[-1][1][1] == {"removedLabelIds": ["lb-fresh"]}
    assert sum(c[0] == "team_labels" for c in ws.calls) == n_label_fetches  # no refetch


def test_unknown_label_is_label_missing(pin_ctx, ws):
    out = linear_issue_update({"id": "RET-2", "add_labels": ["bug", "no-such-label"]})
    assert out == {"ok": False, "error": "LABEL_MISSING", "available": LABELS_AVAILABLE}
    out = linear_issue_create({"title": "x", "labels": ["no-such-label"]})
    assert out == {"ok": False, "error": "LABEL_MISSING", "available": LABELS_AVAILABLE}
    assert ws.writes() == []


def test_label_added_meanwhile_resolves_after_one_refetch(pin_ctx, ws):
    assert linear_issue_update({"id": "RET-2", "add_labels": ["bug"]})["ok"] is True
    ws.labels[TEAM] += [{"id": "lb-a", "name": "A"}, {"id": "lb-b", "name": "B"}]
    assert linear_issue_update({"id": "RET-2", "add_labels": ["a", "b"]})["ok"] is True
    assert ws.issues["uuid-child"]["labels"] == ["lb-bug", "lb-a", "lb-b"]
    assert [c for c in ws.calls if c[0] == "team_labels"] == [("team_labels", TEAM)] * 2


def test_label_missing_after_refetch_fetches_once_more(pin_ctx, ws):
    assert linear_issue_update({"id": "RET-2", "add_labels": ["bug"]})["ok"] is True
    out = linear_issue_update({"id": "RET-2", "add_labels": ["nope", "nada"]})
    assert out["error"] == "LABEL_MISSING"
    assert [c for c in ws.calls if c[0] == "team_labels"] == [("team_labels", TEAM)] * 2


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
        "ok": False, "error": "NOT_FOUND", "message": "Entity not found: Issue"}
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


# ------------------------------------------------ linear_comment threads (RET-121 §1.5)


def _thread(ws, issue="RET-2", resolved_at=None):
    """A human top-level comment on ``issue`` and a human reply to it: ``(top, reply)``."""
    top = ws.add_comment(issue, "question?", user=HUMAN, resolved_at=resolved_at)
    return top, ws.add_comment(issue, "more", user=HUMAN, parent=top)


def test_reply_posts_with_parent(pin_ctx, ws):
    top, _ = _thread(ws)
    out = linear_comment({"id": "RET-2", "body": "answer", "reply_to": top})
    assert out == {"ok": True, "comment_id": out["comment_id"], "edited": False,
                   "issue": "RET-2", "url": "https://linear.app/x/issue/RET-2",
                   "thread_id": top, "resolved": False}
    c = ws.comments[out["comment_id"]]
    assert (c["issue"], c["body"], c["parent"]) == ("uuid-child", "answer", top)
    assert ws.writes() == [("comment_create", ("uuid-child", "answer", top))]


def test_reply_to_reply_uses_thread(pin_ctx, ws):
    """Linear refuses a reply's id as parentId (probe): the thread's top comment is used."""
    top, reply = _thread(ws)
    out = linear_comment({"id": "RET-2", "body": "answer", "reply_to": reply})
    assert out["ok"] is True and out["thread_id"] == top and out["resolved"] is False
    assert ws.comments[out["comment_id"]]["parent"] == top


def test_reply_to_other_issue_refused(pin_ctx, ws):
    """Review Focus 4: a comment on a different (in-scope) issue: no post."""
    top, reply = _thread(ws, issue="RET-1")
    for ref in (top, reply):
        out = linear_comment({"id": "RET-2", "body": "x", "reply_to": ref})
        assert out == {"ok": False, "error": "COMMENT_NOT_ON_ISSUE"}
        out = linear_comment({"id": "RET-2", "body": "x", "reply_to": ref, "resolve": True})
        assert out == {"ok": False, "error": "COMMENT_NOT_ON_ISSUE"}
    assert ws.writes() == []


def test_reply_to_unknown_refused(pin_ctx, ws):
    out = linear_comment({"id": "RET-2", "body": "x", "reply_to": "comment-nope"})
    assert out == {"ok": False, "error": "COMMENT_NOT_ON_ISSUE"}
    assert ws.writes() == []


def test_resolve_posts_then_resolves(pin_ctx, ws):
    top, reply = _thread(ws)
    out = linear_comment({"id": "RET-2", "body": "done", "reply_to": reply, "resolve": True})
    assert out["ok"] is True and out["thread_id"] == top and out["resolved"] is True
    assert "already_resolved" not in out
    new = out["comment_id"]
    assert ws.writes() == [("comment_create", ("uuid-child", "done", top)),
                           ("comment_resolve", (top, new))]
    assert ws.comments[top]["resolved_at"] is not None


def test_resolve_failure_after_post_keeps_the_reply(pin_ctx, ws):
    """R12: the reply is posted, then commentResolve fails: the result names the reply."""
    top, _ = _thread(ws)

    async def refuse(comment_id, resolving_comment_id):
        ws.calls.append(("comment_resolve", (comment_id, resolving_comment_id)))
        raise LinearError("forbidden", code="FORBIDDEN", detail="Not allowed to resolve")

    ws.comment_resolve = refuse
    out = linear_comment({"id": "RET-2", "body": "done", "reply_to": top, "resolve": True})
    new = next(c for c in ws.comments if ws.comments[c]["body"] == "done")
    assert out == {"ok": False, "error": "FORBIDDEN", "message": "Not allowed to resolve",
                   "comment_id": new, "thread_id": top, "posted": True}
    assert ws.comments[top]["resolved_at"] is None

    async def refuse_bare(comment_id, resolving_comment_id):
        raise LinearError("boom")

    ws.comment_resolve = refuse_bare
    out = linear_comment({"id": "RET-2", "body": "again", "reply_to": top, "resolve": True})
    assert out["error"] == "LINEAR_ERROR" and out["message"] == "boom"
    assert out["posted"] is True


def test_comment_description_says_posted_means_do_not_rerun():
    desc = TOOL_SCHEMAS["linear_comment"]["description"]
    assert "posted: true" in desc and "re-run" in desc


def test_resolve_already_resolved(pin_ctx, ws):
    """Linear re-resolves a resolved thread (probe): the tool makes no resolve call."""
    top, _ = _thread(ws, resolved_at="2026-11-20T00:00:00.000Z")
    out = linear_comment({"id": "RET-2", "body": "again", "reply_to": top, "resolve": True})
    assert out["ok"] is True and out["resolved"] is True and out["already_resolved"] is True
    assert [w[0] for w in ws.writes()] == ["comment_create"]
    assert ws.comments[top]["resolved_at"] == "2026-11-20T00:00:00.000Z"


def test_reply_to_resolved_thread_without_resolve(pin_ctx, ws):
    top, _ = _thread(ws, resolved_at="2026-11-20T00:00:00.000Z")
    out = linear_comment({"id": "RET-2", "body": "fyi", "reply_to": top})
    assert out["ok"] is True and out["resolved"] is True and "already_resolved" not in out
    assert [w[0] for w in ws.writes()] == ["comment_create"]


@pytest.mark.parametrize("args", [
    {"resolve": True}, {"resolve": True, "reply_to": None}, {"resolve": True, "reply_to": " "},
    {"reply_to": 5}, {"reply_to": "c", "resolve": "yes"}, {"resolve": False, "reply_to": ""}])
def test_resolve_without_reply_to_bad_args(pin_ctx, ws, args):
    assert linear_comment({"id": "RET-2", "body": "x", **args}) == {
        "ok": False, "error": "BAD_ARGS"}
    assert ws.calls == []  # refused before any I/O


def test_marker_with_reply_to_bad_args(pin_ctx, ws):
    top, _ = _thread(ws)
    assert linear_comment({"id": "RET-2", "body": "x", "reply_to": top,
                           "edit_marker": "status"}) == {"ok": False, "error": "BAD_ARGS"}
    assert ws.calls == []


def test_resolve_false_is_a_plain_reply(pin_ctx, ws):
    top, _ = _thread(ws)
    out = linear_comment({"id": "RET-2", "body": "x", "reply_to": top, "resolve": False})
    assert out["ok"] is True and out["resolved"] is False
    assert [w[0] for w in ws.writes()] == ["comment_create"]


# ------------------------------------------- create / update field parity (RET-121 T4)

ORION = "proj-orion"


def _created_inputs(ws) -> list[dict]:
    return [c[1] for c in ws.calls if c[0] == "issue_create"]


def _updates(ws) -> list[tuple[str, dict]]:
    return [c[1] for c in ws.calls if c[0] == "issue_update"]


def _orion(ws) -> dict:
    return ws.add_project(ORION, "orion")


def _requests(adapter) -> int:
    return adapter._store.conn.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0]


def test_create_fields_input(pin_ctx, ws, adapter):
    ws.cycles_rows[TEAM] = [{"id": "cy-1", "number": 1, "name": "Sprint 1", "current": True}]
    ws.milestones_rows[PROJECT["id"]] = [{"id": "ms-1", "name": "Alpha"}]
    out = linear_issue_create({
        "title": "Full", "description": "d", "parent": "RET-1", "labels": ["bug"],
        "state": "In Progress", "team": "ret", "project": "Hermes", "assignee": "mike",
        "priority": "high", "estimate": 3, "due_date": "2026-12-01", "cycle": "current",
        "milestone": "alpha"})
    assert out["ok"] is True and set(out) == {"ok", "issue"}, out
    assert _created_inputs(ws) == [{
        "teamId": TEAM, "title": "Full", "projectId": PROJECT["id"], "description": "d",
        "parentId": SELF, "stateId": "st-progress", "labelIds": ["lb-bug"],
        "assigneeId": HUMAN, "priority": 2, "estimate": 3, "dueDate": "2026-12-01",
        "cycleId": "cy-1", "projectMilestoneId": "ms-1"}]
    assert _requests(adapter) == 0  # team and project equal to the base: no target


def test_create_null_fields_are_absent(pin_ctx, ws):
    out = linear_issue_create({"title": "x", "project": None, "assignee": "", "priority": None,
                               "estimate": None, "milestone": "  "})
    assert out["ok"] is True
    assert _created_inputs(ws) == [{"teamId": TEAM, "title": "x",
                                     "projectId": PROJECT["id"]}]  # null project: the base


def test_create_follows_parent_project_no_target(pin_ctx, ws, adapter):
    orion = _orion(ws)
    ws.add_issue("RET-5", uuid="uuid-p5", parent=SELF, project=orion)  # in scope, in orion
    out = linear_issue_create({"title": "Kid", "parent": "RET-5"})
    assert out["ok"] is True
    assert ws.issues[out["issue"]["id"]]["project"] == orion
    out = linear_issue_create({"title": "Kid 2", "parent": "RET-5", "project": "orion"})
    assert out["ok"] is True  # explicit project equal to the parent's: no target
    ws.add_issue("RET-6", uuid="uuid-p6", parent=SELF, project=None)
    assert linear_issue_create({"title": "Kid 3", "parent": "RET-6"})["ok"] is True
    assert "projectId" not in _created_inputs(ws)[-1]  # a parent without a project
    assert _requests(adapter) == 0
    # Review Focus 2: an approved out-of-scope parent in another project is the only target;
    # the child follows that parent's project with no prompt of its own.
    ws.add_issue("OTH-7", uuid="uuid-o7", project=orion)
    args = {"title": "Under approved", "parent": "OTH-7"}
    assert needs_approval(linear_issue_create(dict(args)))
    from hermes_linear.tool_approval import digest
    dg = digest("linear_issue_create", args)
    assert adapter._store.request("sk", dg).targets == ["uuid-o7"]
    adapter._store.arm_request("sk", dg)
    out = linear_issue_create(dict(args))
    assert out["ok"] is True and ws.issues[out["issue"]["id"]]["project"] == orion


def test_update_fields_input(pin_ctx, ws):
    ws.cycles_rows[TEAM] = [{"id": "cy-7", "number": 7, "name": "Sprint 7"}]
    ws.milestones_rows[PROJECT["id"]] = [{"id": "ms-1", "name": "Alpha"}]
    out = linear_issue_update({"id": "RET-2", "assignee": "mike", "priority": "4",
                               "estimate": 0, "due_date": "2027-01-31", "cycle": "7",
                               "milestone": "Alpha"})
    assert out["ok"] is True and set(out) == {"ok", "issue"}
    assert _updates(ws) == [("uuid-child", {
        "assigneeId": HUMAN, "priority": 4, "estimate": 0, "dueDate": "2027-01-31",
        "cycleId": "cy-7", "projectMilestoneId": "ms-1"})]


def test_update_clear_fields(pin_ctx, ws):
    out = linear_issue_update({"id": "RET-2", "assignee": None, "delegate": "",
                               "priority": None, "estimate": None, "due_date": None,
                               "cycle": None, "milestone": None})
    assert out["ok"] is True
    assert _updates(ws) == [("uuid-child", {
        "assigneeId": None, "delegateId": None, "priority": 0, "estimate": None,
        "dueDate": None, "cycleId": None, "projectMilestoneId": None})]
    assert linear_issue_update({"id": "RET-2", "priority": "low"})["ok"] is True  # R2


def test_update_milestone_in_resulting_project(pin_ctx, ws):
    orion = _orion(ws)
    ws.add_issue("RET-5", uuid="uuid-p5", parent=SELF, project=orion)
    ws.milestones_rows[ORION] = [{"id": "ms-o", "name": "Launch"}]
    out = linear_issue_update({"id": "RET-2", "project": "orion", "milestone": "launch",
                               "parent": "RET-5"})
    assert out["ok"] is True
    assert _updates(ws)[-1][1] == {"parentId": "uuid-p5", "projectId": ORION,
                                   "projectMilestoneId": "ms-o"}
    out = linear_issue_update({"id": "RET-2", "project": None, "milestone": "launch"})
    assert out == {"ok": False, "error": "MILESTONE_NOT_FOUND", "candidates": []}


CODEX = "codex-user"
SELF_MSG = "Only a human can delegate to this app; ask in the thread."
APP_ASSIGNEE_MSG = ("Linear turns an app assignee into a delegation; use delegate (another "
                    "agent) or ask a human.")


def _add_codex(ws) -> None:
    ws.users_rows.append({"id": CODEX, "name": "Codex", "displayName": "codex",
                          "email": "codex@app.linear", "app": True, "active": True})


def test_self_delegation_refused(pin_ctx, ws, adapter):
    refusal = {"ok": False, "error": "SELF_DELEGATION", "message": SELF_MSG}
    assert linear_issue_create({"title": "x", "delegate": "me"}) == refusal
    for ref in ("Neo", "neo@app.linear", VIEWER):
        assert linear_issue_update({"id": "RET-2", "delegate": ref}) == refusal, ref
    assert linear_issue_update({"id": "RET-2", "delegate": "mike"}) == {
        "ok": False, "error": "DELEGATE_NOT_AGENT"}
    assert ws.writes() == [] and _requests(adapter) == 0


def test_app_assignee_refused(pin_ctx, ws, adapter):
    _add_codex(ws)
    refusal = {"ok": False, "error": "ASSIGNEE_IS_APP", "message": APP_ASSIGNEE_MSG}
    assert linear_issue_create({"title": "x", "assignee": "me"}) == refusal
    for ref in ("me", "Neo", VIEWER, "Codex", CODEX):
        assert linear_issue_update({"id": "RET-2", "assignee": ref}) == refusal, ref
    assert ws.writes() == [] and _requests(adapter) == 0


def test_other_agent_delegate_writes(pin_ctx, ws):
    _add_codex(ws)
    out = linear_issue_create({"title": "x", "delegate": "Codex"})
    assert out["ok"] is True and set(out) == {"ok", "issue"}
    assert _created_inputs(ws)[-1]["delegateId"] == CODEX
    out = linear_issue_update({"id": "RET-2", "delegate": "codex@app.linear"})
    assert out["ok"] is True and set(out) == {"ok", "issue"}
    assert _updates(ws)[-1][1] == {"delegateId": CODEX}


def test_self_delegation_refused_before_approval(pin_ctx, ws, adapter):
    """A first pass that would also need approval (a project outside the scope) refuses
    with the field's code: no write and no approval request."""
    _orion(ws)
    assert needs_approval(linear_issue_update({"id": "RET-2", "project": "orion"}))
    n = _requests(adapter)
    out = linear_issue_update({"id": "RET-2", "project": "orion", "delegate": "me"})
    assert out == {"ok": False, "error": "SELF_DELEGATION", "message": SELF_MSG}
    out = linear_issue_create({"title": "x", "project": "orion", "assignee": "me"})
    assert out == {"ok": False, "error": "ASSIGNEE_IS_APP", "message": APP_ASSIGNEE_MSG}
    assert ws.writes() == [] and _requests(adapter) == n


def test_update_team_bad_args(pin_ctx, ws):
    for args in ({"id": "RET-2", "team": "RET"}, {"id": "RET-2", "team": None},
                 {"id": "RET-2", "title": "T", "team": "RET"}):
        assert linear_issue_update(args) == {"ok": False, "error": "BAD_ARGS"}, args
    assert ws.writes() == []


def test_field_misses_carry_candidates(pin_ctx, ws):
    out = linear_issue_create({"title": "x", "assignee": "nobody"})
    assert out["error"] == "USER_NOT_FOUND" and out["candidates"]
    out = linear_issue_create({"title": "x", "project": "nope"})
    assert out["error"] == "PROJECT_NOT_FOUND" and out["candidates"]
    out = linear_issue_create({"title": "x", "team": "NOPE"})
    assert out["error"] == "TEAM_NOT_FOUND" and out["candidates"]
    out = linear_issue_update({"id": "RET-2", "cycle": "current"})
    assert out == {"ok": False, "error": "CYCLE_NOT_FOUND", "candidates": []}
    for bad in ({"priority": "huge"}, {"estimate": -1}, {"due_date": "2026-02-30"}):
        assert linear_issue_update({"id": "RET-2", **bad}) == {
            "ok": False, "error": "BAD_ARGS"}, bad
    assert ws.writes() == []


def test_update_project_session_or_parent_no_prompt(pin_ctx, ws, adapter):
    orion = _orion(ws)
    ws.add_issue("RET-5", uuid="uuid-p5", parent=SELF, project=orion)
    ws.add_issue("RET-6", uuid="uuid-k6", parent="uuid-p5", project=None)
    # the parent's project
    assert linear_issue_update({"id": "RET-6", "project": "orion"})["ok"] is True
    assert ws.issues["uuid-k6"]["project"] == orion
    # the session ticket's project
    assert linear_issue_update({"id": "RET-6", "project": "Hermes"})["ok"] is True
    assert ws.issues["uuid-k6"]["project"]["id"] == PROJECT["id"]
    # clearing is no target
    for clear in (None, ""):
        assert linear_issue_update({"id": "RET-5", "project": clear})["ok"] is True
        assert ws.issues["uuid-p5"]["project"] is None
    assert _requests(adapter) == 0


def test_update_project_new_parent_same_call(pin_ctx, ws, adapter):
    orion = _orion(ws)
    ws.add_issue("RET-5", uuid="uuid-p5", parent=SELF, project=orion)
    ws.add_issue("RET-6", uuid="uuid-k6", parent=SELF, project=None)
    out = linear_issue_update({"id": "RET-6", "parent": "RET-5", "project": "orion"})
    assert out["ok"] is True and _requests(adapter) == 0
    assert ws.issues["uuid-k6"]["parent"] == "uuid-p5"
    assert ws.issues["uuid-k6"]["project"] == orion
    ws.issues["uuid-k6"]["project"] = None
    # the NEW parent (RET-1, project Hermes) counts, not the current one (orion)
    assert needs_approval(linear_issue_update({"id": "RET-6", "parent": "RET-1",
                                               "project": "orion"}))
    # detaching leaves only the session ticket's project
    assert needs_approval(linear_issue_update({"id": "RET-6", "parent": None,
                                               "project": "orion"}))
    assert ws.issues["uuid-k6"]["parent"] == "uuid-p5"
    assert len(_updates(ws)) == 1


def test_issue_field_schemas():
    from hermes_linear.issue_fields import FIELDS
    create = TOOL_SCHEMAS["linear_issue_create"]["parameters"]["properties"]
    update = TOOL_SCHEMAS["linear_issue_update"]["parameters"]["properties"]
    for name in (*FIELDS, "team"):
        want = "integer" if name == "estimate" else "string"
        assert create[name]["type"] == want, name
        if name != "team":
            assert update[name]["type"] == [want, "null"], name
    assert "team" not in update
    for props in (create, update):
        assert all(w in props["priority"]["description"]
                   for w in ("none", "urgent", "high", "medium", "low", "0", "4"))
    assert "clears the milestone" in update["project"]["description"]
    for props in (create, update):
        assert "ASSIGNEE_IS_APP" in props["assignee"]["description"]
        assert "SELF_DELEGATION" in props["delegate"]["description"]
        assert "starts_session" not in props["delegate"]["description"]
    for key in ("labels",):
        assert create[key]["type"] == "array"
    assert update["add_labels"]["type"] == update["remove_labels"]["type"] == "array"
