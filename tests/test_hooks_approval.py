"""Out-of-scope write approval in the tool hooks (RET-119 spec §2.1 steps 3-6, §2.5).

``pre_tool_call`` arms a stored approval request and returns Hermes's ``approve``
directive; ``post_tool_call`` disarms it; ``transform_tool_result`` rewrites Hermes's
block / timeout text for a gated tool into ``APPROVAL_PENDING`` / ``APPROVAL_DENIED``.
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
TIMEOUT = "BLOCKED: Command timed out without user response. Silence is not consent."
BLOCKED = "BLOCKED: approval is required but no human is present."


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


def test_pre_tool_call_none_non_linear_session(store, session):
    _request(store)
    session(HERMES_SESSION_PLATFORM="slack", HERMES_SESSION_KEY=SK,
            HERMES_SESSION_MESSAGE_ID=MID)
    assert hooks.on_pre_tool_call(TOOL, dict(ARGS), "task-1") is None
    session(HERMES_SESSION_PLATFORM="linear", HERMES_SESSION_MESSAGE_ID=MID)  # no key
    assert hooks.on_pre_tool_call(TOOL, dict(ARGS), "task-1") is None
    assert store.take_armed_request(SK, D) is None


def test_pre_tool_call_none_on_store_error(store, session, monkeypatch):
    _request(store)

    def boom(*a, **k):
        raise RuntimeError("store closed")

    monkeypatch.setattr(store, "arm_request", boom)
    assert hooks.on_pre_tool_call(TOOL, dict(ARGS), "task-1") is None


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


# ----------------------------------------------------------- transform_tool_result


def test_transform_pending(store, session):
    _request(store)
    _pending_row(store)
    out = json.loads(hooks.on_transform_tool_result(TOOL, dict(ARGS), TIMEOUT))
    assert out == {"ok": False, "error": "APPROVAL_PENDING",
                   "message": tool_approval.PENDING_TEXT}


def test_transform_timeout_without_pending_row_is_denied(store, session):
    _request(store)
    _pending_row(store, turn_mid="other-turn")
    out = json.loads(hooks.on_transform_tool_result(TOOL, dict(ARGS), TIMEOUT))
    assert out["error"] == "APPROVAL_DENIED" and out["message"] == TIMEOUT


def test_transform_timeout_live_or_other_digest_row_is_denied(store, session):
    _request(store)
    _pending_row(store, state="live")
    assert json.loads(hooks.on_transform_tool_result(TOOL, dict(ARGS), TIMEOUT))[
        "error"] == "APPROVAL_DENIED"
    _pending_row(store, d="0" * 16)
    assert json.loads(hooks.on_transform_tool_result(TOOL, dict(ARGS), TIMEOUT))[
        "error"] == "APPROVAL_DENIED"


def test_transform_denied(store, session):
    _request(store)
    out = json.loads(hooks.on_transform_tool_result(TOOL, dict(ARGS), BLOCKED))
    assert out == {"ok": False, "error": "APPROVAL_DENIED", "message": BLOCKED}


def test_transform_never_rewrites_tool_json(store, session):
    _request(store)
    _pending_row(store)
    required = json.dumps({"ok": False, "error": "APPROVAL_REQUIRED",
                           "message": "timed out without user response"})
    assert hooks.on_transform_tool_result(TOOL, dict(ARGS), required) is None
    assert hooks.on_transform_tool_result(TOOL, dict(ARGS), json.dumps({"ok": True})) is None


def test_transform_no_request_untouched(store, session):
    _pending_row(store)
    assert hooks.on_transform_tool_result(TOOL, dict(ARGS), TIMEOUT) is None
    assert hooks.on_transform_tool_result(TOOL, dict(ARGS), BLOCKED) is None
    _request(store)
    assert hooks.on_transform_tool_result(TOOL, {**ARGS, "body": "x"}, BLOCKED) is None
    session(HERMES_SESSION_PLATFORM="slack", HERMES_SESSION_KEY=SK,
            HERMES_SESSION_MESSAGE_ID=MID)
    assert hooks.on_transform_tool_result(TOOL, dict(ARGS), BLOCKED) is None


def test_transform_bridged_single_call(store, session):
    _request(store)
    wrapped = {"calls": [{"name": TOOL, "arguments": dict(ARGS)}]}
    out = json.loads(hooks.on_transform_tool_result("tool_call", wrapped, BLOCKED))
    assert out["error"] == "APPROVAL_DENIED"
