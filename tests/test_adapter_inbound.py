"""LinearAdapter inbound path and turns (Task 11).

The gateway is faked: ``adapter.handle_message`` is replaced by a recorder and
tests signal turn completion by calling ``adapter.on_processing_complete``.
Linear is a Python fake client; the store is in-memory; the listener is a fake
except in the one real-bind connect test (port 0).
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import logging
import os
import tempfile
import time

# Keep any Hermes runtime-status writes away from a real profile.
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="hl-adapter-test-home-")

import aiohttp  # noqa: E402
import pytest  # noqa: E402

from conftest import load_fixture  # noqa: E402
from gateway.config import Platform, PlatformConfig  # noqa: E402
from gateway.platform_registry import PlatformEntry, platform_registry  # noqa: E402
from gateway.platforms.base import BasePlatformAdapter  # noqa: E402
from gateway.platforms.event import ProcessingOutcome  # noqa: E402
from gateway.session import build_session_key  # noqa: E402  (test-only, not public API)

platform_registry.register(PlatformEntry(
    name="linear", label="Linear", adapter_factory=lambda c: None, check_fn=lambda: True))

from hermes_linear import listener as listener_mod  # noqa: E402
from hermes_linear import sessions_open as sessions_open_mod  # noqa: E402
from hermes_linear.access import REFUSAL_TEXT  # noqa: E402
from hermes_linear.adapter import ADAPTERS, QUEUED_ACK, LinearAdapter  # noqa: E402
from hermes_linear.models import AwaitDelegation, ContextItem  # noqa: E402
from hermes_linear.settings import Settings  # noqa: E402
from hermes_linear.store import Store  # noqa: E402

APP = "00000000-0000-4000-8000-000000000001"
HUMAN = "00000000-0000-4000-8000-000000000006"

DELEGATION = load_fixture("agent_created_delegation")
MENTION = load_fixture("agent_created_mention")
PROMPTED = load_fixture("agent_prompted_reply")
ISSUE_DELEGATED = load_fixture("issue_update_delegated")
COMMENT_PLAIN = load_fixture("comment_create_toplevel_plain")

D_ISSUE = DELEGATION["agentSession"]["issueId"]
D_SID = DELEGATION["agentSession"]["id"]
D_ACT = DELEGATION["agentActivity"]["id"]
M_ISSUE = MENTION["agentSession"]["issueId"]
M_SID = MENTION["agentSession"]["id"]
TEAM = "00000000-0000-4000-8000-000000000007"


# ----------------------------------------------------------------- fakes


class FakeClient:
    def __init__(self, order: list):
        self.order = order
        self.activities: list[tuple] = []
        self.calls: list[tuple] = []
        self.fail_activity = False
        self.snapshot_fails = False
        self.state_type = "unstarted"
        self.app_sessions: list[dict] = []
        self.user_emails: dict[str, str] = {}
        self.closed = False
        self.activity_delay: dict[str, float] = {}  # agent session id -> seconds
        self.snapshot_delay = 0.0
        self.issues: dict[str, dict] = {}  # issue_get by id (task sub-ticket guard)

    async def issue_get(self, issue_id):
        self.calls.append(("issue_get", issue_id))
        if issue_id not in self.issues:
            raise RuntimeError("no such issue")
        return copy.deepcopy(self.issues[issue_id])

    async def token(self):
        return "tok"

    async def viewer(self):
        return {"id": APP, "name": "Hermes Test", "displayName": "hermestest"}

    async def viewer_id(self):
        return APP

    async def user(self, user_id):
        self.calls.append(("user", user_id))
        return self.user_emails.get(user_id, ""), "Someone"

    async def create_activity(self, sid, content, *, ephemeral=False, signal=None,
                              signal_metadata=None):
        if self.fail_activity:
            raise RuntimeError("linear down")
        delay = self.activity_delay.pop(sid, 0.0)
        if delay:
            await asyncio.sleep(delay)
        self.activities.append((sid, content, ephemeral, signal, signal_metadata))
        self.order.append(("activity", content["type"], content.get("body")))
        return f"act-{len(self.activities)}"

    async def create_session_on_issue(self, issue_id):
        self.calls.append(("create_session_on_issue", issue_id))
        return "sess-issue"

    async def app_sessions_on_issue(self, issue_id):
        self.calls.append(("app_sessions_on_issue", issue_id))
        return list(self.app_sessions)

    async def issue_snapshot(self, issue_id):
        self.calls.append(("issue_snapshot", issue_id))
        if self.snapshot_delay:
            await asyncio.sleep(self.snapshot_delay)
        if self.snapshot_fails:
            raise RuntimeError("snapshot down")
        return {
            "identifier": "RET-62", "title": "Second test ticket",
            "state": {"name": "Todo", "type": self.state_type},
            "labels": ["Feature", "Bug"], "assignee": "Test User",
            "project": {"name": "Valkyrie", "id": "p1"}, "team": {"id": TEAM},
            "description": "x" * 5000,
        }

    async def move_to_first_started(self, issue_id, team_id):
        self.calls.append(("move_to_first_started", issue_id, team_id))
        return True

    async def close(self):
        self.closed = True


class FakeListener:
    def __init__(self, start_error=None):
        self.start_error = start_error
        self.endpoints: dict = {}
        self.unregistered: list[str] = []

    def register(self, ep):
        self.endpoints[ep.profile] = ep

    def unregister(self, profile):
        self.endpoints.pop(profile, None)
        self.unregistered.append(profile)

    async def wait_idle(self):
        return None

    @property
    def running(self):
        return self.start_error is None and bool(self.endpoints)


def make_settings(**kw) -> Settings:
    base = dict(
        client_id="cid", client_secret="cs", webhook_secret="ws",
        allowed_users=frozenset(), allow_all_users=True, auto_start=True,
        session_url="", listener_host="127.0.0.1", listener_port=0,
    )
    base.update(kw)
    return Settings(**base)


class Gateway:
    """Fake gateway: records handed-off events; can be told to raise.

    Like the base ``handle_message``, it admits a turn (``on_processing_start``)
    unless ``admit`` is False (the base dropped the event); ``/stop`` takes the
    busy bypass path, which fires no processing hooks.
    """

    def __init__(self, order: list):
        self.order = order
        self.events: list = []
        self.raise_next = False
        self.admit = True
        self.adapter = None

    async def __call__(self, event):
        self.order.append(("handle", event.text))
        self.events.append(event)
        if self.raise_next:
            self.raise_next = False
            raise RuntimeError("gateway refused")
        if self.admit and event.text != "/stop" and self.adapter is not None:
            await self.adapter.on_processing_start(event)


def _new_adapter(settings=None, listener=None, order=None):
    order = order if order is not None else []
    client = FakeClient(order)
    store = Store.open(":memory:")
    listener = listener or FakeListener()
    a = LinearAdapter(PlatformConfig(enabled=True), settings=settings or make_settings(),
                      client=client, store=store, listener=listener)
    a._write_runtime_status_safe = lambda *args, **kw: None
    gw = Gateway(order)
    gw.adapter = a
    a.handle_message = gw
    a.delegation_wait_s = 0.05
    return a, client, gw, store, listener, order


@pytest.fixture
async def env():
    a, client, gw, store, listener, order = _new_adapter()
    assert await a.connect()
    yield a, client, gw, store, order
    await a.disconnect()


async def deliver(a, *events):
    for ev in events:
        a.deliver(copy.deepcopy(ev))
    await a.wait_idle()


async def complete(a, event, outcome=ProcessingOutcome.SUCCESS):
    # The base runs the hook inside the turn's own background task.
    await asyncio.create_task(a.on_processing_complete(event, outcome))
    await a.wait_idle()


def prompted(sid, issue, body, activity_id, *, mention=False, signal=None):
    ev = copy.deepcopy(PROMPTED)
    ev["agentSession"]["id"] = sid
    ev["agentSession"]["issueId"] = issue
    ev["agentSession"]["issue"]["id"] = issue
    if not mention:
        ev["agentSession"]["sourceMetadata"] = None
    ev["agentActivity"]["id"] = activity_id
    ev["agentActivity"]["agentSessionId"] = sid
    ev["agentActivity"]["content"]["body"] = body
    ev["agentActivity"]["signal"] = signal
    return ev


def thoughts(client):
    return [(sid, c["body"], eph) for sid, c, eph, *_ in client.activities if c["type"] == "thought"]


# ----------------------------------------------------------- class contract


def test_supports_message_editing_false():
    assert LinearAdapter.SUPPORTS_MESSAGE_EDITING is False


def test_delete_message_not_overridden():
    assert LinearAdapter.delete_message is BasePlatformAdapter.delete_message


def test_allow_all_env_prefix():
    assert LinearAdapter.ALLOW_ALL_ENV_PREFIX == "LINEAR"


# ------------------------------------------------------------------ turns


async def test_ack_posted_before_handle_message(env):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    kinds = [o[0] for o in order]
    assert kinds[:2] == ["activity", "handle"]
    assert order[0] == ("activity", "thought", "On it")
    sid, content, ephemeral, *_ = client.activities[0]
    assert sid == D_SID and ephemeral is True


async def test_user_text_never_gateway_control(env):
    a, client, gw, store, order = env
    await deliver(a, MENTION)
    await complete(a, gw.events[0])
    await deliver(a, prompted(M_SID, M_ISSUE, "/new", "act-new", mention=True))
    ev = gw.events[-1]
    assert ev.allow_gateway_control is False
    assert ev.get_command() is None
    assert all(e.allow_gateway_control is False for e in gw.events)


async def test_ticket_session_shared_and_keyed_by_generation(env):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    ev = gw.events[0]
    src = ev.source
    assert src.chat_id == f"{D_ISSUE}:1"
    assert src.thread_id is None
    assert src.chat_type == "dm"
    assert ev.message_id == f"{D_SID}:{D_ACT}"
    assert src.message_id == ev.message_id  # binds HERMES_SESSION_MESSAGE_ID (Q2)
    assert build_session_key(src) == f"agent:main:linear:dm:{D_ISSUE}:1"
    assert store.turn(ev.message_id).hermes_chat_id == f"{D_ISSUE}:1"


async def test_chat_type_dm_on_all_sources(env):
    a, client, gw, store, order = env
    await deliver(a, MENTION)
    await deliver(a, prompted(M_SID, M_ISSUE, "", "act-stop", mention=True, signal="stop"))
    await deliver(a, DELEGATION)
    assert len(gw.events) == 3  # mention turn, synthesized /stop, delegation turn
    assert {e.source.chat_type for e in gw.events} == {"dm"}
    mention_src = gw.events[0].source
    assert (mention_src.chat_id, mention_src.thread_id) == (M_ISSUE, M_SID)
    assert build_session_key(mention_src) == f"agent:main:linear:dm:{M_ISSUE}:{M_SID}"


async def test_first_turn_snapshot_appended(env):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    text = gw.events[0].text
    assert "Second test ticket" in text and "Todo" in text and "Feature, Bug" in text
    assert "Valkyrie" in text and "Test User" in text
    assert "x" * 2000 in text and "x" * 2001 not in text


async def test_snapshot_failure_still_starts_turn(env):
    a, client, gw, store, order = env
    client.snapshot_fails = True
    await deliver(a, DELEGATION)
    assert len(gw.events) == 1
    assert "Work on [RET-62]" in gw.events[0].text


async def test_digest_prepended_and_consumed_only_on_success(env):
    a, client, gw, store, order = env
    store.set_mode(D_ISSUE, "delegated", 1)
    store.add_context([ContextItem(D_ISSUE, "issue", "state", "Todo", "In Progress", "Mike", "t")])
    gw.raise_next = True
    await deliver(a, DELEGATION)
    assert gw.events[0].text.startswith("Since your last turn:")
    assert len(store.pending_context(D_ISSUE)) == 1  # handler raised -> items remain
    # Failed admission frees the FIFO; the next turn shows and consumes the digest.
    await deliver(a, prompted(D_SID, D_ISSUE, "again", "act-2"))
    assert len(gw.events) == 2
    assert gw.events[1].text.startswith("Since your last turn:")
    assert "Status: Todo → In Progress (Mike)" in gw.events[1].text
    assert store.pending_context(D_ISSUE) == []


async def test_empty_batch_no_consume(env, monkeypatch):
    a, client, gw, store, order = env
    calls = []
    monkeypatch.setattr(store, "consume_context", lambda *a, **k: calls.append((a, k)))
    await deliver(a, DELEGATION)
    assert len(gw.events) == 1
    assert calls == []
    assert not gw.events[0].text.startswith("Since your last turn:")


async def test_fold_none_with_items_still_consumes(env):
    a, client, gw, store, order = env
    store.set_mode(D_ISSUE, "delegated", 1)
    store.add_context([
        ContextItem(D_ISSUE, "issue", "labels", None, "+Bug", "Mike", "t1"),
        ContextItem(D_ISSUE, "issue", "labels", None, "-Bug", "Mike", "t2"),
    ])
    await deliver(a, DELEGATION)
    assert "Since your last turn:" not in gw.events[0].text
    assert store.pending_context(D_ISSUE) == []


async def test_queued_ack_when_turn_running(env):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    await deliver(a, prompted(D_SID, D_ISSUE, "more", "act-2"))
    assert [t[1] for t in thoughts(client)] == ["On it", QUEUED_ACK]
    assert QUEUED_ACK == "Queued — finishing current work first"
    assert len(gw.events) == 1


async def test_fifo_queues_second_turn_until_complete(env):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    await deliver(a, prompted(D_SID, D_ISSUE, "more", "act-2"))
    second_mid = f"{D_SID}:act-2"
    assert len(gw.events) == 1
    assert store.turn(second_mid) is None  # not recorded while queued
    await asyncio.create_task(a.on_processing_complete(gw.events[0], ProcessingOutcome.FAILURE))
    # Released via call_soon / a task, never inline.
    assert len(gw.events) == 1
    key = (f"{D_ISSUE}:1", None)
    assert a.last_outcome[key] is ProcessingOutcome.FAILURE
    assert key not in a.turns.inflight
    await a.wait_idle()
    assert len(gw.events) == 2
    assert gw.events[1].message_id == second_mid
    assert store.turn(second_mid) is not None
    assert key not in a.last_outcome  # reset when the next turn is recorded


async def test_complete_for_unknown_event_is_ignored(env):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    await deliver(a, prompted(D_SID, D_ISSUE, "more", "act-2"))
    stray = copy.copy(gw.events[0])
    stray.message_id = "someone-else"
    await complete(a, stray)
    assert len(gw.events) == 1


async def test_stop_synthesizes_control_event(env):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    n_acts = len(client.activities)
    await deliver(a, prompted(D_SID, D_ISSUE, "", "act-stop", signal="stop"))
    assert len(gw.events) == 2
    stop = gw.events[1]
    assert stop.text == "/stop"
    assert stop.allow_gateway_control is True
    assert stop.source.chat_id == gw.events[0].source.chat_id
    assert stop.source.thread_id is None
    assert stop.message_id and stop.message_id != gw.events[0].message_id
    assert stop.message_id in a.stop_message_ids
    assert len(client.activities) == n_acts  # no ack for a stop


async def test_interrupt_without_running_turn_acks_stop(env):
    a, client, gw, store, order = env
    await deliver(a, prompted(D_SID, D_ISSUE, "", "act-stop", signal="stop"))
    assert gw.events == []  # no /stop to the gateway
    assert [(sid, c) for sid, c, *_ in client.activities] == [
        (D_SID, {"type": "response", "body": "Stopped."})]


async def test_interrupt_drops_queued_turns(env):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    await deliver(a, prompted(D_SID, D_ISSUE, "more", "act-2"))
    await deliver(a, prompted(D_SID, D_ISSUE, "", "act-stop", signal="stop"))
    assert gw.events[-1].text == "/stop"
    await complete(a, gw.events[0], ProcessingOutcome.CANCELLED)
    assert [e.text for e in gw.events if e.text != "/stop"] == [gw.events[0].text]


# ------------------------------------------------------- delegation timer


def issue_delegated_event():
    ev = copy.deepcopy(ISSUE_DELEGATED)
    ev["updatedFrom"] = {"delegateId": None}
    return ev


async def test_delegation_wait_opens_session_once(env):
    a, client, gw, store, order = env
    issue = ISSUE_DELEGATED["data"]["id"]
    await deliver(a, issue_delegated_event(), issue_delegated_event())
    timer = a.opener.delegation_timers[issue]
    await a._execute(AwaitDelegation(issue, 0.05), {})
    assert a.opener.delegation_timers[issue] is timer  # no second timer while pending
    await asyncio.sleep(0.2)
    await a.wait_idle()
    opened = [c for c in client.calls if c[0] == "create_session_on_issue"]
    assert opened == [("create_session_on_issue", issue)]
    assert len(gw.events) == 1
    ev = gw.events[0]
    assert "You have been delegated RET-61." in ev.text
    assert "Second test ticket" in ev.text  # snapshot
    assert ev.source.chat_id == f"{issue}:1" and ev.source.user_id == HUMAN
    row = store.session("sess-issue")
    assert row is not None and row.plugin_created
    assert thoughts(client)[0][:2] == ("sess-issue", "On it")


async def test_delegation_wait_reuses_existing_app_session(env):
    a, client, gw, store, order = env
    issue = ISSUE_DELEGATED["data"]["id"]
    client.app_sessions = [{"id": "sess-old"}]
    await deliver(a, issue_delegated_event())
    await asyncio.sleep(0.2)
    await a.wait_idle()
    assert not [c for c in client.calls if c[0] == "create_session_on_issue"]
    assert store.session("sess-old").plugin_created
    assert len(gw.events) == 1


async def test_delegation_wait_cancelled_by_created(env):
    a, client, gw, store, order = env
    await a._execute(AwaitDelegation(D_ISSUE, 0.05), {})
    await deliver(a, DELEGATION)
    await asyncio.sleep(0.2)
    await a.wait_idle()
    assert not [c for c in client.calls if c[0] in ("create_session_on_issue",
                                                     "app_sessions_on_issue")]
    assert len(gw.events) == 1


# ------------------------------------------------- plain comments (RET-108)


async def test_toplevel_comment_on_delegated_is_context_not_a_session(env):
    """RET-108: a plain top-level comment opens no session and runs no turn; the next
    turn in the delegation thread sees it in the digest."""
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    await complete(a, gw.events[0])
    comment = copy.deepcopy(COMMENT_PLAIN)
    comment["data"]["issueId"] = D_ISSUE
    comment["data"]["issue"]["id"] = D_ISSUE
    await deliver(a, comment)
    assert len(gw.events) == 1
    assert not [c for c in client.calls if "session" in c[0]]
    assert [s.agent_session_id for s in store.sessions_for_issue(D_ISSUE)] == [D_SID]
    assert not [x for x in client.activities if x[0] != D_SID]
    await deliver(a, prompted(D_SID, D_ISSUE, "next", "act-2"))
    assert len(gw.events) == 2
    text = gw.events[1].text
    assert text.startswith("Since your last turn:")
    assert 'Comment: "top-level comment with no mention" (Test User)' in text
    assert store.pending_context(D_ISSUE) == []


async def test_existing_comment_session_keeps_working(env):
    """RET-108: a comment session opened before the change still takes prompts."""
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    await complete(a, gw.events[0])
    store.record_session("sess-comment", D_ISSUE, "cmt-old", "comment", plugin_created=True)
    await deliver(a, prompted("sess-comment", D_ISSUE, "follow-up", "act-c1"))
    assert len(gw.events) == 2
    ev = gw.events[1]
    assert ev.source.chat_id == f"{D_ISSUE}:1" and ev.source.thread_id is None
    assert "follow-up" in ev.text


# ------------------------------------------------------------ auto-start


async def test_auto_start_only_for_human_delegation():
    # human creator, unstarted -> move
    a, client, gw, store, listener, order = _new_adapter()
    assert await a.connect()
    await deliver(a, DELEGATION)
    assert ("move_to_first_started", D_ISSUE, TEAM) in client.calls
    await a.disconnect()

    # automation (bot) creator -> no move
    a, client, gw, store, listener, order = _new_adapter()
    assert await a.connect()
    bot = copy.deepcopy(DELEGATION)
    bot["agentSession"]["creator"] = None
    bot["botActor"] = {"id": "bot-1", "type": "workflow", "name": "Triage rule"}
    await deliver(a, bot)
    assert len(gw.events) == 1
    assert not [c for c in client.calls if c[0] == "move_to_first_started"]
    await a.disconnect()

    # started state -> no move; auto_start off -> no move
    for kw, state in (({}, "started"), ({"auto_start": False}, "unstarted")):
        a, client, gw, store, listener, order = _new_adapter(settings=make_settings(**kw))
        client.state_type = state
        assert await a.connect()
        await deliver(a, DELEGATION)
        assert len(gw.events) == 1
        assert not [c for c in client.calls if c[0] == "move_to_first_started"]
        await a.disconnect()


async def test_auto_start_not_on_later_turns(env):
    a, client, gw, store, order = env
    await deliver(a, DELEGATION)
    await complete(a, gw.events[0])
    await deliver(a, prompted(D_SID, D_ISSUE, "more", "act-2"))
    moves = [c for c in client.calls if c[0] == "move_to_first_started"]
    assert len(moves) == 1


# ---------------------------------------------------------- refusal / acks


async def test_refusal_posts_error_no_turn():
    a, client, gw, store, listener, order = _new_adapter(
        settings=make_settings(allow_all_users=False))
    assert await a.connect()
    await deliver(a, DELEGATION)
    assert gw.events == []
    errors = [(sid, c["body"], eph) for sid, c, eph, *_ in client.activities
              if c["type"] == "error"]
    assert errors == [(D_SID, REFUSAL_TEXT, False)]
    await a.disconnect()


async def test_ack_failure_does_not_block_turn_or_degrade(env):
    a, client, gw, store, order = env
    client.fail_activity = True
    await deliver(a, DELEGATION)
    assert len(gw.events) == 1
    assert a.degraded is False
    assert a.ack_degraded is True


# ------------------------------------------------------------ controls


async def test_approval_answer_resolves_card(env, monkeypatch):
    a, client, gw, store, order = env
    resolved = []
    monkeypatch.setattr(sessions_open_mod, "_approval_refusal", lambda *a, **k: None)
    monkeypatch.setattr(sessions_open_mod, "_resolve_approval_card",
                        lambda sk, choice, rid, *, actor_id: resolved.append(
                            (sk, choice, rid, actor_id)) or 1)
    await deliver(a, DELEGATION)
    store.put_control("req-1", "approval", D_SID, 1, ["once", "session", "deny"],
                      session_key="agent:main:linear:dm:x:1")
    await deliver(a, prompted(D_SID, D_ISSUE, "  allow ONCE ", "act-a"))
    assert resolved == [("agent:main:linear:dm:x:1", "once", "req-1", HUMAN)]
    assert store.control_for_session(D_SID) is None
    assert len(gw.events) == 1  # no new turn


async def test_approval_unrecognised_answer_reasks(env, monkeypatch):
    a, client, gw, store, order = env
    monkeypatch.setattr(sessions_open_mod, "_resolve_approval_card",
                        lambda *a, **k: pytest.fail("must not resolve"))
    store.put_control("req-1", "approval", D_SID, 1, ["once", "session", "deny"],
                      session_key="sk")
    await deliver(a, prompted(D_SID, D_ISSUE, "yes please", "act-a"))
    sid, content, eph, signal, meta = client.activities[-1]
    assert (sid, content["type"], signal) == (D_SID, "elicitation", "select")
    assert [o["value"] for o in meta["options"]] == ["once", "session", "deny"]
    assert store.control_for_session(D_SID) is not None
    assert gw.events == []


async def test_approval_refused_actor_keeps_control(env, monkeypatch):
    a, client, gw, store, order = env
    monkeypatch.setattr(sessions_open_mod, "_approval_refusal", lambda *a, **k: "Alice")
    monkeypatch.setattr(sessions_open_mod, "_refusal_text", lambda who: f"only {who}")
    monkeypatch.setattr(sessions_open_mod, "_resolve_approval_card",
                        lambda *a, **k: pytest.fail("must not resolve"))
    store.put_control("req-1", "approval", D_SID, 1, ["once", "deny"], session_key="sk")
    await deliver(a, prompted(D_SID, D_ISSUE, "deny", "act-a"))
    assert client.activities[-1][1] == {"type": "thought", "body": "only Alice"}
    assert store.control_for_session(D_SID) is not None


async def test_clarify_answer_resolves(env, monkeypatch):
    a, client, gw, store, order = env
    got = []
    monkeypatch.setattr(sessions_open_mod, "_resolve_clarify",
                        lambda rid, answer: got.append((rid, answer)) or True)
    store.put_control("clar-1", "clarify", D_SID, 1, [], session_key="sk")
    await deliver(a, prompted(D_SID, D_ISSUE, "the blue one", "act-c"))
    assert got == [("clar-1", "the blue one")]
    assert store.control_for_session(D_SID) is None
    assert gw.events == []


# ------------------------------------------------------------- lifecycle


async def test_fatal_decision_sets_fatal_and_unregisters():
    a, client, gw, store, listener, order = _new_adapter()
    assert await a.connect()
    await deliver(a, {"type": "OAuthApp", "action": "revoked"})
    assert a.has_fatal_error
    assert a.fatal_error_retryable is False
    assert "default" in listener.unregistered
    await a.disconnect()


async def test_connect_registers_endpoint_and_adapters_map():
    a, client, gw, store, listener, order = _new_adapter()
    a.set_owner_profile("work")
    assert await a.connect()
    assert ADAPTERS["work"] is a
    ep = listener.endpoints["work"]
    assert ep.client_id == "cid" and ep.webhook_secret() == b"ws"
    assert ep.seen("w1") is False and ep.seen("w1") is True
    assert a._router_ctx.app_user_id == APP
    assert a._router_ctx.app_user_handle == "hermestest"
    await a.disconnect()
    assert "work" not in ADAPTERS
    assert "work" in listener.unregistered
    assert client.closed


async def test_connect_fails_on_listener_start_error():
    listener = FakeListener(start_error=OSError("address in use"))
    a, client, gw, store, _, order = _new_adapter(listener=listener)
    assert await a.connect() is False
    assert a.has_fatal_error
    assert "default" not in ADAPTERS
    assert "default" in listener.unregistered


async def test_connect_fails_on_settings_error(caplog):
    a = LinearAdapter(PlatformConfig(enabled=True), get_config=lambda k, d=None: d,
                      client=FakeClient([]), store=Store.open(":memory:"),
                      listener=FakeListener())
    a._write_runtime_status_safe = lambda *args, **kw: None
    with caplog.at_level(logging.ERROR):
        assert await a.connect() is False
    assert a.has_fatal_error and a.fatal_error_retryable is False
    assert "client_id" in caplog.text


async def test_connect_warns_when_only_gateway_allowlist_set(monkeypatch, caplog):
    monkeypatch.setenv("GATEWAY_ALLOWED_USERS", "someone")
    monkeypatch.delenv("LINEAR_ALLOWED_USERS", raising=False)
    a, client, gw, store, listener, order = _new_adapter()
    with caplog.at_level(logging.WARNING):
        assert await a.connect()
    assert "GATEWAY_ALLOWED_USERS" in caplog.text
    await a.disconnect()


async def test_connect_real_listener_port0():
    listener_mod._reset_listener_for_tests()
    order: list = []
    client = FakeClient(order)
    a = LinearAdapter(PlatformConfig(enabled=True), settings=make_settings(),
                      client=client, store=Store.open(":memory:"))
    a._write_runtime_status_safe = lambda *args, **kw: None
    gw = Gateway(order)
    gw.adapter = a
    a.handle_message = gw
    try:
        assert await a.connect()
        lst = listener_mod.get_listener("127.0.0.1", 0)
        assert lst.running and lst.bound_port
        payload = {**DELEGATION, "webhookTimestamp": int(time.time() * 1000),
                   "webhookId": "wh-1", "oauthClientId": "cid"}
        body = json.dumps(payload).encode()
        sig = hmac.new(b"ws", body, hashlib.sha256).hexdigest()
        async with aiohttp.ClientSession() as http:
            async with http.post(f"http://127.0.0.1:{lst.bound_port}/linear/default/webhook",
                                 data=body, headers={"Linear-Signature": sig}) as resp:
                assert resp.status == 200
        for _ in range(50):
            if gw.events:
                break
            await asyncio.sleep(0.02)
        await a.wait_idle()
        assert len(gw.events) == 1
    finally:
        await a.disconnect()
        await listener_mod.get_listener("127.0.0.1", 0).wait_idle()
        assert not listener_mod.get_listener("127.0.0.1", 0).running
        listener_mod._reset_listener_for_tests()


# ---------------------------------------------------------------- access


async def test_access_policy_attributes_and_resolved_ids():
    uid = "00000000-0000-4000-8000-0000000000aa"
    a, client, gw, store, listener, order = _new_adapter(settings=make_settings(
        allow_all_users=False, allowed_users=frozenset({uid, "alice@example.com"})))
    client.user_emails[HUMAN] = "Alice@Example.com"
    assert await a.connect()
    assert a._dm_policy == a._group_policy == "allowlist"
    assert a._group_allow_from == set()
    assert a.resolved_allowlist_user_ids() == {uid}
    assert a._is_dm_allowed(uid) and not a._is_dm_allowed(HUMAN)
    await deliver(a, DELEGATION)  # creator HUMAN admitted via email
    assert len(gw.events) == 1
    assert HUMAN in a.resolved_allowlist_user_ids()
    assert a._is_dm_allowed(HUMAN)
    await a.disconnect()


async def test_allow_all_adds_star():
    a, client, gw, store, listener, order = _new_adapter()
    assert await a.connect()
    assert "*" in a.resolved_allowlist_user_ids()
    assert a._is_dm_allowed("anyone")
    await a.disconnect()


async def test_get_chat_info(env):
    a, client, gw, store, order = env
    store.upsert_ticket(D_ISSUE, identifier="RET-62")
    info = await a.get_chat_info(f"{D_ISSUE}:1")
    assert info["type"] == "dm" and "RET-62" in info["name"]


async def test_dispatch_error_frees_fifo(env, monkeypatch):
    a, client, gw, store, order = env
    real = a.build_source
    calls = {"n": 0}

    def flaky(*args, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("profile route rejected")
        return real(*args, **kw)

    monkeypatch.setattr(a, "build_source", flaky)
    await deliver(a, DELEGATION)
    assert gw.events == [] and a.turns.inflight == {}
    await deliver(a, prompted(D_SID, D_ISSUE, "again", "act-2"))
    assert len(gw.events) == 1



# ------------------------------------------------- robustness (fix round 1)


async def _until(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


async def deliver_nowait(a, *events):
    """Queue events and wait for the worker only (not for turn tasks)."""
    for ev in events:
        a.deliver(copy.deepcopy(ev))
    await a._events.join()


async def test_fifo_release_after_real_base_session_guard():
    a, client, gw, store, listener, order = _new_adapter()
    del a.handle_message  # the REAL BasePlatformAdapter.handle_message path
    handled, busy, completed = [], [], []

    async def handler(event):
        handled.append(event.message_id)
        await asyncio.sleep(0.02)
        return None

    a.set_message_handler(handler)
    real_busy = a._handle_message_while_active

    async def busy_spy(event, session_key):
        busy.append(event.message_id)
        return await real_busy(event, session_key)

    a._handle_message_while_active = busy_spy
    real_complete = a.on_processing_complete

    async def complete_spy(event, outcome):
        completed.append((event.message_id, outcome))
        return await real_complete(event, outcome)

    a.on_processing_complete = complete_spy
    assert await a.connect()
    try:
        await deliver_nowait(a, DELEGATION, prompted(D_SID, D_ISSUE, "more", "act-2"))
        first, second = f"{D_SID}:{D_ACT}", f"{D_SID}:act-2"
        assert await _until(lambda: len(completed) == 2, 5.0), (handled, busy, completed)
        assert handled == [first, second]
        assert busy == []  # the second turn never took the base's busy path
        assert completed == [(first, ProcessingOutcome.SUCCESS),
                             (second, ProcessingOutcome.SUCCESS)]
        await a.wait_idle()
        assert a.turns.inflight == {} and a.turns.queues == {}
    finally:
        await a.disconnect()


async def test_stop_before_handoff_cancels_dispatch(env):
    a, client, gw, store, order = env
    client.snapshot_delay = 5.0  # first-turn dispatch parks in the snapshot fetch
    await deliver_nowait(a, DELEGATION)
    await asyncio.sleep(0.05)
    assert gw.events == [] and a.turns.inflight  # in flight, not handed off yet
    await deliver(a, prompted(D_SID, D_ISSUE, "", "act-stop", signal="stop"))
    assert gw.events == []  # neither the turn nor a /stop reached the gateway
    assert a.turns.inflight == {}
    responses = [(sid, c["body"]) for sid, c, *_ in client.activities if c["type"] == "response"]
    assert responses == [(D_SID, "Stopped.")]
    client.snapshot_delay = 0.0
    await deliver(a, prompted(D_SID, D_ISSUE, "again", "act-2"))
    assert [e.message_id for e in gw.events] == [f"{D_SID}:act-2"]  # FIFO is free


async def test_unadmitted_turn_frees_fifo(env, caplog):
    a, client, gw, store, order = env
    gw.admit = False  # handle_message returns but the base never starts processing
    with caplog.at_level(logging.WARNING):
        await deliver(a, DELEGATION)
    assert len(gw.events) == 1
    assert a.turns.inflight == {}
    assert "not admitted" in caplog.text
    gw.admit = True
    await deliver(a, prompted(D_SID, D_ISSUE, "again", "act-2"))
    assert len(gw.events) == 2


async def test_turn_safety_timeout_frees_fifo(env, monkeypatch, caplog):
    from hermes_linear import constants
    a, client, gw, store, order = env
    monkeypatch.setattr(constants, "TURN_SAFETY_TIMEOUT_S", 0.05)
    with caplog.at_level(logging.ERROR):
        await deliver(a, DELEGATION)
        await deliver(a, prompted(D_SID, D_ISSUE, "more", "act-2"))
        assert len(gw.events) == 1
        assert await _until(lambda: len(gw.events) == 2, 2.0)
    assert gw.events[1].message_id == f"{D_SID}:act-2"
    assert "safety timeout" in caplog.text


async def test_slow_linear_does_not_block_next_event_ack(env):
    a, client, gw, store, order = env
    client.activity_delay[D_SID] = 5.0  # the first event's ack hangs
    a.deliver(copy.deepcopy(DELEGATION))
    a.deliver(copy.deepcopy(MENTION))
    assert await _until(lambda: any(sid == M_SID for sid, *_ in client.activities), 1.0)
    assert await _until(lambda: any(e.source.thread_id == M_SID for e in gw.events), 1.0)


async def test_dispatch_exception_before_handoff_frees_fifo(env, monkeypatch):
    a, client, gw, store, order = env
    real = store.pending_context_batch
    calls = {"n": 0}

    def flaky(issue_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db locked")
        return real(issue_id)

    monkeypatch.setattr(store, "pending_context_batch", flaky)
    await deliver(a, DELEGATION)
    assert gw.events == [] and a.turns.inflight == {}
    errors = [sid for sid, c, *_ in client.activities if c["type"] == "error"]
    assert errors == [D_SID]
    await deliver(a, prompted(D_SID, D_ISSUE, "again", "act-2"))
    assert len(gw.events) == 1


async def test_connect_twice_does_not_reregister(env, caplog):
    a, client, gw, store, order = env
    worker = a._worker
    with caplog.at_level(logging.INFO):
        assert await a.connect() is True
    assert a._worker is worker
    assert "already connected" in caplog.text


async def test_deliver_after_disconnect_is_noop(caplog):
    a, client, gw, store, listener, order = _new_adapter()
    assert await a.connect()
    await a.disconnect()
    assert a._events is None
    with caplog.at_level(logging.WARNING):
        a.deliver(copy.deepcopy(DELEGATION))
    assert "not connected" in caplog.text


# ------------------------------------------------- task sub-tickets (RET-107)

PARENT = "00000000-0000-4000-8000-0000000000aa"
TASK_TEXT = "This is a task of RET-94; work on it continues in RET-94's session."


def task_issue(issue_id, *, title="T1 · Alpha", creator=APP, parent=PARENT):
    return {"id": issue_id, "identifier": "RET-200", "title": title,
            "creator": {"id": creator} if creator else None,
            "parent": {"id": parent, "identifier": "RET-94"} if parent else None}


def responses(client):
    return [(sid, c["body"]) for sid, c, *_ in client.activities if c["type"] == "response"]


@pytest.fixture
def task_env(env):
    a, client, gw, store, order = env
    store.upsert_ticket(PARENT, identifier="RET-94")
    store.set_mode(PARENT, "delegated", 1)
    return env


async def test_task_parent_rules(task_env):
    a, client, gw, store, order = task_env
    client.issues["i1"] = task_issue("i1")
    assert await a._task_parent("i1", "T1 · Alpha") == "RET-94"
    assert await a._task_parent("i1", None) == "RET-94"  # no title in the payload: fetched
    n = len(client.calls)
    assert await a._task_parent("i1", "Plain title") is None
    assert client.calls[n:] == []  # payload title decides without a fetch
    client.issues["i2"] = task_issue("i2", creator=HUMAN)  # a human's `T1 ·` child
    assert await a._task_parent("i2", "T1 · Alpha") is None
    client.issues["i3"] = task_issue("i3", parent="not-delegated")
    assert await a._task_parent("i3", "T1 · Alpha") is None
    client.issues["i4"] = task_issue("i4", parent=None)
    assert await a._task_parent("i4", "T1 · Alpha") is None
    client.issues["i5"] = task_issue("i5", title="Renamed")
    assert await a._task_parent("i5", None) is None
    assert await a._task_parent("missing", "T1 · Alpha") is None  # lookup fails: allowed


async def test_delegated_task_ticket_created_replies_once(task_env):
    a, client, gw, store, order = task_env
    client.issues[D_ISSUE] = task_issue(D_ISSUE)
    ev = copy.deepcopy(DELEGATION)
    ev["agentSession"]["issue"]["title"] = "T1 · Alpha"
    await deliver(a, ev)
    assert responses(client) == [(D_SID, TASK_TEXT)]
    assert gw.events == [] and store.ticket(D_ISSUE).mode != "delegated"
    # The Issue update for the same delegation arrives after: no second reply.
    up = issue_delegated_event()
    up["data"].update(id=D_ISSUE, title="T1 · Alpha")
    await deliver(a, up)
    await asyncio.sleep(0.2)
    await a.wait_idle()
    assert responses(client) == [(D_SID, TASK_TEXT)] and gw.events == []
    assert not [c for c in client.calls if c[0] in ("create_session_on_issue",
                                                     "app_sessions_on_issue")]


async def test_delegated_task_ticket_timer_path_replies(task_env):
    a, client, gw, store, order = task_env
    issue = ISSUE_DELEGATED["data"]["id"]
    client.issues[issue] = task_issue(issue)
    up = issue_delegated_event()
    up["data"]["title"] = "T1 · Alpha"
    await deliver(a, up)
    assert store.ticket(issue).mode != "delegated"
    await asyncio.sleep(0.2)
    await a.wait_idle()
    assert responses(client) == [("sess-issue", TASK_TEXT)]
    assert gw.events == [] and store.ticket(issue).mode != "delegated"
    assert store.session("sess-issue").plugin_created
    assert not thoughts(client)  # no "On it" ack: no turn


async def test_human_task_titled_child_still_delegates(task_env):
    a, client, gw, store, order = task_env
    client.issues[D_ISSUE] = task_issue(D_ISSUE, creator=HUMAN)
    ev = copy.deepcopy(DELEGATION)
    ev["agentSession"]["issue"]["title"] = "T1 · Alpha"
    await deliver(a, ev)
    assert responses(client) == [] and len(gw.events) == 1
    assert store.ticket(D_ISSUE).mode == "delegated"
