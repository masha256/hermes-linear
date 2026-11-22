"""Agent tool runtime (RET-80): adapter resolution, loop bridge, write scope.

Thread tests run a real asyncio loop in a background thread, attach it as the adapter's
``event_loop`` and call the sync runtime from the test thread, as a tool worker would
(integration notes Q3).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time

import pytest

from test_adapter_inbound import (  # noqa: F401  (also registers the linear platform)
    D_ISSUE, DELEGATION, deliver, env, make_settings, prompted, D_SID)
from fake_linear import FakeLinear

from gateway.config import PlatformConfig
from gateway.platforms.event import ProcessingOutcome

from hermes_linear import adapter as adapter_mod
from hermes_linear import agent_tools
from hermes_linear.adapter import LinearAdapter
from hermes_linear.constants import TOOL_TIMEOUT_S
from hermes_linear.linear_client import LinearClient, LinearError
from hermes_linear.models import StartTurn
from hermes_linear.turns import Turn


# ----------------------------------------------------------------- helpers


@pytest.fixture
def bg_loop():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="GATEWAY-LOOP", daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)
    loop.close()


@pytest.fixture
def session(monkeypatch):
    """Bind fake session vars for ``agent_tools.get_session_env``."""
    values: dict[str, str] = {}
    monkeypatch.setattr(agent_tools, "get_session_env",
                        lambda name, default="": values.get(name, default))

    def bind(**kw):
        values.clear()
        names = {"platform": "HERMES_SESSION_PLATFORM", "key": "HERMES_SESSION_KEY",
                 "message_id": "HERMES_SESSION_MESSAGE_ID", "chat_id": "HERMES_SESSION_CHAT_ID",
                 "thread_id": "HERMES_SESSION_THREAD_ID"}
        values.update({names[k]: v for k, v in kw.items()})
    return bind


@pytest.fixture
def registry(monkeypatch):
    adapters: dict[str, LinearAdapter] = {}
    monkeypatch.setattr(adapter_mod, "ADAPTERS", adapters)
    return adapters


def new_adapter(loop=None, client=None) -> LinearAdapter:
    a = LinearAdapter(PlatformConfig(enabled=True), settings=make_settings(), client=client)
    a.event_loop = loop
    return a


def put_turn(a: LinearAdapter, session_key: str, chat_id: str, mid: str = "m1") -> None:
    d = StartTurn(agent_session_id="sid-" + mid, activity_id="act", issue_id=chat_id.split(":")[0],
                  chat_id=chat_id, thread_id=None, user_id="u", user_name="U", text="t",
                  first_turn=True, ack="")
    a.turns.inflight[(chat_id, None)] = Turn(decision=d, mid=mid, session_key=session_key)


def ctx_for(a, *, issue="ISSUE-SELF", key="sk", linear=True):
    return agent_tools.ToolContext(adapter=a, client=a._client if a else None,
                                   session_issue_id=issue if linear else None,
                                   session_key=key if linear else None, is_linear=linear)


# Issue tree on the fake API: self <- child <- grandchild; "unrelated" has no parent.
# "RET-9" is the identifier form of "grandchild".
TREE = {
    "ISSUE-SELF": {"id": "ISSUE-SELF", "parent": None},
    "child": {"id": "child", "parent": {"id": "ISSUE-SELF"}},
    "grandchild": {"id": "grandchild", "parent": {"id": "child"}},
    "RET-9": {"id": "grandchild", "parent": {"id": "child"}},
    "unrelated": {"id": "unrelated", "parent": None},
    "made": {"id": "made", "parent": None},
}
for _i in range(1, 8):  # deep chain: d1's parent is ISSUE-SELF, d7 is 7 levels below it
    TREE[f"d{_i}"] = {"id": f"d{_i}", "parent": {"id": "ISSUE-SELF" if _i == 1 else f"d{_i - 1}"}}


@pytest.fixture
async def linear():
    fake = FakeLinear()
    await fake.start()
    fake.on("IssueParent", lambda call: {"data": {"issue": TREE.get(call.variables["id"])}})
    client = LinearClient("cid", lambda: "secret", base_url=fake.url)
    yield fake, client
    await client.close()
    await fake.close()


# ----------------------------------------------------------------- write scope


async def test_writes_refused_outside_linear_session(session, registry, linear):
    _fake, client = linear
    a = new_adapter(client=client)
    registry["default"] = a
    session(platform="slack", key="agent:main:slack:dm:x")
    ctx = agent_tools.resolve_context()
    assert ctx.is_linear is False and ctx.adapter is a  # reads use the first adapter
    assert ctx.session_issue_id is None
    assert await agent_tools.check_write_scope(ctx, "ISSUE-SELF") == "NOT_LINEAR_SESSION"


async def test_scope_allows_self_descendant_created(linear):
    _fake, client = linear
    a = new_adapter(client=client)
    a.tool_created["sk"] = {"made"}
    ctx = ctx_for(a)
    assert await agent_tools.check_write_scope(ctx, "ISSUE-SELF") is None
    assert await agent_tools.check_write_scope(ctx, "child") is None
    assert await agent_tools.check_write_scope(ctx, "grandchild") is None
    assert await agent_tools.check_write_scope(ctx, "RET-9") is None  # identifier form
    assert await agent_tools.check_write_scope(ctx, "made") is None
    # created issues are per session key
    assert await agent_tools.check_write_scope(ctx_for(a, key="other"), "made") == "OUT_OF_SCOPE"


async def test_scope_refuses_unrelated_issue(linear):
    _fake, client = linear
    ctx = ctx_for(new_adapter(client=client))
    assert await agent_tools.check_write_scope(ctx, "unrelated") == "OUT_OF_SCOPE"


async def test_scope_walk_is_bounded_to_five_levels(linear):
    fake, client = linear
    ctx = ctx_for(new_adapter(client=client))
    assert await agent_tools.check_write_scope(ctx, "d5") is None  # 5 levels below
    fake.calls.clear()
    assert await agent_tools.check_write_scope(ctx, "d7") == "OUT_OF_SCOPE"  # 7 levels below
    assert len(fake.gql_calls("IssueParent")) <= 5


async def test_scope_needs_a_session_issue(linear):
    _fake, client = linear
    ctx = ctx_for(new_adapter(client=client), issue=None)
    assert await agent_tools.check_write_scope(ctx, "ISSUE-SELF") == "NOT_LINEAR_SESSION"


# ----------------------------------------------------------------- run_on_adapter


def test_tool_times_out_cleanly(bg_loop):
    a = new_adapter(bg_loop)
    cancelled = threading.Event()

    async def slow():
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {"ok": True}

    start = time.monotonic()
    out = agent_tools.run_on_adapter(ctx_for(a), slow, timeout_s=0.1)
    assert out == {"ok": False, "error": "TIMEOUT"}
    assert time.monotonic() - start < 1
    assert cancelled.wait(1)  # the future was cancelled on the loop


def test_linear_error_maps_to_code(bg_loop):
    a = new_adapter(bg_loop)

    async def boom():
        raise LinearError("slow down", code="RATELIMITED")

    async def other():
        raise KeyError("x")

    out = agent_tools.run_on_adapter(ctx_for(a), boom)
    assert out["ok"] is False and out["error"] == "RATELIMITED"
    assert agent_tools.run_on_adapter(ctx_for(a), other) == {"ok": False, "error": "KeyError"}


def test_result_passthrough_and_runs_on_loop(bg_loop):
    a = new_adapter(bg_loop)

    async def where():
        return {"ok": True, "thread": threading.current_thread().name}

    assert agent_tools.run_on_adapter(ctx_for(a), where) == {"ok": True, "thread": "GATEWAY-LOOP"}
    assert TOOL_TIMEOUT_S == 30.0


def test_wrong_thread_guard(bg_loop):
    a = new_adapter(bg_loop)
    called = []
    result: concurrent.futures.Future = concurrent.futures.Future()

    async def coro():
        called.append(1)
        return {"ok": True}

    def on_loop():
        start = time.monotonic()
        out = agent_tools.run_on_adapter(ctx_for(a), coro, timeout_s=5)
        result.set_result((out, time.monotonic() - start))

    bg_loop.call_soon_threadsafe(on_loop)
    out, took = result.result(2)
    assert out == {"ok": False, "error": "WRONG_THREAD"}
    assert took < 0.5 and called == []


def test_not_connected(registry, session):
    session(platform="linear", key="sk")
    ctx = agent_tools.resolve_context()  # no adapters
    assert ctx.adapter is None
    assert agent_tools.run_on_adapter(ctx, lambda: None) == {"ok": False, "error": "NOT_CONNECTED"}
    stopped = asyncio.new_event_loop()  # never run
    try:
        for loop in (None, stopped):
            out = agent_tools.run_on_adapter(ctx_for(new_adapter(loop)), lambda: None)
            assert out == {"ok": False, "error": "NOT_CONNECTED"}
    finally:
        stopped.close()
    out = agent_tools.run_on_adapter(ctx_for(new_adapter(stopped)), lambda: None)  # closed
    assert out == {"ok": False, "error": "NOT_CONNECTED"}


# ----------------------------------------------------------------- resolve_context


def test_resolve_context_by_session_key(bg_loop, registry, session):
    first, second = new_adapter(bg_loop), new_adapter(bg_loop)
    registry["a"], registry["b"] = first, second
    put_turn(first, "agent:a:linear:dm:other:1", "other:1")
    put_turn(second, "agent:b:linear:dm:ISSUE-2:1", "ISSUE-2:1")
    session(platform="linear", key="agent:b:linear:dm:ISSUE-2:1", message_id="nope")
    ctx = agent_tools.resolve_context()
    assert ctx.adapter is second
    assert ctx.is_linear is True
    assert ctx.session_issue_id == "ISSUE-2"
    assert ctx.session_key == "agent:b:linear:dm:ISSUE-2:1"


def test_resolve_context_falls_back_to_message_id(bg_loop, registry, session, monkeypatch):
    from hermes_linear.outbound import Target
    first, second = new_adapter(bg_loop), new_adapter(bg_loop)
    registry["a"], registry["b"] = first, second
    seen = []

    def by_msg(env, *, by_message):
        seen.append(threading.current_thread().name)  # store is touched on the loop only
        if by_message and env.get("message_id") == "m9":
            return Target("sid", "m9", ("ISSUE-9:sid", None), "hook")
        return None

    monkeypatch.setattr(second.outbound, "tool_target", by_msg)
    session(platform="linear", key="agent:gone", message_id="m9")
    ctx = agent_tools.resolve_context()
    assert ctx.adapter is second and ctx.session_issue_id == "ISSUE-9"
    assert set(seen) == {"GATEWAY-LOOP"}


def test_resolve_context_unknown_linear_turn_has_no_ticket(bg_loop, registry, session):
    a = new_adapter(bg_loop)
    registry["a"] = a
    session(platform="linear", key="agent:gone")
    ctx = agent_tools.resolve_context()
    assert ctx.adapter is a and ctx.is_linear is True and ctx.session_issue_id is None


# ----------------------------------------------------------------- tool_created lifecycle


async def test_tool_created_cleared_when_session_turns_finish(env):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    key = a.turns.inflight[(f"{D_ISSUE}:1", None)].session_key
    a.tool_created[key] = {"x"}
    await deliver(a, prompted(D_SID, D_ISSUE, "more", "act-2"))  # queued behind the first
    await asyncio.create_task(a.on_processing_complete(gw.events[0], ProcessingOutcome.SUCCESS))
    assert a.tool_created[key] == {"x"}  # the session still has a queued turn
    await a.wait_idle()
    await asyncio.create_task(a.on_processing_complete(gw.events[1], ProcessingOutcome.SUCCESS))
    assert key not in a.tool_created


async def test_tool_created_cleared_on_disconnect(env):
    a, *_ = env
    a.tool_created["k"] = {"x"}
    await a.disconnect()
    assert a.tool_created == {}
