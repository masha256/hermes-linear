"""Gated out-of-scope writes (RET-119 spec §2.1 steps 1, 2 and 5; Task 4, RET-126).

``linear_comment``, ``linear_issue_update`` and ``linear_issue_create`` (parent) record an
approval request for an out-of-scope target instead of refusing it, and write only after
taking an *armed* request whose stored targets hold that issue. The hook that arms a
request is a later task: tests arm it with ``store.arm_request`` directly.
"""

from __future__ import annotations

import dataclasses

import pytest

from test_agent_tools_generic import SELF, adapter, loop, pin_ctx, ws  # noqa: F401 - fixtures

from hermes_linear.linear_client import LinearError
from hermes_linear.tool_approval import digest
from hermes_linear.agent_tools import (TOOL_SCHEMAS, linear_comment, linear_issue_create,
                                       linear_issue_update, linear_task_state)


def _mode(adapter, mode):
    adapter._settings = dataclasses.replace(adapter._settings, out_of_scope_writes=mode)


def _requests(adapter) -> int:
    return adapter._store.conn.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0]


def _comments_on(ws, issue_id):
    return [c for c in ws.comments.values() if c["issue"] == issue_id]


COMMENT = {"id": "OTH-9", "body": "hello there"}


def test_in_scope_comment_unchanged_no_request(pin_ctx, ws, adapter):
    out = linear_comment({"id": "RET-2", "body": "hi"})
    assert out["ok"] is True
    assert len(_comments_on(ws, "uuid-child")) == 1
    assert _requests(adapter) == 0


def test_out_of_scope_comment_first_call_requires_approval(pin_ctx, ws, adapter):
    out = linear_comment(dict(COMMENT))
    assert out["ok"] is False and out["error"] == "APPROVAL_REQUIRED"
    assert "OTH-9" in out["message"] and "linear_comment" in out["message"]
    assert set(out) == {"ok", "error", "message"}
    assert _comments_on(ws, "uuid-other") == [] and ws.writes() == []
    dg = digest("linear_comment", COMMENT)
    req = adapter._store.request("sk", dg)
    assert req is not None and req.targets == ["uuid-other"] and req.tool == "linear_comment"
    assert req.armed is False
    assert req.message.endswith(f"digest: {dg}")
    assert "OTH-9" in req.message and "Unrelated" in req.message


def test_armed_request_writes_once(pin_ctx, ws, adapter):
    dg = digest("linear_comment", COMMENT)
    assert linear_comment(dict(COMMENT))["error"] == "APPROVAL_REQUIRED"
    assert adapter._store.arm_request("sk", dg) is not None
    out = linear_comment(dict(COMMENT))
    assert out["ok"] is True and out["issue"] == "OTH-9"
    assert [c["body"] for c in _comments_on(ws, "uuid-other")] == ["hello there"]
    assert adapter._store.request("sk", dg) is None  # consumed
    again = linear_comment(dict(COMMENT))
    assert again["error"] == "APPROVAL_REQUIRED"
    assert len(_comments_on(ws, "uuid-other")) == 1


def test_unarmed_request_never_writes(pin_ctx, ws, adapter):
    """Review Focus 2: a stored but unarmed request never lets a write through."""
    for _ in range(2):
        assert linear_comment(dict(COMMENT))["error"] == "APPROVAL_REQUIRED"
    assert ws.writes() == []
    assert adapter._store.request("sk", digest("linear_comment", COMMENT)) is not None


def test_armed_request_is_per_session_key(pin_ctx, ws, adapter):
    dg = digest("linear_comment", COMMENT)
    linear_comment(dict(COMMENT))
    adapter._store.arm_request("sk", dg)
    pin_ctx(key="sk-other")
    assert linear_comment(dict(COMMENT))["error"] == "APPROVAL_REQUIRED"
    assert ws.writes() == []


def test_update_checks_both_ends_before_writing(pin_ctx, ws, adapter):
    ws.add_issue("OTH-8", title="Other parent", uuid="uuid-other-8", project=None)
    args = {"id": "RET-2", "parent": "OTH-9"}
    out = linear_issue_update(dict(args))
    assert out["error"] == "APPROVAL_REQUIRED" and "OTH-9" in out["message"]
    assert ws.writes() == []
    assert adapter._store.request("sk", digest("linear_issue_update", args)).targets == [
        "uuid-other"]
    both = {"id": "OTH-9", "parent": "OTH-8"}
    out = linear_issue_update(dict(both))
    assert out["error"] == "APPROVAL_REQUIRED"
    assert "OTH-9" in out["message"] and "OTH-8" in out["message"]
    assert ws.writes() == []
    req = adapter._store.request("sk", digest("linear_issue_update", both))
    assert req.targets == ["uuid-other", "uuid-other-8"]
    adapter._store.arm_request("sk", req.digest)
    assert linear_issue_update(dict(both))["ok"] is True
    assert ws.issues["uuid-other"]["parent"] == "uuid-other-8"


def test_update_cycle_refused_before_approval(pin_ctx, ws, adapter):
    ws.issues["uuid-other"]["parent"] = SELF  # OTH-9 is RET-1's child now: in scope
    ws.add_issue("OTH-7", uuid="uuid-o7", project=None)
    ws.issues[SELF]["parent"] = "uuid-o7"  # RET-1 under an out-of-scope issue
    # making OTH-7 a child of OTH-9 would be a cycle: refused before any request
    out = linear_issue_update({"id": "OTH-7", "parent": "OTH-9"})
    assert out == {"ok": False, "error": "PARENT_CYCLE"}
    assert _requests(adapter) == 0 and ws.writes() == []


def test_create_with_out_of_scope_parent_then_in_scope(pin_ctx, ws, adapter):
    args = {"title": "Under other", "parent": "OTH-9"}
    assert linear_issue_create(dict(args))["error"] == "APPROVAL_REQUIRED"
    assert ws.writes() == [] and adapter._store.created_by(SELF) == set()
    adapter._store.arm_request("sk", digest("linear_issue_create", args))
    out = linear_issue_create(dict(args))
    assert out["ok"] is True
    new_id = out["issue"]["id"]
    assert ws.issues[new_id]["parent"] == "uuid-other"
    assert adapter._store.created_by(SELF) == {new_id}
    c = linear_comment({"id": new_id, "body": "mine"})
    assert c["ok"] is True
    assert _requests(adapter) == 0


def test_target_moved_between_calls_refused(pin_ctx, ws, adapter):
    dg = digest("linear_comment", COMMENT)
    linear_comment(dict(COMMENT))
    adapter._store.arm_request("sk", dg)
    ws.issues["uuid-other"]["identifier"] = "OTH-OLD"  # OTH-9 now names another issue
    ws.add_issue("OTH-9", title="Elsewhere", uuid="uuid-moved", project=None)
    assert linear_comment(dict(COMMENT)) == {"ok": False, "error": "OUT_OF_SCOPE"}
    assert ws.writes() == []
    assert _requests(adapter) == 0  # taken, and no new request stored


def test_mode_deny_refuses(pin_ctx, ws, adapter):
    _mode(adapter, "deny")
    assert linear_comment(dict(COMMENT)) == {"ok": False, "error": "OUT_OF_SCOPE"}
    assert linear_issue_create({"title": "x", "parent": "OTH-9"}) == {
        "ok": False, "error": "OUT_OF_SCOPE"}
    assert ws.writes() == [] and _requests(adapter) == 0


def test_mode_allow_writes_without_request(pin_ctx, ws, adapter):
    _mode(adapter, "allow")
    out = linear_comment(dict(COMMENT))
    assert out["ok"] is True
    assert [c["body"] for c in _comments_on(ws, "uuid-other")] == ["hello there"]
    assert linear_issue_update({"id": "OTH-9", "title": "Renamed"})["ok"] is True
    assert ws.issues["uuid-other"]["title"] == "Renamed"
    assert _requests(adapter) == 0


def test_no_settings_defaults_to_approve(pin_ctx, ws, adapter):
    adapter._settings = None
    assert linear_comment(dict(COMMENT))["error"] == "APPROVAL_REQUIRED"
    assert _requests(adapter) == 1 and ws.writes() == []


def test_no_store_refuses_out_of_scope(pin_ctx, ws, adapter):
    adapter._store = None
    assert linear_comment(dict(COMMENT)) == {"ok": False, "error": "OUT_OF_SCOPE"}
    assert ws.writes() == []


def test_undigestable_args_refused_without_request(pin_ctx, ws, adapter):
    args = {**COMMENT, "extra": object()}  # not JSON: no digest, so no request
    assert linear_comment(args) == {"ok": False, "error": "OUT_OF_SCOPE"}
    assert ws.writes() == [] and _requests(adapter) == 0


def test_not_linear_session_never_gated(pin_ctx, ws, adapter):
    pin_ctx(linear=False)
    assert linear_comment(dict(COMMENT)) == {"ok": False, "error": "NOT_LINEAR_SESSION"}
    assert ws.writes() == [] and _requests(adapter) == 0


def test_scope_error_code_not_gated(pin_ctx, ws, adapter, monkeypatch):
    async def limited(query, variables=None):
        raise LinearError("slow down", code="RATELIMITED")
    monkeypatch.setattr(ws, "gql", limited)
    assert linear_comment(dict(COMMENT)) == {"ok": False, "error": "RATELIMITED"}
    assert ws.writes() == [] and _requests(adapter) == 0


def test_sync_and_task_state_still_out_of_scope(pin_ctx, ws, adapter):
    assert linear_task_state({"id": "OTH-9", "state": "in_progress"}) == {
        "ok": False, "error": "OUT_OF_SCOPE"}
    assert ws.writes() == [] and _requests(adapter) == 0


@pytest.mark.parametrize("name", ["linear_comment", "linear_issue_update",
                                  "linear_issue_create"])
def test_gated_schemas_describe_approval(name):
    desc = TOOL_SCHEMAS[name]["description"]
    assert "APPROVAL_REQUIRED" in desc and "identical arguments" in desc
    assert "Only the session issue" not in desc
