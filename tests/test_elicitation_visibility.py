"""Linear selects stay visible: nothing is posted after a pending elicitation.

Linear only shows an elicitation's select input while it is the agent session's latest
activity, so while an approval / clarify control is pending the plugin posts nothing else to
that session (tool actions, typing / heartbeat / interim thoughts) until the control is
resolved, expired or dropped. Errors, the turn's response and the control's own elicitation
still post. The ``clarify`` tool never posts actions at all: the elicitation is its rendering.
"""

from __future__ import annotations

import dataclasses

import pytest

from test_adapter_inbound import D_ISSUE, D_SID, DELEGATION, deliver, prompted
from test_adapter_outbound import (  # noqa: F401  (fixtures)
    D_CHAT, acts, approval_prompt, complete, env, session_env, settle,
)

from hermes_linear import hooks, sessions_open as sessions_open_mod
from hermes_linear import outbound as outbound_mod

META = {"thread_id": None}
CLARIFY_ARGS = {"question": "Workflow framework?", "choices": ["superpowers"]}


def kinds(client, start):
    return [(sid, c["type"], c.get("body") or c.get("action")) for sid, c, *_
            in client.activities[start:]]


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(outbound_mod, "FAST_TOOL_SKIP_S", 0.02)


async def _delegated(a, gw, session_env):
    await deliver(a, DELEGATION)
    mid = gw.events[0].message_id
    session_env(platform="linear", message_id=mid, chat_id=D_CHAT)
    return mid


# ------------------------------------------------------------ 1. clarify tool never posts


@pytest.mark.parametrize("name,args", [
    ("clarify", CLARIFY_ARGS),
    ("tool_call", {"calls": [{"name": "clarify", "arguments": CLARIFY_ARGS}]}),
    ("tool_call", {"name": "clarify", "arguments": CLARIFY_ARGS}),
    ("tool_call", {"calls": '[{"name": "clarify", "arguments": {}}]'}),
])
async def test_clarify_tool_posts_no_actions(env, session_env, fast, name, args):
    a, client, gw, store, order = env
    await _delegated(a, gw, session_env)
    n = len(client.activities)
    hooks.on_pre_tool_call(name, args, "t", tool_call_id="c1")
    await settle(a, 0.06)  # past the fast-tool skip
    hooks.on_post_tool_call(name, args, '{"answer": "superpowers"}', "t", tool_call_id="c1")
    await settle(a)
    assert acts(client, "action") == [] and len(client.activities) == n
    assert a.outbound.pending_starts == {}


async def test_bridged_other_tool_still_posts(env, session_env):
    a, client, gw, store, order = env
    await _delegated(a, gw, session_env)
    hooks.on_post_tool_call("tool_call", {"calls": [{"name": "web_search", "arguments": {}}]},
                            "r", "t", tool_call_id="c1")
    await settle(a)
    assert [c["action"] for _, c in acts(client, "action")] == ["tool_call"]


# ------------------------------------------------------------ 2. pending control gate


async def test_pending_clarify_suppresses_everything_until_resolved(env, session_env, fast,
                                                                     monkeypatch):
    """End to end: clarify elicitation, then typing + a hook action + a heartbeat + an
    interim send -> nothing posted; after the answer resolves the control, actions post."""
    a, client, gw, store, order = env
    monkeypatch.setattr(sessions_open_mod, "_resolve_clarify", lambda rid, answer: True)
    await _delegated(a, gw, session_env)
    res = await a.send_clarify(D_CHAT, "Workflow framework?", ["superpowers"], "clar-1", "sk",
                               metadata=META)
    assert res.success
    n = len(client.activities)
    assert client.activities[-1][1]["type"] == "elicitation"
    a.outbound.typing_at.clear()  # typing is not rate-limited away
    await a.send_typing(D_CHAT, metadata=META)
    hooks.on_pre_tool_call("terminal", {"command": "ls"}, "t", tool_call_id="c1")
    await settle(a, 0.06)
    hooks.on_post_tool_call("terminal", {"command": "ls"}, "x", "t", tool_call_id="c1")
    await a.outbound.sleep.tick()  # heartbeat
    r = await a.send(D_CHAT, "progress", metadata={**META, "_interim_send": True})
    await settle(a)
    assert r.success
    assert len(client.activities) == n, kinds(client, n)
    assert client.activities[-1][1]["type"] == "elicitation"
    # The user answers: the control is resolved, activities post again.
    await deliver(a, prompted(D_SID, D_ISSUE, "superpowers", "act-c"))
    assert store.control_for_session(D_SID) is None
    hooks.on_post_tool_call("terminal", {"command": "ls"}, "y", "t", tool_call_id="c2")
    await settle(a)
    assert acts(client, "action")[-1][1]["result"] == "y"


async def test_pending_control_allows_error_response_and_expiry(env, session_env):
    a, client, gw, store, order = env
    mid = await _delegated(a, gw, session_env)
    await a.send_clarify(D_CHAT, "Which?", None, "clar-1", "sk", metadata=META)
    n = len(client.activities)
    a.outbound.safety_expired(a.turns.find(mid)[1])  # an error activity
    await settle(a)
    assert kinds(client, n) == [(D_SID, "error", outbound_mod.TIMED_OUT_TEXT)]
    await a.retire_clarify_card("clar-1", "x")
    await settle(a)
    assert kinds(client, n)[-1] == (D_SID, "thought", outbound_mod.EXPIRED_TEXT)


async def test_final_response_posts_while_control_pending(env, session_env):
    a, client, gw, store, order = env
    mid = await _delegated(a, gw, session_env)
    await a.send_clarify(D_CHAT, "Which?", None, "clar-1", "sk", metadata=META)
    await a.send(D_CHAT, "Gave up asking", reply_to=mid, metadata={"notify": True})
    await settle(a)
    assert client.activities[-1][1] == {"type": "response", "body": "Gave up asking"}


async def test_control_only_mutes_its_own_session(env, session_env):
    a, client, gw, store, order = env
    await _delegated(a, gw, session_env)
    store.put_control("other", "clarify", "sess-elsewhere", 1, [], session_key="k")
    hooks.on_post_tool_call("terminal", {"command": "ls"}, "x", "t", tool_call_id="c1")
    await settle(a)
    assert acts(client, "action")[-1][0] == D_SID


async def test_abandoned_control_does_not_mute_next_turn(env, session_env):
    """A control left behind by a finished turn never silences a later turn."""
    a, client, gw, store, order = env
    await _delegated(a, gw, session_env)
    await a.send_clarify(D_CHAT, "Which?", None, "clar-1", "sk", metadata=META)
    await complete(a, gw.events[0])
    store.drop_control("clar-1")  # let the next prompt start a turn, not answer the control
    await deliver(a, prompted(D_SID, D_ISSUE, "more", "act-2"))
    assert len(gw.events) == 2
    store.put_control("clar-1", "clarify", D_SID, 1, [], session_key="sk")  # row lingers
    session_env(platform="linear", message_id=gw.events[-1].message_id, chat_id=D_CHAT)
    hooks.on_post_tool_call("terminal", {"command": "ls"}, "x", "t", tool_call_id="c1")
    await settle(a)
    assert acts(client, "action")[-1][1]["result"] == "x"


async def test_stale_row_without_elicitation_does_not_mute(env, session_env):
    a, client, gw, store, order = env
    await _delegated(a, gw, session_env)
    store.put_control("stale", "clarify", D_SID, 1, [], session_key="sk")  # from a restart
    hooks.on_post_tool_call("terminal", {"command": "ls"}, "x", "t", tool_call_id="c1")
    await settle(a)
    assert acts(client, "action")[-1][1]["result"] == "x"


# ------------------------------------------------------------ 3. approval ordering


async def test_gated_tool_start_after_approval_elicitation_suppressed(env, session_env, fast,
                                                                      monkeypatch):
    """pre_tool_call(terminal) queues its start action; the approval elicitation posts
    before the fast-tool timer fires; the start must not land after the elicitation."""
    a, client, gw, store, order = env
    monkeypatch.setattr(sessions_open_mod, "_approval_refusal", lambda *a, **k: None)
    monkeypatch.setattr(sessions_open_mod, "_resolve_approval_card", lambda *a, **k: 1)
    await _delegated(a, gw, session_env)
    hooks.on_pre_tool_call("terminal", {"command": "rm -rf build"}, "t", tool_call_id="c1")
    await settle(a)  # the start is scheduled, not yet posted
    assert a.outbound.pending_starts
    res = await a._send_exec_approval_prompt(
        dataclasses.replace(approval_prompt(thread_id=None), chat_id=D_CHAT))
    assert res.success
    await settle(a, 0.06)  # the start timer fires while the approval is pending
    assert client.activities[-1][1]["type"] == "elicitation"
    assert acts(client, "action") == []
    await deliver(a, prompted(D_SID, D_ISSUE, "once", "act-a"))
    assert store.control_for_session(D_SID) is None
    hooks.on_post_tool_call("terminal", {"command": "rm -rf build"}, "done", "t",
                            tool_call_id="c1")
    await settle(a)
    assert [(c["action"], c.get("result")) for _, c in acts(client, "action")] == [
        ("terminal", "done")]

