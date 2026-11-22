"""Tool hooks: ``pre_tool_call`` / ``post_tool_call`` -> Linear ``action`` activities (spec §8.2).

Contract (integration notes Q2): the hooks fire for gateway-session tools with the session
ContextVars bound, on a tool worker thread or a hook timeout worker, and ``pre_tool_call``
fails closed on timeout. So a hook only reads the session vars (in its own thread, where they
are bound), copies the event, and hands it to each connected adapter's loop with
``loop.call_soon_threadsafe``. Store lookups, adapter selection and Linear writes all happen
on the loop; nothing here blocks or does I/O.

Adapter selection (Task 12 brief): the adapter whose store has ``turn(HERMES_SESSION_MESSAGE_ID)``,
else the one with ``latest_turn_for_chat(HERMES_SESSION_CHAT_ID, HERMES_SESSION_THREAD_ID)``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from gateway.session_context import get_session_env

from . import adapter as adapter_mod

log = logging.getLogger(__name__)

PLATFORM = "linear"


def _session(kw: dict) -> dict | None:
    """The bound Linear session vars, or None when this is not a Linear session."""
    if get_session_env("HERMES_SESSION_PLATFORM") != PLATFORM:
        return None
    return {
        "message_id": get_session_env("HERMES_SESSION_MESSAGE_ID") or None,
        "chat_id": get_session_env("HERMES_SESSION_CHAT_ID") or None,
        "thread_id": get_session_env("HERMES_SESSION_THREAD_ID") or None,
        "session_id": kw.get("session_id") or get_session_env("HERMES_SESSION_ID") or None,
    }


def _copy_args(args: Any) -> Any:
    return dict(args) if isinstance(args, dict) else args


def _route(adapters: list, phase: str, env: dict, ev: dict) -> None:
    """On the loop: pick the owning adapter (message id first, then chat) and render."""
    for by_message in (True, False):
        for ad in adapters:
            try:
                target = ad.outbound.tool_target(env, by_message=by_message)
            except Exception as exc:  # noqa: BLE001 - e.g. store closed mid-disconnect
                log.debug("linear: tool hook lookup failed: %s", type(exc).__name__)
                continue
            if target is not None:
                ad.outbound.tool_event(phase, target, ev)
                return
    log.debug("linear: tool event for an unknown turn dropped")


def _schedule(phase: str, env: dict, ev: dict) -> None:
    groups: dict[asyncio.AbstractEventLoop, list] = {}
    for ad in list(adapter_mod.ADAPTERS.values()):
        loop = getattr(ad, "event_loop", None)
        if loop is not None and not loop.is_closed():
            groups.setdefault(loop, []).append(ad)
    for loop, adapters in groups.items():
        try:
            loop.call_soon_threadsafe(_route, adapters, phase, env, ev)
        except RuntimeError:  # loop closed between the check and the call
            pass


def on_pre_tool_call(tool_name: str, args: Any = None, task_id: Any = None, **kw: Any) -> None:
    """Observer only: always returns None (never blocks or approves a tool)."""
    try:
        env = _session(kw)
        if env is not None:
            _schedule("start", env, {"tool_name": tool_name, "args": _copy_args(args),
                                     "task_id": task_id, "tool_call_id": kw.get("tool_call_id"),
                                     "session_id": env["session_id"]})
    except Exception as exc:  # noqa: BLE001 - a hook must never break a tool call
        log.debug("linear: pre_tool_call hook failed: %s", type(exc).__name__)
    return None


def on_post_tool_call(tool_name: str, args: Any = None, result: Any = None, task_id: Any = None,
                      duration_ms: int = 0, **kw: Any) -> None:
    try:
        env = _session(kw)
        if env is not None:
            _schedule("finish", env, {
                "tool_name": tool_name, "args": _copy_args(args),
                "result": result if isinstance(result, str) or result is None else str(result),
                "task_id": task_id, "tool_call_id": kw.get("tool_call_id"),
                "status": kw.get("status"), "duration_ms": duration_ms,
                "session_id": env["session_id"]})
    except Exception as exc:  # noqa: BLE001
        log.debug("linear: post_tool_call hook failed: %s", type(exc).__name__)
    return None


__all__ = ["on_post_tool_call", "on_pre_tool_call"]
