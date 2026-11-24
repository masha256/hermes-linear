"""``linear_issue_get`` fields, relations, attachments and comment threads (RET-120 spec
§1.1, §2.2).

Handlers are called from the test thread; the adapter's loop runs in a background thread,
as in ``test_linear_query``. Context resolution is pinned with ``pin_ctx``.
"""

from __future__ import annotations

import asyncio
import json
import random
import threading

import pytest

from test_agent_tools_runtime import new_adapter

from hermes_linear import agent_tools, tools_read
from hermes_linear.agent_tools import TOOL_HANDLERS, TOOL_SCHEMAS

linear_issue_get = TOOL_HANDLERS["linear_issue_get"]
APP = "app-user"

ISSUE = {"id": "uuid-1", "identifier": "RET-1", "title": "T", "description": "D",
         "url": "https://linear.app/x/issue/RET-1", "state": {"id": "s1", "name": "Todo",
                                                             "type": "unstarted"},
         "team": {"id": "t1"}, "project": None, "parent": None, "labels": [], "children": []}

DETAILS = {
    "assignee": {"id": "u1", "name": "Mike"}, "delegate": None,
    "priority": 2, "priorityLabel": "High", "estimate": 3, "dueDate": "2026-12-01",
    "cycle": {"id": "c4", "number": 4, "name": "C4"}, "projectMilestone": None,
    "relations": {"nodes": [{"type": "blocks", "relatedIssue": {
        "identifier": "RET-2", "title": "Two", "state": {"name": "Todo"}}}]},
    "inverseRelations": {"nodes": [{"type": "related", "issue": {
        "identifier": "RET-3", "title": "Three", "state": {"name": "Done"}}}]},
    "attachments": {"nodes": [{"id": "a1", "title": "PR", "subtitle": "open",
                               "url": "https://github.com/o/r/pull/1", "sourceType": "github"}]},
}


def comment(cid, created, *, user=("u1", "Mike"), body="b", resolved=None, replies=()):
    node = {"id": cid, "body": body, "createdAt": created, "resolvedAt": resolved,
            "user": {"id": user[0], "name": user[1]} if user else None}
    if replies is not None:
        node["url"] = f"https://linear.app/x/issue/RET-1#comment-{cid}"
        node["children"] = {"nodes": list(replies)}
    return node


def reply(cid, created, **kw):
    return comment(cid, created, replies=None, **kw)


class StubClient:
    """Answers the three reads; records every call."""

    def __init__(self, *, issue=None, details=None, threads=()):
        self.issue = dict(ISSUE if issue is None else issue)
        self.details = DETAILS if details is None else details
        self.threads = list(threads)
        self.calls: list[tuple] = []

    async def issue_get(self, ref):
        self.calls.append(("issue_get", ref))
        return dict(self.issue)

    async def issue_details(self, issue_id):
        self.calls.append(("issue_details", issue_id))
        return self.details

    async def issue_threads(self, issue_id, first):
        self.calls.append(("issue_threads", issue_id, first))
        return self.threads

    async def viewer_id(self):
        self.calls.append(("viewer_id",))
        return APP


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
def stub():
    return StubClient()


@pytest.fixture
def adapter(loop, stub):
    return new_adapter(loop, client=stub)


@pytest.fixture
def pin_ctx(monkeypatch, adapter):
    def pin(*, linear=True, issue="uuid-self", key="sk", ad=adapter):
        ctx = agent_tools.ToolContext(adapter=ad, client=getattr(ad, "client", None),
                                      session_issue_id=issue if linear else None,
                                      session_key=key if linear else None, is_linear=linear)
        monkeypatch.setattr(agent_tools, "resolve_context", lambda session_id=None: ctx)
        return ctx
    pin()
    return pin


def size(result: dict) -> int:
    return len(json.dumps(result, ensure_ascii=False, default=str))


def test_details_mapping(pin_ctx, stub):
    out = linear_issue_get({"id": "RET-1"})
    assert out["ok"] is True
    issue = out["issue"]
    for key, val in ISSUE.items():  # every existing issue_get key unchanged
        assert issue[key] == val
    assert {k: issue[k] for k in issue if k not in ISSUE} == {
        "assignee": {"id": "u1", "name": "Mike"},
        "delegate": None,
        "priority": 2,
        "priority_label": "High",
        "estimate": 3,
        "due_date": "2026-12-01",
        "cycle": {"id": "c4", "number": 4, "name": "C4"},
        "milestone": None,
        "relations": [
            {"type": "blocks", "direction": "outbound",
             "issue": {"identifier": "RET-2", "title": "Two", "state": "Todo"}},
            {"type": "related", "direction": "inbound",
             "issue": {"identifier": "RET-3", "title": "Three", "state": "Done"}},
        ],
        "attachments": [{"id": "a1", "title": "PR", "subtitle": "open",
                         "url": "https://github.com/o/r/pull/1", "source_type": "github"}],
    }
    assert stub.calls == [("issue_get", "RET-1"), ("issue_details", "uuid-1")]


def test_details_nulls_and_milestone():
    out = tools_read.shape_details({
        "assignee": None, "delegate": {"id": "app", "name": "Neo"}, "priority": 0,
        "priorityLabel": "No priority", "estimate": None, "dueDate": None, "cycle": None,
        "projectMilestone": {"id": "m1", "name": "Beta"}, "relations": None,
        "inverseRelations": {"nodes": []}, "attachments": {"nodes": []}})
    assert out["assignee"] is None and out["cycle"] is None
    assert out["delegate"] == {"id": "app", "name": "Neo"}
    assert out["milestone"] == {"id": "m1", "name": "Beta"}
    assert out["relations"] == [] and out["attachments"] == []


@pytest.mark.parametrize("args", [{"id": "RET-1"}, {"id": "RET-1", "comments": 0}])
def test_comments_zero_runs_no_thread_query(pin_ctx, stub, args):
    out = linear_issue_get(args)
    assert out["ok"] is True
    assert "comments" not in out["issue"] and "comments_truncated" not in out["issue"]
    assert [c[0] for c in stub.calls] == ["issue_get", "issue_details"]


def test_thread_order(pin_ctx, stub):
    rs = [reply(f"r{i}", f"2026-11-0{i}T00:00:00Z", user=(APP, "Neo") if i == 2 else ("u1", "Mike"),
                resolved="2026-11-09T00:00:00Z" if i == 3 else None) for i in range(1, 5)]
    random.Random(7).shuffle(rs)
    threads = [comment("t2", "2026-11-12T00:00:00Z", user=(APP, "Neo"), replies=rs),
               comment("t3", "2026-11-13T00:00:00Z", resolved="2026-11-14T00:00:00Z"),
               comment("t1", "2026-11-11T00:00:00Z", user=None)]
    stub.threads = threads
    out = linear_issue_get({"id": "RET-1", "comments": 3})
    issue = out["issue"]
    assert stub.calls[-2:] == [("issue_threads", "uuid-1", 3), ("viewer_id",)]
    assert issue["comments_truncated"] is False
    got = issue["comments"]
    assert [t["id"] for t in got] == ["t3", "t2", "t1"]
    assert [t["author_is_app"] for t in got] == [False, True, False]
    assert [t["resolved"] for t in got] == [True, False, False]
    assert got[2]["author"] is None
    assert got[0] == {"id": "t3", "url": "https://linear.app/x/issue/RET-1#comment-t3",
                      "author": {"id": "u1", "name": "Mike"}, "author_is_app": False,
                      "created_at": "2026-11-13T00:00:00Z", "body": "b", "resolved": True,
                      "replies": []}
    replies = got[1]["replies"]
    assert [r["id"] for r in replies] == ["r1", "r2", "r3", "r4"]
    assert [r["author_is_app"] for r in replies] == [False, True, False, False]
    assert [r["resolved"] for r in replies] == [False, False, True, False]
    assert set(replies[0]) == {"id", "author", "author_is_app", "created_at", "body",
                               "resolved"}


def big_threads(n: int, chars: int) -> list[dict]:
    return [comment(f"t{i}", f"2026-11-{10 + i}T00:00:00Z", body="x" * chars)
            for i in range(n)]


def test_trim_drops_oldest_threads(pin_ctx, stub):
    stub.threads = big_threads(3, 20_000)
    out = linear_issue_get({"id": "RET-1", "comments": 3})
    issue = out["issue"]
    assert [t["id"] for t in issue["comments"]] == ["t2", "t1"]  # the two newest
    assert issue["comments_truncated"] is True
    assert size(out) <= tools_read.ISSUE_GET_MAX_CHARS


def test_single_huge_thread_drops_all_threads(pin_ctx, stub):
    """Review Focus 1: the newest thread alone is over the limit."""
    stub.threads = [comment("t9", "2026-11-20T00:00:00Z", body="x" * 60_000),
                    comment("t1", "2026-11-01T00:00:00Z")]
    out = linear_issue_get({"id": "RET-1", "comments": 2})
    assert out["ok"] is True and out["issue"]["identifier"] == "RET-1"
    assert out["issue"]["comments"] == [] and out["issue"]["comments_truncated"] is True


def test_huge_description_not_trimmed(pin_ctx, stub):
    stub.issue = {**ISSUE, "description": "d" * 60_000}
    out = linear_issue_get({"id": "RET-1", "comments": 0})
    assert out["issue"]["description"] == "d" * 60_000
    assert "comments_truncated" not in out["issue"]


def test_trim_leaves_small_result_alone():
    result = {"ok": True, "issue": {"comments": [{"id": "t"}], "comments_truncated": False}}
    assert tools_read.trim(result, limit=10_000) == result


@pytest.mark.parametrize("n", [51, -1, "5", True, 2.0])
def test_comments_bounds(pin_ctx, stub, n):
    assert linear_issue_get({"id": "RET-1", "comments": n}) == {"ok": False,
                                                                "error": "BAD_ARGS"}
    assert stub.calls == []


def test_comments_max_allowed(pin_ctx, stub):
    assert linear_issue_get({"id": "RET-1", "comments": 50})["ok"] is True
    assert ("issue_threads", "uuid-1", 50) in stub.calls


def test_schema():
    assert next(iter(TOOL_SCHEMAS)) == "linear_issue_get"  # keeps its first slot
    assert next(iter(TOOL_HANDLERS)) == "linear_issue_get"
    assert TOOL_HANDLERS["linear_issue_get"] is agent_tools.linear_issue_get
    s = TOOL_SCHEMAS["linear_issue_get"]
    assert s is tools_read.SCHEMAS["linear_issue_get"]
    assert s["name"] == "linear_issue_get"
    props = s["parameters"]["properties"]
    assert props["id"]["type"] == "string"
    comments = props["comments"]
    assert {k: v for k, v in comments.items() if k != "description"} == {
        "type": "integer", "minimum": 0, "maximum": 50}
    assert set(comments) == {"type", "minimum", "maximum", "description"}
    assert comments["description"]
    assert s["parameters"]["required"] == ["id"]
