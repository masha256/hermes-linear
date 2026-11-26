"""RET-180: the asking-time approval authority, captured with public Hermes calls only.

Real path: ``tools.approval.check_all_command_guards`` enqueues a real pending approval and calls
the gateway notify on the agent thread, which is where the adapter's ``approval_prompt`` takes the
snapshot (Hermes still holds the entry). Inside notify this test runs the plugin's real
``_approval_authority``, then checks ``_may_answer`` and the refusal wording against what Hermes's
own rule gives for each ``approvals.approvers`` shape. Stand-in seams: the notify callback (instead
of the gateway runner), the approval mode/config patches, and ``load_config_readonly`` returning
the same ``approvals`` block Hermes reads.
"""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

from hermes_linear import sessions_open as sessions_open_mod

SK = "agent:main:linear:issue:ret180-e2e"
ALICE, BOB, EVE = "u-alice", "u-bob", "u-eve"
ADMINS_ONLY = "Only an admin or a listed approver can answer this approval."


def _snapshot(approvers, user_id):
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools import approval
    from tools.approval_context import reset_current_session_key, set_current_session_key

    out: dict = {}

    def notify(data):
        rid = data["request_id"]
        out["authority"] = sessions_open_mod._approval_authority(SK, rid, user_id or ALICE)
        approval.resolve_gateway_approval(SK, "deny", request_id=rid)

    def turn():
        kw = dict(platform="linear", session_key=SK, chat_id="c", chat_type="dm")
        if user_id:  # no user: a turn merged from two senders has no requester
            kw.update(user_id=user_id, user_name="Alice")
        tokens = set_session_vars(**kw)
        token = set_current_session_key(SK)
        try:
            approval.check_all_command_guards("rm -rf /tmp/ret180-e2e", "local")
        finally:
            reset_current_session_key(token)
            clear_session_vars(tokens)

    cfg = {"mode": "manual", "timeout": 10, "approvers": approvers}
    approval.register_gateway_notify(SK, notify)
    try:
        with patch.object(approval, "_YOLO_MODE_FROZEN", False), \
                patch("tools.approval_context._get_approval_mode", return_value="manual"), \
                patch("tools.approval_context._get_approval_config", return_value=cfg), \
                patch("hermes_cli.config.load_config_readonly",
                      return_value={"approvals": cfg}):
            worker = threading.Thread(target=turn)
            worker.start()
            worker.join(5)
            assert not worker.is_alive()
    finally:
        approval.unregister_gateway_notify(SK)
    return out["authority"]


@pytest.mark.parametrize("approvers,user,expect,refusal", [
    (["*"], ALICE, (True, True, True), None),
    ([], ALICE, (True, False, False), "Only Alice can answer this approval."),
    ([BOB], ALICE, (True, True, False), "Only Alice can answer this approval."),
    ([BOB], None, (False, True, False), ADMINS_ONLY),
    ([], None, (False, False, False), ADMINS_ONLY),
])
def test_snapshot_matches_hermes_rule(approvers, user, expect, refusal):
    from gateway.approval_actor import refusal_text

    authority = _snapshot(approvers, user)
    assert authority is not None
    got = tuple(sessions_open_mod._may_answer(authority, u) for u in (ALICE, BOB, EVE))
    assert got == expect
    if refusal is not None:
        assert refusal_text(sessions_open_mod._requester_of(authority)) == refusal


def test_no_pending_entry_with_narrowed_config_is_not_open():
    """Nothing pending: ``approval_refusal`` passes everyone, so a narrowed config must not
    turn that into an open snapshot."""
    with patch("hermes_cli.config.load_config_readonly",
               return_value={"approvals": {"approvers": []}}):
        assert sessions_open_mod._approval_authority(SK, "no-such-request", ALICE) is None
