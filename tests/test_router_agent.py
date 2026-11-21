"""Router tests: agent session events and ticket lifecycle (spec §6.1–6.4)."""

from __future__ import annotations

import copy

import pytest

from conftest import load_fixture
from hermes_linear.access import REFUSAL_TEXT
from hermes_linear.constants import DELEGATION_WAIT_S
from hermes_linear.models import (
    AwaitDelegation,
    Ignore,
    Interrupt,
    Refuse,
    ResolveControl,
    SetMode,
    StartTurn,
)
from hermes_linear.router import (
    RouterContext,
    message_id,
    route,
    ticket_chat_id,
    turn_header,
)
from hermes_linear.store import Store

APP = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000099"

MENTION = load_fixture("agent_created_mention")
DELEGATION = load_fixture("agent_created_delegation")
PROMPTED = load_fixture("agent_prompted_reply")
ISSUE_DELEGATED = load_fixture("issue_update_delegated")

M_ISSUE = MENTION["agentSession"]["issueId"]
M_SID = MENTION["agentSession"]["id"]
M_ROOT = MENTION["agentSession"]["commentId"]
D_ISSUE = DELEGATION["agentSession"]["issueId"]
D_SID = DELEGATION["agentSession"]["id"]
D_ROOT = DELEGATION["agentSession"]["commentId"]


async def _yes(_uid):
    return True


async def _no(_uid):
    return False


@pytest.fixture
def store():
    s = Store.open(":memory:")
    yield s
    s.close()


@pytest.fixture
def ctx(store):
    return RouterContext(store=store, app_user_id=APP, allowed=_yes)


def _turns(decisions):
    return [d for d in decisions if isinstance(d, StartTurn)]


# ------------------------------------------------------------------ helpers


def test_key_helpers():
    assert ticket_chat_id("I", 3) == "I:3"
    assert turn_header("RET-1", "C1", "Ann") == "[Linear RET-1 · thread C1 · Ann]"
    assert message_id("S", "A") == "S:A"


# ------------------------------------------------------------------ created


async def test_mention_untouched_starts_thread_session(ctx, store):
    out = await route(copy.deepcopy(MENTION), ctx)
    assert len(out) == 2
    mode, turn = out
    assert mode == SetMode(issue_id=M_ISSUE, mode="mention", generation=0)
    assert isinstance(turn, StartTurn)
    assert turn.chat_id == M_ISSUE
    assert turn.thread_id == M_SID
    assert turn.first_turn is True
    assert turn.agent_session_id == M_SID
    assert turn.activity_id == MENTION["agentActivity"]["id"]
    assert turn.issue_id == M_ISSUE
    assert turn.user_id == MENTION["agentSession"]["creatorId"]
    assert turn.user_name == "Test User"
    assert turn.ack == "On it"
    assert turn.text.startswith(turn_header("RET-61", M_ROOT, "Test User"))
    assert MENTION["promptContext"] in turn.text
    # session recorded, ticket placeholder filled from the payload
    row = store.session(M_SID)
    assert row.kind == "mention" and row.root_comment_id == M_ROOT
    t = store.ticket(M_ISSUE)
    assert t.identifier == "RET-61"
    assert t.team_id == MENTION["agentSession"]["issue"]["teamId"]


async def test_fresh_delegation_starts_ticket_session(ctx, store):
    out = await route(copy.deepcopy(DELEGATION), ctx)
    assert out[0] == SetMode(issue_id=D_ISSUE, mode="delegated", generation=1)
    (turn,) = _turns(out)
    assert turn.chat_id == f"{D_ISSUE}:1"
    assert turn.thread_id is None
    assert turn.first_turn is True
    assert turn.text.startswith(turn_header("RET-62", D_ROOT, "Test User"))
    assert DELEGATION["promptContext"] in turn.text
    assert store.session(D_SID).kind == "delegation"


async def test_delegation_created_after_issue_update_keeps_generation(ctx, store):
    store.set_mode(D_ISSUE, "delegated", 4)
    out = await route(copy.deepcopy(DELEGATION), ctx)
    assert out[0] == SetMode(issue_id=D_ISSUE, mode="delegated", generation=4)
    assert _turns(out)[0].chat_id == f"{D_ISSUE}:4"


async def test_mention_on_delegated_joins_ticket(ctx, store):
    store.set_mode(M_ISSUE, "delegated", 2)
    out = await route(copy.deepcopy(MENTION), ctx)
    assert not any(isinstance(d, SetMode) for d in out)
    (turn,) = _turns(out)
    assert turn.chat_id == f"{M_ISSUE}:2"
    assert turn.thread_id is None
    assert turn.first_turn is False
    assert "<primary-directive-thread" in turn.text
    assert "</primary-directive-thread>" in turn.text
    assert "<user-request>" in turn.text
    assert "<issue" not in turn.text
    assert turn.text.startswith(turn_header("RET-61", M_ROOT, "Test User"))


async def test_plugin_created_session_created_ignored(ctx, store):
    store.record_session(D_SID, D_ISSUE, D_ROOT, "plugin", plugin_created=True)
    assert await route(copy.deepcopy(DELEGATION), ctx) == [Ignore("plugin-created")]


async def test_plugin_created_matched_by_root_comment(ctx, store):
    # adapter recorded its own session under a provisional id before the webhook
    store.record_session("provisional", M_ISSUE, M_ROOT, "comment", plugin_created=True)
    assert await route(copy.deepcopy(MENTION), ctx) == [Ignore("plugin-created")]


async def test_denied_actor_refused(store):
    ctx = RouterContext(store=store, app_user_id=APP, allowed=_no)
    assert await route(copy.deepcopy(MENTION), ctx) == [Refuse(M_SID, REFUSAL_TEXT)]
    assert await route(copy.deepcopy(PROMPTED), ctx) == [Refuse(M_SID, REFUSAL_TEXT)]


async def test_allowed_receives_actor_id(store):
    seen = []

    async def allowed(uid):
        seen.append(uid)
        return True

    ctx = RouterContext(store=store, app_user_id=APP, allowed=allowed)
    await route(copy.deepcopy(MENTION), ctx)
    await route(copy.deepcopy(PROMPTED), ctx)
    assert seen == [MENTION["agentSession"]["creatorId"], PROMPTED["agentActivity"]["userId"]]


async def test_missing_actor_refused(ctx):
    ev = copy.deepcopy(PROMPTED)
    ev["agentActivity"].pop("userId")
    ev["agentActivity"].pop("user")
    assert await route(ev, ctx) == [Refuse(M_SID, REFUSAL_TEXT)]


# ----------------------------------------------------------------- prompted


async def test_prompted_routes_to_mapped_session(ctx, store):
    await route(copy.deepcopy(MENTION), ctx)
    out = await route(copy.deepcopy(PROMPTED), ctx)
    (turn,) = out
    assert isinstance(turn, StartTurn)
    assert turn.chat_id == M_ISSUE and turn.thread_id == M_SID
    assert turn.first_turn is False
    assert turn.activity_id == PROMPTED["agentActivity"]["id"]
    assert turn.text == (
        turn_header("RET-61", M_ROOT, "Test User") + "\n\n" + "second reply in thread"
    )


async def test_prompted_on_delegation_session_uses_ticket_key(ctx, store):
    store.record_session(M_SID, M_ISSUE, M_ROOT, "delegation")
    store.set_mode(M_ISSUE, "delegated", 3)
    (turn,) = await route(copy.deepcopy(PROMPTED), ctx)
    assert turn.chat_id == f"{M_ISSUE}:3" and turn.thread_id is None
    assert store.session(M_SID).kind == "delegation"  # prompted must not re-kind


async def test_prompted_stop_signal_interrupts(ctx, store):
    await route(copy.deepcopy(MENTION), ctx)
    ev = copy.deepcopy(PROMPTED)
    ev["agentActivity"]["signal"] = "stop"
    assert await route(ev, ctx) == [Interrupt(M_SID)]


async def test_prompted_with_pending_control_resolves(ctx, store):
    await route(copy.deepcopy(MENTION), ctx)
    store.put_control("req-1", "clarify", M_SID, 1, ["a", "b"])
    out = await route(copy.deepcopy(PROMPTED), ctx)
    assert out == [
        ResolveControl(
            agent_session_id=M_SID,
            request_id="req-1",
            kind="clarify",
            actor_id=PROMPTED["agentActivity"]["userId"],
            answer="second reply in thread",
        )
    ]


async def test_chat_id_stable_across_store_reopen(tmp_path):
    path = tmp_path / "state.db"
    s1 = Store.open(path)
    s1.record_session(M_SID, M_ISSUE, M_ROOT, "delegation")
    s1.set_mode(M_ISSUE, "delegated", 2)
    (t1,) = await route(copy.deepcopy(PROMPTED), RouterContext(s1, APP, _yes))
    s1.close()
    s2 = Store.open(path)
    (t2,) = await route(copy.deepcopy(PROMPTED), RouterContext(s2, APP, _yes))
    s2.close()
    assert (t1.chat_id, t1.thread_id) == (t2.chat_id, t2.thread_id) == (f"{M_ISSUE}:2", None)


async def test_chat_id_stable_across_store_reopen_mention(tmp_path):
    path = tmp_path / "state.db"
    s1 = Store.open(path)
    (_, t1) = await route(copy.deepcopy(MENTION), RouterContext(s1, APP, _yes))
    s1.close()
    s2 = Store.open(path)
    (t2,) = await route(copy.deepcopy(PROMPTED), RouterContext(s2, APP, _yes))
    s2.close()
    assert (t1.chat_id, t1.thread_id) == (t2.chat_id, t2.thread_id) == (M_ISSUE, M_SID)


# -------------------------------------------------------------- Issue update


def _delegate_event(old, new, state_type="unstarted"):
    ev = copy.deepcopy(ISSUE_DELEGATED)
    ev["updatedFrom"]["delegateId"] = old
    if new is None:
        ev["data"].pop("delegateId", None)
        ev["data"].pop("delegate", None)
    else:
        ev["data"]["delegateId"] = new
    ev["data"]["state"]["type"] = state_type
    return ev


ISSUE = ISSUE_DELEGATED["data"]["id"]


async def test_delegate_change_without_created_awaits(ctx, store):
    out = await route(copy.deepcopy(ISSUE_DELEGATED), ctx)
    assert out == [
        SetMode(issue_id=ISSUE, mode="delegated", generation=1),
        AwaitDelegation(issue_id=ISSUE, deadline_s=DELEGATION_WAIT_S),
    ]
    assert DELEGATION_WAIT_S == 5
    # ticket row and name caches filled from the payload
    t = store.ticket(ISSUE)
    assert t.project_id == ISSUE_DELEGATED["data"]["projectId"]
    assert t.state_type == "unstarted"
    assert store.state_name(ISSUE_DELEGATED["data"]["stateId"]) == "Todo"


async def test_delegate_change_when_already_delegated_ignored(ctx, store):
    # the `created` arrived first and its SetMode was applied
    store.set_mode(ISSUE, "delegated", 1)
    out = await route(copy.deepcopy(ISSUE_DELEGATED), ctx)
    assert not any(isinstance(d, (SetMode, AwaitDelegation)) for d in out)


async def test_undelegate_interrupts_and_untouches(ctx, store):
    store.set_mode(ISSUE, "delegated", 2)
    store.record_session("s1", ISSUE, "c1", "delegation")
    store.record_session("s2", ISSUE, "c2", "mention")
    store.record_session("elsewhere", "other-issue", "c3", "mention")
    out = await route(_delegate_event(APP, None), ctx)
    assert out[-1] == SetMode(issue_id=ISSUE, mode="untouched", generation=2)
    assert sorted(d.agent_session_id for d in out[:-1]) == ["s1", "s2"]
    assert all(isinstance(d, Interrupt) for d in out[:-1])


async def test_undelegate_to_other_user(ctx, store):
    store.set_mode(ISSUE, "delegated", 1)
    out = await route(_delegate_event(APP, OTHER), ctx)
    assert out == [SetMode(issue_id=ISSUE, mode="untouched", generation=1)]


async def test_redelegate_done_bumps_generation(ctx, store):
    store.set_mode(ISSUE, "delegated", 2)
    store.set_mode(ISSUE, "untouched")
    out = await route(_delegate_event(None, APP, "completed"), ctx)
    assert out[0] == SetMode(issue_id=ISSUE, mode="delegated", generation=3)
    assert isinstance(out[1], AwaitDelegation)


async def test_redelegate_canceled_bumps_generation(ctx, store):
    store.set_mode(ISSUE, "delegated", 2)
    store.set_mode(ISSUE, "untouched")
    out = await route(_delegate_event(OTHER, APP, "canceled"), ctx)
    assert out[0].generation == 3


async def test_redelegate_open_keeps_generation(ctx, store):
    store.set_mode(ISSUE, "delegated", 2)
    store.set_mode(ISSUE, "untouched")
    out = await route(_delegate_event(None, APP, "started"), ctx)
    assert out[0] == SetMode(issue_id=ISSUE, mode="delegated", generation=2)


async def test_issue_update_without_delegate_change_is_empty(ctx, store):
    ev = copy.deepcopy(ISSUE_DELEGATED)
    ev["updatedFrom"].pop("delegateId")
    # Untouched ticket: the remaining (assignee) edit is not context (Task 8).
    assert await route(ev, ctx) == [Ignore("not delegated")]


async def test_issue_update_caches_labels(ctx, store):
    ev = copy.deepcopy(ISSUE_DELEGATED)
    ev["updatedFrom"].pop("delegateId")
    ev["data"]["labels"] = [{"id": "L1", "name": "bug", "color": "#f00"}]
    await route(ev, ctx)
    assert store.label_name("L1") == "bug"


async def test_unknown_type_ignored(ctx):
    assert await route({"type": "Reaction", "action": "create"}, ctx) == [
        Ignore("unhandled type")
    ]


# ------------------------------------------------- review fixes (round 1)


async def test_root_comment_match_records_real_session_id(ctx, store):
    store.record_session("provisional", M_ISSUE, M_ROOT, "comment", plugin_created=True)
    assert await route(copy.deepcopy(MENTION), ctx) == [Ignore("plugin-created")]
    row = store.session(M_SID)
    assert row is not None
    assert row.plugin_created
    assert (row.issue_id, row.root_comment_id, row.kind) == (M_ISSUE, M_ROOT, "comment")


def _no_await(out):
    return not any(isinstance(d, (SetMode, AwaitDelegation)) for d in out)


async def test_denied_delegator_no_await(store):
    seen = []

    async def allowed(uid):
        seen.append(uid)
        return False

    ctx = RouterContext(store=store, app_user_id=APP, allowed=allowed)
    out = await route(copy.deepcopy(ISSUE_DELEGATED), ctx)
    assert out == [Ignore("denied delegator")]
    assert seen == [ISSUE_DELEGATED["actor"]["id"]]
    assert store.ticket(ISSUE).mode != "delegated"


async def test_bot_delegator_no_await(ctx):
    ev = copy.deepcopy(ISSUE_DELEGATED)
    ev["botActor"] = {"id": "bot-1", "type": "workflow", "name": "Triage rule"}
    assert await route(ev, ctx) == [Ignore("denied delegator")]
    ev = copy.deepcopy(ISSUE_DELEGATED)
    ev["actor"]["type"] = "integration"
    assert await route(ev, ctx) == [Ignore("denied delegator")]


async def test_bot_created_issue_human_delegator_allowed(ctx):
    # Issue data.botActor is the issue's creator, not the delegator.
    ev = copy.deepcopy(ISSUE_DELEGATED)
    ev["data"]["botActor"] = {"id": "bot-1", "type": "workflow", "name": "Triage rule"}
    out = await route(ev, ctx)
    assert isinstance(out[0], SetMode) and out[0].mode == "delegated"


async def test_missing_actor_delegator_no_await(ctx):
    ev = copy.deepcopy(ISSUE_DELEGATED)
    ev.pop("actor")
    out = await route(ev, ctx)
    assert out == [Ignore("denied delegator")] and _no_await(out)


async def test_denied_undelegate_still_interrupts(store):
    ctx = RouterContext(store=store, app_user_id=APP, allowed=_no)
    store.set_mode(ISSUE, "delegated", 1)
    store.record_session("s1", ISSUE, "c1", "delegation")
    out = await route(_delegate_event(APP, None), ctx)
    assert out == [Interrupt("s1"), SetMode(issue_id=ISSUE, mode="untouched", generation=1)]
