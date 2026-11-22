"""LinearAdapter outbound path, tool hooks and Task 11 carry items (Task 12).

Same fakes as the inbound tests: the gateway is a recorder, Linear is a Python
fake client, the store is in-memory and the listener is a fake.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import threading
import time

import pytest

from test_adapter_inbound import (  # noqa: F401  (also registers the linear platform)
    D_ISSUE, D_SID, DELEGATION, HUMAN, ISSUE_DELEGATED, M_ISSUE, M_SID, MENTION, FakeClient,
    FakeListener, Gateway, complete, deliver, make_settings, prompted,
)
from gateway.config import PlatformConfig
from gateway.platforms import base as base_mod
from gateway.platforms.base import ExecApprovalPrompt
from gateway.platforms.event import ProcessingOutcome

from hermes_linear import constants, hooks
from hermes_linear import outbound as outbound_mod
from hermes_linear.adapter import ADAPTERS, LinearAdapter
from hermes_linear.store import Store

D_CHAT = f"{D_ISSUE}:1"


# ----------------------------------------------------------------- fakes


class OutClient(FakeClient):
    def __init__(self, order):
        super().__init__(order)
        self.updates: list[tuple] = []
        self.added_urls: list[tuple] = []
        self.type_delay: dict[str, float] = {}  # activity type -> seconds
        self.fail_types: set[str] = set()
        self.app_delay = 0.0
        self.create_delay = 0.0

    async def create_activity(self, sid, content, **kw):
        if content["type"] in self.fail_types:
            raise RuntimeError("linear down")
        if self.type_delay.get(content["type"]):
            await asyncio.sleep(self.type_delay[content["type"]])
        return await super().create_activity(sid, content, **kw)

    async def update_session(self, sid, *, plan=None, external_urls=None,
                             added_external_urls=None):
        self.updates.append((sid, plan, external_urls))
        if added_external_urls is not None:
            self.added_urls.append((sid, added_external_urls))
        return True

    async def app_sessions_on_issue(self, issue_id):
        if self.app_delay:
            await asyncio.sleep(self.app_delay)
        return await super().app_sessions_on_issue(issue_id)

    async def create_session_on_issue(self, issue_id):
        if self.create_delay:
            await asyncio.sleep(self.create_delay)
        return await super().create_session_on_issue(issue_id)


class FakeSleep:
    """Fake clock for the heartbeat: each ``tick()`` ends every pending sleep."""

    def __init__(self):
        self.calls: list[float] = []
        self.waiters: list[asyncio.Future] = []

    async def __call__(self, seconds):
        self.calls.append(seconds)
        fut = asyncio.get_running_loop().create_future()
        self.waiters.append(fut)
        await fut

    async def tick(self):
        waiters, self.waiters = self.waiters, []
        for fut in waiters:
            if not fut.done():
                fut.set_result(None)
        for _ in range(5):
            await asyncio.sleep(0)


def new_adapter(profile="default", settings=None):
    order: list = []
    client = OutClient(order)
    store = Store.open(":memory:")
    a = LinearAdapter(PlatformConfig(enabled=True), settings=settings or make_settings(),
                      client=client, store=store, listener=FakeListener())
    a._write_runtime_status_safe = lambda *args, **kw: None
    if profile != "default":
        a._owner_profile = profile
    gw = Gateway(order)
    gw.adapter = a
    a.handle_message = gw
    a.delegation_wait_s = 0.05
    a.outbound.sleep = FakeSleep()
    return a, client, gw, store, order


@pytest.fixture
async def env():
    a, client, gw, store, order = new_adapter()
    assert await a.connect()
    yield a, client, gw, store, order
    await a.disconnect()


def acts(client, kind=None):
    return [(sid, c) for sid, c, *_ in client.activities if kind is None or c["type"] == kind]


def bodies(client, kind):
    return [(sid, c.get("body")) for sid, c in acts(client, kind)]


def second_mention(sid="sess-m2", comment="comment-m2", activity="act-m2"):
    ev = copy.deepcopy(MENTION)
    s = ev["agentSession"]
    s["id"] = sid
    s["commentId"] = comment
    s["comment"]["id"] = comment
    s["sourceMetadata"]["agentSessionMetadata"]["sourceCommentId"] = comment
    ev["agentActivity"]["id"] = activity
    ev["agentActivity"]["agentSessionId"] = sid
    ev["agentActivity"]["sourceCommentId"] = comment
    return ev


class SessionEnv:
    """Stand-in for ``gateway.session_context.get_session_env`` (values per test)."""

    def __init__(self, **values):
        self.values = {f"HERMES_SESSION_{k.upper()}": v for k, v in values.items()}

    def __call__(self, name, default=""):
        return self.values.get(name, default)


async def settle(a, seconds=0.0):
    await asyncio.sleep(seconds)
    for _ in range(3):
        await asyncio.sleep(0)
    await a.wait_idle()


# ------------------------------------------------------------ class contract


def test_supports_exec_approval_buttons():
    assert LinearAdapter.supports_exec_approval_buttons() is True


def test_format_tool_event_none():
    a, *_ = new_adapter()
    assert a.format_tool_event(object()) is None
    assert a.format_tool_event(object(), mode="verbose", preview_max_len=0) is None


# ------------------------------------------------------------ send contract


async def test_send_final_is_response_in_asking_session(env):
    a, client, gw, store, order = env
    await deliver(a, MENTION)
    await deliver(a, second_mention())
    mid1, mid2 = gw.events[0].message_id, gw.events[1].message_id
    assert (gw.events[0].source.thread_id, gw.events[1].source.thread_id) == (M_SID, "sess-m2")
    # Interleaved: thread 2's answer first, then thread 1's.
    r2 = await a.send(M_ISSUE, "answer two", reply_to=mid2,
                      metadata={"thread_id": "sess-m2", "notify": True})
    r1 = await a.send(M_ISSUE, "answer one", reply_to=mid1,
                      metadata={"thread_id": M_SID, "notify": True})
    assert r1.success and r2.success
    assert bodies(client, "response") == [("sess-m2", "answer two"), (M_SID, "answer one")]


async def test_send_resolves_reply_to(env):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    mid = gw.events[0].message_id
    await a.send("unrelated-chat", "done", reply_to=mid, metadata={"notify": True})
    assert bodies(client, "response") == [(D_SID, "done")]


async def test_send_resolves_reply_to_message_id(env):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    mid = gw.events[0].message_id
    await a.send(D_CHAT, "commentary", metadata={
        "thread_id": None, "reply_to_message_id": mid, "_interim_send": True})
    assert bodies(client, "thought")[-1] == (D_SID, "commentary")


async def test_send_resolves_latest_turn_for_chat_and_thread(env):
    a, client, gw, store, order = env
    await deliver(a, MENTION)
    # No thread metadata: the mention session (thread=M_SID) must not be reached.
    res = await a.send(M_ISSUE, "x", metadata={"_interim_send": True})
    assert not res.success and res.error == "no linear session"
    res = await a.send(M_ISSUE, "y", metadata={"thread_id": M_SID, "_interim_send": True})
    assert res.success
    assert bodies(client, "thought")[-1] == (M_SID, "y")


async def test_unresolvable_send_dropped(env, caplog):
    a, client, gw, store, order = env
    n = len(client.activities)
    with caplog.at_level(logging.WARNING):
        res = await a.send("no-such-chat", "hello")
    assert res.success is False and res.error == "no linear session"
    assert len(client.activities) == n
    assert "no linear session" in caplog.text or "dropped" in caplog.text


async def test_interim_send_is_thought(env):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    mid = gw.events[0].message_id
    await a.send(D_CHAT, "thinking", reply_to=mid, metadata={"_interim_send": True})
    assert bodies(client, "thought")[-1] == (D_SID, "thinking")
    assert bodies(client, "response") == []


async def test_expect_edits_is_thought_with_warning(env, caplog):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    mid = gw.events[0].message_id
    with caplog.at_level(logging.WARNING):
        await a.send(D_CHAT, "partial", metadata={"reply_to_message_id": mid,
                                                  "expect_edits": True})
    assert bodies(client, "thought")[-1] == (D_SID, "partial")
    assert bodies(client, "response") == []
    assert "expect_edits" in caplog.text


async def test_final_send_is_response(env):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    mid = gw.events[0].message_id
    res = await a.send(D_CHAT, "All done", reply_to=mid, metadata={"notify": True})
    assert res.success and res.message_id
    assert bodies(client, "response") == [(D_SID, "All done")]


async def test_second_final_is_additional_response(env):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    mid = gw.events[0].message_id
    await a.send(D_CHAT, "first", reply_to=mid, metadata={"notify": True})
    await a.send(D_CHAT, "second", reply_to=mid, metadata={"notify": True})
    # Ruling 33: never a thought after the response; content is kept as another response.
    assert bodies(client, "response") == [(D_SID, "first"), (D_SID, "second")]
    kinds = [c["type"] for _s, c, *_ in client.activities]
    assert kinds[kinds.index("response"):] == ["response", "response"]


async def test_turn_error_notice_posted_as_error(env):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    await deliver(a, prompted(D_SID, D_ISSUE, "more", "act-2"))
    first = gw.events[0]

    async def base_task():  # the base's exception path: hook, then _notify_turn_error
        await a.on_processing_complete(first, ProcessingOutcome.FAILURE)
        await a.send(D_CHAT, "Sorry, I hit an error.", metadata=None)

    await asyncio.create_task(base_task())
    await a.wait_idle()
    assert bodies(client, "error") == [(D_SID, "Sorry, I hit an error.")]
    assert bodies(client, "response") == []
    err_at = order.index(("activity", "error", "Sorry, I hit an error."))
    second_at = next(i for i, o in enumerate(order) if o[0] == "handle" and "more" in o[1])
    assert err_at < second_at  # the queued item starts only afterwards
    # last_outcome was consumed: a later anchorless send is not another error.
    await complete(a, gw.events[1])
    await a.send(D_CHAT, "later notice")
    assert bodies(client, "error") == [(D_SID, "Sorry, I hit an error.")]


async def test_turn_error_notice_consumed_once(env):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)

    async def base_task():
        await a.on_processing_complete(gw.events[0], ProcessingOutcome.FAILURE)
        await a.send(D_CHAT, "turn failed")

    await asyncio.create_task(base_task())
    await a.wait_idle()
    await a.send(D_CHAT, "an unrelated unmarked send")  # no new turn recorded in between
    assert bodies(client, "error") == [(D_SID, "turn failed")]
    assert bodies(client, "thought")[-1] == (D_SID, "an unrelated unmarked send")


async def test_anchorless_send_after_success_is_thought(env, caplog):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    await complete(a, gw.events[0])
    with caplog.at_level(logging.INFO):
        res = await a.send(D_CHAT, "an unmarked notice")
    assert res.success
    assert bodies(client, "thought")[-1] == (D_SID, "an unmarked notice")
    assert bodies(client, "response") == [] and bodies(client, "error") == []


async def test_pairing_text_delegated_posts_response(env):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)  # turn in flight
    await a.send(D_CHAT, "pairing code 1234")
    assert bodies(client, "response") == [(D_SID, "pairing code 1234")]


async def test_pairing_text_mention_dropped(env):
    a, client, gw, store, order = env
    await deliver(a, MENTION)
    n = len(client.activities)
    res = await a.send(M_ISSUE, "pairing code 1234")
    assert res.success is False and len(client.activities) == n


async def test_response_failure_sets_degraded_thought_failure_does_not(env):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    mid = gw.events[0].message_id
    client.fail_types = {"thought", "action"}
    await a.send(D_CHAT, "interim", reply_to=mid, metadata={"_interim_send": True})
    await a.send_typing(D_CHAT)
    await settle(a)
    assert a.degraded is False
    client.fail_types = {"response"}
    res = await a.send(D_CHAT, "final", reply_to=mid, metadata={"notify": True})
    assert res.success is False
    assert a.degraded is True


async def test_promotes_last_thought_when_no_response(env):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    mid = gw.events[0].message_id
    await a.send(D_CHAT, "the answer", metadata={"reply_to_message_id": mid,
                                                "_interim_send": True})
    await complete(a, gw.events[0])
    assert bodies(client, "response") == [(D_SID, "the answer")]


async def test_no_promotion_when_response_posted(env):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    mid = gw.events[0].message_id
    await a.send(D_CHAT, "commentary", reply_to=mid, metadata={"_interim_send": True})
    await a.send(D_CHAT, "final", reply_to=mid, metadata={"notify": True})
    await complete(a, gw.events[0])
    assert bodies(client, "response") == [(D_SID, "final")]


# ------------------------------------------------------------------ stop


async def test_stop_posts_stopped_once_and_suppresses_gateway_ack(env):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    first = gw.events[0]
    await deliver(a, prompted(D_SID, D_ISSUE, "", "act-stop", signal="stop"))
    stop_mid = gw.events[-1].message_id
    assert stop_mid in a.stop_message_ids
    n = len(client.activities)
    ack = await a.send(D_CHAT, "⚡ Stopped. You can continue this session.", reply_to=stop_mid,
                       metadata={"notify": True})
    assert ack.success and len(client.activities) == n  # not forwarded to Linear
    await complete(a, first, ProcessingOutcome.CANCELLED)
    assert bodies(client, "response") == [(D_SID, "Stopped.")]
    assert stop_mid not in a.stop_message_ids
    # A duplicate/late ack is still suppressed; no second Stopped.
    await a.send(D_CHAT, "No active task to stop.", reply_to=stop_mid, metadata={"notify": True})
    await a.wait_idle()
    assert bodies(client, "response") == [(D_SID, "Stopped.")]
    assert len(acts(client)) == n + 1


# --------------------------------------------------------- typing/heartbeat


async def test_typing_rate_limited(env):
    a, client, gw, store, order = env
    now = [1000.0]
    a.outbound.clock = lambda: now[0]
    await deliver(a, MENTION)
    meta = {"thread_id": M_SID}
    for t in (0, 10, 59, 61, 70, 125):
        now[0] = 1000.0 + t
        await a.send_typing(M_ISSUE, metadata=meta)
        await settle(a)
    working = [(sid, c["body"], eph) for sid, c, eph, *_ in client.activities
               if c.get("body") == "Working…"]
    assert working == [(M_SID, "Working…", True)] * 3  # t=0, 61, 125


async def test_typing_never_blocks(env):
    a, client, gw, store, order = env
    await deliver(a, MENTION)
    client.activity_delay[M_SID] = 5.0
    t0 = time.monotonic()
    await a.send_typing(M_ISSUE, metadata={"thread_id": M_SID})
    assert time.monotonic() - t0 < 0.1


async def test_typing_without_turn_is_noop(env):
    a, client, gw, store, order = env
    await a.send_typing("nobody")
    await settle(a)
    assert client.activities == []


async def test_heartbeat_fires_on_long_turn(env):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    sleep = a.outbound.sleep
    await asyncio.sleep(0)
    assert sleep.calls and sleep.calls[0] == outbound_mod.HEARTBEAT_S == 600
    n = len(client.activities)
    await sleep.tick()
    await sleep.tick()
    beats = [(sid, c["body"], eph) for sid, c, eph, *_ in client.activities[n:]]
    assert beats == [(D_SID, "Still working…", True)] * 2


async def test_heartbeat_cancelled_on_complete(env):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    task = a.outbound.heartbeats[gw.events[0].message_id]
    await complete(a, gw.events[0])
    assert task.done()
    assert a.outbound.heartbeats == {}
    n = len(client.activities)
    await a.outbound.sleep.tick()
    assert len(client.activities) == n


# ------------------------------------------------------------- approvals


def approval_prompt(request_id="req-1", actions=None, thread_id=M_SID):
    actions = actions if actions is not None else [
        ("Allow Once", "once", "primary"), ("Allow Session", "session", ""),
        ("Always Allow", "always", ""), ("Deny", "deny", "danger")]
    return ExecApprovalPrompt(
        chat_id=M_ISSUE, session_key="agent:main:linear:dm:x", text="Run `ls`?",
        actions=actions, command="ls", description="flagged", smart_denied=False,
        metadata={"thread_id": thread_id}, request_id=request_id)


async def test_approval_prompt_elicitation_select(env):
    a, client, gw, store, order = env
    await deliver(a, MENTION)
    res = await a._send_exec_approval_prompt(approval_prompt())
    assert res.success and res.message_id == "approval:req-1"
    sid, content, eph, signal, smeta = client.activities[-1]
    assert (sid, content, eph, signal) == (M_SID, {"type": "elicitation", "body": "Run `ls`?"},
                                           False, "select")
    assert [o["value"] for o in smeta["options"]] == ["once", "session", "deny"]
    row = store.control_for_session(M_SID)
    assert (row.request_id, row.kind, row.session_key) == ("req-1", "approval",
                                                            "agent:main:linear:dm:x")
    assert list(row.choices) == ["once", "session", "deny"]
    # A Deny-only card stays narrowed.
    await a._send_exec_approval_prompt(approval_prompt("req-2", [("Deny", "deny", "danger")]))
    assert [o["value"] for o in client.activities[-1][4]["options"]] == ["deny"]


async def test_approval_timeout_edit_posts_expiry_and_drops_control(env):
    a, client, gw, store, order = env
    await deliver(a, MENTION)
    await a._send_exec_approval_prompt(approval_prompt())
    res = await a.edit_message(M_ISSUE, "approval:req-1", "timed out\n```ls```")
    assert res.success and res.message_id == "approval:req-1"
    last = client.activities[-1][1]
    assert last["type"] == "thought" and "expired" in last["body"]
    assert "ls" not in last["body"]  # never echoes the card body
    assert store.control_for_session(M_SID) is None
    other = await a.edit_message(M_ISSUE, "act-9", "x")
    assert other.success is False
    # An approval id never retires a clarify row.
    await a.send_clarify(M_ISSUE, "Which?", None, "clar-1", "sk", metadata={"thread_id": M_SID})
    n = len(client.activities)
    await a.edit_message(M_ISSUE, "approval:clar-1", "x")
    assert len(client.activities) == n and store.control("clar-1") is not None


# --------------------------------------------------------------- clarify


async def test_clarify_free_text_and_select(env):
    a, client, gw, store, order = env
    await deliver(a, MENTION)
    meta = {"thread_id": M_SID}
    res = await a.send_clarify(M_ISSUE, "Which env?", ["staging", "prod"], "clar-1", "sk",
                               metadata=meta)
    assert res.success
    sid, content, eph, signal, smeta = client.activities[-1]
    assert (sid, content, signal) == (M_SID, {"type": "elicitation", "body": "Which env?"},
                                      "select")
    assert smeta == {"options": [{"label": "staging", "value": "staging"},
                                 {"label": "prod", "value": "prod"}]}
    assert store.control_for_session(M_SID).request_id == "clar-1"
    res = await a.send_clarify(M_ISSUE, "Anything else?", None, "clar-2", "sk", metadata=meta)
    assert res.success
    sid, content, eph, signal, smeta = client.activities[-1]
    assert content == {"type": "elicitation", "body": "Anything else?"} and signal is None
    row = store.control_for_session(M_SID)
    assert (row.request_id, row.kind, row.session_key) == ("clar-2", "clarify", "sk")


async def test_expired_clarify_posts_thought_and_drops_control(env):
    a, client, gw, store, order = env
    await deliver(a, MENTION)
    await a.send_clarify(M_ISSUE, "Which env?", None, "clar-1", "sk",
                         metadata={"thread_id": M_SID})
    await a.retire_clarify_card("clar-1", "⏳ This prompt expired — please send a new request.")
    await a.wait_idle()
    assert bodies(client, "thought")[-1] == (M_SID, "This request expired.")
    assert store.control("clar-1") is None
    n = len(client.activities)
    await a.retire_clarify_card("clar-1", "again")  # already resolved: ignored
    await a.wait_idle()
    assert len(client.activities) == n


def test_store_control_lookup():
    store = Store.open(":memory:")
    store.put_control("r1", "clarify", "s1", 1, ["a"], session_key="k")
    row = store.control("r1")
    assert (row.kind, row.agent_session_id, row.session_key) == ("clarify", "s1", "k")
    assert store.control("nope") is None


# ----------------------------------------------------------------- hooks


@pytest.fixture
def session_env(monkeypatch):
    def bind(**values):
        fake = SessionEnv(**values)
        monkeypatch.setattr(hooks, "get_session_env", fake)
        return fake
    return bind


async def test_hook_ignores_other_platforms(env, session_env):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    session_env(platform="slack", message_id=gw.events[0].message_id, chat_id=D_CHAT)
    n = len(client.activities)
    assert hooks.on_pre_tool_call("terminal", {"command": "ls"}, "task-1",
                                  tool_call_id="c1") is None
    hooks.on_post_tool_call("terminal", {"command": "ls"}, "ok", "task-1", tool_call_id="c1")
    await settle(a, 0.05)
    assert len(client.activities) == n
    assert a.outbound.pending_starts == {}


async def test_tool_fast_skips_start(env, session_env, monkeypatch):
    a, client, gw, store, order = env
    monkeypatch.setattr(outbound_mod, "FAST_TOOL_SKIP_S", 0.1)
    await deliver(a, DELEGATION)
    session_env(platform="linear", message_id=gw.events[0].message_id, chat_id=D_CHAT)
    n = len(client.activities)
    hooks.on_pre_tool_call("read_file", {"path": "/etc/hosts"}, "task-1", tool_call_id="c1")
    await settle(a)
    hooks.on_post_tool_call("read_file", {"path": "/etc/hosts"}, "127.0.0.1 localhost",
                            "task-1", duration_ms=5, tool_call_id="c1", status="ok")
    await settle(a, 0.2)
    new = [(sid, c, eph) for sid, c, eph, *_ in client.activities[n:]]
    assert new == [(D_SID, {"type": "action", "action": "read_file", "parameter": "/etc/hosts",
                            "result": "127.0.0.1 localhost"}, False)]


async def test_tool_slow_posts_start_and_finish(env, session_env, monkeypatch):
    a, client, gw, store, order = env
    monkeypatch.setattr(outbound_mod, "FAST_TOOL_SKIP_S", 0.05)
    await deliver(a, DELEGATION)
    session_env(platform="linear", message_id=gw.events[0].message_id, chat_id=D_CHAT)
    n = len(client.activities)
    hooks.on_pre_tool_call("terminal", {"command": "make test"}, "task-1", tool_call_id="c2")
    await settle(a, 0.15)
    hooks.on_post_tool_call("terminal", {"command": "make test"}, "boom", "task-1",
                            tool_call_id="c2", status="error")
    await settle(a)
    new = [(c, eph) for sid, c, eph, *_ in client.activities[n:]]
    assert new[0] == ({"type": "action", "action": "terminal", "parameter": "make test"}, True)
    final, eph = new[1]
    assert eph is False and final["action"] == "terminal" and final["parameter"] == "make test"
    assert final["result"].startswith("Failed") and "boom" in final["result"]
    assert len(new) == 2


async def test_hook_falls_back_to_latest_turn_for_chat(env, session_env):
    a, client, gw, store, order = env
    await deliver(a, MENTION)
    session_env(platform="linear", message_id="", chat_id=M_ISSUE, thread_id=M_SID)
    hooks.on_post_tool_call("web_search", {"query": "q"}, "r", "t", tool_call_id="c3")
    await settle(a)
    assert acts(client, "action")[-1][0] == M_SID


async def test_hook_threadsafe_schedule(env, session_env):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    session_env(platform="linear", message_id=gw.events[0].message_id, chat_id=D_CHAT)
    loop_thread = threading.get_ident()
    seen_threads: set[int] = set()
    real_turn, real_latest = store.turn, store.latest_turn_for_chat

    def spy_turn(*args, **kw):
        seen_threads.add(threading.get_ident())
        return real_turn(*args, **kw)

    def spy_latest(*args, **kw):
        seen_threads.add(threading.get_ident())
        return real_latest(*args, **kw)

    store.turn, store.latest_turn_for_chat = spy_turn, spy_latest
    hook_threads: list[int] = []

    def worker():
        hook_threads.append(threading.get_ident())
        hooks.on_post_tool_call("terminal", {"command": "ls"}, "a b", "task-9",
                                tool_call_id="c9")

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    await settle(a, 0.05)
    assert hook_threads and hook_threads[0] != loop_thread
    assert seen_threads == {loop_thread}  # store only touched on the loop
    assert acts(client, "action")[-1] == (D_SID, {"type": "action", "action": "terminal",
                                                   "parameter": "ls", "result": "a b"})


async def test_todo_updates_plan(env, session_env):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    session_env(platform="linear", message_id=gw.events[0].message_id, chat_id=D_CHAT)
    result = json.dumps({"todos": [
        {"id": "1", "content": "Write tests", "status": "completed"},
        {"id": "2", "content": "Implement", "status": "in_progress"}], "revision": 2})
    hooks.on_post_tool_call("todo_list", {"todos": [{"id": "2", "status": "in_progress"}],
                                          "merge": True}, result, "t", tool_call_id="c1")
    await settle(a)
    assert client.updates == [(D_SID, [{"content": "Write tests", "status": "completed"},
                                       {"content": "Implement", "status": "inProgress"}], None)]
    # Legacy name, unparsable result: the plan comes from the args.
    hooks.on_post_tool_call("todo", {"todos": [{"id": "1", "content": "Ship",
                                                "status": "pending"}]}, "ok", "t",
                            tool_call_id="c2")
    # Nothing to show: no plan update.
    hooks.on_post_tool_call("todo_list", {"todos": []}, json.dumps({"todos": []}), "t",
                            tool_call_id="c3")
    await settle(a)
    assert client.updates[1:] == [(D_SID, [{"content": "Ship", "status": "pending"}], None)]
    assert acts(client, "action") == []  # the plan is the todo's rendering


async def test_external_url_once():
    a, client, gw, store, order = new_adapter(
        settings=make_settings(session_url="https://hermes.example/s/{session_id}"))
    assert await a.connect()
    try:
        await deliver(a, DELEGATION)
        hooks.get_session_env = SessionEnv(platform="linear", chat_id=D_CHAT,
                                           message_id=gw.events[0].message_id)
        for i in range(3):
            hooks.on_post_tool_call("terminal", {"command": "ls"}, "x", "t",
                                    tool_call_id=f"c{i}", session_id="hs-1")
        await settle(a)
        assert [u for u in client.updates if u[2] is not None] == []  # never a full replace
        assert client.added_urls == [(D_SID, [{"label": "Hermes session",
                                               "url": "https://hermes.example/s/hs-1"}])]
    finally:
        from gateway.session_context import get_session_env
        hooks.get_session_env = get_session_env
        await a.disconnect()


async def test_two_adapters_hook_routes_by_turn(session_env):
    a1, c1, gw1, s1, _ = new_adapter("pa")
    a2, c2, gw2, s2, _ = new_adapter("pb")
    assert await a1.connect() and await a2.connect()
    try:
        assert ADAPTERS["pa"] is a1 and ADAPTERS["pb"] is a2
        await deliver(a1, MENTION)
        await deliver(a2, second_mention())
        # Same chat on both profiles: only the message id tells them apart.
        session_env(platform="linear", message_id=gw2.events[0].message_id, chat_id=M_ISSUE,
                    thread_id=M_SID)
        n1, n2 = len(c1.activities), len(c2.activities)
        hooks.on_post_tool_call("terminal", {"command": "ls"}, "x", "t", tool_call_id="c1")
        await settle(a1)
        await settle(a2)
        assert len(c1.activities) == n1
        assert [s for s, *_ in c2.activities[n2:]] == ["sess-m2"]
    finally:
        await a1.disconnect()
        await a2.disconnect()


# --------------------------------------- fix round 1: no activity after response (I1)


def stop_event(activity="act-stop"):
    return prompted(D_SID, D_ISSUE, "", activity, signal="stop")


async def test_tool_event_after_response_dropped(env, session_env, monkeypatch, caplog):
    a, client, gw, store, order = env
    monkeypatch.setattr(outbound_mod, "FAST_TOOL_SKIP_S", 0.02)
    await deliver(a, DELEGATION)
    mid = gw.events[0].message_id
    await a.send(D_CHAT, "All done", reply_to=mid, metadata={"notify": True})
    # A background-review fork still has the turn's session vars bound.
    session_env(platform="linear", message_id=mid, chat_id=D_CHAT)
    n = len(client.activities)
    with caplog.at_level(logging.DEBUG, logger="hermes_linear.outbound"):
        hooks.on_pre_tool_call("memory", {"action": "add"}, "t", tool_call_id="c1")
        await settle(a, 0.05)  # past the fast-tool skip: the start is not shown either
        hooks.on_post_tool_call("memory", {"action": "add"}, "ok", "t", tool_call_id="c1")
        await settle(a)
    assert len(client.activities) == n
    assert a.outbound.pending_starts == {}
    assert "answered turn" in caplog.text
    # Still in flight and not answered: tool events are posted as usual.
    await complete(a, gw.events[0])
    await deliver(a, prompted(D_SID, D_ISSUE, "more", "act-2"))
    session_env(platform="linear", message_id=gw.events[-1].message_id, chat_id=D_CHAT)
    hooks.on_post_tool_call("terminal", {"command": "ls"}, "x", "t", tool_call_id="c2")
    await settle(a)
    assert acts(client, "action")[-1][0] == D_SID


async def test_interim_after_response_dropped(env, caplog):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    mid = gw.events[0].message_id
    await a.send(D_CHAT, "All done", reply_to=mid, metadata={"notify": True})
    await complete(a, gw.events[0])
    n = len(client.activities)
    with caplog.at_level(logging.DEBUG, logger="hermes_linear.outbound"):
        r1 = await a.send(D_CHAT, "💾 Memory updated", metadata={
            "reply_to_message_id": mid, "_interim_send": True})
        r2 = await a.send(D_CHAT, "review note", metadata={"thread_id": None,
                                                           "_interim_send": True})
        r3 = await a.send(D_CHAT, "late final", reply_to=mid, metadata={"notify": True})
    await a.wait_idle()
    assert r1.success and r2.success and r3.success  # never retried / notified
    # Ruling 33: real content addressed to the answered turn is an additional response.
    assert [(s, c["type"], c.get("body")) for s, c, *_ in client.activities[n:]] == [
        (D_SID, "response", "late final")]
    assert "dropped" in caplog.text
    # A new turn of the same agent session is in flight: thoughts are posted again.
    await deliver(a, prompted(D_SID, D_ISSUE, "more", "act-2"))
    await a.send(D_CHAT, "note", metadata={"reply_to_message_id": mid, "_interim_send": True})
    assert bodies(client, "thought")[-1] == (D_SID, "note")


async def test_post_tool_after_stop_dropped(env, session_env, monkeypatch):
    a, client, gw, store, order = env
    monkeypatch.setattr(outbound_mod, "FAST_TOOL_SKIP_S", 0.05)
    await deliver(a, DELEGATION)
    first = gw.events[0]
    session_env(platform="linear", message_id=first.message_id, chat_id=D_CHAT)
    hooks.on_pre_tool_call("terminal", {"command": "sleep 99"}, "t", tool_call_id="c1")
    await settle(a)
    await deliver(a, stop_event())
    await complete(a, first, ProcessingOutcome.CANCELLED)
    # The interrupted tool reports after Stopped.
    hooks.on_post_tool_call("terminal", {"command": "sleep 99"}, "interrupted", "t",
                            tool_call_id="c1", status="error")
    await settle(a, 0.1)
    assert acts(client, "action") == []
    assert bodies(client, "response") == [(D_SID, "Stopped.")]
    assert client.activities[-1][1]["type"] == "response"


async def test_writes_ordered_per_session(env, session_env):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    mid = gw.events[0].message_id
    client.type_delay["action"] = 0.05  # the action's write is slow (backoff)
    session_env(platform="linear", message_id=mid, chat_id=D_CHAT)
    hooks.on_post_tool_call("terminal", {"command": "ls"}, "x", "t", tool_call_id="c1")
    await asyncio.sleep(0)  # the hook's call_soon_threadsafe: the action is issued
    assert ("t", "c1") not in a.outbound.pending_starts
    res = await a.send(D_CHAT, "All done", reply_to=mid, metadata={"notify": True})
    assert res.success
    kinds = [c["type"] for _sid, c, *_ in client.activities]
    assert kinds[-2:] == ["action", "response"]  # issued first, written first


# ------------------------------------------- fix round 1: stop always answered (I2)


async def test_stop_settled_as_failure_still_posts_stopped(env):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    first = gw.events[0]
    await deliver(a, stop_event())

    async def base_task():  # the base's exception path after an unexpected cancel
        await a.on_processing_complete(first, ProcessingOutcome.FAILURE)
        await a.send(D_CHAT, "Sorry, I hit an error.")

    await asyncio.create_task(base_task())
    await a.wait_idle()
    assert bodies(client, "response") == [(D_SID, "Stopped.")]
    assert bodies(client, "error") == []
    assert a.last_outcome.get((D_CHAT, None)) is None
    assert a.stop_message_ids == {}


async def test_stop_never_settles_posts_stopped_after_30s(env, monkeypatch, caplog):
    a, client, gw, store, order = env
    assert constants.STOP_SETTLE_S == 30
    monkeypatch.setattr(constants, "STOP_SETTLE_S", 0.05)
    await deliver(a, DELEGATION)
    first = gw.events[0]
    await deliver(a, stop_event())
    stop_mid = gw.events[-1].message_id
    await deliver(a, prompted(D_SID, D_ISSUE, "next", "act-3"))  # queued behind the stop
    assert [e.text for e in gw.events].count("next") == 0
    with caplog.at_level(logging.WARNING):
        await settle(a, 0.1)
    assert bodies(client, "response") == [(D_SID, "Stopped.")]
    assert a.stop_message_ids == {}
    assert "after /stop" in caplog.text
    assert gw.events[-1].text.endswith("next")  # the FIFO was freed
    # The gateway finally completes the stopped turn and acks late: nothing more is posted.
    n = len(client.activities)
    await complete(a, first, ProcessingOutcome.CANCELLED)
    await a.send(D_CHAT, "⚡ Stopped.", reply_to=stop_mid, metadata={"notify": True})
    await a.wait_idle()
    assert len(client.activities) == n


async def test_stop_settle_cancelled_when_turn_finishes(env, monkeypatch):
    a, client, gw, store, order = env
    monkeypatch.setattr(constants, "STOP_SETTLE_S", 0.05)
    await deliver(a, DELEGATION)
    await deliver(a, stop_event())
    await complete(a, gw.events[0], ProcessingOutcome.CANCELLED)
    await settle(a, 0.1)
    assert bodies(client, "response") == [(D_SID, "Stopped.")]  # once, not again at 0.05


async def test_safety_expired_posts_error(env, monkeypatch):
    a, client, gw, store, order = env
    monkeypatch.setattr(constants, "TURN_SAFETY_TIMEOUT_S", 0.05)
    await deliver(a, DELEGATION)
    await settle(a, 0.1)
    assert bodies(client, "error") == [(D_SID, "This turn timed out.")]
    # A turn that already answered gets no timeout error.
    await deliver(a, prompted(D_SID, D_ISSUE, "more", "act-2"))
    await a.send(D_CHAT, "done", reply_to=gw.events[-1].message_id, metadata={"notify": True})
    await settle(a, 0.1)
    assert bodies(client, "error") == [(D_SID, "This turn timed out.")]


# ----------------------------------- fix round 1: status notices never promoted (I3)


async def test_heartbeat_thought_never_promoted(env):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    mid = gw.events[0].message_id
    await a.send(D_CHAT, "the answer", metadata={"thread_id": None, "reply_to_message_id": mid,
                                                "_interim_send": True})
    # The gateway's heartbeat: thread metadata only, no reply_to_message_id.
    await a.send(D_CHAT, "⏳ Still working... (10 min elapsed)",
                 metadata={"thread_id": None, "_interim_send": True})
    await complete(a, gw.events[0])  # SUCCESS; the final send was deduplicated
    assert bodies(client, "response") == [(D_SID, "the answer")]  # commentary, not heartbeat
    # A turn whose only thought is a status notice gets no promoted response.
    await deliver(a, prompted(D_SID, D_ISSUE, "more", "act-2"))
    await a.send(D_CHAT, "⏳ Still working...", metadata={"thread_id": None,
                                                         "_interim_send": True})
    await complete(a, gw.events[-1])
    assert bodies(client, "response") == [(D_SID, "the answer")]
    assert a.outbound.last_thought == {}


# ---------------------------------------------------------- fix round 1: minors


async def test_two_profiles_same_chat_routed_by_session_key(session_env):
    a1, c1, gw1, s1, _ = new_adapter("pa")
    a2, c2, gw2, s2, _ = new_adapter("pb")
    assert await a1.connect() and await a2.connect()
    try:
        await deliver(a1, MENTION)
        await deliver(a2, MENTION)  # same issue, thread and message id on both profiles
        assert gw1.events[0].message_id == gw2.events[0].message_id
        n1, n2 = len(c1.activities), len(c2.activities)
        session_env(platform="linear", message_id=gw2.events[0].message_id, chat_id=M_ISSUE,
                    thread_id=M_SID, key=f"agent:pb:linear:dm:{M_ISSUE}:{M_SID}")
        hooks.on_post_tool_call("terminal", {"command": "ls"}, "x", "t", tool_call_id="c1")
        await settle(a1)
        await settle(a2)
        assert len(c1.activities) == n1
        assert [s for s, *_ in c2.activities[n2:]] == [M_SID]
        # A key of another namespace reaches neither adapter.
        session_env(platform="linear", message_id=gw2.events[0].message_id, chat_id=M_ISSUE,
                    thread_id=M_SID, key=f"agent:main:linear:dm:{M_ISSUE}:{M_SID}")
        hooks.on_post_tool_call("terminal", {"command": "ls"}, "x", "t", tool_call_id="c2")
        await settle(a1)
        await settle(a2)
        assert (len(c1.activities), len(c2.activities)) == (n1, n2 + 1)
    finally:
        await a1.disconnect()
        await a2.disconnect()


async def test_typing_suppressed_after_recent_activity(env):
    a, client, gw, store, order = env
    now = [1000.0]
    a.outbound.clock = lambda: now[0]
    await deliver(a, MENTION)
    mid, meta = gw.events[0].message_id, {"thread_id": M_SID}

    def working():
        return [c for _s, c, *_ in client.activities if c.get("body") == "Working…"]

    await a.send(M_ISSUE, "commentary", metadata={**meta, "reply_to_message_id": mid,
                                                  "_interim_send": True})
    now[0] = 1010.0
    await a.send_typing(M_ISSUE, metadata=meta)
    await settle(a)
    assert working() == []  # an activity 10 s ago: no gap to fill
    now[0] = 1071.0
    await a.send_typing(M_ISSUE, metadata=meta)
    await settle(a)
    assert len(working()) == 1  # 71 s of silence


def test_outbound_memory_bounded():
    a, *_ = new_adapter()
    ob = a.outbound
    for i in range(outbound_mod._REMEMBER + 50):
        ob.last_thought.put(f"m{i}", "x")
        ob.typing_at.put(f"s{i}", float(i))
        ob.url_set.add(f"s{i}")
    assert len(ob.last_thought) == len(ob.typing_at) == len(ob.url_set) == outbound_mod._REMEMBER
    assert "m0" not in ob.last_thought and f"m{outbound_mod._REMEMBER + 49}" in ob.last_thought


# ------------------------------------------------------------- fix round 2


def _final_metadata(ev):
    """The metadata ``_process_message_background`` gives the final text and attachments."""
    return base_mod._mark_notify_metadata(base_mod._thread_metadata_for_event(ev))


async def _base_final(a, ev, text, *, images=(), media_files=(), local_files=()):
    """Drive the base's own final-reply delivery: final text, then ``_deliver_attachments``."""
    md = _final_metadata(ev)
    results: list = []
    if text:
        results.append(await a.send(ev.source.chat_id, text,
                                    reply_to=base_mod._reply_anchor_for_event(ev), metadata=md))
    extracted = base_mod._ExtractedResponse(
        text_content=text, images=list(images), media_files=list(media_files),
        local_files=list(local_files), force_document_attachments=False, pre_extract=text)
    await a._deliver_attachments(ev, extracted, md, anything_sent=bool(text),
                                 record_delivery=results.append)
    return md, results


async def test_attachment_after_response_posted_as_response(env, tmp_path):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    ev = gw.events[0]
    await a.send(D_CHAT, "looking", metadata={"reply_to_message_id": ev.message_id,
                                              "_interim_send": True})
    report = tmp_path / "secret-dir" / "report.pdf"
    md, results = await _base_final(a, ev, "Here is the chart",
                                    images=[("https://x.example/chart.png", "chart")],
                                    local_files=[str(report)])
    assert md.get("notify") is True and "reply_to_message_id" not in md  # the real base shape
    assert [r.success for r in results] == [True, True, True]
    await complete(a, ev)
    kinds = [c["type"] for _s, c, *_ in client.activities]
    first = kinds.index("response")
    assert kinds[first:] == ["response", "response", "response"]  # final, image, document
    got = [b for _s, b in bodies(client, "response")]
    assert got[0] == "Here is the chart"
    assert got[1].startswith("![chart](https://x.example/chart.png)")
    assert got[2] == "Attached file: report.pdf (not uploaded to Linear)"
    assert not any(str(tmp_path) in b for b in got)  # never a local path


async def test_media_only_and_local_media_rendered(env, tmp_path):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    ev = gw.events[0]
    img, vid, aud = (str(tmp_path / n) for n in ("plot.png", "clip.mp4", "note.mp3"))
    _md, results = await _base_final(a, ev, "", local_files=[img, vid],
                                     media_files=[(aud, False)])
    assert all(r.success for r in results)
    await complete(a, ev)
    got = sorted(b for _s, b in bodies(client, "response"))
    assert got == sorted(["Attached image: plot.png (not uploaded to Linear)",
                          "Attached file: clip.mp4 (not uploaded to Linear)",
                          "Attached file: note.mp3 (not uploaded to Linear)"])
    assert [c["type"] for _s, c, *_ in client.activities][-3:] == ["response"] * 3


async def test_media_rendering_direct(env):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    ev = gw.events[0]
    md = _final_metadata(ev)
    await a.send_image(D_CHAT, "https://x.example/a.png", metadata=md)
    await a.send_image(D_CHAT, "file:///home/u/private/b%20c.png", caption="pic", metadata=md)
    await a.send_document(D_CHAT, "https://x.example/r.pdf", metadata=md)
    await a.send_document(D_CHAT, "https://x.example/r.pdf", caption="Report", metadata=md)
    await a.send_document(D_CHAT, "/home/u/private/x.bin", file_name="nice.bin",
                          caption="the file", metadata=md)
    got = [b for _s, b in bodies(client, "response")]
    assert got == ["![image](https://x.example/a.png)",
                   "Attached image: b c.png (not uploaded to Linear)\n\npic",
                   "[r.pdf](https://x.example/r.pdf)",
                   "[Report](https://x.example/r.pdf)",
                   "Attached file: nice.bin (not uploaded to Linear)\n\nthe file"]
    assert not any("/home/u" in b for b in got)


async def test_heartbeat_after_response_still_dropped(env, caplog):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    ev = gw.events[0]
    await _base_final(a, ev, "All done")
    n = len(client.activities)
    thread_md = base_mod._thread_metadata_for_event(ev)  # what heartbeat/stuck/error use
    with caplog.at_level(logging.DEBUG, logger="hermes_linear.outbound"):
        # gateway heartbeat / stuck warning: _interim_metadata(thread metadata), no notify
        r1 = await a.send(D_CHAT, "⏳ Still working...", metadata={**(thread_md or {}),
                                                                 "_interim_send": True})
        # an unmarked status send with thread metadata only (no notify) is still dropped
        r2 = await a.send(D_CHAT, "status", metadata=thread_md)
        # notify never rescues an interim send
        r3 = await a.send(D_CHAT, "interim", metadata={"notify": True, "_interim_send": True})
    await a.wait_idle()
    assert r1.success and r2.success and r3.success
    assert len(client.activities) == n
    await complete(a, ev)
    assert client.activities[-1][1]["type"] == "response"


async def test_turn_error_notice_metadata_has_no_notify(env):
    """Pin the base: ``_notify_turn_error`` sends bare thread metadata (no notify)."""
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    ev = gw.events[0]
    await complete(a, ev, ProcessingOutcome.FAILURE)
    used = await a._notify_turn_error(ev, RuntimeError("boom"))
    assert not (used or {}).get("notify")
    assert [c["type"] for _s, c, *_ in client.activities][-1] == "error"


async def test_status_notice_after_response_dropped_while_turn_still_in_flight(env, caplog):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    mid = gw.events[0].message_id
    await a.send(D_CHAT, "All done", reply_to=mid, metadata={"notify": True})
    assert a.turns.find(mid) is not None  # still in flight (before on_processing_complete)
    n = len(client.activities)
    with caplog.at_level(logging.DEBUG, logger="hermes_linear.outbound"):
        r1 = await a.send(D_CHAT, "⏳ Still working...", metadata={"thread_id": None,
                                                                 "_interim_send": True})
        r2 = await a.send(D_CHAT, "💾 Memory updated", metadata={
            "reply_to_message_id": mid, "_interim_send": True})
        r3 = await a.send(D_CHAT, "unmarked status", metadata={"thread_id": None})
    await a.wait_idle()
    assert r1.success and r2.success and r3.success
    assert len(client.activities) == n
    assert "dropped" in caplog.text
    await complete(a, gw.events[0])
    assert client.activities[-1][1]["type"] == "response"


@pytest.mark.parametrize("write_fails", [False, True])
async def test_cancelled_final_send_still_answers(env, write_fails):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    first = gw.events[0]
    client.type_delay["thought"] = 0.1
    a.outbound._post(D_SID, {"type": "thought", "body": "slow"})  # the final queues behind it
    if write_fails:
        real, failed = client.create_activity, []

        async def fail_first_response(sid, content, **kw):
            if content["type"] == "response" and not failed:
                failed.append(content)
                raise RuntimeError("linear down")
            return await real(sid, content, **kw)

        client.create_activity = fail_first_response
    task = asyncio.create_task(a.send(D_CHAT, "final", reply_to=first.message_id,
                                      metadata={"notify": True}))
    await asyncio.sleep(0.01)
    a.turns.interrupt(D_SID)  # the stop webhook: /stop goes to the gateway
    await asyncio.sleep(0)
    assert a.stop_message_ids
    task.cancel()  # /stop cancels the base task during the final send
    with pytest.raises(asyncio.CancelledError):
        await task
    # The base completes the cancelled turn while the final is still queued.
    await asyncio.create_task(a.on_processing_complete(first, ProcessingOutcome.CANCELLED))
    assert bodies(client, "response") == []
    await settle(a, 0.2)
    want = "Stopped." if write_fails else "final"
    assert bodies(client, "response") == [(D_SID, want)]
    assert client.activities[-1][1]["type"] == "response"


async def test_primary_multiplexed_profile_routes_tool_events(session_env):
    a, client, gw, store, _ = new_adapter()  # primary adapter: no _owner_profile
    a._session_key_profile = lambda source=None: "neo"  # launch profile under multiplexing
    assert await a.connect()
    try:
        await deliver(a, DELEGATION)
        mid = gw.events[0].message_id
        key = f"agent:neo:linear:dm:{D_ISSUE}:1"
        assert a.turns.owns_session_key(key)
        assert a.turns.turn_for_session_key(key)[1].mid == mid
        session_env(platform="linear", message_id=mid, chat_id=D_CHAT, key=key)
        hooks.on_post_tool_call("terminal", {"command": "ls"}, "x", "t", tool_call_id="c1")
        await settle(a)
        assert acts(client, "action") == [(D_SID, {"type": "action", "action": "terminal",
                                                   "parameter": "ls", "result": "x"})]
    finally:
        await a.disconnect()


@pytest.mark.parametrize("net", ["safety", "stop_settle"])
async def test_safety_release_waits_for_base_guard(env, monkeypatch, net):
    a, client, gw, store, order = env
    if net == "safety":
        monkeypatch.setattr(constants, "TURN_SAFETY_TIMEOUT_S", 0.05)
    else:
        monkeypatch.setattr(constants, "STOP_SETTLE_S", 0.05)
    await deliver(a, DELEGATION)
    key = a.turns.current_turn_for(D_CHAT, None).session_key
    a._active_sessions[key] = asyncio.Event()  # the base still holds the session guard
    if net == "stop_settle":
        a.turns.interrupt(D_SID)  # /stop sent; the gateway never completes the turn
    await deliver(a, prompted(D_SID, D_ISSUE, "next", "act-2"))
    await asyncio.sleep(0.15)
    if net == "safety":
        assert bodies(client, "error") == [(D_SID, "This turn timed out.")]
    else:
        assert bodies(client, "response") == [(D_SID, "Stopped.")]
    assert not gw.events[-1].text.endswith("next")  # not released into the busy path
    del a._active_sessions[key]
    await settle(a, 0.1)
    assert gw.events[-1].text.endswith("next")
