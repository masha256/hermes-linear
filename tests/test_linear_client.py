"""Tests for hermes_linear.linear_client against tests/fake_linear."""

from __future__ import annotations

import asyncio
import base64

import aiohttp
import pytest

from hermes_linear.constants import TOKEN_RENEW_MARGIN_S, WRITE_RETRIES
from hermes_linear.linear_client import LinearClient, LinearError
from fake_linear import FakeLinear, Resp

APP_USER = "app-user-1"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
async def fake():
    f = FakeLinear()
    await f.start()
    f.on("Viewer", {"data": {"viewer": {"id": APP_USER, "name": "Hermes"}}})
    yield f
    await f.close()


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
async def client(fake, sleeps):
    async def fake_sleep(s):
        sleeps.append(s)

    c = LinearClient("cid", lambda: "s3cret", base_url=fake.url, sleep=fake_sleep)
    yield c
    await c.close()


# -- tokens ------------------------------------------------------------


async def test_token_client_credentials_basic_auth(fake, client):
    tok = await client.token()
    assert tok == "tok-1"
    (call,) = fake.token_calls()
    assert call.form == {
        "grant_type": "client_credentials",
        "scope": "read,write,app:mentionable,app:assignable",
    }
    expected = base64.b64encode(b"cid:s3cret").decode()
    assert call.headers["Authorization"] == f"Basic {expected}"
    assert call.headers["Content-Type"].startswith("application/x-www-form-urlencoded")


async def test_token_cached_until_margin(fake):
    clock = Clock()
    c = LinearClient("cid", lambda: "s", base_url=fake.url, clock=clock)
    try:
        assert await c.token() == "tok-1"
        clock.t += fake.expires_in - TOKEN_RENEW_MARGIN_S - 1
        assert await c.token() == "tok-1"
        assert len(fake.token_calls()) == 1
        clock.t += 1
        assert await c.token() == "tok-2"
        assert len(fake.token_calls()) == 2
    finally:
        await c.close()


async def test_client_secret_read_lazily(fake):
    secrets = iter(["first", "second"])
    clock = Clock()
    c = LinearClient("cid", lambda: next(secrets), base_url=fake.url, clock=clock)
    try:
        await c.token()
        clock.t += fake.expires_in
        await c.token()
        auths = [k.headers["Authorization"] for k in fake.token_calls()]
        assert auths == [
            "Basic " + base64.b64encode(b"cid:first").decode(),
            "Basic " + base64.b64encode(b"cid:second").decode(),
        ]
    finally:
        await c.close()


async def test_token_endpoint_error_raises(fake, client):
    fake.token_responses = [Resp(401, {"error": "invalid_client"})]
    with pytest.raises(LinearError) as ei:
        await client.token()
    assert ei.value.status == 401
    assert ei.value.code == "invalid_client"
    assert "s3cret" not in str(ei.value)


async def test_bearer_header_on_graphql(fake, client):
    await client.viewer_id()
    (call,) = fake.gql_calls("Viewer")
    assert call.headers["Authorization"] == "Bearer tok-1"


async def test_401_renews_once_and_retries(fake, client):
    await client.token()
    fake.reject_tokens = {"tok-1"}
    assert await client.viewer_id() == APP_USER
    assert len(fake.token_calls()) == 2
    assert [c.bearer for c in fake.gql_calls("Viewer")] == ["tok-1", "tok-2"]


async def test_401_twice_raises(fake, client):
    fake.reject_tokens = {"tok-1", "tok-2", "tok-3"}
    with pytest.raises(LinearError) as ei:
        await client.viewer_id()
    assert ei.value.status == 401
    assert len(fake.token_calls()) == 2
    assert len(fake.gql_calls("Viewer")) == 2


async def test_concurrent_401_single_renew(fake, client):
    await client.token()
    fake.reject_tokens = {"tok-1"}
    fake.on("User", lambda call: {"data": {"user": {
        "email": f"{call.variables['id']}@x", "name": call.variables["id"]}}})
    results = await asyncio.gather(*(client.user(f"u{i}") for i in range(10)))
    assert results == [(f"u{i}@x", f"u{i}") for i in range(10)]
    assert len(fake.token_calls()) == 2  # initial + exactly one renewal
    bearers = [c.bearer for c in fake.gql_calls("User")]
    assert bearers.count("tok-1") == 10
    assert bearers.count("tok-2") == 10


# -- rate limits / errors -----------------------------------------------


async def test_429_respects_retry_after(fake, client, sleeps):
    ok = {"data": {"viewer": {"id": APP_USER, "name": "Hermes"}}}
    fake.on("Viewer", Resp(429, None, {"Retry-After": "7"}),
            Resp(400, {"errors": [{"message": "slow down",
                                   "extensions": {"code": "RATELIMITED"}}]},
                 {"Retry-After": "2"}),
            ok)
    assert await client.viewer_id() == APP_USER
    assert sleeps == [7, 2]
    assert len(fake.gql_calls("Viewer")) == 3


async def test_429_gives_up_after_write_retries(fake, client, sleeps):
    fake.on("Viewer", Resp(429, None, {"Retry-After": "1"}))
    with pytest.raises(LinearError) as ei:
        await client.viewer_id()
    assert ei.value.code == "RATELIMITED"
    assert ei.value.status == 429
    assert len(sleeps) == WRITE_RETRIES
    assert len(fake.gql_calls("Viewer")) == WRITE_RETRIES + 1


async def test_graphql_errors_raise(fake, client):
    fake.on("Viewer", Resp(400, {"errors": [{"message": "Argument Validation Error",
                                             "extensions": {"code": "INVALID_INPUT"}}]}))
    with pytest.raises(LinearError) as ei:
        await client.viewer_id()
    assert ei.value.code == "INVALID_INPUT"
    assert ei.value.status == 400
    assert "Argument Validation Error" in str(ei.value)


async def test_graphql_errors_with_200_raise(fake, client):
    fake.on("Viewer", {"data": None, "errors": [{"message": "boom"}]})
    with pytest.raises(LinearError):
        await client.viewer_id()


async def test_server_error_non_json_raises(fake, client):
    fake.on("Viewer", Resp(502, None))
    with pytest.raises(LinearError) as ei:
        await client.viewer_id()
    assert ei.value.status == 502


# -- queries / mutations ------------------------------------------------


async def test_viewer_id_cached(fake, client):
    assert await client.viewer_id() == APP_USER
    assert await client.viewer_id() == APP_USER
    assert len(fake.gql_calls("Viewer")) == 1


async def test_user_returns_email_and_name(fake, client):
    fake.on("User", {"data": {"user": {"email": "a@b.c", "name": "Ann"}}})
    assert await client.user("u1") == ("a@b.c", "Ann")
    assert fake.gql_calls("User")[0].variables == {"id": "u1"}


async def test_create_activity_payload(fake, client):
    fake.on("AgentActivityCreate",
            {"data": {"agentActivityCreate": {"success": True, "agentActivity": {"id": "act-1"}}}})
    content = {"type": "elicitation", "body": "Pick one"}
    act = await client.create_activity(
        "sess-1", content, ephemeral=True, signal="select",
        signal_metadata={"options": [{"value": "a"}]},
    )
    assert act == "act-1"
    (call,) = fake.gql_calls("AgentActivityCreate")
    assert call.variables == {"input": {
        "agentSessionId": "sess-1",
        "content": content,
        "ephemeral": True,
        "signal": "select",
        "signalMetadata": {"options": [{"value": "a"}]},
    }}


async def test_create_activity_minimal_payload(fake, client):
    fake.on("AgentActivityCreate",
            {"data": {"agentActivityCreate": {"success": True, "agentActivity": {"id": "act-2"}}}})
    await client.create_activity("sess-1", {"type": "response", "body": "done"})
    (call,) = fake.gql_calls("AgentActivityCreate")
    assert call.variables == {"input": {
        "agentSessionId": "sess-1",
        "content": {"type": "response", "body": "done"},
        "ephemeral": False,
    }}


async def test_create_activity_unsuccessful_raises(fake, client):
    fake.on("AgentActivityCreate",
            {"data": {"agentActivityCreate": {"success": False, "agentActivity": None}}})
    with pytest.raises(LinearError):
        await client.create_activity("sess-1", {"type": "thought", "body": "x"})


async def test_create_session_on_issue_returns_id(fake, client):
    fake.on("AgentSessionCreateOnIssue", {"data": {"agentSessionCreateOnIssue": {
        "success": True, "agentSession": {"id": "sess-i"}}}})
    assert await client.create_session_on_issue("iss-1") == "sess-i"
    (call,) = fake.gql_calls("AgentSessionCreateOnIssue")
    assert call.variables == {"input": {"issueId": "iss-1"}}


def _session(sid, created, *, app=APP_USER, issue="iss-1", dismissed=None):
    return {"id": sid, "createdAt": created, "dismissedAt": dismissed, "status": "active",
            "appUser": {"id": app}, "issue": {"id": issue, "identifier": "ENG-1"}}


async def test_app_sessions_filtered_to_viewer(fake, client):
    fake.on("AppSessions", {"data": {"agentSessions": {"nodes": [
        _session("old", "2026-11-01T00:00:00.000Z"),
        _session("other-app", "2026-11-05T00:00:00.000Z", app="someone-else"),
        _session("dismissed", "2026-11-06T00:00:00.000Z", dismissed="2026-11-07T00:00:00.000Z"),
        _session("other-issue", "2026-11-08T00:00:00.000Z", issue="iss-2"),
        _session("new", "2026-11-10T00:00:00.000Z"),
    ]}}})
    sessions = await client.app_sessions_on_issue("iss-1")
    assert [s["id"] for s in sessions] == ["new", "old"]


async def test_update_session_plan_and_urls(fake, client):
    fake.on("AgentSessionUpdate", {"data": {"agentSessionUpdate": {"success": True}}})
    plan = [{"content": "Investigate", "status": "inProgress"}]
    urls = [{"label": "PR", "url": "https://example.com/pr/1"}]
    await client.update_session("sess-1", plan=plan, external_urls=urls)
    (call,) = fake.gql_calls("AgentSessionUpdate")
    assert call.variables == {"id": "sess-1", "input": {"plan": plan, "externalUrls": urls}}


async def test_update_session_added_urls(fake, client):
    fake.on("AgentSessionUpdate", {"data": {"agentSessionUpdate": {"success": True}}})
    urls = [{"label": "Hermes session", "url": "https://h/s/1"}]
    await client.update_session("sess-1", added_external_urls=urls)
    (call,) = fake.gql_calls("AgentSessionUpdate")
    assert call.variables == {"id": "sess-1", "input": {"addedExternalUrls": urls}}
    with pytest.raises(ValueError):
        await client.update_session("sess-1", external_urls=urls, added_external_urls=urls)


async def test_update_session_noop_without_fields(fake, client):
    await client.update_session("sess-1")
    assert fake.gql_calls("AgentSessionUpdate") == []


async def test_issue_snapshot_shape(fake, client):
    fake.on("IssueSnapshot", {"data": {"issue": {
        "identifier": "ENG-1", "title": "Fix it", "description": "Body",
        "state": {"name": "Todo", "type": "unstarted"},
        "labels": {"nodes": [{"id": "l1", "name": "bug"}, {"id": "l2", "name": "p1"}]},
        "assignee": {"name": "Ann"},
        "project": {"id": "p1", "name": "Proj"},
        "team": {"id": "t1"},
    }}})
    snap = await client.issue_snapshot("iss-1")
    assert snap == {
        "identifier": "ENG-1", "title": "Fix it", "description": "Body",
        "state": {"name": "Todo", "type": "unstarted"},
        "labels": ["bug", "p1"],
        "assignee": "Ann",
        "project": {"id": "p1", "name": "Proj"},
        "team": {"id": "t1"},
    }
    assert fake.gql_calls("IssueSnapshot")[0].variables == {"id": "iss-1"}


async def test_issue_snapshot_nulls(fake, client):
    fake.on("IssueSnapshot", {"data": {"issue": {
        "identifier": "ENG-2", "title": "T", "description": None,
        "state": {"name": "Backlog", "type": "backlog"},
        "labels": {"nodes": []}, "assignee": None, "project": None, "team": {"id": "t1"},
    }}})
    snap = await client.issue_snapshot("iss-2")
    assert snap["assignee"] is None and snap["project"] is None
    assert snap["labels"] == [] and snap["description"] == ""


async def test_move_to_first_started_picks_lowest_position(fake, client):
    fake.on("TeamStartedStates", {"data": {"team": {"states": {"nodes": [
        {"id": "s-review", "name": "In Review", "position": 3.0},
        {"id": "s-prog", "name": "In Progress", "position": 1.5},
        {"id": "s-qa", "name": "QA", "position": 2.0},
    ]}}}})
    fake.on("IssueUpdate", {"data": {"issueUpdate": {"success": True}}})
    assert await client.move_to_first_started("iss-1", "t1") is True
    assert fake.gql_calls("TeamStartedStates")[0].variables == {"teamId": "t1"}
    (upd,) = fake.gql_calls("IssueUpdate")
    assert upd.variables == {"id": "iss-1", "input": {"stateId": "s-prog"}}


async def test_move_to_first_started_none(fake, client):
    fake.on("TeamStartedStates", {"data": {"team": {"states": {"nodes": []}}}})
    assert await client.move_to_first_started("iss-1", "t1") is False
    assert fake.gql_calls("IssueUpdate") == []


# -- session lifecycle -------------------------------------------------


async def test_close_leaves_injected_session_open(fake):
    async with aiohttp.ClientSession() as s:
        c = LinearClient("cid", lambda: "s", base_url=fake.url, session=s)
        await c.token()
        await c.close()
        assert not s.closed


async def test_close_closes_own_session(fake):
    c = LinearClient("cid", lambda: "s", base_url=fake.url)
    await c.token()
    own = c._session
    await c.close()
    assert own.closed


async def test_viewer_returns_handle_and_caches_id(fake, client):
    fake.on("Viewer", {"data": {"viewer": {"id": APP_USER, "name": "Hermes",
                                           "displayName": "hermes"}}})
    v = await client.viewer()
    assert v == {"id": APP_USER, "name": "Hermes", "displayName": "hermes"}
    assert "displayName" in fake.gql_calls("Viewer")[0].json["query"]
    assert await client.viewer_id() == APP_USER
    assert len(fake.gql_calls("Viewer")) == 1


# -- agent tool operations (RET-76) --------------------------------------

ISSUE_NODE = {
    "id": "uuid-1", "identifier": "RET-1", "title": "T", "description": "D",
    "url": "https://linear.app/x/issue/RET-1",
    "state": {"id": "s1", "name": "Todo", "type": "unstarted"},
    "team": {"id": "t1"},
    "project": {"id": "p1", "name": "P",
                "externalLinks": {"nodes": [{"label": "Spec", "url": "https://e.test/s"}]}},
    "parent": {"id": "uuid-0", "identifier": "RET-0"},
    "labels": {"nodes": [{"id": "l1", "name": "Bug"}]},
    "children": {"nodes": [{"id": "uuid-2", "identifier": "RET-2", "title": "C",
                            "subIssueSortOrder": 1.5, "state": {"type": "started"}}]},
}


async def test_client_issue_get_shape(fake, client):
    fake.on("IssueGet", {"data": {"issue": ISSUE_NODE}})
    i = await client.issue_get("RET-1")
    (call,) = fake.gql_calls("IssueGet")
    assert call.variables == {"id": "RET-1"}
    for field in ("subIssueSortOrder", "externalLinks", "children", "parent", "url",
                  "creator { id }"):
        assert field in call.json["query"]
    assert i["id"] == "uuid-1" and i["state"]["type"] == "unstarted"
    assert i["labels"] == [{"id": "l1", "name": "Bug"}]
    assert i["children"] == ISSUE_NODE["children"]["nodes"]
    assert i["project"]["externalLinks"] == [{"label": "Spec", "url": "https://e.test/s"}]
    assert i["parent"] == {"id": "uuid-0", "identifier": "RET-0"}


DETAILS_NODE = {
    "assignee": {"id": "u1", "name": "Mike"}, "delegate": None, "priority": 2,
    "priorityLabel": "High", "estimate": 3, "dueDate": "2026-12-01",
    "cycle": {"id": "c4", "number": 4, "name": "C4"}, "projectMilestone": None,
    "relations": {"nodes": []}, "inverseRelations": {"nodes": []},
    "attachments": {"nodes": [{"id": "a1", "title": "PR", "subtitle": None,
                               "url": "https://e.test/a", "sourceType": "github"}]},
}


async def test_issue_details_query(fake, client):
    fake.on("IssueDetails", {"data": {"issue": DETAILS_NODE}})
    assert await client.issue_details("uuid-1") == DETAILS_NODE
    (call,) = fake.gql_calls("IssueDetails")
    assert call.variables == {"id": "uuid-1"}
    q = call.json["query"]
    for field in ("delegate { id name }", "priorityLabel", "projectMilestone { id name }",
                  "relations(first: 50)", "inverseRelations(first: 50)",
                  "attachments(first: 50)", "sourceType"):
        assert field in q


async def test_issue_details_null_is_not_found(fake, client):
    fake.on("IssueDetails", {"data": {"issue": None}})
    with pytest.raises(LinearError) as ei:
        await client.issue_details("uuid-404")
    assert ei.value.code == "NOT_FOUND"


async def test_issue_threads_query(fake, client):
    threads = [{"id": "c1", "url": "u", "body": "b", "createdAt": "2026-11-01T00:00:00Z",
                "resolvedAt": None, "user": {"id": "u1", "name": "Mike"},
                "children": {"nodes": [{"id": "c2", "body": "r", "createdAt": "x",
                                        "resolvedAt": None, "user": None}]}}]
    fake.on("IssueThreads", {"data": {"issue": {"comments": {"nodes": threads}}}})
    assert await client.issue_threads("uuid-1", 7) == threads
    (call,) = fake.gql_calls("IssueThreads")
    assert call.variables == {"id": "uuid-1", "first": 7}
    q = " ".join(call.json["query"].split())
    assert ("comments(first: $first, orderBy: createdAt, filter: { parent: { null: true } })"
            in q)
    assert "children(first: 50, orderBy: createdAt)" in q


async def test_issue_threads_null_issue_is_not_found(fake, client):
    fake.on("IssueThreads", {"data": {"issue": None}})
    with pytest.raises(LinearError) as ei:
        await client.issue_threads("uuid-404", 1)
    assert ei.value.code == "NOT_FOUND"


SEARCH_FIELDS = ("identifier", "title", "url", "updatedAt", "priority", "state { name }",
                 "assignee { name }", "delegate { name }", "project { name }",
                 "pageInfo { hasNextPage endCursor }")
SEARCH_CONN = {"nodes": [{"identifier": "RET-1", "title": "T", "url": "u", "updatedAt": "t",
                          "priority": 0, "state": {"name": "Todo"}, "assignee": None,
                          "delegate": None, "project": None}],
               "pageInfo": {"hasNextPage": True, "endCursor": "e1"}}


async def test_search_issues_query(fake, client):
    fake.on("SearchIssues", {"data": {"searchIssues": SEARCH_CONN}})
    flt = {"team": {"key": {"eq": "RET"}}}
    assert await client.search_issues("attach", flt, True, 7, "c1") == SEARCH_CONN
    (call,) = fake.gql_calls("SearchIssues")
    assert call.variables == {"term": "attach", "filter": flt, "includeComments": True,
                              "first": 7, "after": "c1"}
    q = " ".join(call.json["query"].split())
    assert ("searchIssues(term: $term, filter: $filter, includeComments: $includeComments, "
            "first: $first, after: $after)") in q
    for field in SEARCH_FIELDS:
        assert field in q, field


async def test_list_issues_query(fake, client):
    fake.on("ListIssues", {"data": {"issues": SEARCH_CONN}})
    assert await client.list_issues({}, 25, None) == SEARCH_CONN
    (call,) = fake.gql_calls("ListIssues")
    assert call.variables == {"filter": {}, "first": 25, "after": None}
    q = " ".join(call.json["query"].split())
    assert "issues(filter: $filter, first: $first, after: $after, orderBy: updatedAt)" in q
    for field in SEARCH_FIELDS:
        assert field in q, field


@pytest.mark.parametrize("op, key", [("SearchIssues", "searchIssues"), ("ListIssues", "issues")])
async def test_search_and_list_missing_connection_is_linear_error(fake, client, op, key):
    fake.on(op, {"data": {key: None}})
    with pytest.raises(LinearError):
        if op == "SearchIssues":
            await client.search_issues("x", {}, False, 1, None)
        else:
            await client.list_issues({}, 1, None)


async def test_client_issue_get_null_is_not_found(fake, client):
    fake.on("IssueGet", {"data": {"issue": None}})
    with pytest.raises(LinearError) as ei:
        await client.issue_get("RET-404")
    assert ei.value.code == "NOT_FOUND"


async def test_client_issue_create(fake, client):
    created = {"id": "uuid-9", "identifier": "RET-9", "title": "N", "url": "u"}
    fake.on("IssueCreate", {"data": {"issueCreate": {"success": True, "issue": created}}})
    inp = {"teamId": "t1", "title": "N", "parentId": "uuid-1"}
    assert await client.issue_create(inp) == created
    assert fake.gql_calls("IssueCreate")[0].variables == {"input": inp}


async def test_client_issue_create_unsuccessful(fake, client):
    fake.on("IssueCreate", {"data": {"issueCreate": {"success": False, "issue": None}}})
    with pytest.raises(LinearError):
        await client.issue_create({"teamId": "t1", "title": "N"})


async def test_client_issue_update(fake, client):
    out = {"id": "uuid-1", "identifier": "RET-1", "title": "T", "url": "u"}
    fake.on("IssueUpdate", {"data": {"issueUpdate": {"success": True, "issue": out}}})
    assert await client.issue_update("uuid-1", {"addedLabelIds": ["l1"]}) == out
    assert fake.gql_calls("IssueUpdate")[0].variables == {"id": "uuid-1",
                                                          "input": {"addedLabelIds": ["l1"]}}


async def test_client_issue_update_parent_passes_through(fake, client):
    """Reparent and detach (RET-110): ``parentId`` reaches Linear as given, null included."""
    out = {"id": "uuid-1", "identifier": "RET-1", "title": "T", "url": "u"}
    fake.on("IssueUpdate", {"data": {"issueUpdate": {"success": True, "issue": out}}})
    await client.issue_update("uuid-1", {"parentId": "uuid-p"})
    await client.issue_update("uuid-1", {"parentId": None})
    sent = [c.variables["input"] for c in fake.gql_calls("IssueUpdate")]
    assert sent == [{"parentId": "uuid-p"}, {"parentId": None}]


async def test_client_comment_create_and_update(fake, client):
    fake.on("CommentCreate", {"data": {"commentCreate": {"success": True, "comment": {"id": "c1"}}}})
    fake.on("CommentUpdate", {"data": {"commentUpdate": {"success": True, "comment": {"id": "c1"}}}})
    assert await client.comment_create("uuid-1", "hi") == "c1"
    assert fake.gql_calls("CommentCreate")[0].variables == {
        "input": {"issueId": "uuid-1", "body": "hi"}}
    assert await client.comment_update("c1", "edited") is None
    assert fake.gql_calls("CommentUpdate")[0].variables == {"id": "c1",
                                                            "input": {"body": "edited"}}


async def test_client_app_comments_only_viewer(fake, client):
    fake.on("IssueAppComments", {"data": {"issue": {"comments": {"nodes": [
        {"id": "c1", "body": "mine", "user": {"id": APP_USER}},
        {"id": "c2", "body": "human", "user": {"id": "human-1"}},
        {"id": "c3", "body": "bot?", "user": None},
    ]}}}})
    assert await client.app_comments("uuid-1") == [{"id": "c1", "body": "mine"}]
    call = fake.gql_calls("IssueAppComments")[0]
    assert call.variables == {"id": "uuid-1", "userId": APP_USER, "after": None}


async def test_client_team_states_cached(fake, client):
    fake.on("TeamStates", {"data": {"team": {"states": {"nodes": [
        {"id": "s2", "name": "Done", "type": "completed", "position": 5},
        {"id": "s1", "name": "Todo", "type": "unstarted", "position": 1},
    ]}}}})
    want = [{"id": "s1", "name": "Todo", "type": "unstarted"},
            {"id": "s2", "name": "Done", "type": "completed"}]
    assert await client.team_states("t1") == want
    assert await client.team_states("t1") == want
    assert len(fake.gql_calls("TeamStates")) == 1
    assert fake.gql_calls("TeamStates")[0].variables == {"teamId": "t1"}


async def test_client_team_labels_team_and_workspace(fake, client):
    fake.on("TeamLabels", {"data": {"issueLabels": {"nodes": [
        {"id": "l1", "name": "Bug", "isGroup": False},
        {"id": "g1", "name": "Area", "isGroup": True},
        {"id": "l2", "name": "superpowers:plan", "isGroup": False},
    ]}}})
    assert await client.team_labels("t1") == [{"id": "l1", "name": "Bug"},
                                              {"id": "l2", "name": "superpowers:plan"}]
    assert fake.gql_calls("TeamLabels")[0].variables == {"teamId": "t1", "after": None}


# Shape of real Linear's answer for an unknown issue (live probe, app actor).
NOT_FOUND_BODY = {"data": None, "errors": [{
    "message": "Entity not found: Issue", "path": ["issue"],
    "extensions": {"code": "INPUT_ERROR", "type": "invalid input",
                   "userError": True, "userPresentableMessage": "Entity not found: Issue"}}]}


@pytest.mark.parametrize("ref", ["RET-404", "00000000-0000-0000-0000-000000000000", "garbage"])
async def test_client_entity_not_found_maps_to_not_found(fake, client, ref):
    fake.on("IssueGet", NOT_FOUND_BODY)
    with pytest.raises(LinearError) as ei:
        await client.issue_get(ref)
    assert ei.value.code == "NOT_FOUND" and ei.value.status == 200
    assert str(ei.value) == "Entity not found: Issue"


async def test_client_other_input_errors_keep_their_code(fake, client):
    fake.on("IssueUpdate", {"data": None, "errors": [{
        "message": "Argument Validation Error", "extensions": {"code": "INPUT_ERROR"}}]})
    with pytest.raises(LinearError) as ei:
        await client.issue_update("uuid-1", {"title": ""})
    assert ei.value.code == "INPUT_ERROR"


async def test_issue_get_sorts_children_by_sub_issue_sort_order(fake, client):
    def child(ident, sso, created="2026-11-01T00:00:00.000Z"):
        return {"id": f"u-{ident}", "identifier": ident, "title": ident, "createdAt": created,
                "subIssueSortOrder": sso, "state": {"type": "unstarted"}}
    # The order live Linear returned (RET-88:30, RET-87:150, ...), plus an unset and a tie.
    nodes = [child("RET-88", 30), child("RET-87", 150), child("RET-89", None),
             child("RET-86", 140), child("RET-85", 130), child("RET-84", 110),
             child("RET-91", 110, "2026-11-02T00:00:00.000Z")]
    fake.on("IssueGet", {"data": {"issue": {**ISSUE_NODE, "children": {"nodes": nodes}}}})
    i = await client.issue_get("RET-1")
    assert [c["identifier"] for c in i["children"]] == [
        "RET-88", "RET-84", "RET-91", "RET-85", "RET-86", "RET-87", "RET-89"]
    assert "createdAt" in fake.gql_calls("IssueGet")[0].json["query"]


def _page(nodes, cursor=None):
    return {"nodes": nodes, "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}}


async def test_issue_get_pages_children(fake, client):
    def child(ident, sso):
        return {"id": f"u-{ident}", "identifier": ident, "title": ident, "subIssueSortOrder": sso,
                "state": {"type": "unstarted"}, "creator": {"id": APP_USER}}
    fake.on("IssueGet", {"data": {"issue": {**ISSUE_NODE, "children": _page(
        [child("RET-2", 20)], "c1")}}})
    fake.on("IssueChildren",
            {"data": {"issue": {"children": _page([child("RET-3", 10)], "c2")}}},
            {"data": {"issue": {"children": _page([child("RET-4", 30)])}}})
    i = await client.issue_get("RET-1")
    assert [c["identifier"] for c in i["children"]] == ["RET-3", "RET-2", "RET-4"]
    assert i["children"][0]["creator"] == {"id": APP_USER}
    calls = fake.gql_calls("IssueChildren")
    assert [c.variables for c in calls] == [{"id": "uuid-1", "after": "c1"},
                                            {"id": "uuid-1", "after": "c2"}]
    assert "pageInfo { hasNextPage endCursor }" in fake.gql_calls("IssueGet")[0].json["query"]


async def test_client_app_comments_pages_oldest_first(fake, client):
    def c(cid, created, user=APP_USER):
        return {"id": cid, "body": cid, "createdAt": created, "user": {"id": user}}
    fake.on("IssueAppComments",
            {"data": {"issue": {"comments": _page([c("c3", "2026-11-03"), c("c2", "2026-11-02")],
                                                  "cur-1")}}},
            {"data": {"issue": {"comments": _page([c("c1", "2026-11-01"),
                                                   c("h1", "2026-11-00", user="human")])}}})
    assert await client.app_comments("uuid-1") == [
        {"id": "c1", "body": "c1"}, {"id": "c2", "body": "c2"}, {"id": "c3", "body": "c3"}]
    calls = fake.gql_calls("IssueAppComments")
    assert [call.variables["after"] for call in calls] == [None, "cur-1"]
    assert "pageInfo { hasNextPage endCursor }" in calls[0].json["query"]


async def test_client_team_labels_pages(fake, client):
    fake.on("TeamLabels",
            {"data": {"issueLabels": _page([{"id": "l1", "name": "Bug", "isGroup": False}],
                                           "cur-1")}},
            {"data": {"issueLabels": _page([{"id": "l2", "name": "Blocked", "isGroup": False}])}})
    assert await client.team_labels("t1") == [{"id": "l1", "name": "Bug"},
                                              {"id": "l2", "name": "Blocked"}]
    assert [c.variables["after"] for c in fake.gql_calls("TeamLabels")] == [None, "cur-1"]


async def test_client_teams_pages_workspace_connection(fake, client):
    # The app user is not a team member: viewer.teams is empty in production (P11),
    # so teams come from the root `teams` connection.
    fake.on("Teams",
            {"data": {"teams": _page([{"id": "t1", "key": "ENG", "name": "Eng"}], "cur-1")}},
            {"data": {"teams": _page([{"id": "t2", "key": "RET", "name": "Ret"}])}})
    assert await client.teams() == [{"id": "t1", "key": "ENG", "name": "Eng"},
                                    {"id": "t2", "key": "RET", "name": "Ret"}]
    calls = fake.gql_calls("Teams")
    assert [c.variables["after"] for c in calls] == [None, "cur-1"]
    assert "viewer" not in calls[0].json["query"]


async def test_client_paging_stops_at_cap(fake, client):
    from hermes_linear.linear_client import QUERY_TEAM_LABELS
    n = iter(range(100))

    def page(call):  # an endless connection
        return {"data": {"issueLabels": _page(
            [{"id": f"l{next(n)}", "name": "x", "isGroup": False} for _ in range(2)], "more")}}
    fake.on("TeamLabels", page)
    labels = await client._paged(QUERY_TEAM_LABELS, {"teamId": "t1"},
                                 lambda d: d.get("issueLabels"), cap=3)
    assert [lb["id"] for lb in labels] == ["l0", "l1", "l2"]
    assert len(fake.gql_calls("TeamLabels")) == 2


# -- error detail (RET-119) ----------------------------------------------

#: Live-probe shape of Linear's complexity refusal.
COMPLEX_BODY = {"errors": [{"message": "Query too complex", "extensions": {
    "type": "invalid input", "code": "INPUT_ERROR", "statusCode": 400, "userError": True,
    "userPresentableMessage": "The query is too complex. Complexity: 18937750. "
                              "Maximum allowed complexity: 10000."}}]}


def test_first_error_reads_user_presentable_message():
    from hermes_linear.linear_client import _first_error

    assert _first_error(COMPLEX_BODY) == (
        "INPUT_ERROR", "Query too complex",
        "The query is too complex. Complexity: 18937750. Maximum allowed complexity: 10000.")
    assert _first_error({"errors": [{"message": "boom"}]}) == (None, "boom", None)
    assert _first_error({"data": {}}) == (None, "", None)


async def test_graphql_error_carries_detail(fake, client):
    fake.on("Viewer", Resp(400, COMPLEX_BODY))
    with pytest.raises(LinearError) as ei:
        await client.viewer_id()
    assert ei.value.code == "INPUT_ERROR"
    assert ei.value.detail.startswith("The query is too complex.")


# -- uploads and attachments (RET-133) -----------------------------------


def _upload_resp(fake, name="a1"):
    return {"data": {"fileUpload": {"success": True, "uploadFile": {
        "uploadUrl": f"{fake.url}/upload/{name}",
        "assetUrl": f"https://uploads.linear.app/{name}",
        "headers": [{"key": "x-goog-content-length-range", "value": "5,5"},
                    {"key": "Content-Disposition", "value": 'attachment; filename="x.png"'}],
    }}}}


async def test_file_upload_puts_bytes_without_bearer(fake, client):
    fake.on("FileUpload", _upload_resp(fake))
    url = await client.file_upload("x.png", "image/png", b"\x89PNG!")
    assert url == "https://uploads.linear.app/a1"
    (gql,) = fake.gql_calls("FileUpload")
    assert gql.variables == {"size": 5, "filename": "x.png", "contentType": "image/png"}
    assert gql.bearer == "tok-1"
    (put,) = fake.upload_calls()
    assert (put.method, put.path, put.body) == ("PUT", "/upload/a1", b"\x89PNG!")
    assert put.headers["Content-Type"] == "image/png"
    assert put.headers["Cache-Control"] == "public, max-age=31536000"
    assert put.headers["x-goog-content-length-range"] == "5,5"
    assert put.headers["Content-Disposition"] == 'attachment; filename="x.png"'
    assert "Authorization" not in put.headers


def test_upload_put_timeout_is_below_the_tool_budget():
    from hermes_linear.constants import PLAN_SYNC_TIMEOUT_S, UPLOAD_PUT_TIMEOUT_S

    assert UPLOAD_PUT_TIMEOUT_S < PLAN_SYNC_TIMEOUT_S


async def test_file_upload_put_uses_upload_put_timeout(fake, client, monkeypatch):
    """The PUT's own timeout fails it inside the tool's PLAN_SYNC_TIMEOUT_S budget."""
    from hermes_linear.constants import UPLOAD_PUT_TIMEOUT_S

    real, totals = aiohttp.ClientTimeout, []

    def spy(*a, **kw):
        totals.append(kw.get("total"))
        return real(*a, **kw)

    monkeypatch.setattr(aiohttp, "ClientTimeout", spy)
    fake.on("FileUpload", _upload_resp(fake))
    await client.file_upload("x.png", "image/png", b"\x89PNG!")
    assert UPLOAD_PUT_TIMEOUT_S in totals


async def test_file_upload_non_2xx_raises_upload_error(fake, client):
    from hermes_linear.linear_client import UploadError

    fake.on("FileUpload", _upload_resp(fake))
    fake.upload_status = 403
    with pytest.raises(UploadError) as ei:
        await client.file_upload("x.png", "image/png", b"\x89PNG!")
    assert ei.value.status == 403
    assert ei.value.code == "UPLOAD_FAILED"
    assert isinstance(ei.value, LinearError)


async def test_file_upload_unsuccessful_mutation_raises(fake, client):
    fake.on("FileUpload", {"data": {"fileUpload": {"success": False, "uploadFile": None}}})
    with pytest.raises(LinearError):
        await client.file_upload("x.png", "image/png", b"x")
    assert fake.upload_calls() == []


ATTACHMENT = {"id": "att-1", "url": "https://example.com/doc"}


async def test_attachment_create_input(fake, client):
    fake.on("AttachmentCreate",
            {"data": {"attachmentCreate": {"success": True, "attachment": ATTACHMENT}}},
            {"data": {"attachmentCreate": {"success": True, "attachment": ATTACHMENT}}},
            {"data": {"attachmentCreate": {"success": False, "attachment": None}}})
    got = await client.attachment_create("uuid-1", "x.png", "sub", "https://uploads.linear.app/a1")
    assert got == ATTACHMENT
    (call,) = fake.gql_calls("AttachmentCreate")
    assert call.variables == {"input": {"issueId": "uuid-1", "title": "x.png",
                                        "subtitle": "sub",
                                        "url": "https://uploads.linear.app/a1"}}
    await client.attachment_create("uuid-1", "t", None, "u")
    assert fake.gql_calls("AttachmentCreate")[1].variables["input"] == {
        "issueId": "uuid-1", "title": "t", "url": "u"}  # no subtitle key when None
    with pytest.raises(LinearError):
        await client.attachment_create("uuid-1", "t", None, "u")


async def test_attachment_link_url_input(fake, client):
    fake.on("AttachmentLinkURL",
            {"data": {"attachmentLinkURL": {"success": True, "attachment": ATTACHMENT}}},
            {"data": {"attachmentLinkURL": {"success": True, "attachment": ATTACHMENT}}},
            {"data": {"attachmentLinkURL": {"success": False, "attachment": None}}})
    assert await client.attachment_link_url("uuid-1", "https://example.com/doc", "Doc") \
        == ATTACHMENT
    assert await client.attachment_link_url("uuid-1", "https://example.com/doc", None) \
        == ATTACHMENT
    first, second = fake.gql_calls("AttachmentLinkURL")
    assert first.variables == {"issueId": "uuid-1", "url": "https://example.com/doc",
                               "title": "Doc"}
    assert second.variables == {"issueId": "uuid-1", "url": "https://example.com/doc",
                                "title": None}
    q = " ".join(first.json["query"].split())
    assert "attachmentLinkURL(issueId: $issueId, url: $url, title: $title)" in q
    with pytest.raises(LinearError):
        await client.attachment_link_url("uuid-1", "https://example.com/doc", None)


async def test_attachment_link_github_pr_input(fake, client):
    pr = "https://github.com/masha256/hermes-linear/pull/11"
    fake.on("AttachmentLinkGitHubPR",
            {"data": {"attachmentLinkGitHubPR": {"success": True,
                                                 "attachment": {"id": "att-2", "url": pr}}}},
            {"data": {"attachmentLinkGitHubPR": {"success": False, "attachment": None}}})
    assert await client.attachment_link_github_pr("uuid-1", pr) == {"id": "att-2", "url": pr}
    (call,) = fake.gql_calls("AttachmentLinkGitHubPR")
    assert call.variables == {"issueId": "uuid-1", "url": pr}
    q = " ".join(call.json["query"].split())
    assert "attachmentLinkGitHubPR(issueId: $issueId, url: $url)" in q
    with pytest.raises(LinearError):
        await client.attachment_link_github_pr("uuid-1", pr)
