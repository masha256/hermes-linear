"""Timed-out Linear questions become pending (RET-94 T3, spec "States", §2, §4).

Hermes's own timeout callbacks (``retire_clarify_card``, the approval-timeout ``edit_message``)
switch a live control row to ``pending``: nothing is posted and the ``Blocked`` label goes on
the target issue (the one open task, else the session ticket). A plugin-issued stop or a stale
generation keeps the old expiry (row dropped, expiry thought posted).
"""

from __future__ import annotations

import dataclasses
import json
import logging

import pytest

from test_adapter_inbound import (
    APP, D_ISSUE, D_SID, DELEGATION, HUMAN, ISSUE_DELEGATED, M_ISSUE, MENTION, complete,
    deliver, prompted,
)
from test_adapter_outbound import (  # noqa: F401  (fixtures)
    D_CHAT, approval_prompt, env, new_adapter, session_env, settle,
)

from hermes_linear import adapter as adapter_mod
from hermes_linear import hooks
from hermes_linear import outbound as outbound_mod
from hermes_linear import questions
from hermes_linear import sessions_open as sessions_open_mod
from hermes_linear.models import SetMode
from hermes_linear.router import message_id
from hermes_linear.store import Store

META = {"thread_id": None}
BLOCKED = "lb-blocked"


async def _delegated(a, gw, session_env):
    await deliver(a, DELEGATION)
    mid = gw.events[0].message_id
    session_env(platform="linear", message_id=mid, chat_id=D_CHAT)
    return mid


async def _clarify(a, client, cid="clar-1", question="Which env?"):
    res = await a.send_clarify(D_CHAT, question, ["staging", "prod"], cid, "sk", metadata=META)
    assert res.success and client.activities[-1][1]["type"] == "elicitation"
    return len(client.activities)


def _approval(rid="req-1"):
    return dataclasses.replace(approval_prompt(rid), chat_id=D_CHAT, metadata=META)


def _added(client):
    return [(i, u["addedLabelIds"]) for i, u in client.issue_updates if "addedLabelIds" in u]


async def test_clarify_timeout_defers(env, session_env):
    a, client, gw, store, order = env
    mid = await _delegated(a, gw, session_env)
    n = await _clarify(a, client)
    row = store.control("clar-1")
    assert (row.question, row.turn_mid) == ("Which env?", mid)
    await a.retire_clarify_card("clar-1", "expired")
    await settle(a)
    row = store.control("clar-1")
    assert row is not None and row.state == "pending"
    assert store.session(D_SID).issue_id == D_ISSUE
    assert row.ticket_id == D_ISSUE
    assert len(client.activities) == n  # nothing posted, no "This request expired."
    assert _added(client) == [(D_ISSUE, [BLOCKED])]
    # A second retire (run end after the timeout) leaves the pending row alone.
    await a.retire_clarify_card("clar-1", "run ended")
    await settle(a)
    assert store.control("clar-1").state == "pending" and len(client.activities) == n


async def test_pending_row_still_gates_while_turn_in_flight(env, session_env):
    a, client, gw, store, order = env
    await _delegated(a, gw, session_env)
    n = await _clarify(a, client)
    await a.retire_clarify_card("clar-1", "expired")
    r = await a.send(D_CHAT, "progress", metadata={**META, "_interim_send": True})
    await settle(a)
    assert r.success and len(client.activities) == n  # the thought would hide the select


async def test_clarify_timeout_during_stop_expires(env, session_env):
    a, client, gw, store, order = env
    await _delegated(a, gw, session_env)
    n = await _clarify(a, client)
    a.turns.stop_message_ids["x"] = D_SID
    await a.retire_clarify_card("clar-1", "cancelled")
    await settle(a)
    assert store.control("clar-1") is None
    assert [c["body"] for _, c, *_ in client.activities[n:]] == [outbound_mod.EXPIRED_TEXT]
    assert client.issue_updates == []


async def test_clarify_timeout_stale_generation_expires(env, session_env):
    a, client, gw, store, order = env
    await _delegated(a, gw, session_env)
    n = await _clarify(a, client)
    store.set_mode(D_ISSUE, "delegated", store.ticket(D_ISSUE).generation + 1)
    await a.retire_clarify_card("clar-1", "stale")
    await settle(a)
    assert store.control("clar-1") is None
    assert [c["body"] for _, c, *_ in client.activities[n:]] == [outbound_mod.EXPIRED_TEXT]
    assert client.issue_updates == []


async def test_approval_timeout_defers(env, session_env):
    a, client, gw, store, order = env
    mid = await _delegated(a, gw, session_env)
    res = await a._send_exec_approval_prompt(_approval())
    assert res.success
    n = len(client.activities)
    row = store.control("req-1")
    assert (row.question, row.command, row.turn_mid) == ("Run `ls`?", "ls", mid)
    res = await a.edit_message(D_CHAT, "approval:req-1", "timed out\n```ls```")
    await settle(a)
    assert res.success and res.message_id == "approval:req-1"
    row = store.control("req-1")
    assert row.state == "pending" and row.command == "ls" and row.ticket_id == D_ISSUE
    assert len(client.activities) == n
    assert _added(client) == [(D_ISSUE, [BLOCKED])]


async def test_approval_timeout_during_stop_expires(env, session_env):
    a, client, gw, store, order = env
    await _delegated(a, gw, session_env)
    await a._send_exec_approval_prompt(_approval())
    n = len(client.activities)
    a.turns.stop_message_ids["x"] = D_SID
    await a.edit_message(D_CHAT, "approval:req-1", "x")
    await settle(a)
    assert store.control("req-1") is None
    assert [c["body"] for _, c, *_ in client.activities[n:]] == [
        outbound_mod.APPROVAL_EXPIRED_TEXT]


@pytest.mark.parametrize("ticket_generation", [None, 3])
async def test_mention_approval_timeout_defers(env, ticket_generation):
    """A mention thread's chat id carries no generation (row generation 0); a ticket that was
    delegated before (generation >= 1, mode mention now) must not make it look stale."""
    a, client, gw, store, order = env
    await deliver(a, MENTION)
    if ticket_generation is not None:
        store.set_mode(M_ISSUE, "mention", ticket_generation)
    await a._send_exec_approval_prompt(approval_prompt())
    n = len(client.activities)
    await a.edit_message(M_ISSUE, "approval:req-1", "x")
    await settle(a)
    assert store.control("req-1").state == "pending" and len(client.activities) == n
    assert _added(client) == [(M_ISSUE, [BLOCKED])]


async def test_blocked_target_open_task(env, session_env):
    a, client, gw, store, order = env
    await _delegated(a, gw, session_env)
    store.set_open_task(D_ISSUE, "T1", True)
    await _clarify(a, client)
    await a.retire_clarify_card("clar-1", "expired")
    await settle(a)
    assert store.control("clar-1").ticket_id == "T1"
    assert _added(client) == [("T1", [BLOCKED])]
    store.set_open_task(D_ISSUE, "T2", True)
    assert questions.blocked_target(store, store.control("clar-1")) == D_ISSUE


async def test_blocked_target_unknown_session(env):
    a, client, gw, store, order = env
    store.put_control("r", "clarify", "no-such-session", 1, [])
    assert questions.blocked_target(store, store.control("r")) is None


async def test_label_missing_still_defers(env, session_env, caplog):
    a, client, gw, store, order = env
    client.team_label_list = [{"id": "lb-bug", "name": "Bug"}]
    await _delegated(a, gw, session_env)
    n = await _clarify(a, client)
    with caplog.at_level(logging.DEBUG, logger="hermes_linear.questions"):
        await a.retire_clarify_card("clar-1", "expired")
        await settle(a)
    assert store.control("clar-1").state == "pending"
    assert len(client.activities) == n and client.issue_updates == []


async def test_set_blocked_adds_and_removes_only_on_change(env, caplog):
    a, client, gw, store, order = env
    client.team_label_list = [{"id": BLOCKED, "name": "bLoCkEd"}]  # case-insensitive
    assert await questions.set_blocked(client, "I", True)
    assert await questions.set_blocked(client, "I", True)  # already carried: no write
    assert client.issue_updates == [("I", {"addedLabelIds": [BLOCKED]})]
    assert await questions.set_blocked(client, "I", False)
    assert await questions.set_blocked(client, "I", False)
    assert client.issue_updates[1:] == [("I", {"removedLabelIds": [BLOCKED]})]


async def test_set_blocked_failure_and_missing_label_return_false(env, caplog, monkeypatch):
    a, client, gw, store, order = env

    async def boom(issue_id):
        raise RuntimeError("linear down")

    monkeypatch.setattr(client, "issue_get", boom)
    assert await questions.set_blocked(client, "I", True) is False
    monkeypatch.undo()
    client.team_label_list = []
    questions._warned_teams.clear()
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="hermes_linear.questions"):
        assert await questions.set_blocked(client, "I", True) is False
        assert await questions.set_blocked(client, "J", True) is False
    levels = [r.levelno for r in caplog.records if "label" in r.getMessage()]
    assert levels == [logging.WARNING, logging.DEBUG]  # warned once per team


async def test_new_question_replaces_pending(env, session_env):
    a, client, gw, store, order = env
    await _delegated(a, gw, session_env)
    await _clarify(a, client)
    await a.retire_clarify_card("clar-1", "expired")
    await settle(a)
    assert store.control("clar-1").state == "pending"
    await _clarify(a, client, "clar-2", "Second?")
    assert store.control("clar-1") is None
    rows = store.controls_for_issue(D_ISSUE)
    assert [(r.request_id, r.state) for r in rows] == [("clar-2", "live")]
    assert not any("removedLabelIds" in u for _, u in client.issue_updates)  # Blocked stays


def test_copy_verbatim():
    assert questions.PENDING_NOTICE == (
        "Not answered. The question stays open in the Linear ticket; the answer arrives as the "
        "next message. End this turn now; do not assume an answer.")
    assert questions.APPROVAL_PENDING_TEXT == (
        "Command not run: approval is pending in Linear. End this turn. If it is approved, the "
        "next message says so, and re-running this exact command is pre-approved once.")


# ------------------------------------------------------------ transform_tool_result (T5)

SK = "agent:main:linear:dm:x"
TIMED_OUT = json.dumps({"responses": [{"question": "Which env?", "status": "timed_out",
                                       "user_response": None}], "outcome": "timed_out"})
APPROVAL_TIMEOUT = "BLOCKED: Command timed out without user response. Silence is not consent."


MID = "turn-mid-1"  # the asking turn's message id (row.turn_mid == HERMES_SESSION_MESSAGE_ID)


def _row(store, rid="clar-1", kind="clarify", command=None, sid=D_SID, turn_mid=MID,
         question="Which env?"):
    store.put_control(rid, kind, sid, 1, [], session_key=SK, question=question,
                      command=command, turn_mid=turn_mid)


def _answered(question="Which env?"):
    # Hermes's real answered outcome (gateway/run_turn_runner.py: outcome "submitted").
    return json.dumps({"responses": [{"question": question, "status": "answered",
                                      "user_response": "prod"}], "outcome": "submitted"})


async def test_transform_rewrites_timed_out_clarify(env, session_env):
    a, client, gw, store, order = env
    _row(store)
    session_env(platform="linear", key=SK, message_id=MID)
    assert hooks.on_transform_tool_result("clarify", {}, TIMED_OUT) is not None  # live row
    assert store.mark_control_pending("clar-1", D_ISSUE)
    out = hooks.on_transform_tool_result("clarify", {"question": "Which env?"}, TIMED_OUT,
                                         status="ok")
    got = json.loads(out)
    assert got["outcome"] == "pending" and got["notice"] == questions.PENDING_NOTICE
    assert got["responses"] == json.loads(TIMED_OUT)["responses"]
    # Bridged (still wrapped) clarify is recognised the same way.
    bridged = {"calls": [{"name": "clarify", "arguments": {"question": "Which env?"}}]}
    assert json.loads(hooks.on_transform_tool_result("tool_call", bridged, TIMED_OUT))[
        "outcome"] == "pending"


async def test_transform_ignores_answered_and_non_linear(env, session_env):
    a, client, gw, store, order = env
    session_env(platform="linear", key=SK, message_id=MID)
    assert hooks.on_transform_tool_result("clarify", {}, TIMED_OUT) is None  # no row
    _row(store)
    session_env(platform="slack", key=SK, message_id=MID)
    assert hooks.on_transform_tool_result("clarify", {}, TIMED_OUT) is None
    session_env(platform="linear", message_id=MID)  # no session key bound
    assert hooks.on_transform_tool_result("clarify", {}, TIMED_OUT) is None
    session_env(platform="linear", key=SK, message_id=MID)
    for bad in ("not json", json.dumps(["x"]), None, 42):
        assert hooks.on_transform_tool_result("clarify", {}, bad) is None
    assert hooks.on_transform_tool_result("terminal", {"command": "ls"}, TIMED_OUT) is None
    assert hooks.on_transform_tool_result("clarify", {}, _answered()) is None
    session_env(platform="linear", key=SK)  # no turn message id bound: act on nothing
    assert hooks.on_transform_tool_result("clarify", {}, TIMED_OUT) is None


async def test_transform_drops_rows_when_answered(env, session_env):
    a, client, gw, store, order = env
    _row(store, "clar-1")
    _row(store, "req-1", kind="approval", command="ls")
    session_env(platform="linear", key=SK, message_id=MID)
    for outcome in ("cancelled", "undelivered"):
        res = json.dumps({"responses": [], "outcome": outcome})
        assert hooks.on_transform_tool_result("clarify", {}, res) is None
        assert store.control("clar-1") is not None
    assert hooks.on_transform_tool_result("clarify", {}, _answered()) is None
    assert store.control("clar-1") is None
    assert store.control("req-1") is not None  # approval rows are not clarify answers


async def test_transform_rewrites_approval_timeout(env, session_env):
    a, client, gw, store, order = env
    _row(store, "req-1", kind="approval", command="rm -rf build")
    session_env(platform="linear", key=SK, message_id=MID)
    assert hooks.on_transform_tool_result(
        "terminal", {"command": "rm -rf build"}, APPROVAL_TIMEOUT) == questions.APPROVAL_PENDING_TEXT
    # Contained in a longer string value, nested args.
    assert hooks.on_transform_tool_result(
        "execute_code", {"opts": {"cmds": ["cd x && rm -rf build"]}},
        APPROVAL_TIMEOUT) == questions.APPROVAL_PENDING_TEXT
    assert hooks.on_transform_tool_result(
        "terminal", {"command": "rm -rf dist"}, APPROVAL_TIMEOUT) is None
    assert hooks.on_transform_tool_result(
        "terminal", {"command": "rm -rf build"}, "BLOCKED: user denied") is None
    session_env(platform="slack", key=SK, message_id=MID)
    assert hooks.on_transform_tool_result(
        "terminal", {"command": "rm -rf build"}, APPROVAL_TIMEOUT) is None


async def test_transform_approval_command_with_quotes(env, session_env):
    a, client, gw, store, order = env
    cmd = 'echo "hi there" > out.txt'
    _row(store, "req-1", kind="approval", command=cmd)
    session_env(platform="linear", key=SK, message_id=MID)
    assert hooks.on_transform_tool_result(
        "terminal", {"command": cmd}, APPROVAL_TIMEOUT) == questions.APPROVAL_PENDING_TEXT


async def test_transform_sessions_sharing_a_key(env, session_env):
    """Two Linear agent sessions on one Hermes key: an event for one turn leaves the other
    session's row untouched."""
    a, client, gw, store, order = env
    _row(store, "clar-a", sid="sess-a", turn_mid="mid-a", question="A?")
    _row(store, "clar-b", sid="sess-b", turn_mid="mid-b", question="B?")
    assert store.mark_control_pending("clar-a", D_ISSUE)
    session_env(platform="linear", key=SK, message_id="mid-b")
    assert hooks.on_transform_tool_result("clarify", {}, _answered("B?")) is None
    assert store.control("clar-b") is None
    assert store.control("clar-a").state == "pending"  # A's answer routing survives
    # Several rows in one turn: the one whose question was answered is dropped.
    _row(store, "clar-c1", turn_mid="mid-c", question="C1?")
    _row(store, "clar-c2", turn_mid="mid-c", question="C2?")
    session_env(platform="linear", key=SK, message_id="mid-c")
    assert hooks.on_transform_tool_result("clarify", {}, _answered("C1?")) is None
    assert store.control("clar-c1") is None and store.control("clar-c2") is not None


async def test_transform_refuses_rows_of_another_turn(env, session_env):
    a, client, gw, store, order = env
    _row(store, "clar-1", turn_mid="other-turn")
    _row(store, "req-1", kind="approval", command="rm -rf build", turn_mid="other-turn")
    session_env(platform="linear", key=SK, message_id=MID)
    assert hooks.on_transform_tool_result("clarify", {}, TIMED_OUT) is None
    assert hooks.on_transform_tool_result(
        "terminal", {"command": "rm -rf build"}, APPROVAL_TIMEOUT) is None
    assert hooks.on_transform_tool_result("clarify", {}, _answered()) is None
    assert store.control("clar-1") is not None and store.control("req-1") is not None


# ----------------------------------- T6: re-post after the final response (spec §5)


def _session_acts(client, sid=D_SID):
    return [(c["type"], c.get("body"), signal, sm)
            for s, c, _eph, signal, sm in client.activities if s == sid]


async def _deferred_clarify(a, client, gw, session_env):
    mid = await _delegated(a, gw, session_env)
    await _clarify(a, client)
    original = _session_acts(client)[-1]
    await a.retire_clarify_card("clar-1", "expired")
    await settle(a)
    assert a._store.control("clar-1").state == "pending"
    return mid, original


async def test_final_response_then_repost(env, session_env):
    a, client, gw, store, order = env
    _, original = await _deferred_clarify(a, client, gw, session_env)
    r = await a.send(D_CHAT, "Waiting for your answer.", metadata=META)
    await settle(a)
    assert r.success
    acts = _session_acts(client)
    assert acts[-2][:2] == ("response", "Waiting for your answer.")
    assert acts[-1] == original
    assert original[2] == "select" and original[3] == {
        "options": [{"label": "staging", "value": "staging"},
                    {"label": "prod", "value": "prod"}]}
    row = store.control("clar-1")
    assert row is not None and row.state == "pending"  # same row, state kept


async def test_repost_after_attachment_more_response(env, session_env):
    a, client, gw, store, order = env
    _, original = await _deferred_clarify(a, client, gw, session_env)
    await a.send(D_CHAT, "final", metadata={**META, "notify": True})
    # Rulings 33/35: an attachment of the final (notify, no reply_to) is a ``more`` response.
    r = await a.send(D_CHAT, "attachment", metadata={**META, "notify": True})
    await settle(a)
    assert r.success
    acts = _session_acts(client)
    assert [x[:2] for x in acts[-4:-1]] == [
        ("response", "final"), original[:2], ("response", "attachment")]
    assert acts[-3] == original and acts[-1] == original  # the question is the last activity
    assert sum(1 for t, *_ in acts if t == "elicitation") == 3  # original + one per response
    assert len(store.controls_for_issue(D_ISSUE)) == 1  # no new row
    assert store.control("clar-1").state == "pending"


async def test_no_repost_without_pending(env, session_env):
    a, client, gw, store, order = env
    await _delegated(a, gw, session_env)
    await a.send(D_CHAT, "done", metadata=META)
    await a.send(D_CHAT, "attachment", metadata={**META, "notify": True})  # a ``more``
    await settle(a)
    acts = _session_acts(client)
    assert [x[:2] for x in acts[-2:]] == [("response", "done"), ("response", "attachment")]
    assert not any(t == "elicitation" for t, *_ in acts)


async def test_no_repost_for_live_row(env, session_env):
    a, client, gw, store, order = env
    await _delegated(a, gw, session_env)
    await _clarify(a, client)
    await a.send(D_CHAT, "done", metadata=META)
    await settle(a)
    acts = _session_acts(client)
    assert acts[-1][:2] == ("response", "done")
    assert sum(1 for t, *_ in acts if t == "elicitation") == 1


async def test_repost_after_promoted_thought(env, session_env):
    a, client, gw, store, order = env
    mid, original = await _deferred_clarify(a, client, gw, session_env)
    # Commentary while the question is pending: not posted (it would hide the select), but
    # kept as the turn-end fallback.
    await a.send(D_CHAT, "the answer", metadata={**META, "reply_to_message_id": mid,
                                                "_interim_send": True})
    await complete(a, gw.events[0])  # SUCCESS; the final send was deduplicated
    await settle(a)
    acts = _session_acts(client)
    assert acts[-2][:2] == ("response", "the answer")
    assert acts[-1] == original


async def test_repost_survives_memory_loss(env, session_env):
    a, client, gw, store, order = env
    _, original = await _deferred_clarify(a, client, gw, session_env)
    a.outbound.elicited.clear()
    await a.send(D_CHAT, "done", metadata=META)
    await settle(a)
    acts = _session_acts(client)
    assert acts[-2][:2] == ("response", "done") and acts[-1] == original


async def test_repost_pending_approval(env, session_env):
    a, client, gw, store, order = env
    await _delegated(a, gw, session_env)
    await a._send_exec_approval_prompt(_approval())
    original = _session_acts(client)[-1]
    await a.edit_message(D_CHAT, "approval:req-1", "timed out")
    await settle(a)
    await a.send(D_CHAT, "done", metadata=META)
    await settle(a)
    acts = _session_acts(client)
    assert acts[-2][:2] == ("response", "done") and acts[-1] == original
    assert original[2] == "select" and original[3]["options"]


async def test_repost_clarify_without_choices(env, session_env):
    a, client, gw, store, order = env
    await _delegated(a, gw, session_env)
    await a.send_clarify(D_CHAT, "Free text?", None, "clar-9", "sk", metadata=META)
    original = _session_acts(client)[-1]
    assert original[2] is None
    await a.retire_clarify_card("clar-9", "expired")
    await settle(a)
    await a.send(D_CHAT, "done", metadata=META)
    await settle(a)
    acts = _session_acts(client)
    assert acts[-2][:2] == ("response", "done") and acts[-1] == original


# ----------------------------------- T7: the answer starts the next turn (spec §6)

RM = "rm -rf build"


def _removed(client):
    return [(i, u["removedLabelIds"]) for i, u in client.issue_updates if "removedLabelIds" in u]


async def _pending_clarify(a, client, gw, session_env, question="Workflow framework?",
                           choices=("superpowers", "plain")):
    await _delegated(a, gw, session_env)
    await a.send_clarify(D_CHAT, question, list(choices), "clar-1", "sk", metadata=META)
    await a.retire_clarify_card("clar-1", "expired")
    await settle(a)
    assert a._store.control("clar-1").state == "pending"
    await complete(a, gw.events[0])  # the asking turn ends
    await settle(a)
    return len(gw.events)


async def _pending_approval(a, client, gw, session_env, rid="req-1"):
    await _delegated(a, gw, session_env)
    prompt = dataclasses.replace(_approval(rid), command=RM, text=f"Run `{RM}`?")
    assert (await a._send_exec_approval_prompt(prompt)).success
    await a.edit_message(D_CHAT, f"approval:{rid}", "timed out")
    await settle(a)
    assert a._store.control(rid).state == "pending"
    await complete(a, gw.events[0])
    await settle(a)
    return len(gw.events)


def _grants(store):
    return [tuple(r) for r in store.conn.execute(
        "SELECT session_key, command, choice, actor_id, turn_mid FROM approval_grants")]


async def test_click_answers_pending_clarify(env, session_env):
    a, client, gw, store, order = env
    n = await _pending_clarify(a, client, gw, session_env)
    await deliver(a, prompted(D_SID, D_ISSUE, "superpowers", "act-ans"))
    await settle(a)
    assert len(gw.events) == n + 1
    ev = gw.events[-1]
    assert ev.text.endswith('Answer to "Workflow framework?": superpowers')
    assert ev.text.startswith("[Linear ")
    assert ev.message_id == message_id(D_SID, "act-ans")
    assert ev.source.chat_id == D_CHAT
    assert store.control("clar-1") is None
    assert _removed(client) == [(D_ISSUE, [BLOCKED])]


async def test_prose_answers_pending_clarify(env, session_env):
    a, client, gw, store, order = env
    n = await _pending_clarify(a, client, gw, session_env)
    before = len(client.activities)
    await deliver(a, prompted(D_SID, D_ISSUE, "use the other repo please", "act-ans"))
    await settle(a)
    assert len(gw.events) == n + 1
    assert gw.events[-1].text.endswith(
        'Answer to "Workflow framework?": use the other repo please')
    assert store.control("clar-1") is None
    # never re-asked: no elicitation after the answer
    assert not any(c["type"] == "elicitation" for _, c, *_ in client.activities[before:])


async def test_live_answer_unchanged(env, session_env, monkeypatch):
    a, client, gw, store, order = env
    got = []
    monkeypatch.setattr(sessions_open_mod, "_resolve_clarify",
                        lambda rid, answer: got.append((rid, answer)) or True)
    await _delegated(a, gw, session_env)
    await _clarify(a, client)
    await deliver(a, prompted(D_SID, D_ISSUE, "staging", "act-ans"))
    assert got == [("clar-1", "staging")]
    assert len(gw.events) == 1  # no new turn
    assert store.control("clar-1") is None
    assert client.issue_updates == []


async def test_other_session_does_not_answer(env, session_env):
    """Review Focus 2: a pending row belongs to its own agent session."""
    a, client, gw, store, order = env
    n = await _pending_clarify(a, client, gw, session_env)
    await deliver(a, prompted("sess-B", D_ISSUE, "superpowers", "act-b", mention=True))
    await settle(a)
    assert len(gw.events) == n + 1
    ev = gw.events[-1]
    assert ev.text.endswith("\n\nsuperpowers") and "Answer to" not in ev.text
    assert ev.message_id == message_id("sess-B", "act-b")
    row = store.control("clar-1")
    assert row is not None and row.state == "pending" and row.agent_session_id == D_SID
    assert _removed(client) == []


async def test_pending_approval_approve(env, session_env, monkeypatch):
    a, client, gw, store, order = env
    monkeypatch.setattr(sessions_open_mod, "_approval_refusal", lambda *a, **k: None)
    monkeypatch.setattr(sessions_open_mod, "_resolve_approval_card",
                        lambda *a, **k: pytest.fail("a pending approval has no waiter"))
    n = await _pending_approval(a, client, gw, session_env)
    await deliver(a, prompted(D_SID, D_ISSUE, "Allow once", "act-ans"))
    await settle(a)
    assert len(gw.events) == n + 1
    ev = gw.events[-1]
    assert ev.text.endswith(f"Answer to approval for `{RM}`: approved (once)")
    assert _grants(store) == [("agent:main:linear:dm:x", RM, "once", HUMAN, ev.message_id)]
    assert ev.message_id == message_id(D_SID, "act-ans")
    assert store.control("req-1") is None
    assert _removed(client) == [(D_ISSUE, [BLOCKED])]


async def test_pending_approval_deny(env, session_env, monkeypatch):
    a, client, gw, store, order = env
    monkeypatch.setattr(sessions_open_mod, "_approval_refusal", lambda *a, **k: None)
    n = await _pending_approval(a, client, gw, session_env)
    await deliver(a, prompted(D_SID, D_ISSUE, "deny", "act-ans"))
    await settle(a)
    assert len(gw.events) == n + 1
    assert gw.events[-1].text.endswith(f"Answer to approval for `{RM}`: denied")
    assert _grants(store) == []
    assert store.control("req-1") is None
    assert _removed(client) == [(D_ISSUE, [BLOCKED])]


async def test_pending_approval_gibberish_reasks(env, session_env):
    a, client, gw, store, order = env
    n = await _pending_approval(a, client, gw, session_env)
    await deliver(a, prompted(D_SID, D_ISSUE, "yes please", "act-ans"))
    await settle(a)
    sid, content, eph, signal, meta = client.activities[-1]
    assert (sid, content, signal) == (
        D_SID, {"type": "elicitation", "body": sessions_open_mod.REASK_TEXT}, "select")
    assert [o["value"] for o in meta["options"]] == ["once", "session", "deny"]
    assert store.control("req-1").state == "pending"
    assert len(gw.events) == n and _grants(store) == [] and _removed(client) == []


async def test_pending_approval_refusal_keeps_row(env, session_env, monkeypatch):
    a, client, gw, store, order = env
    seen = []
    monkeypatch.setattr(sessions_open_mod, "_approval_refusal",
                        lambda sk, actor, **k: seen.append((sk, actor, k)) or "Alice")
    monkeypatch.setattr(sessions_open_mod, "_refusal_text", lambda who: f"only {who}")
    n = await _pending_approval(a, client, gw, session_env)
    await deliver(a, prompted(D_SID, D_ISSUE, "once", "act-ans"))
    await settle(a)
    assert seen == [("agent:main:linear:dm:x", HUMAN, {"request_id": "req-1"})]
    assert client.activities[-1][1] == {"type": "thought", "body": "only Alice"}
    assert store.control("req-1").state == "pending"
    assert len(gw.events) == n and _grants(store) == [] and _removed(client) == []


async def test_stop_while_pending_drops(env, session_env):
    a, client, gw, store, order = env
    n = await _pending_clarify(a, client, gw, session_env)
    await deliver(a, prompted(D_SID, D_ISSUE, "", "act-stop", signal="stop"))
    await settle(a)
    assert store.control("clar-1") is None
    assert _removed(client) == [(D_ISSUE, [BLOCKED])]
    acts = _session_acts(client)
    assert acts[-1][:2] == ("response", "Stopped.")  # no stale re-post after it
    assert len(gw.events) == n


async def test_undelegation_drops_pending(env, session_env):
    a, client, gw, store, order = env
    await _pending_clarify(a, client, gw, session_env)
    ev = json.loads(json.dumps(ISSUE_DELEGATED))
    ev["data"]["id"] = D_ISSUE
    ev["data"].pop("delegateId", None)
    ev["data"].pop("delegate", None)
    ev["updatedFrom"] = {"delegateId": APP}
    await deliver(a, ev)
    await settle(a)
    assert store.ticket(D_ISSUE).mode == "untouched"
    assert store.control("clar-1") is None
    assert _removed(client) == [(D_ISSUE, [BLOCKED])]


async def test_redelegation_drops_pending_but_not_live(env, session_env):
    a, client, gw, store, order = env
    await _pending_clarify(a, client, gw, session_env)
    store.put_control("live-1", "clarify", D_SID, 1, [], session_key="sk")
    gen = store.ticket(D_ISSUE).generation
    await a._execute(SetMode(D_ISSUE, "delegated", gen), {})
    assert store.control("clar-1") is not None  # same generation: nothing dropped
    await a._execute(SetMode(D_ISSUE, "delegated", gen + 1), {})
    await settle(a)
    assert store.control("clar-1") is None
    assert store.control("live-1") is not None  # live rows are Hermes's to cancel
    assert _removed(client) == [(D_ISSUE, [BLOCKED])]


# --- T8: one-shot grant on the re-run (spec §7, Review Focus 4) ----------------------------

def _rerun(rid="req-2", key="K", command=RM):
    return dataclasses.replace(_approval(rid), session_key=key, command=command,
                               text=f"Run `{command}`?")


def _stub_grant_path(monkeypatch, refusal=None, resolved=1):
    calls = []
    monkeypatch.setattr(sessions_open_mod, "_approval_refusal",
                        lambda sk, actor, **k: calls.append(("refusal", sk, actor, k)) or refusal)
    monkeypatch.setattr(sessions_open_mod, "_resolve_approval_card",
                        lambda *a, **k: calls.append(("resolve", a, k)) or resolved)
    return calls


def _elicitations(client, since=0):
    return [c for _, c, *_ in client.activities[since:] if c["type"] == "elicitation"]


async def test_grant_auto_approves_exact_rerun(env, session_env, monkeypatch):
    a, client, gw, store, order = env
    calls = _stub_grant_path(monkeypatch)
    await _delegated(a, gw, session_env)
    store.put_grant("K", RM, "once", "u1", "answer-mid")
    n = len(client.activities)
    res = await a._send_exec_approval_prompt(_rerun())
    assert res.success and res.message_id == "approval:req-2"
    assert calls == [("refusal", "K", "u1", {"request_id": "req-2"}),
                     ("resolve", ("K", "once", "req-2"), {"actor_id": "u1"})]
    assert len(client.activities) == n  # nothing posted
    assert store.control("req-2") is None
    assert _grants(store) == []


@pytest.mark.parametrize("key,command", [("K", RM + "2"), ("K2", RM), ("K", RM + " ")])
async def test_grant_ignores_other_command_and_key(env, session_env, monkeypatch, key, command):
    a, client, gw, store, order = env
    calls = _stub_grant_path(monkeypatch)
    await _delegated(a, gw, session_env)
    store.put_grant("K", RM, "once", "u1", "answer-mid")
    n = len(client.activities)
    assert (await a._send_exec_approval_prompt(_rerun(key=key, command=command))).success
    assert calls == []
    assert [e["body"] for e in _elicitations(client, n)] == [f"Run `{command}`?"]
    assert store.control("req-2").state == "live"
    assert _grants(store) == [("K", RM, "once", "u1", "answer-mid")]


async def test_grant_refused_falls_back(env, session_env, monkeypatch):
    a, client, gw, store, order = env
    calls = _stub_grant_path(monkeypatch, refusal="Alice")
    await _delegated(a, gw, session_env)
    store.put_grant("K", RM, "once", "u1", "answer-mid")
    n = len(client.activities)
    assert (await a._send_exec_approval_prompt(_rerun())).success
    assert [c[0] for c in calls] == ["refusal"]  # never resolved
    assert len(_elicitations(client, n)) == 1
    assert store.control("req-2").state == "live"
    assert _grants(store) == []  # one use, even when refused


async def test_grant_resolve_fails_falls_back(env, session_env, monkeypatch):
    """Task 1 concern 2: the resolve returns 0 (the actor is not this turn's requester)."""
    a, client, gw, store, order = env
    calls = _stub_grant_path(monkeypatch, resolved=0)
    await _delegated(a, gw, session_env)
    store.put_grant("K", RM, "once", "u1", "answer-mid")
    n = len(client.activities)
    assert (await a._send_exec_approval_prompt(_rerun())).success
    assert [c[0] for c in calls] == ["refusal", "resolve"]
    assert len(_elicitations(client, n)) == 1
    assert store.control("req-2").state == "live"
    assert _grants(store) == []


async def test_grant_kept_on_deny_only_card(env, session_env, monkeypatch):
    """Task 1 concern 1: the Deny-only mark stayed (truncated command), so "once" is not
    offered; the grant is not consumed and the normal (deny-only) select is posted."""
    a, client, gw, store, order = env
    calls = _stub_grant_path(monkeypatch)
    await _delegated(a, gw, session_env)
    store.put_grant("K", RM, "once", "u1", "answer-mid")
    n = len(client.activities)
    prompt = dataclasses.replace(_rerun(), actions=[("Deny", "deny", "danger")])
    assert (await a._send_exec_approval_prompt(prompt)).success
    assert calls == []
    assert len(_elicitations(client, n)) == 1
    assert client.activities[-1][4]["options"][0]["value"] == "deny"
    assert _grants(store) == [("K", RM, "once", "u1", "answer-mid")]


async def test_grant_dropped_at_turn_end(env, session_env):
    a, client, gw, store, order = env
    mid = await _delegated(a, gw, session_env)
    store.put_grant("K", RM, "once", "u1", "another-turn")
    store.put_grant("K", "ls", "once", "u1", mid)
    await complete(a, gw.events[0])  # the turn `mid` ends
    await settle(a)
    assert store.take_grant("K", "ls") is None
    assert _grants(store) == [("K", RM, "once", "u1", "another-turn")]


# ----------------------------------------------- T9: restart sweep at connect (spec §8)


async def _before_restart(tmp_path, session_env, setup):
    """A first gateway process: delegation, then ``setup(a, client, gw, store)``; then it dies."""
    db = tmp_path / "linear.db"
    a, client, gw, _, _ = new_adapter()
    a._store = Store.open(db)
    assert await a.connect()
    await _delegated(a, gw, session_env)
    await setup(a, client, gw, a._store)
    await a.disconnect()  # closes the store; the Hermes waiters die with the process
    return db


async def _after_restart(db, *, new_process=True, is_reconnect=False):
    if new_process:
        adapter_mod._SWEPT_PROFILES.clear()
    a, client, gw, _, _ = new_adapter()
    a._store = Store.open(db)
    assert await a.connect(is_reconnect=is_reconnect)
    await settle(a)
    return a, client, gw, a._store


async def _live_clarify(a, client, gw, store):
    await _clarify(a, client)
    assert store.control("clar-1").state == "live"


async def test_connect_marks_live_rows_pending(tmp_path, session_env):
    db = await _before_restart(tmp_path, session_env, _live_clarify)
    a, client, gw, store = await _after_restart(db)
    try:
        row = store.control("clar-1")
        assert row.state == "pending" and row.ticket_id == D_ISSUE
        assert row.question == "Which env?"
        assert _added(client) == [(D_ISSUE, [BLOCKED])]  # one label write
        assert client.activities == []  # nothing posted
    finally:
        await a.disconnect()


async def test_restart_sweep_clears_grants(tmp_path, session_env):
    async def setup(a, client, gw, store):
        store.put_grant("K", RM, "once", "u1", "answer-mid")

    db = await _before_restart(tmp_path, session_env, setup)
    a, client, gw, store = await _after_restart(db)
    try:
        assert _grants(store) == []
    finally:
        await a.disconnect()


async def test_restart_drops_live_row_without_question(tmp_path, session_env, caplog):
    async def setup(a, client, gw, store):
        # A row written by pre-RET-94 code: no question text, so it cannot be framed.
        store.put_control("old-1", "clarify", D_SID, 1, ["a", "b"], session_key="sk")

    db = await _before_restart(tmp_path, session_env, setup)
    with caplog.at_level(logging.INFO, logger="hermes_linear.adapter"):
        a, client, gw, store = await _after_restart(db)
    try:
        assert store.control("old-1") is None
        assert client.issue_updates == [] and client.activities == []
        assert any("old-1" in r.getMessage() for r in caplog.records)
    finally:
        await a.disconnect()


async def test_reconnect_leaves_live_rows_live(tmp_path, session_env):
    """In the same process the Hermes waiters are still alive: a reconnect must not orphan them."""
    async def setup(a, client, gw, store):
        await _live_clarify(a, client, gw, store)
        store.put_grant("K", RM, "once", "u1", "answer-mid")

    db = await _before_restart(tmp_path, session_env, setup)
    a, client, gw, store = await _after_restart(db, new_process=False, is_reconnect=True)
    try:
        assert store.control("clar-1").state == "live"
        assert len(_grants(store)) == 1
        assert client.issue_updates == []
    finally:
        await a.disconnect()


async def test_first_connect_of_process_sweeps_even_as_reconnect(tmp_path, session_env):
    """The cold-boot connect failed before the store opened; the watcher's reconnect is the
    process's first real connect and must sweep (no waiter of this process can exist)."""
    db = await _before_restart(tmp_path, session_env, _live_clarify)
    a, client, gw, store = await _after_restart(db, is_reconnect=True)
    try:
        assert store.control("clar-1").state == "pending"
    finally:
        await a.disconnect()


async def test_restart_then_answer(tmp_path, session_env):
    """Review Focus 3: after the sweep the answer starts the framed turn, and a later
    response re-posts nothing (the row is gone)."""
    db = await _before_restart(tmp_path, session_env, _live_clarify)
    a, client, gw, store = await _after_restart(db)
    try:
        await deliver(a, prompted(D_SID, D_ISSUE, "staging", "act-ans"))
        await settle(a)
        assert len(gw.events) == 1
        ev = gw.events[0]
        assert ev.text.endswith('Answer to "Which env?": staging')
        assert ev.message_id == message_id(D_SID, "act-ans")
        assert store.control("clar-1") is None
        assert _removed(client) == [(D_ISSUE, [BLOCKED])]
        session_env(platform="linear", message_id=ev.message_id, chat_id=D_CHAT)
        before = len(client.activities)
        r = await a.send(D_CHAT, "Deploying to staging.", metadata=META)
        await complete(a, ev)
        await settle(a)
        assert r.success
        acts = _session_acts(client)[-1:]
        assert acts[0][:2] == ("response", "Deploying to staging.")
        assert not any(c["type"] == "elicitation" for _, c, *_ in client.activities[before:])
    finally:
        await a.disconnect()
