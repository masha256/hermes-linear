"""``linear_issue_get`` fields, relations, attachments and comment threads (RET-120 spec
§1.1, §2.2).

Handlers are called from the test thread; the adapter's loop runs in a background thread,
as in ``test_linear_query``. Context resolution is pinned with ``pin_ctx``.
"""

from __future__ import annotations

import asyncio
import json
import random
import subprocess
import sys
import threading

import pytest

from conftest import REPO_ROOT
from test_agent_tools_runtime import new_adapter

from hermes_linear import agent_tools, tools_read
from hermes_linear.agent_tools import TOOL_HANDLERS, TOOL_SCHEMAS
from hermes_linear.linear_client import LinearError

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

    page: dict = {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}

    async def search_issues(self, term, filter, include_comments, first, after):  # noqa: A002
        self.calls.append(("search_issues", term, filter, include_comments, first, after))
        return self.page

    async def list_issues(self, filter, first, after):  # noqa: A002
        self.calls.append(("list_issues", filter, first, after))
        return self.page


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
    assert "up to its 50 newest replies" in comments["description"]  # QUERY_ISSUE_THREADS cap
    assert s["parameters"]["required"] == ["id"]


# --- linear_search (RET-120 spec §1.2, §2.3) -----------------------------------------------

linear_search = TOOL_HANDLERS["linear_search"] if "linear_search" in TOOL_HANDLERS else None
U = "0a1b2c3d-0000-4000-8000-00000000abcd"
P = "uuid-parent"
ME = {"isMe": {"eq": True}}
EMAIL = {"email": {"eqIgnoreCase": "a@b.c"}}
NAMED = {"or": [{"name": {"eqIgnoreCase": "Mike"}}, {"displayName": {"eqIgnoreCase": "Mike"}}]}


@pytest.mark.parametrize("args, parent_id, want", [
    ({}, None, {}),
    ({"team": "ret"}, None, {"team": {"key": {"eq": "RET"}}}),
    ({"project": U}, None, {"project": {"id": {"eq": U}}}),
    ({"project": U.upper()}, None, {"project": {"id": {"eq": U.upper()}}}),
    ({"project": "hermes-linear"}, None,
     {"project": {"name": {"eqIgnoreCase": "hermes-linear"}}}),
    ({"state": "Started"}, None, {"state": {"type": {"eq": "started"}}}),
    ({"state": "canceled"}, None, {"state": {"type": {"eq": "canceled"}}}),
    ({"state": "In Review"}, None, {"state": {"name": {"eqIgnoreCase": "In Review"}}}),
    ({"assignee": "me"}, None, {"assignee": ME}),
    ({"assignee": "a@b.c"}, None, {"assignee": EMAIL}),
    ({"assignee": "Mike"}, None, {"assignee": NAMED}),
    ({"delegate": "me"}, None, {"delegate": ME}),
    ({"delegate": "a@b.c"}, None, {"delegate": EMAIL}),
    ({"delegate": "Mike"}, None, {"delegate": NAMED}),
    ({"label": "bug"}, None, {"labels": {"some": {"name": {"eqIgnoreCase": "bug"}}}}),
    ({"parent": "RET-5"}, P, {"parent": {"id": {"eq": P}}}),
    ({"text": "x", "limit": 5, "cursor": "c"}, None, {}),  # non-filter args add nothing
    ({"team": "RET", "state": "started", "label": "bug", "parent": "RET-5"}, P,
     {"and": [{"team": {"key": {"eq": "RET"}}}, {"state": {"type": {"eq": "started"}}},
              {"labels": {"some": {"name": {"eqIgnoreCase": "bug"}}}},
              {"parent": {"id": {"eq": P}}}]}),
])
def test_build_issue_filter(args, parent_id, want):
    assert tools_read.build_issue_filter(args, parent_id) == want


def test_build_issue_filter_order_is_argument_order():
    """The ``and`` list follows the signature's argument order, not the dict's key order."""
    args = {"label": "bug", "delegate": "me", "assignee": "me", "state": "x",
            "project": "p", "team": "t"}
    got = tools_read.build_issue_filter(args, None)
    assert [next(iter(c)) for c in got["and"]] == [
        "team", "project", "state", "assignee", "delegate", "labels"]


NODE = {"identifier": "RET-7", "title": "Seven", "url": "https://linear.app/x/issue/RET-7",
        "updatedAt": "2026-11-20T00:00:00Z", "priority": 2, "state": {"name": "Todo"},
        "assignee": {"name": "Mike"}, "delegate": {"name": "Neo"}, "project": {"name": "HL"}}
BARE = {"identifier": "RET-8", "title": "Eight", "url": "u8", "updatedAt": "t8", "priority": 0,
        "state": None, "assignee": None, "delegate": None, "project": None}
ROW = {"identifier": "RET-7", "title": "Seven", "state": "Todo", "assignee": "Mike",
       "delegate": "Neo", "priority": 2, "project": "HL",
       "url": "https://linear.app/x/issue/RET-7", "updated_at": "2026-11-20T00:00:00Z"}
BARE_ROW = {"identifier": "RET-8", "title": "Eight", "state": None, "assignee": None,
            "delegate": None, "priority": 0, "project": None, "url": "u8", "updated_at": "t8"}


def page(nodes, more=False, end=None):
    return {"nodes": nodes, "pageInfo": {"hasNextPage": more, "endCursor": end}}


def test_search_text_uses_search_issues(pin_ctx, stub):
    stub.page = page([NODE, BARE])
    out = linear_search({"text": "attach", "team": "ret", "include_comments": True,
                         "limit": 7, "cursor": "c1"})
    assert out == {"ok": True, "issues": [ROW, BARE_ROW], "next_cursor": None}
    assert stub.calls == [("search_issues", "attach", {"team": {"key": {"eq": "RET"}}},
                           True, 7, "c1")]


def test_search_text_defaults(pin_ctx, stub):
    linear_search({"text": "attach"})
    assert stub.calls == [("search_issues", "attach", {}, False, 25, None)]


def test_no_text_uses_list_issues(pin_ctx, stub):
    stub.page = page([NODE])
    out = linear_search({"state": "started", "limit": 50, "cursor": "c2"})
    assert out == {"ok": True, "issues": [ROW], "next_cursor": None}
    assert stub.calls == [("list_issues", {"state": {"type": {"eq": "started"}}}, 50, "c2")]


def test_no_arguments_lists_recent(pin_ctx, stub):
    assert linear_search({})["ok"] is True
    assert stub.calls == [("list_issues", {}, 25, None)]


@pytest.mark.parametrize("more, end, want", [(True, "e1", "e1"), (False, "e1", None),
                                             (False, None, None)])
def test_next_cursor(pin_ctx, stub, more, end, want):
    stub.page = page([NODE], more=more, end=end)
    assert linear_search({})["next_cursor"] == want


def test_parent_resolved_by_issue_get(pin_ctx, stub):
    stub.issue = {**ISSUE, "id": "uuid-5", "identifier": "RET-5"}
    out = linear_search({"parent": "RET-5", "label": "bug"})
    assert out["ok"] is True
    assert stub.calls == [
        ("issue_get", "RET-5"),
        ("list_issues", {"and": [{"labels": {"some": {"name": {"eqIgnoreCase": "bug"}}}},
                                 {"parent": {"id": {"eq": "uuid-5"}}}]}, 25, None)]


def test_unknown_parent_is_not_found(pin_ctx, stub):
    async def missing(ref):
        stub.calls.append(("issue_get", ref))
        raise LinearError("Entity not found", code="NOT_FOUND")

    stub.issue_get = missing
    assert linear_search({"parent": "NOPE-1"}) == {"ok": False, "error": "NOT_FOUND",
                                                    "message": "Entity not found"}
    assert stub.calls == [("issue_get", "NOPE-1")]  # no search after the failed lookup


@pytest.mark.parametrize("method, args", [("search_issues", {"text": "x"}),
                                          ("list_issues", {})])
def test_search_passes_linear_error_code(pin_ctx, stub, method, args):
    async def limited(*a):
        raise LinearError("rate limited", code="RATELIMITED")

    setattr(stub, method, limited)
    assert linear_search(args) == {"ok": False, "error": "RATELIMITED",
                                   "message": "rate limited"}


@pytest.mark.parametrize("args", [
    {"include_comments": True},
    {"include_comments": True, "team": "RET"},
    {"text": "x", "include_comments": "yes"},
    {"limit": 0}, {"limit": 51}, {"limit": "5"}, {"limit": True}, {"limit": 2.0},
    {"team": 5}, {"project": ["p"]}, {"state": 1}, {"assignee": True},
    {"delegate": {}}, {"label": 1.5}, {"parent": 7}, {"cursor": 3}, {"text": 1},
    {"text": ""}, {"text": "   "}, {"team": ""}, {"parent": " "},
])
def test_bad_args(pin_ctx, stub, args):
    assert linear_search(args) == {"ok": False, "error": "BAD_ARGS"}
    assert stub.calls == []


def test_search_schema():
    s = TOOL_SCHEMAS["linear_search"]
    assert s is tools_read.SCHEMAS["linear_search"] and s["name"] == "linear_search"
    assert "linear_query" in s["description"]
    params = s["parameters"]
    assert "required" not in params
    props = params["properties"]
    assert list(props) == ["text", "team", "project", "state", "assignee", "delegate",
                           "label", "parent", "include_comments", "limit", "cursor"]
    for k in ("text", "team", "project", "state", "assignee", "delegate", "label", "parent",
              "cursor"):
        assert props[k]["type"] == "string", k
    assert props["include_comments"]["type"] == "boolean"
    assert {k: v for k, v in props["limit"].items() if k != "description"} == {
        "type": "integer", "minimum": 1, "maximum": 50}
    assert all(p.get("description") for p in props.values())
    assert agent_tools.linear_search is TOOL_HANDLERS["linear_search"]


# --- Ruling R3: the roster merge works whichever module is imported first ------------------

@pytest.mark.parametrize("imports", [
    "import hermes_linear.tools_read; from hermes_linear import agent_tools",
    "from hermes_linear import agent_tools; import hermes_linear.tools_read",
])
def test_roster_merge_any_import_order(imports):
    code = (f"{imports}; print(list(agent_tools.TOOL_SCHEMAS)[0], "
            "'linear_search' in agent_tools.TOOL_HANDLERS)")
    proc = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "linear_issue_get True"


def test_include_comments_false_without_text_is_allowed(pin_ctx, stub):
    """Only ``include_comments: true`` needs ``text``; ``false`` is the default anyway."""
    assert linear_search({"include_comments": False})["ok"] is True
    assert stub.calls == [("list_issues", {}, 25, None)]
