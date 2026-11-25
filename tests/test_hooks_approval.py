"""Out-of-scope write approval in the tool hooks (RET-119 spec §2.1 steps 3-6, §2.5).

``pre_tool_call`` arms a stored approval request and returns Hermes's ``approve``
directive; ``post_tool_call`` disarms it. ``transform_tool_result`` leaves a gated tool's
result alone: Hermes never runs it for pre-hook blocks (Ruling R9).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hermes_linear import adapter as adapter_mod
from hermes_linear import hooks, tool_approval
from hermes_linear.store import Store

SK = "agent:main:linear:dm:chat-1"
MID = "turn-mid-1"
TOOL = "linear_comment"
ARGS = {"issue": "RET-50", "body": "hello"}
D = tool_approval.digest(TOOL, ARGS)
MESSAGE = f"Approve `{TOOL}` outside this session's tickets?\ndigest: {D}"
# Hermes's real pre-hook block results: json.dumps({"error": "BLOCKED: ..."}).
TIMEOUT = json.dumps({"error": "BLOCKED: Command timed out without user response. "
                               "Silence is not consent."})
BLOCKED = json.dumps({"error": "BLOCKED: approval is required but no human is present."})


class FakeLoop:
    def __init__(self):
        self.calls = []

    def is_closed(self):
        return False

    def call_soon_threadsafe(self, fn, *args):
        self.calls.append((fn, args))


@pytest.fixture
def store(monkeypatch):
    s = Store.open(":memory:")
    loop = FakeLoop()
    ad = SimpleNamespace(_store=s, event_loop=loop)
    monkeypatch.setattr(adapter_mod, "ADAPTERS", {"neo": ad})
    yield s
    s.close()


@pytest.fixture
def session(monkeypatch):
    def bind(**env):
        monkeypatch.setattr(hooks, "get_session_env",
                            lambda name, default="": env.get(name, default))
    bind(HERMES_SESSION_PLATFORM="linear", HERMES_SESSION_KEY=SK,
         HERMES_SESSION_MESSAGE_ID=MID)
    return bind


def _request(store, tool=TOOL, args=ARGS):
    store.put_request(SK, tool_approval.digest(tool, args), tool, ["uuid-y"], MESSAGE)


def _pending_row(store, d=D, turn_mid=MID, state="pending", kind="approval"):
    command = tool_approval.grant_command(f"<{TOOL}> (plugin approval rule)", d)
    store.put_control("req-1", kind, "sess-1", 1, ["once", "deny"], session_key=SK,
                      question=MESSAGE, command=command, turn_mid=turn_mid)
    if state == "pending":
        assert store.mark_control_pending("req-1", "issue-1")


# ------------------------------------------------------------------ pre_tool_call


def test_pre_tool_call_approves_and_arms(store, session):
    _request(store)
    out = hooks.on_pre_tool_call(TOOL, dict(ARGS), "task-1", tool_call_id="c1")
    assert out == {"action": "approve", "message": MESSAGE,
                   "rule_key": tool_approval.rule_key(TOOL, D)}
    assert store.take_armed_request(SK, D) is not None


def test_pre_tool_call_unwraps_single_bridge_call(store, session):
    _request(store)
    for args in ({"calls": [{"name": TOOL, "arguments": dict(ARGS)}]},
                 {"calls": json.dumps([{"name": TOOL, "arguments": ARGS}])},
                 {"name": TOOL, "arguments": json.dumps(ARGS)}):
        out = hooks.on_pre_tool_call("tool_call", args, "task-1")
        assert out is not None and out["rule_key"] == tool_approval.rule_key(TOOL, D), args
    batch = {"calls": [{"name": TOOL, "arguments": ARGS}, {"name": TOOL, "arguments": ARGS}]}
    store.disarm_request(SK, D)
    assert hooks.on_pre_tool_call("tool_call", batch, "task-1") is None
    assert store.take_armed_request(SK, D) is None


def test_pre_tool_call_none_without_request(store, session):
    assert hooks.on_pre_tool_call(TOOL, dict(ARGS), "task-1") is None
    _request(store)
    assert hooks.on_pre_tool_call(TOOL, {**ARGS, "body": "other"}, "task-1") is None
    assert hooks.on_pre_tool_call(TOOL, {"x": object()}, "task-1") is None  # digest TypeError
    assert store.take_armed_request(SK, D) is None


def test_pre_tool_call_none_non_gated_tool(store, session):
    store.put_request(SK, tool_approval.digest("terminal", ARGS), "terminal", [], MESSAGE)
    assert hooks.on_pre_tool_call("terminal", dict(ARGS), "task-1") is None
    assert hooks.on_pre_tool_call("linear_issue_get", dict(ARGS), "task-1") is None


SLACK_SK = "agent:main:slack:dm:C1:1700000000.0001"


def _slack(session, key: str | None = SLACK_SK):
    env = {"HERMES_SESSION_PLATFORM": "slack", "HERMES_SESSION_MESSAGE_ID": "slack-m1"}
    if key:
        env["HERMES_SESSION_KEY"] = key
    session(**env)


def test_outside_request_armed_by_slack_key(store, session):
    """RET-148: a request stored by an off-Linear call is armed by that session's key."""
    store.put_request(SLACK_SK, D, TOOL, ["uuid-y"], MESSAGE)
    _slack(session)
    out = hooks.on_pre_tool_call(TOOL, dict(ARGS), "task-1", tool_call_id="c1")
    # Final review: an outside grain, so a permanent Slack grant never applies in Linear.
    assert out == {"action": "approve", "message": MESSAGE,
                   "rule_key": f"{TOOL}:outside:{D}"}
    assert store.take_armed_request(SLACK_SK, D) is not None


def test_inside_rule_key_unchanged(store, session):
    """In a Linear session the key stays ``<tool>:<digest>`` (existing grants keep working)."""
    _request(store)
    out = hooks.on_pre_tool_call(TOOL, dict(ARGS), "task-1", tool_call_id="c1")
    assert out is not None and out["rule_key"] == f"{TOOL}:{D}"


def test_unbound_platform_counts_as_outside(store, session):
    """No bound ``HERMES_SESSION_PLATFORM`` (a script, a thread without session vars) is
    outside a Linear session, the same predicate ``agent_tools._resolve`` uses."""
    store.put_request("script-key", D, TOOL, ["uuid-y"], MESSAGE)
    session(HERMES_SESSION_KEY="script-key")
    out = hooks.on_pre_tool_call(TOOL, dict(ARGS), "task-1", tool_call_id="c1")
    assert out is not None and out["rule_key"] == f"{TOOL}:outside:{D}"


def test_slack_request_not_armed_from_linear_key(store, session):
    store.put_request(SLACK_SK, D, TOOL, ["uuid-y"], MESSAGE)
    session(HERMES_SESSION_PLATFORM="linear", HERMES_SESSION_KEY="lk",
            HERMES_SESSION_MESSAGE_ID=MID)
    assert hooks.on_pre_tool_call(TOOL, dict(ARGS), "task-1") is None
    assert store.take_armed_request(SLACK_SK, D) is None


def test_linear_request_not_armed_from_slack_key(store, session):
    _request(store)  # stored under the Linear key SK
    _slack(session)
    assert hooks.on_pre_tool_call(TOOL, dict(ARGS), "task-1") is None
    assert store.take_armed_request(SK, D) is None


def test_outside_no_key_no_directive(store, session):
    _request(store)
    store.put_request(SLACK_SK, D, TOOL, ["uuid-y"], MESSAGE)
    _slack(session, key=None)
    assert hooks.on_pre_tool_call(TOOL, dict(ARGS), "task-1") is None
    session(HERMES_SESSION_PLATFORM="linear", HERMES_SESSION_MESSAGE_ID=MID)  # no key
    assert hooks.on_pre_tool_call(TOOL, dict(ARGS), "task-1") is None
    assert store.take_armed_request(SK, D) is None
    assert store.take_armed_request(SLACK_SK, D) is None


def test_outside_posts_no_activities(store, session):
    store.put_request(SLACK_SK, D, TOOL, ["uuid-y"], MESSAGE)
    _slack(session)
    assert hooks._session({}) is None
    assert hooks.on_pre_tool_call(TOOL, dict(ARGS), "task-1", tool_call_id="c1") is not None
    hooks.on_post_tool_call(TOOL, dict(ARGS), '{"ok": true}', "task-1", tool_call_id="c1")
    assert adapter_mod.ADAPTERS["neo"].event_loop.calls == []  # no _route scheduled
    assert store.request(SLACK_SK, D).armed is False  # post_tool_call disarmed it


def test_pre_tool_call_none_on_store_error(store, session, monkeypatch):
    _request(store)

    def boom(*a, **k):
        raise RuntimeError("store closed")

    monkeypatch.setattr(store, "arm_request", boom)
    assert hooks.on_pre_tool_call(TOOL, dict(ARGS), "task-1") is None


def test_pre_tool_call_none_on_request_read_error(store, session, monkeypatch):
    _request(store)

    def boom(*a, **k):
        raise RuntimeError("store closed")

    monkeypatch.setattr(store, "request", boom)
    assert hooks.on_pre_tool_call(TOOL, dict(ARGS), "task-1", tool_call_id="c1") is None
    calls = adapter_mod.ADAPTERS["neo"].event_loop.calls
    assert len(calls) == 1 and calls[0][1][1] == "start"  # action event still scheduled


def test_pre_tool_call_still_schedules_action_event(store, session):
    _request(store)
    assert hooks.on_pre_tool_call(TOOL, dict(ARGS), "task-1", tool_call_id="c1") is not None
    assert len(adapter_mod.ADAPTERS["neo"].event_loop.calls) == 1
    fn, (adapters, phase, env, ev) = adapter_mod.ADAPTERS["neo"].event_loop.calls[0]
    assert fn is hooks._route and phase == "start"
    assert ev["tool_name"] == TOOL and ev["args"] == ARGS and ev["tool_call_id"] == "c1"
    # Also without a request (the existing behaviour).
    assert hooks.on_pre_tool_call("terminal", {"command": "ls"}, "task-1") is None
    assert len(adapter_mod.ADAPTERS["neo"].event_loop.calls) == 2


# ----------------------------------------------------------------- post_tool_call


def test_post_tool_call_disarms(store, session):
    _request(store)
    assert store.arm_request(SK, D) is not None
    hooks.on_post_tool_call(TOOL, dict(ARGS), "BLOCKED: denied", "task-1")
    assert store.take_armed_request(SK, D) is None
    assert store.request(SK, D) is not None  # the request itself stays (§2.1 step 6)
    assert adapter_mod.ADAPTERS["neo"].event_loop.calls[-1][1][1] == "finish"


def test_post_tool_call_disarm_error_is_swallowed(store, session, monkeypatch):
    _request(store)

    def boom(*a, **k):
        raise RuntimeError("store closed")

    monkeypatch.setattr(store, "disarm_request", boom)
    assert hooks.on_post_tool_call(TOOL, dict(ARGS), BLOCKED, "task-1") is None
    assert adapter_mod.ADAPTERS["neo"].event_loop.calls[-1][1][1] == "finish"


# ----------------------------------------------------------- transform_tool_result


def test_transform_never_rewrites_gated_result(store, session):
    """Even with a stored request and this turn's pending row for the digest, the
    transform leaves a gated tool's result alone (no APPROVAL_PENDING / _DENIED)."""
    _request(store)
    _pending_row(store)
    for result in (TIMEOUT, BLOCKED, json.dumps({"ok": True})):
        assert hooks.on_transform_tool_result(TOOL, dict(ARGS), result) is None
        wrapped = {"calls": [{"name": TOOL, "arguments": dict(ARGS)}]}
        assert hooks.on_transform_tool_result("tool_call", wrapped, result) is None
