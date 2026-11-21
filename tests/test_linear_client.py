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
    fake.on("Viewer", Resp(400, {"errors": [{"message": "Entity not found",
                                             "extensions": {"code": "INVALID_INPUT"}}]}))
    with pytest.raises(LinearError) as ei:
        await client.viewer_id()
    assert ei.value.code == "INVALID_INPUT"
    assert ei.value.status == 400
    assert "Entity not found" in str(ei.value)


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


async def test_create_session_on_comment_returns_id(fake, client):
    fake.on("AgentSessionCreateOnComment", {"data": {"agentSessionCreateOnComment": {
        "success": True, "agentSession": {"id": "sess-c"}}}})
    assert await client.create_session_on_comment("cmt-1") == "sess-c"
    (call,) = fake.gql_calls("AgentSessionCreateOnComment")
    assert call.variables == {"input": {"commentId": "cmt-1"}}


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
