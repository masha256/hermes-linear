import asyncio

import pytest

from hermes_linear import renderer as r
from hermes_linear.constants import RESULT_PREVIEW
from hermes_linear.linear_client import LinearError


def test_body_shapes():
    assert r.thought("hi") == {"type": "thought", "body": "hi"}
    assert r.response("done") == {"type": "response", "body": "done"}
    assert r.error("boom") == {"type": "error", "body": "boom"}
    assert r.elicitation("ok?") == {"type": "elicitation", "body": "ok?"}


def test_action_shapes():
    assert r.action("terminal", "ls") == {"type": "action", "action": "terminal", "parameter": "ls"}
    assert "result" not in r.action("terminal", "ls", None)
    assert r.action("terminal", "ls", "a b") == {
        "type": "action", "action": "terminal", "parameter": "ls", "result": "a b",
    }


def test_approval_options_exact():
    assert r.approval_options() == [
        {"label": "Allow once", "value": "once"},
        {"label": "Allow for this session", "value": "session"},
        {"label": "Deny", "value": "deny"},
    ]
    # fresh copy each call
    r.approval_options()[0]["value"] = "x"
    assert r.approval_options()[0]["value"] == "once"


def test_approval_answer_strict():
    assert r.parse_approval_answer("once") == "once"
    assert r.parse_approval_answer("Allow once") == "once"
    assert r.parse_approval_answer("  allow ONCE \n") == "once"
    assert r.parse_approval_answer("SESSION") == "session"
    assert r.parse_approval_answer("Allow for this session") == "session"
    assert r.parse_approval_answer("deny") == "deny"
    assert r.parse_approval_answer("Deny") == "deny"
    for bad in ("sure go ahead", "yes", "always", "allow", "once please", "", "  ", "Allow  once", "no"):
        assert r.parse_approval_answer(bad) is None, bad


def test_plan_mapping():
    todos = [
        {"id": "1", "content": "a", "status": "pending"},
        {"id": "2", "content": "b", "status": "in_progress"},
        {"id": "3", "content": "c", "status": "completed"},
        {"id": "4", "content": "d", "status": "cancelled"},
    ]
    assert r.plan_from_todos(todos) == [
        {"content": "a", "status": "pending"},
        {"content": "b", "status": "inProgress"},
        {"content": "c", "status": "completed"},
        {"content": "d", "status": "canceled"},
    ]
    assert r.plan_from_todos([{"content": "x", "status": "weird"}]) == [{"content": "x", "status": "pending"}]
    assert r.plan_from_todos([]) is None
    assert r.plan_from_todos(None) is None


def test_plan_limits():
    many = [{"content": f"t{i}", "status": "pending"} for i in range(150)]
    plan = r.plan_from_todos(many)
    assert len(plan) == 100
    assert [p["content"] for p in plan] == [f"t{i}" for i in range(100)]
    long = r.plan_from_todos([{"content": "x" * 20000, "status": "pending"}])
    assert len(long[0]["content"]) == 10000 and long[0]["content"].endswith("…")
    mixed = r.plan_from_todos([
        {"content": "", "status": "pending"},
        {"content": "   \n", "status": "pending"},
        {"status": "pending"},
        {"content": "keep", "status": "completed"},
    ])
    assert mixed == [{"content": "keep", "status": "completed"}]
    assert r.plan_from_todos([{"content": " "}, {"content": None}]) is None
    # blank entries do not count toward the 100 cap
    padded = [{"content": ""}] * 50 + many
    assert len(r.plan_from_todos(padded)) == 100


def test_tool_parameter():
    assert r.tool_parameter("terminal", {"command": "ls -la"}) == "ls -la"
    assert r.tool_parameter("read_file", {"path": "/a/b", "offset": 3}) == "/a/b"
    p = r.tool_parameter("terminal", {"command": "echo " + "x" * 500 + "\nnext"})
    assert len(p) <= 120 and "\n" not in p and p.endswith("…")
    j = r.tool_parameter("custom", {"a": 1, "b": "two"})
    assert j == '{"a":1,"b":"two"}'
    assert r.tool_parameter("noargs", {}) == ""
    assert r.tool_parameter("noargs", None) == ""
    secret = "sk-" + "a1B2c3D4e5" * 4
    assert secret not in r.tool_parameter("terminal", {"command": f"curl -H 'x: {secret}'"})


def test_result_preview_redacts_and_caps():
    secret = "sk-" + "a1B2c3D4e5" * 4
    out = r.result_preview(f"token={secret} and more\n" * 100)
    assert secret not in out
    assert len(out) <= RESULT_PREVIEW
    assert out.endswith("…")
    bearer = r.result_preview("Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456")
    assert "abcdefghijklmnopqrstuvwxyz123456" not in bearer
    assert r.result_preview("short") == "short"
    assert r.result_preview(None) == ""


async def test_update_plan_none_is_noop():
    c = FakeClient()
    w = r.ActivityWriter(c)
    assert await w.update_plan("s1", None) is True
    assert await w.update_plan("s1", r.plan_from_todos([])) is True
    assert c.updates == []
    assert w.degraded is False


class FakeClient:
    def __init__(self, exc=None, delay=0.0):
        self.exc = exc
        self.delay = delay
        self.calls = []
        self.updates = []

    async def create_activity(self, sid, content, *, ephemeral=False, signal=None, signal_metadata=None):
        self.calls.append((sid, content, ephemeral, signal, signal_metadata))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        return "act-1"

    async def update_session(self, sid, *, plan=None, external_urls=None,
                             added_external_urls=None):
        self.updates.append((sid, plan, external_urls, added_external_urls))
        if self.exc:
            raise self.exc


async def test_writer_posts_and_passes_signal():
    c = FakeClient()
    w = r.ActivityWriter(c)
    opts = r.approval_options()
    ok = await w.post("s1", r.elicitation("ok?"), signal="select", signal_metadata={"options": opts})
    assert ok is True
    assert c.calls == [("s1", {"type": "elicitation", "body": "ok?"}, False, "select", {"options": opts})]
    assert w.degraded is False and w.last_error is None


async def test_writer_refuses_ephemeral_for_persistent_types():
    c = FakeClient()
    w = r.ActivityWriter(c)
    for content in (r.response("x"), r.error("x"), r.elicitation("x")):
        assert await w.post("s1", content, ephemeral=True) is False
    assert c.calls == []
    assert await w.post("s1", r.thought("x"), ephemeral=True) is True
    assert await w.post("s1", r.action("t", "p"), ephemeral=True) is True
    assert len(c.calls) == 2


@pytest.mark.parametrize("exc", [LinearError("rate limited", status=429), RuntimeError("socket gone")])
async def test_writer_never_raises_and_degrades(exc):
    c = FakeClient(exc=exc)
    w = r.ActivityWriter(c)
    assert await w.post("s1", r.response("x")) is False
    assert w.degraded is True
    assert w.last_error and len(w.last_error) <= 200
    assert len(c.calls) == 1  # client owns retries; writer adds none
    assert await w.update_plan("s1", [{"content": "a", "status": "pending"}]) is False
    assert await w.set_external_url("s1", "Hermes", "https://x") is False
    w.clear_degraded()
    assert w.degraded is False and w.last_error is None


async def test_writer_timeout():
    c = FakeClient(delay=1.0)
    w = r.ActivityWriter(c, timeout_s=0.05)
    assert await w.post("s1", r.thought("Working…"), ephemeral=True) is False
    assert w.degraded is True
    w2 = r.ActivityWriter(FakeClient(delay=1.0))
    assert await w2.post("s1", r.thought("x"), ephemeral=True, timeout_s=0.05) is False


async def test_writer_session_updates():
    c = FakeClient()
    w = r.ActivityWriter(c)
    plan = [{"content": "a", "status": "pending"}]
    assert await w.update_plan("s1", plan) is True
    assert await w.set_external_url("s1", "Hermes session", "https://h/s") is True
    assert c.updates == [  # the link is added (incremental), never a full replace
        ("s1", plan, None, None),
        ("s1", None, None, [{"label": "Hermes session", "url": "https://h/s"}]),
    ]


@pytest.mark.parametrize("make_exc", [
    lambda m: LinearError(m, code="INVALID_INPUT", status=400),
    lambda m: RuntimeError(m),
    lambda m: ValueError(m),
])
async def test_writer_errors_sanitized(make_exc, caplog):
    secret = "sk-" + "Q7w8E9r0T1" * 4
    snippet = "body snippet: please deploy the payroll fix"
    exc = make_exc(f"Argument {secret} invalid; {snippet}")
    w = r.ActivityWriter(FakeClient(exc=exc))
    with caplog.at_level("DEBUG", logger="hermes_linear.renderer"):
        assert await w.post("s1", r.response(snippet)) is False
        assert await w.update_plan("s1", [{"content": snippet, "status": "pending"}]) is False
    for text in (w.last_error, caplog.text):
        assert secret not in text
        assert "payroll" not in text
        assert "invalid" not in text
    assert w.last_error.startswith(type(exc).__name__)
    assert w.last_error in caplog.text
    if isinstance(exc, LinearError):
        assert "INVALID_INPUT" in w.last_error and "400" in w.last_error
    else:
        assert w.last_error == type(exc).__name__
