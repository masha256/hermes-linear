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


def test_out_of_scope_reply_needs_approval_writes_nothing(pin_ctx, ws, adapter):
    """RET-121 §1.5: a thread reply on an out-of-scope issue asks first (comment check
    included), posts and resolves only on the approved pass, and only once."""
    top = ws.add_comment("OTH-9", "question?", user="human-1")
    args = {"id": "OTH-9", "body": "answer", "reply_to": top, "resolve": True}
    out = linear_comment(dict(args))
    assert out["error"] == "APPROVAL_REQUIRED" and ws.writes() == []
    dg = digest("linear_comment", args)
    assert adapter._store.request("sk", dg).targets == ["uuid-other"]
    assert adapter._store.arm_request("sk", dg) is not None
    out = linear_comment(dict(args))
    assert out["ok"] is True and out["thread_id"] == top and out["resolved"] is True
    assert [w[0] for w in ws.writes()] == ["comment_create", "comment_resolve"]
    assert linear_comment(dict(args))["error"] == "APPROVAL_REQUIRED"
    assert len(ws.writes()) == 2


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


# ----------------------------------------------------- outside a Linear session (RET-148)


def _outside(adapter, mode):
    adapter._settings = dataclasses.replace(adapter._settings, outside_session_writes=mode)


IN_TREE = {"id": "RET-2", "body": "hi"}  # in scope inside RET-1's session


def test_outside_comment_first_call_requires_approval(pin_ctx, ws, adapter):
    pin_ctx(linear=False)
    out = linear_comment(dict(IN_TREE))  # outside, every target is out of scope
    assert out["ok"] is False and out["error"] == "APPROVAL_REQUIRED"
    assert "outside a Linear session" in out["message"] and "RET-2" in out["message"]
    assert "linear_comment" in out["message"]
    assert ws.writes() == [] and _requests(adapter) == 1
    req = adapter._store.request("sk", digest("linear_comment", IN_TREE))
    assert req.targets == ["uuid-child"] and req.tool == "linear_comment"
    assert req.message.splitlines()[0] == (
        "Approve `linear_comment` from outside a Linear session?")


def test_outside_armed_request_writes_once(pin_ctx, ws, adapter):
    pin_ctx(linear=False)
    dg = digest("linear_comment", IN_TREE)
    assert linear_comment(dict(IN_TREE))["error"] == "APPROVAL_REQUIRED"
    assert adapter._store.arm_request("sk", dg) is not None
    out = linear_comment(dict(IN_TREE))
    assert out["ok"] is True and out["issue"] == "RET-2"
    assert [c["body"] for c in _comments_on(ws, "uuid-child")] == ["hi"]
    assert adapter._store.request("sk", dg) is None  # consumed
    assert linear_comment(dict(IN_TREE))["error"] == "APPROVAL_REQUIRED"
    assert len(_comments_on(ws, "uuid-child")) == 1


def test_outside_deny_refuses_not_linear_session(pin_ctx, ws, adapter):
    pin_ctx(linear=False)
    _outside(adapter, "deny")
    assert linear_comment(dict(IN_TREE)) == {"ok": False, "error": "NOT_LINEAR_SESSION"}
    assert linear_issue_update({"id": "RET-2", "title": "x"}) == {
        "ok": False, "error": "NOT_LINEAR_SESSION"}
    assert ws.writes() == [] and _requests(adapter) == 0


def test_outside_allow_writes_without_request(pin_ctx, ws, adapter):
    pin_ctx(linear=False)
    _outside(adapter, "allow")
    out = linear_comment(dict(IN_TREE))
    assert out["ok"] is True
    assert [c["body"] for c in _comments_on(ws, "uuid-child")] == ["hi"]
    assert _requests(adapter) == 0


def test_outside_no_session_key_refused(pin_ctx, ws, adapter):
    pin_ctx(linear=False, key=None)  # approve mode, but nobody can be asked
    assert linear_comment(dict(IN_TREE)) == {"ok": False, "error": "OUT_OF_SCOPE"}
    assert ws.writes() == [] and _requests(adapter) == 0


def test_knobs_are_independent(pin_ctx, ws, adapter):
    _mode(adapter, "allow")
    _outside(adapter, "deny")
    pin_ctx(linear=False)
    assert linear_comment(dict(IN_TREE)) == {"ok": False, "error": "NOT_LINEAR_SESSION"}
    pin_ctx()
    assert linear_comment(dict(COMMENT))["ok"] is True  # inside OTH-9: written
    assert len(_comments_on(ws, "uuid-other")) == 1
    _mode(adapter, "deny")
    _outside(adapter, "allow")
    pin_ctx(linear=False)
    assert linear_comment(dict(IN_TREE))["ok"] is True
    pin_ctx()
    assert linear_comment(dict(COMMENT)) == {"ok": False, "error": "OUT_OF_SCOPE"}
    assert len(_comments_on(ws, "uuid-other")) == 1 and _requests(adapter) == 0


def test_outside_update_both_ends_are_targets(pin_ctx, ws, adapter):
    ws.add_issue("RET-3", title="Sibling", uuid="uuid-sib", parent=SELF)
    pin_ctx(linear=False)
    args = {"id": "RET-2", "parent": "RET-3"}
    out = linear_issue_update(dict(args))
    assert out["error"] == "APPROVAL_REQUIRED"
    assert "RET-2" in out["message"] and "RET-3" in out["message"]
    assert ws.writes() == [] and _requests(adapter) == 1
    req = adapter._store.request("sk", digest("linear_issue_update", args))
    assert set(req.targets) == {"uuid-child", "uuid-sib"}
    adapter._store.arm_request("sk", req.digest)
    assert linear_issue_update(dict(args))["ok"] is True
    assert ws.issues["uuid-child"]["parent"] == "uuid-sib"


def test_linear_platform_without_ticket_still_refused(pin_ctx, ws, adapter):
    pin_ctx(linear=True, issue=None)  # a Linear call whose turn can't be found: not outside
    assert linear_comment(dict(IN_TREE)) == {"ok": False, "error": "NOT_LINEAR_SESSION"}
    assert ws.writes() == [] and _requests(adapter) == 0


def test_session_only_tools_refuse_outside(pin_ctx, ws, adapter):
    pin_ctx(linear=False)  # the session key is bound this time
    assert linear_task_state({"id": "RET-2", "state": "in_progress"}) == {
        "ok": False, "error": "NOT_LINEAR_SESSION"}
    assert linear_issue_create({"title": "x"}) == {"ok": False, "error": "NOT_LINEAR_SESSION"}
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


# ------------------------------------- team / project approval targets (RET-121 T4)

ORION = "proj-orion"


def _creates(ws):
    return [c[1] for c in ws.calls if c[0] == "issue_create"]


def test_create_other_project_needs_approval(pin_ctx, ws, adapter):
    ws.add_project(ORION, "orion")
    args = {"title": "Elsewhere", "project": "orion"}
    out = linear_issue_create(dict(args))
    assert out["ok"] is False and out["error"] == "APPROVAL_REQUIRED"
    assert "orion" in out["message"] and "linear_issue_create" in out["message"]
    assert _creates(ws) == [] and ws.writes() == []
    req = adapter._store.request("sk", digest("linear_issue_create", args))
    assert req.targets == [ORION]
    assert '\n- project "orion"\n' in req.message
    _mode(adapter, "deny")
    assert linear_issue_create(dict(args)) == {"ok": False, "error": "OUT_OF_SCOPE"}
    assert ws.writes() == []


def test_create_other_team_needs_approval(pin_ctx, ws, adapter):
    ws.add_team("team-2", "OPS", "Operations")
    args = {"title": "Ops work", "team": "operations", "state": "Todo", "labels": ["bug"]}
    out = linear_issue_create(dict(args))
    assert out["error"] == "APPROVAL_REQUIRED" and "OPS" in out["message"]
    assert ws.writes() == []
    req = adapter._store.request("sk", digest("linear_issue_create", args))
    assert req.targets == ["team-2"] and '\n- team "OPS"\n' in req.message
    # state and labels resolved in the resulting team
    assert ("team_states", "team-2") in ws.calls and ("team_labels", "team-2") in ws.calls
    adapter._store.arm_request("sk", req.digest)
    out = linear_issue_create(dict(args))
    assert out["ok"] is True
    assert ws.issues[out["issue"]["id"]]["team"] == "team-2"
    # R8: another team without a project creates the issue with no project
    [inp] = _creates(ws)
    assert "projectId" not in inp and ws.issues[out["issue"]["id"]]["project"] is None


def test_create_other_team_milestone_needs_its_project(pin_ctx, ws, adapter):
    """R8: a milestone resolves in the resulting project; another team without a project
    has none, so the base project's milestone is refused, before any approval."""
    ws.add_team("team-2", "OPS", "Operations")
    ws.milestones_rows["proj-1"] = [{"id": "ms-1", "name": "Alpha"}]
    out = linear_issue_create({"title": "x", "team": "OPS", "milestone": "alpha"})
    assert out == {"ok": False, "error": "MILESTONE_NOT_FOUND", "candidates": []}
    assert ws.writes() == [] and _requests(adapter) == 0
    # with an explicit project the milestone resolves there
    ws.add_project(ORION, "orion")
    ws.milestones_rows[ORION] = [{"id": "ms-o", "name": "Alpha"}]
    args = {"title": "x", "team": "OPS", "project": "orion", "milestone": "alpha"}
    assert linear_issue_create(dict(args))["error"] == "APPROVAL_REQUIRED"
    adapter._store.arm_request("sk", digest("linear_issue_create", args))
    assert linear_issue_create(dict(args))["ok"] is True
    [inp] = _creates(ws)
    assert (inp["teamId"], inp["projectId"], inp["projectMilestoneId"]) == (
        "team-2", ORION, "ms-o")


def test_create_approved_writes_only_approved_target(pin_ctx, ws, adapter):
    ws.add_project(ORION, "orion")
    ws.add_team("team-2", "OPS")
    args = {"title": "Elsewhere", "project": "orion"}
    linear_issue_create(dict(args))
    adapter._store.arm_request("sk", digest("linear_issue_create", args))
    out = linear_issue_create(dict(args))
    assert out["ok"] is True
    [inp] = _creates(ws)
    assert inp["projectId"] == ORION and inp["teamId"] == "team-1"
    assert adapter._store.created_by("uuid-self") == {out["issue"]["id"]}
    # an armed request holding only the project does not cover a team in the same call
    both = {"title": "Both", "project": "orion", "team": "OPS"}
    dg = digest("linear_issue_create", both)
    adapter._store.put_request("sk", dg, "linear_issue_create", [ORION], "msg\ndigest: x")
    adapter._store.arm_request("sk", dg)
    assert linear_issue_create(dict(both)) == {"ok": False, "error": "OUT_OF_SCOPE"}
    assert len(_creates(ws)) == 1
    # the name now resolving to another project: the approved UUID does not cover it
    ws.projects_rows = [p for p in ws.projects_rows if p["id"] != ORION]
    ws.add_project("proj-orion-2", "orion")
    from hermes_linear import directory
    directory._DIRECTORIES.pop(ws, None)  # fresh directory: the rename is visible at once
    linear_issue_create(dict(args))  # first pass again: a new request
    dg = digest("linear_issue_create", args)
    adapter._store.put_request("sk", dg, "linear_issue_create", [ORION], "msg\ndigest: x")
    adapter._store.arm_request("sk", dg)
    assert linear_issue_create(dict(args)) == {"ok": False, "error": "OUT_OF_SCOPE"}
    assert len(_creates(ws)) == 1


def test_update_other_project_needs_approval(pin_ctx, ws, adapter):
    ws.add_project(ORION, "orion")
    args = {"id": "RET-2", "project": "orion"}
    out = linear_issue_update(dict(args))
    assert out["error"] == "APPROVAL_REQUIRED" and "orion" in out["message"]
    assert ws.writes() == []
    req = adapter._store.request("sk", digest("linear_issue_update", args))
    assert req.targets == [ORION] and '\n- project "orion"\n' in req.message
    adapter._store.arm_request("sk", req.digest)
    assert linear_issue_update(dict(args))["ok"] is True
    assert ws.issues["uuid-child"]["project"] == {"id": ORION, "name": "orion"}
    # an out-of-scope issue and another project: both are targets, issue first
    both = {"id": "OTH-9", "project": "orion"}
    out = linear_issue_update(dict(both))
    assert out["error"] == "APPROVAL_REQUIRED"
    req = adapter._store.request("sk", digest("linear_issue_update", both))
    assert req.targets == ["uuid-other", ORION]
    assert '- OTH-9 "Unrelated"\n- project "orion"\n' in req.message


def test_mode_allow_approves_project_target(pin_ctx, ws, adapter):
    ws.add_project(ORION, "orion")
    _mode(adapter, "allow")
    out = linear_issue_create({"title": "Elsewhere", "project": "orion"})
    assert out["ok"] is True and _creates(ws)[0]["projectId"] == ORION
    assert _requests(adapter) == 0
