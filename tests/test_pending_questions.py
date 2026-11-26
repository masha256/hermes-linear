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
from hermes_linear.models import ResolveControl, SetMode, StartTurn
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


async def test_set_blocked_label_lookup_is_cached(env, monkeypatch):
    """The Blocked label comes from the name directory (RET-121): one fetch per TTL."""
    a, client, gw, store, order = env
    fetches = []
    real = client.team_labels

    async def counted(team_id):
        fetches.append(team_id)
        return await real(team_id)

    monkeypatch.setattr(client, "team_labels", counted)
    assert await questions.set_blocked(client, "I", True)
    assert await questions.set_blocked(client, "I", False)
    assert len(fetches) == 1


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
    """Final review 3: the replaced pending row is dropped, and its Blocked removed, only
    once the new elicitation posted; the new question adds Blocked again if it goes pending."""
    a, client, gw, store, order = env
    await _delegated(a, gw, session_env)
    await _clarify(a, client)
    await a.retire_clarify_card("clar-1", "expired")
    await settle(a)
    assert store.control("clar-1").state == "pending"
    await _clarify(a, client, "clar-2", "Second?")
    await settle(a)
    assert store.control("clar-1") is None
    rows = store.controls_for_issue(D_ISSUE)
    assert [(r.request_id, r.state) for r in rows] == [("clar-2", "live")]
    assert _removed(client) == [(D_ISSUE, [BLOCKED])]
    await a.retire_clarify_card("clar-2", "expired")
    await settle(a)
    assert _added(client) == [(D_ISSUE, [BLOCKED]), (D_ISSUE, [BLOCKED])]


async def test_new_question_answered_live_leaves_no_blocked(env, session_env, monkeypatch):
    a, client, gw, store, order = env
    monkeypatch.setattr(sessions_open_mod, "_resolve_clarify", lambda rid, answer: True)
    await _delegated(a, gw, session_env)
    await _clarify(a, client)
    await a.retire_clarify_card("clar-1", "expired")
    await settle(a)
    await _clarify(a, client, "clar-2", "Second?")
    await deliver(a, prompted(D_SID, D_ISSUE, "staging", "act-ans"))
    await settle(a)
    assert store.controls_for_issue(D_ISSUE) == []
    assert _removed(client) == [(D_ISSUE, [BLOCKED])]  # nothing leaves Blocked behind


async def test_new_question_post_fails_keeps_pending(env, session_env):
    a, client, gw, store, order = env
    await _delegated(a, gw, session_env)
    await _clarify(a, client)
    await a.retire_clarify_card("clar-1", "expired")
    await settle(a)
    client.fail_types = {"elicitation"}
    res = await a.send_clarify(D_CHAT, "Second?", ["a"], "clar-2", "sk", metadata=META)
    await settle(a)
    assert not res.success
    assert store.control("clar-2") is None
    assert store.control("clar-1").state == "pending"  # the old question still stands
    assert _removed(client) == []


async def test_new_approval_replaces_pending_after_post(env, session_env):
    a, client, gw, store, order = env
    await _delegated(a, gw, session_env)
    await _clarify(a, client)
    await a.retire_clarify_card("clar-1", "expired")
    await settle(a)
    assert (await a._send_exec_approval_prompt(_approval())).success
    await settle(a)
    assert store.control("clar-1") is None and store.control("req-1").state == "live"
    assert _removed(client) == [(D_ISSUE, [BLOCKED])]


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


def _repost(original, text=outbound_mod.REPOST_CLARIFY_TEXT):
    """RET-116: the re-post keeps the original's select (signal and options) but carries a
    one-line pointer: Linear keeps the original question in the thread as a comment."""
    return (original[0], text, original[2], original[3])


def _asked_once(client, original):
    """The full question body is in the thread once (the original), never re-sent."""
    return sum(1 for a in _session_acts(client) if a[1] == original[1]) == 1


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
    assert acts[-1] == _repost(original)
    assert _asked_once(client, original)
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
        ("response", "final"), _repost(original)[:2], ("response", "attachment")]
    # the question's select is the last activity; its full text stays posted once
    assert acts[-3] == _repost(original) and acts[-1] == _repost(original)
    assert _asked_once(client, original)
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
    assert acts[-1] == _repost(original)


async def test_repost_survives_memory_loss(env, session_env):
    a, client, gw, store, order = env
    _, original = await _deferred_clarify(a, client, gw, session_env)
    a.outbound.elicited.clear()
    await a.send(D_CHAT, "done", metadata=META)
    await settle(a)
    acts = _session_acts(client)
    assert acts[-2][:2] == ("response", "done") and acts[-1] == _repost(original)


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
    assert acts[-2][:2] == ("response", "done")
    assert acts[-1] == _repost(original, outbound_mod.REPOST_APPROVAL_TEXT)
    assert _asked_once(client, original)
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
    assert acts[-2][:2] == ("response", "done") and acts[-1] == _repost(original)


async def test_repost_store_error_still_returns_send_result(env, session_env, monkeypatch,
                                                            caplog):
    """Final review 5: a store error in the re-post never turns the queued response into a
    raised send (the base would then send the final again)."""
    a, client, gw, store, order = env
    await _deferred_clarify(a, client, gw, session_env)

    def boom(_sid):
        raise RuntimeError("database is locked: secret detail")
    monkeypatch.setattr(store, "control_for_session", boom)
    with caplog.at_level(logging.WARNING, logger="hermes_linear.outbound"):
        r = await a.send(D_CHAT, "Waiting for your answer.", metadata=META)
    await settle(a)
    assert r.success
    assert _session_acts(client)[-1][:2] == ("response", "Waiting for your answer.")
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("RuntimeError" in m for m in msgs)
    assert not any("secret detail" in m for m in msgs)


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


NARROWED = {"open": False, "allowed": [HUMAN],
            "refusal": "Human"}  # approvals.approvers narrowed: only the requester answers


def _authority(monkeypatch, authority=NARROWED):
    """Stub the asking-time capture (public ``approval_refusal`` probes, RET-180)."""
    seen = []
    monkeypatch.setattr(sessions_open_mod, "_approval_authority",
                        lambda sk, rid, requester_id=None: seen.append((sk, rid, requester_id))
                        or (None if authority is None else dict(authority)))
    return seen


async def test_pending_approval_approve(env, session_env, monkeypatch):
    a, client, gw, store, order = env
    seen = _authority(monkeypatch)
    monkeypatch.setattr(sessions_open_mod, "_approval_refusal",
                        lambda *a, **k: pytest.fail("pending answers use the stored authority"))
    monkeypatch.setattr(sessions_open_mod, "_resolve_approval_card",
                        lambda *a, **k: pytest.fail("a pending approval has no waiter"))
    n = await _pending_approval(a, client, gw, session_env)
    assert seen == [("agent:main:linear:dm:x", "req-1", HUMAN)]  # the asking turn's user
    assert store.control("req-1").authority == NARROWED
    await deliver(a, prompted(D_SID, D_ISSUE, "Allow once", "act-ans"))
    await settle(a)
    assert len(gw.events) == n + 1
    ev = gw.events[-1]
    assert ev.text.endswith(f"Answer to approval for `{RM}`: approved (once)")
    assert _grants(store) == [("agent:main:linear:dm:x", RM, "once", HUMAN, ev.message_id)]
    assert ev.message_id == message_id(D_SID, "act-ans")
    assert store.control("req-1") is None
    assert _removed(client) == [(D_ISSUE, [BLOCKED])]


async def test_pending_approval_non_approver_refused(env, session_env, monkeypatch):
    """Final review 1: Hermes no longer has the timed-out approval, so the check uses the
    authority captured when it was asked; another user's Approve is refused, row kept."""
    a, client, gw, store, order = env
    _authority(monkeypatch, {"open": False, "allowed": ["u-alice", "u-bob"],
                             "refusal": "Alice"})
    monkeypatch.setattr(sessions_open_mod, "_refusal_text", lambda who: f"only {who}")
    n = await _pending_approval(a, client, gw, session_env)
    await deliver(a, prompted(D_SID, D_ISSUE, "once", "act-ans"))
    await settle(a)
    assert client.activities[-1][1] == {"type": "thought", "body": "only Alice"}
    assert store.control("req-1").state == "pending"
    assert len(gw.events) == n and _grants(store) == [] and _removed(client) == []


async def test_pending_approval_listed_approver_accepted(env, session_env, monkeypatch):
    a, client, gw, store, order = env
    _authority(monkeypatch, {"open": False, "allowed": ["u-alice", HUMAN], "refusal": "Alice"})
    n = await _pending_approval(a, client, gw, session_env)
    await deliver(a, prompted(D_SID, D_ISSUE, "session", "act-ans"))
    await settle(a)
    assert len(gw.events) == n + 1
    assert [g[2:4] for g in _grants(store)] == [("session", HUMAN)]


async def test_pending_approval_without_authority_refused(env, session_env, monkeypatch):
    a, client, gw, store, order = env
    _authority(monkeypatch, None)  # not captured (e.g. Hermes had no entry): never approved
    got = []
    monkeypatch.setattr(sessions_open_mod, "_refusal_text", lambda who: got.append(who) or "no")
    n = await _pending_approval(a, client, gw, session_env)
    assert store.control("req-1").authority is None
    await deliver(a, prompted(D_SID, D_ISSUE, "once", "act-ans"))
    await settle(a)
    assert got == [sessions_open_mod.NO_REQUESTER_NAME]
    assert client.activities[-1][1] == {"type": "thought", "body": "no"}
    assert store.control("req-1").state == "pending"
    assert len(gw.events) == n and _grants(store) == [] and _removed(client) == []


@pytest.mark.parametrize("authority", [None, {"open": False, "allowed": ["u-alice"],
                                               "refusal": "Alice"}])
async def test_pending_approval_deny(env, session_env, monkeypatch, authority):
    """Deny needs no authority (ruling R7 revised): anyone allowed in the thread may deny."""
    a, client, gw, store, order = env
    _authority(monkeypatch, authority)
    monkeypatch.setattr(sessions_open_mod, "_may_answer",
                        lambda *a, **k: pytest.fail("deny is never checked"))
    n = await _pending_approval(a, client, gw, session_env)
    await deliver(a, prompted(D_SID, D_ISSUE, "deny", "act-ans"))
    await settle(a)
    assert len(gw.events) == n + 1
    assert gw.events[-1].text.endswith(f"Answer to approval for `{RM}`: denied")
    assert _grants(store) == []
    assert store.control("req-1") is None
    assert _removed(client) == [(D_ISSUE, [BLOCKED])]


def _refusals(monkeypatch, verdicts, approvers=None):
    """Stub the PUBLIC Hermes calls ``_approval_authority`` makes: ``approval_refusal`` answers
    from ``verdicts`` (actor -> refusal name or None) and the profile's ``approvals`` block."""
    import gateway.approval_actor as actor_mod
    import hermes_cli.config as config_mod
    calls = []

    def refusal(sk, actor, *, request_id=None, **kw):
        calls.append((sk, actor, request_id))
        return verdicts(actor) if callable(verdicts) else verdicts.get(actor, "Alice")
    monkeypatch.setattr(actor_mod, "approval_refusal", refusal)
    block = {} if approvers is None else {"approvers": approvers}
    monkeypatch.setattr(config_mod, "load_config_readonly", lambda: {"approvals": block})
    return calls


def test_approval_authority_open_by_default(monkeypatch):
    calls = _refusals(monkeypatch, lambda actor: None)  # approvers unset = ["*"]
    assert sessions_open_mod._approval_authority("K", "req-1", "u1") == {
        "open": True, "allowed": [], "refusal": None}
    assert calls == [("K", sessions_open_mod.PROBE_ACTOR, "req-1")]


def test_approval_authority_narrowed(monkeypatch):
    calls = _refusals(monkeypatch, {"u1": None, "u-bob": None, "u-gone": "Alice"},
                      approvers=["u-bob", "u-gone", "u1"])
    assert sessions_open_mod._approval_authority("K", "req-1", "u1") == {
        "open": False, "allowed": ["u1", "u-bob"], "refusal": "Alice"}
    assert [c[1] for c in calls] == [sessions_open_mod.PROBE_ACTOR, "u1", "u-bob", "u-gone"]
    assert all(c[2] == "req-1" for c in calls)


def test_approval_authority_no_requester(monkeypatch):
    _refusals(monkeypatch, {"u-bob": None}, approvers=["u-bob"])
    assert sessions_open_mod._approval_authority("K", "req-1", None) == {
        "open": False, "allowed": ["u-bob"], "refusal": "Alice"}


def test_approval_authority_open_but_config_narrowed_fails_closed(monkeypatch):
    """``approval_refusal`` also passes everyone when Hermes has no such entry: a narrowed
    config with an "open" probe is that case, so nothing is captured (late approvals refused)."""
    _refusals(monkeypatch, lambda actor: None, approvers=[])
    assert sessions_open_mod._approval_authority("K", "req-1", "u1") is None


def test_approval_authority_error_is_none(monkeypatch):
    import gateway.approval_actor as actor_mod

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(actor_mod, "approval_refusal", boom)
    assert sessions_open_mod._approval_authority("K", "req-1", "u1") is None


def test_may_answer_snapshot():
    closed = {"open": False, "allowed": ["u1"], "refusal": "Alice"}
    assert sessions_open_mod._may_answer(closed, "u1")
    assert not sessions_open_mod._may_answer(closed, "u2")
    assert not sessions_open_mod._may_answer(closed, "")
    assert sessions_open_mod._may_answer({"open": True, "allowed": [], "refusal": None}, "u2")


def test_may_answer_legacy_rows():
    """Rows stored before RET-180 hold Hermes's own entry (requester_id, approvers)."""
    assert sessions_open_mod._may_answer({"requester_id": "u1", "approvers": []}, "u1")
    assert not sessions_open_mod._may_answer({"requester_id": "u1", "approvers": []}, "u2")
    assert sessions_open_mod._may_answer({"requester_id": "u1", "approvers": ["u2"]}, "u2")
    assert sessions_open_mod._may_answer({"requester_id": "u1", "approvers": ["*"]}, "u2")
    assert not sessions_open_mod._may_answer({"requester_id": "", "approvers": []}, "")


def test_requester_of_both_shapes():
    f = sessions_open_mod._requester_of
    assert f({"open": False, "allowed": [], "refusal": "Alice"}) == "Alice"
    assert f({"requester_id": "u1", "requester_name": "Alice", "approvers": []}) == "Alice"
    assert f({"requester_id": "u1", "approvers": []}) == "u1"
    assert f({"requester_id": "", "approvers": []}) == sessions_open_mod.NO_REQUESTER_NAME
    assert f(None) == sessions_open_mod.NO_REQUESTER_NAME


def test_no_private_core_imports():
    """RET-180: the plugin imports only public Hermes names (catalog rule 9)."""
    import pathlib
    src = "".join(p.read_text() for p in
                  pathlib.Path(sessions_open_mod.__file__).parent.glob("*.py"))
    for name in ("list_gateway_approval_authority", "import may_answer", "_NO_REQUESTER"):
        assert name not in src, name


# --- final review 2: a grant lives only as long as its answer turn ------------------------


async def test_no_grant_without_session(env, session_env, monkeypatch):
    a, client, gw, store, order = env
    _authority(monkeypatch)
    n = await _pending_approval(a, client, gw, session_env)
    row = store.control("req-1")
    store.delete_session(D_SID)
    await a.opener.resolve_control(ResolveControl(
        D_SID, "req-1", "approval", HUMAN, "once", state="pending", activity_id="act-ans"))
    await settle(a)
    assert row is not None and len(gw.events) == n and _grants(store) == []


async def test_grant_dropped_when_answer_turn_fails_before_hand_off(env, session_env,
                                                                     monkeypatch):
    a, client, gw, store, order = env
    _authority(monkeypatch)
    n = await _pending_approval(a, client, gw, session_env)
    gw.raise_next = True  # the gateway refuses the answer turn
    await deliver(a, prompted(D_SID, D_ISSUE, "once", "act-ans"))
    await settle(a)
    assert len(gw.events) == n + 1
    assert _grants(store) == []


async def test_grant_dropped_when_queued_answer_turn_is_stopped(env, session_env,
                                                                 monkeypatch):
    a, client, gw, store, order = env
    _authority(monkeypatch)
    await _delegated(a, gw, session_env)
    prompt = dataclasses.replace(_approval(), command=RM, text=f"Run `{RM}`?")
    assert (await a._send_exec_approval_prompt(prompt)).success
    await a.edit_message(D_CHAT, "approval:req-1", "timed out")
    await settle(a)
    # the asking turn is still in flight: the answer turn queues behind it
    await deliver(a, prompted(D_SID, D_ISSUE, "once", "act-ans"))
    await settle(a)
    assert len(_grants(store)) == 1 and len(gw.events) == 1
    await deliver(a, prompted(D_SID, D_ISSUE, "", "act-stop", signal="stop"))
    await settle(a)
    assert _grants(store) == []


async def test_grant_dropped_when_queue_closes(env, session_env, monkeypatch):
    """A disconnect drops the queued answer turn; its grant goes with it (a reconnect in the
    same process does not sweep grants)."""
    a, client, gw, store, order = env
    _authority(monkeypatch)
    await _delegated(a, gw, session_env)
    prompt = dataclasses.replace(_approval(), command=RM, text=f"Run `{RM}`?")
    assert (await a._send_exec_approval_prompt(prompt)).success
    await a.edit_message(D_CHAT, "approval:req-1", "timed out")
    await settle(a)
    await deliver(a, prompted(D_SID, D_ISSUE, "once", "act-ans"))
    await settle(a)
    assert len(_grants(store)) == 1
    a.turns.close()
    assert _grants(store) == []


async def test_free_drops_grants_only_of_unstarted_turns(env, session_env):
    a, client, gw, store, order = env
    from hermes_linear.turns import Turn
    d = StartTurn(agent_session_id=D_SID, activity_id="x", issue_id=D_ISSUE, chat_id=D_CHAT,
                  thread_id=None, user_id=HUMAN, user_name="H", text="t", first_turn=False,
                  ack="a")
    for handed_off, kept in ((False, []), (True, [("K", RM, "once", "u1", "m1")])):
        turn = Turn(d, "m1", handed_off=handed_off)
        a.turns.inflight[(D_CHAT, None)] = turn
        store.put_grant("K", RM, "once", "u1", "m1")
        a.turns._free((D_CHAT, None), turn)
        assert _grants(store) == kept
        store.drop_grants_for_turn("m1")
    await settle(a)


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


# --- RET-128: plugin tool approvals (spec §2.4) -------------------------------------------

TOOL_RULE = "<linear_comment> (plugin approval rule)"
D1, D2 = "0123456789abcdef", "fedcba9876543210"


def _tool_prompt(rid="req-1", digest=D1, key="agent:main:linear:dm:x", text=None,
                 description=None):
    desc = description if description is not None else (
        f"Approve `linear_comment` outside this session's tickets?\n- ENG-9 \"Other\"\n"
        f"Arguments:\n{{\"body\": \"hi\"}}\ndigest: {digest}")
    # Hermes may cut ``text`` (``_ea_fit``): the digest must come from ``description``.
    return dataclasses.replace(_approval(rid), session_key=key, command=TOOL_RULE,
                               description=desc, text=text or "Approve `linear_comment`…")


async def _pending_tool_approval(a, gw, session_env, **kw):
    await _delegated(a, gw, session_env)
    assert (await a._send_exec_approval_prompt(_tool_prompt(**kw))).success
    await a.edit_message(D_CHAT, "approval:req-1", "timed out")
    await settle(a)
    assert a._store.control("req-1").state == "pending"
    await complete(a, gw.events[0])
    await settle(a)
    return len(gw.events)


async def test_tool_approval_offers_once_and_deny(env, session_env):
    a, client, gw, store, order = env
    await _delegated(a, gw, session_env)
    assert (await a._send_exec_approval_prompt(_tool_prompt())).success
    assert [o["value"] for o in client.activities[-1][4]["options"]] == ["once", "deny"]
    assert list(store.control("req-1").choices) == ["once", "deny"]


async def test_tool_approval_deny_only_stays_deny_only(env, session_env):
    a, client, gw, store, order = env
    await _delegated(a, gw, session_env)
    prompt = dataclasses.replace(_tool_prompt(), actions=[("Deny", "deny", "danger")])
    assert (await a._send_exec_approval_prompt(prompt)).success
    assert [o["value"] for o in client.activities[-1][4]["options"]] == ["deny"]


async def test_tool_approval_row_command_has_digest(env, session_env):
    a, client, gw, store, order = env
    await _delegated(a, gw, session_env)
    assert (await a._send_exec_approval_prompt(_tool_prompt(text="cut…"))).success
    assert store.control("req-1").command == f"{TOOL_RULE}#{D1}"


async def test_tool_approval_answer_text_and_grant(env, session_env, monkeypatch):
    a, client, gw, store, order = env
    _authority(monkeypatch)
    n = await _pending_tool_approval(a, gw, session_env)
    await deliver(a, prompted(D_SID, D_ISSUE, "Allow once", "act-ans"))
    await settle(a)
    assert len(gw.events) == n + 1
    ev = gw.events[-1]
    assert ev.text.endswith(
        f"Answer to approval for `linear_comment` call {D1}: approved (once). "
        "Re-run the same call with identical arguments.")
    assert store.take_grant("agent:main:linear:dm:x", f"{TOOL_RULE}#{D1}") == ("once", HUMAN)


async def test_tool_approval_deny_text(env, session_env, monkeypatch):
    a, client, gw, store, order = env
    _authority(monkeypatch)
    n = await _pending_tool_approval(a, gw, session_env)
    await deliver(a, prompted(D_SID, D_ISSUE, "deny", "act-ans"))
    await settle(a)
    assert len(gw.events) == n + 1
    assert gw.events[-1].text.endswith(
        f"Answer to approval for `linear_comment` call {D1}: denied")
    assert _grants(store) == []


async def test_tool_grant_matches_only_same_digest(env, session_env, monkeypatch):
    a, client, gw, store, order = env
    calls = _stub_grant_path(monkeypatch)
    await _delegated(a, gw, session_env)
    store.put_grant("K", f"{TOOL_RULE}#{D1}", "once", "u1", "answer-mid")
    n = len(client.activities)
    assert (await a._send_exec_approval_prompt(_tool_prompt("req-2", D2, key="K"))).success
    assert calls == []
    assert len(_elicitations(client, n)) == 1
    assert _grants(store) == [("K", f"{TOOL_RULE}#{D1}", "once", "u1", "answer-mid")]
    n = len(client.activities)
    res = await a._send_exec_approval_prompt(_tool_prompt("req-3", D1, key="K"))
    assert res.success and res.message_id == "approval:req-3"
    assert calls == [("refusal", "K", "u1", {"request_id": "req-3"}),
                     ("resolve", ("K", "once", "req-3"), {"actor_id": "u1"})]
    assert len(client.activities) == n and store.control("req-3") is None
    assert _grants(store) == []


async def test_tool_approval_live_answer_unchanged(env, session_env, monkeypatch):
    """Answered inside Hermes's wait: the live path resolves the card; the digest-bound row
    command plays no part, and no turn or grant follows."""
    a, client, gw, store, order = env
    calls = _stub_grant_path(monkeypatch)
    await _delegated(a, gw, session_env)
    assert (await a._send_exec_approval_prompt(_tool_prompt())).success
    await deliver(a, prompted(D_SID, D_ISSUE, "Allow once", "act-ans"))
    await settle(a)
    assert calls == [("refusal", "agent:main:linear:dm:x", HUMAN, {"request_id": "req-1"}),
                     ("resolve", ("agent:main:linear:dm:x", "once", "req-1"),
                      {"actor_id": HUMAN})]
    assert store.control("req-1") is None and len(gw.events) == 1 and _grants(store) == []


async def test_tool_approval_without_digest_is_plain(env, session_env, monkeypatch):
    a, client, gw, store, order = env
    _authority(monkeypatch)
    n = await _pending_tool_approval(a, gw, session_env, description="no digest here")
    row = store.control("req-1")
    assert row.command == TOOL_RULE and list(row.choices) == ["once", "session", "deny"]
    await deliver(a, prompted(D_SID, D_ISSUE, "Allow once", "act-ans"))
    await settle(a)
    assert len(gw.events) == n + 1
    assert gw.events[-1].text.endswith(
        f"Answer to approval for `{TOOL_RULE}`: approved (once)")
    assert [g[1] for g in _grants(store)] == [TOOL_RULE]


async def test_ungated_grant_command_answers_in_shell_form(env, session_env, monkeypatch):
    """A ``command#digest`` row for a tool outside GATED_TOOLS keeps the shell form."""
    a, client, gw, store, order = env
    _authority(monkeypatch)
    n = await _pending_approval(a, client, gw, session_env)
    other = f"<other_tool> (plugin approval rule)#{D1}"
    store.conn.execute("UPDATE pending_controls SET command = ? WHERE request_id = ?", (other, "req-1"))
    await deliver(a, prompted(D_SID, D_ISSUE, "deny", "act-ans"))
    await settle(a)
    assert len(gw.events) == n + 1
    assert gw.events[-1].text.endswith(f"Answer to approval for `{other}`: denied")


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


async def test_restart_sweep_drops_requests(tmp_path, session_env):
    async def setup(a, client, gw, store):
        store.put_request("K", "d1", "linear_comment", ["u-1"], "msg")
        store.arm_request("K", "d1")

    db = await _before_restart(tmp_path, session_env, setup)
    a, client, gw, store = await _after_restart(db)
    try:
        assert store.request("K", "d1") is None
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
