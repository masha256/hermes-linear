"""linear_task_state (RET-79) against ``FakeWorkspace`` on a real background loop.

Fixtures as in ``test_agent_tools_generic``: the session issue ``RET-1`` is the project
ticket; ``RET-2`` is a task sub-ticket; context resolution is pinned with ``pin_ctx``.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from fake_workspace import VIEWER, FakeWorkspace
from test_agent_tools_runtime import new_adapter

from hermes_linear import agent_tools
from hermes_linear.agent_tools import (TOOL_HANDLERS, TOOL_SCHEMAS, linear_issue_update,
                                       linear_task_state)

SELF = "uuid-self"
TASK = "uuid-task"
BLOCKED = "lb-ws-blocked"


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
    w.add_issue("RET-1", title="Project ticket", uuid=SELF)
    w.add_issue("RET-2", title="T1 · Task", uuid=TASK, parent=SELF)
    w.add_issue("OTH-9", title="Unrelated", uuid="uuid-other", project=None)
    return w


@pytest.fixture
def adapter(loop, ws):
    return new_adapter(loop, client=ws)


@pytest.fixture
def pin_ctx(monkeypatch, adapter):
    def pin(*, linear=True, issue=SELF, key="sk", ad=adapter):
        ctx = agent_tools.ToolContext(adapter=ad, client=getattr(ad, "client", None),
                                      session_issue_id=issue if linear else None,
                                      session_key=key if linear else None, is_linear=linear)
        monkeypatch.setattr(agent_tools, "resolve_context", lambda session_id=None: ctx)
        return ctx
    pin()
    return pin


def _kinds(ws):
    return [c[0] for c in ws.writes()]


def test_schema_and_handler():
    s = TOOL_SCHEMAS["linear_task_state"]
    assert s["name"] == "linear_task_state" and "Linear app" in s["description"]
    props = s["parameters"]["properties"]
    assert set(props) == {"id", "state", "summary", "question"}
    assert props["state"]["enum"] == ["in_progress", "in_review", "done", "blocked",
                                      "unblocked"]
    assert s["parameters"]["required"] == ["id", "state"]
    assert TOOL_HANDLERS["linear_task_state"] is linear_task_state


def test_each_transition(pin_ctx, ws):
    task = ws.issues[TASK]
    out = linear_task_state({"id": "RET-2", "state": "in_progress"})
    assert out == {"ok": True, "issue": "RET-2", "state": "In Progress", "label_missing": False}
    assert task["state"] == "st-progress"
    out = linear_task_state({"id": "RET-2", "state": "in_review"})
    assert out["ok"] is True and out["state"] == "In Review" and task["state"] == "st-review"
    out = linear_task_state({"id": TASK, "state": "done"})
    assert out["ok"] is True and out["state"] == "Done" and task["state"] == "st-done"
    assert [w[1] for w in ws.writes()] == [(TASK, {"stateId": "st-progress"}),
                                           (TASK, {"stateId": "st-review"}),
                                           (TASK, {"stateId": "st-done"})]
    # already there: no redundant write
    out = linear_task_state({"id": "RET-2", "state": "done"})
    assert out["ok"] is True and out["state"] == "Done"
    assert len(ws.writes()) == 3


def test_in_progress_falls_back_to_started_type(pin_ctx, ws):
    ws.states["team-1"] = [s for s in ws.states["team-1"] if s["name"] != "In Progress"]
    out = linear_task_state({"id": "RET-2", "state": "in_progress"})
    assert out["ok"] is True and out["state"] == "In Review"  # first `started` state
    assert ws.issues[TASK]["state"] == "st-review"


def test_in_review_falls_back_to_started(pin_ctx, ws):
    ws.states["team-1"] = [
        {"id": "st-todo", "name": "Todo", "type": "unstarted"},
        {"id": "st-progress", "name": "In Progress", "type": "started"},
        {"id": "st-doing", "name": "Doing", "type": "started"},
        {"id": "st-done", "name": "Done", "type": "completed"},
    ]
    # not started yet: moved to the first started state
    out = linear_task_state({"id": "RET-2", "state": "in_review"})
    assert out["ok"] is True and out["state"] == "In Progress"
    assert ws.issues[TASK]["state"] == "st-progress"
    # already in a started state: stays there, no write
    ws.issues[TASK]["state"] = "st-doing"
    n = len(ws.writes())
    out = linear_task_state({"id": "RET-2", "state": "in_review"})
    assert out["ok"] is True and out["state"] == "Doing"
    assert ws.issues[TASK]["state"] == "st-doing" and len(ws.writes()) == n


def test_in_review_name_is_case_insensitive(pin_ctx, ws):
    for s in ws.states["team-1"]:
        if s["id"] == "st-review":
            s["name"] = "in review"
    assert linear_task_state({"id": "RET-2", "state": "in_review"})["ok"] is True
    assert ws.issues[TASK]["state"] == "st-review"


def test_done_posts_summary(pin_ctx, ws):
    out = linear_task_state({"id": "RET-2", "state": "done", "summary": "Shipped in abc123."})
    assert out["ok"] is True and out["state"] == "Done" and out["summary_posted"] is True
    assert ws.issues[TASK]["state"] == "st-done"
    (cid, c), = ws.comments.items()
    assert (c["issue"], c["body"], c["user"]) == (TASK, "Shipped in abc123.", VIEWER)
    assert sorted(_kinds(ws)) == ["comment_create", "issue_update"]
    # retried with the same summary (modulo surrounding whitespace): nothing is posted again
    out = linear_task_state({"id": "RET-2", "state": "done", "summary": "  Shipped in abc123.\n"})
    assert out["ok"] is True and out["summary_posted"] is False and out["state"] == "Done"
    assert len(ws.comments) == 1 and sorted(_kinds(ws)) == ["comment_create", "issue_update"]
    # a different summary is posted; the state is not rewritten
    out = linear_task_state({"id": "RET-2", "state": "done", "summary": "Follow-up."})
    assert out["ok"] is True and out["summary_posted"] is True
    assert _kinds(ws).count("issue_update") == 1 and _kinds(ws).count("comment_create") == 2


def test_done_summary_dedupe_ignores_human_comments(pin_ctx, ws):
    ws.add_comment(TASK, "Shipped in abc123.", user="human-1")
    ws.add_comment(SELF, "Shipped in abc123.")  # the app's, but on another ticket
    out = linear_task_state({"id": "RET-2", "state": "done", "summary": "Shipped in abc123."})
    assert out["ok"] is True and out["summary_posted"] is True
    assert _kinds(ws).count("comment_create") == 1


def test_done_keeps_any_completed_state(pin_ctx, ws):
    ws.states["team-1"].append({"id": "st-shipped", "name": "Shipped", "type": "completed"})
    ws.issues[TASK]["state"] = "st-shipped"
    out = linear_task_state({"id": "RET-2", "state": "done"})
    assert out["ok"] is True and out["state"] == "Shipped"
    assert ws.issues[TASK]["state"] == "st-shipped" and ws.writes() == []


def test_done_without_summary_posts_nothing(pin_ctx, ws):
    out = linear_task_state({"id": "RET-2", "state": "done"})
    assert out["ok"] is True and out.get("summary_posted", False) is False
    assert ws.comments == {} and _kinds(ws) == ["issue_update"]


def test_blocked_labels_and_asks_on_project_ticket(pin_ctx, ws):
    out = linear_task_state({"id": "RET-2", "state": "blocked",
                             "question": "Which DB should we use?"})
    assert out == {"ok": True, "issue": "RET-2", "state": "Todo", "label_missing": False,
                   "question_posted": True}
    assert ws.issues[TASK]["labels"] == [BLOCKED]
    assert ws.issues[TASK]["state"] == "st-todo"  # state unchanged
    (_, c), = ws.comments.items()
    assert c["issue"] == SELF  # the project (session) ticket, not the task ticket
    assert c["body"] == "**RET-2 needs input:** Which DB should we use?"
    # already labelled: the label is not re-added, the question is still posted
    out = linear_task_state({"id": "RET-2", "state": "blocked", "question": "Again?"})
    assert out["ok"] is True
    assert _kinds(ws).count("issue_update") == 1 and _kinds(ws).count("comment_create") == 2


def test_blocked_label_matched_case_insensitively(pin_ctx, ws):
    ws.workspace_labels = []
    ws.labels["team-1"].append({"id": "lb-team-blocked", "name": "BLOCKED"})
    out = linear_task_state({"id": "RET-2", "state": "blocked", "question": "q"})
    assert out["ok"] is True and ws.issues[TASK]["labels"] == ["lb-team-blocked"]


def test_blocked_without_label_still_asks(pin_ctx, ws):
    ws.workspace_labels = []
    out = linear_task_state({"id": "RET-2", "state": "blocked", "question": "Need a key."})
    assert out == {"ok": False, "error": "LABEL_MISSING", "issue": "RET-2",
                   "label_missing": True, "question_posted": True}
    (_, c), = ws.comments.items()
    assert (c["issue"], c["body"]) == (SELF, "**RET-2 needs input:** Need a key.")
    assert _kinds(ws) == ["comment_create"]  # no issue_update at all
    assert ws.issues[TASK]["labels"] == [] and ws.issues[TASK]["state"] == "st-todo"


@pytest.mark.parametrize("question", [None, "", "   ", 7])
def test_blocked_needs_question(pin_ctx, ws, question):
    args = {"id": "RET-2", "state": "blocked"}
    if question is not None:
        args["question"] = question
    assert linear_task_state(args) == {"ok": False, "error": "BAD_ARGS"}
    assert ws.writes() == [] and ws.calls == []


def test_unblocked_removes_label(pin_ctx, ws):
    ws.issues[TASK]["labels"] = ["lb-bug", BLOCKED]
    out = linear_task_state({"id": "RET-2", "state": "unblocked"})
    assert out == {"ok": True, "issue": "RET-2", "state": "Todo", "label_missing": False}
    assert ws.issues[TASK]["labels"] == ["lb-bug"]
    assert [w[1] for w in ws.writes()] == [(TASK, {"removedLabelIds": [BLOCKED]})]


def test_unblocked_noop_makes_no_writes(pin_ctx, ws):
    # not labelled
    out = linear_task_state({"id": "RET-2", "state": "unblocked"})
    assert out["ok"] is True and ws.writes() == []
    # no Blocked label on the team at all
    ws.workspace_labels = []
    out = linear_task_state({"id": "RET-2", "state": "unblocked"})
    assert out == {"ok": True, "issue": "RET-2", "state": "Todo", "label_missing": True}
    assert ws.writes() == []


def test_task_state_out_of_scope(pin_ctx, ws):
    for st in ("in_progress", "done", "unblocked"):
        assert linear_task_state({"id": "OTH-9", "state": st}) == {"ok": False,
                                                                  "error": "OUT_OF_SCOPE"}
    out = linear_task_state({"id": "OTH-9", "state": "blocked", "question": "q"})
    assert out == {"ok": False, "error": "OUT_OF_SCOPE"}
    out = linear_task_state({"id": "OTH-9", "state": "done", "summary": "s"})
    assert out == {"ok": False, "error": "OUT_OF_SCOPE"}
    assert ws.writes() == []
    pin_ctx(linear=False)
    assert linear_task_state({"id": "RET-2", "state": "done"}) == {
        "ok": False, "error": "NOT_LINEAR_SESSION"}
    assert ws.writes() == []


@pytest.mark.parametrize("args", [None, "x", {}, {"id": "RET-2"}, {"state": "done"},
                                  {"id": "RET-2", "state": "finished"},
                                  {"id": "RET-2", "state": 3},
                                  {"id": "RET-2", "state": "done", "summary": 5}])
def test_bad_args(pin_ctx, ws, args):
    assert linear_task_state(args) == {"ok": False, "error": "BAD_ARGS"}
    assert ws.writes() == []


# ----------------------------------------------------------------- shared update path (N1)


def test_update_noop_label_removal_sends_no_write(pin_ctx, ws):
    """Task 2 minor N1: an update whose only change is a no-op removal writes nothing."""
    out = linear_issue_update({"id": "RET-2", "remove_labels": ["no-such-label"]})
    assert out["ok"] is True and out["issue"]["identifier"] == "RET-2"
    out = linear_issue_update({"id": "RET-2", "remove_labels": ["Bug"]})  # not carried
    assert out["ok"] is True and out["issue"]["id"] == TASK
    assert ws.writes() == []


# ------------------------------------------------------------- open-task tracking (RET-94 T4)


def test_task_state_tracks_open_tasks(pin_ctx, ws, adapter):
    """in_progress / in_review open the task for the session ticket; done, blocked and
    unblocked close it; a failed call changes nothing."""
    from hermes_linear.store import Store
    adapter._store = store = Store.open(":memory:")
    assert linear_task_state({"id": "RET-2", "state": "in_progress"})["ok"] is True
    assert store.open_tasks(SELF) == [TASK]
    assert linear_task_state({"id": "RET-2", "state": "in_review"})["ok"] is True
    assert store.open_tasks(SELF) == [TASK]
    assert linear_task_state({"id": "RET-2", "state": "done"})["ok"] is True
    assert store.open_tasks(SELF) == []
    for st, extra in (("blocked", {"question": "q?"}), ("unblocked", {})):
        store.set_open_task(SELF, TASK, True)
        assert linear_task_state({"id": "RET-2", "state": st, **extra})["ok"] is True
        assert store.open_tasks(SELF) == [], st
    # a refusal (out of scope) changes nothing, either way
    store.set_open_task(SELF, "uuid-other", True)
    for st in ("in_progress", "done"):
        assert linear_task_state({"id": "OTH-9", "state": st})["error"] == "OUT_OF_SCOPE"
    assert store.open_tasks(SELF) == ["uuid-other"]
    assert linear_task_state({"id": "RET-2", "state": "finished"})["error"] == "BAD_ARGS"
    assert store.open_tasks(SELF) == ["uuid-other"]


def test_task_state_blocked_label_missing_still_closes(pin_ctx, ws, adapter):
    """LABEL_MISSING with the question posted: the task is blocked, so no longer open."""
    from hermes_linear.store import Store
    adapter._store = store = Store.open(":memory:")
    store.set_open_task(SELF, TASK, True)
    ws.workspace_labels = []
    out = linear_task_state({"id": "RET-2", "state": "blocked", "question": "q?"})
    assert out["error"] == "LABEL_MISSING" and out["question_posted"] is True
    assert store.open_tasks(SELF) == []


def test_task_state_linear_error_changes_nothing(pin_ctx, ws, adapter, monkeypatch):
    from hermes_linear.store import Store
    adapter._store = store = Store.open(":memory:")

    async def boom(*_a, **_k):
        raise agent_tools.LinearError("boom")
    monkeypatch.setattr(ws, "issue_update", boom)
    assert linear_task_state({"id": "RET-2", "state": "in_progress"})["ok"] is False
    assert store.open_tasks(SELF) == []


def test_task_state_without_store_still_works(pin_ctx, ws, adapter, loop):
    adapter._store = None
    assert linear_task_state({"id": "RET-2", "state": "in_progress"})["ok"] is True
    # no adapter at all: tracking is skipped, the transition still succeeds
    ctx = agent_tools.ToolContext(adapter=None, client=ws, session_issue_id=SELF,
                                  session_key="sk", is_linear=True)
    a = agent_tools._parse_task_state({"id": "RET-2", "state": "done"})
    out = asyncio.run_coroutine_threadsafe(agent_tools._task_state(ctx, a), loop).result(5)
    assert out["ok"] is True and out["state"] == "Done"
