"""RET-94 Task 1 feasibility gate: a plugin can answer a gateway exec approval from inside its
own approval-prompt callback, with the public ``tools.approval.resolve_gateway_approval_card``.

Production shape, modelled here: the agent thread runs the real command guard
(``check_all_command_guards``), which enqueues the request and calls the registered gateway
notify callback on that same thread. The callback (like ``GatewayRunner._render_approval_sync``)
schedules ``adapter.send_exec_approval`` on the gateway's event loop and blocks on the future.
The base ``send_exec_approval`` lifts the request's Deny-only mark, then calls the adapter's
``_send_exec_approval_prompt`` -> ``Outbound.approval_prompt``. That is where the Task 8 grant
lives: instead of posting a select, it resolves the fresh request with the ``request_id`` it was
handed, while the agent thread is still inside the notify call.
"""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import patch

from test_adapter_inbound import FakeClient, make_settings  # noqa: F401  (registers the platform)
from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult

from hermes_linear.adapter import LinearAdapter
from hermes_linear.store import Store

SK = "agent:main:linear:issue:ret94-t1"
ACTOR = "linear-user-1"
STRANGER = "linear-user-2"
CHAT = "issue-1:1"


class GrantingOutbound:
    """Stands in for ``Outbound``: ``approval_prompt`` answers the request instead of posting."""

    def __init__(self):
        self.seen: list[str] = []
        self.stranger_resolved: int | None = None
        self.resolved: int | None = None

    async def approval_prompt(self, prompt) -> SendResult:
        from tools.approval import resolve_gateway_approval_card

        rid = prompt.request_id
        self.seen.append(rid)
        # The actor check is live (approvers narrowed to []): someone else cannot take the grant.
        self.stranger_resolved = resolve_gateway_approval_card(SK, "once", rid, actor_id=STRANGER)
        self.resolved = resolve_gateway_approval_card(SK, "once", rid, actor_id=ACTOR)
        return SendResult(success=True, message_id=f"approval:{rid}")


def test_resolve_from_inside_notify_unblocks_waiter():
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools import approval
    from tools.approval_context import reset_current_session_key, set_current_session_key

    adapter = LinearAdapter(PlatformConfig(enabled=True), settings=make_settings(),
                            client=FakeClient([]), store=Store.open(":memory:"))
    fake = GrantingOutbound()
    adapter.outbound = fake

    loop = asyncio.new_event_loop()  # the gateway loop
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()

    notified: list[str] = []

    def notify(data: dict) -> None:
        # Runs on the agent thread, like GatewayRunner._approval_notify_sync -> _render_approval_sync.
        notified.append(data["request_id"])
        fut = asyncio.run_coroutine_threadsafe(adapter.send_exec_approval(
            chat_id=CHAT, command=data.get("command", ""), session_key=SK,
            description=data.get("description"), metadata=None,
            allow_permanent=data.get("allow_permanent", True),
            allow_session=data.get("allow_session", True),
            smart_denied=data.get("smart_denied", False),
            request_id=data["request_id"]), loop)
        assert fut.result(timeout=15).success

    out: dict = {}

    def agent_turn() -> None:
        tokens = set_session_vars(platform="linear", session_key=SK, chat_id=CHAT,
                                  chat_type="dm", user_id=ACTOR)
        token = set_current_session_key(SK)
        try:
            out["result"] = approval.check_all_command_guards("rm -rf /tmp/ret94-t1", "local")
        finally:
            reset_current_session_key(token)
            clear_session_vars(tokens)

    config = {"mode": "manual", "timeout": 30, "approvers": []}
    approval.register_gateway_notify(SK, notify)
    try:
        with patch.object(approval, "_YOLO_MODE_FROZEN", False), \
                patch("tools.approval_context._get_approval_mode", return_value="manual"), \
                patch("tools.approval_context._get_approval_config", return_value=config):
            started = time.monotonic()
            worker = threading.Thread(target=agent_turn)
            worker.start()
            worker.join(2.0)
            elapsed = time.monotonic() - started
            assert not worker.is_alive(), "the agent wait did not end within 2s of the grant"
    finally:
        approval.unregister_gateway_notify(SK)  # wakes the worker if it is still blocked
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(5)
        loop.close()

    assert len(notified) == 1 and notified[0]
    assert fake.seen == notified  # the prompt callback saw the request_id the waiter enqueued
    assert fake.stranger_resolved == 0
    assert fake.resolved == 1
    result = out["result"]
    assert result["approved"] is True, result
    assert result.get("user_approved") is True
    assert elapsed < 2.0
    assert approval.list_gateway_approvals(SK) == []
