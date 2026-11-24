"""Tool hooks: ``pre_tool_call`` / ``post_tool_call`` -> Linear ``action`` activities (spec §8.2).

Contract (integration notes Q2): the hooks fire for gateway-session tools with the session
ContextVars bound, on a tool worker thread or a hook timeout worker, and ``pre_tool_call``
fails closed on timeout. So a hook only reads the session vars (in its own thread, where they
are bound), copies the event, and hands it to each connected adapter's loop with
``loop.call_soon_threadsafe``. Store lookups, adapter selection and Linear writes all happen
on the loop; nothing here blocks or does I/O.

Adapter selection: when ``HERMES_SESSION_KEY`` is bound, the adapter with an in-flight turn
whose gateway session key (the exact key the base computed at hand-off,
``TurnEngine.turn_for_session_key``) equals it owns the event. The key's profile namespace is
never derived here: under ``multiplex_profiles`` the primary adapter's keys carry the launch
profile name, not ``_owner_profile``. If no adapter claims the key (no key bound, or the turn
already ended), the fallback is the adapter whose store has
``turn(HERMES_SESSION_MESSAGE_ID)``, else the one with
``latest_turn_for_chat(HERMES_SESSION_CHAT_ID, HERMES_SESSION_THREAD_ID)``, skipping a turn
that is in flight under a different known key, so two profiles serving the same chat never
cross.

The ``clarify`` tool posts no action at all (neither phase): its elicitation is its rendering,
and anything posted after the elicitation would hide Linear's select input. The Tool Search
bridge normally unwraps ``tool_call`` to the real name before hooks run; a call it leaves
wrapped (scope / probe block) is recognised by its ``calls`` entries.

``transform_tool_result`` (RET-94 spec §3) is the exception to "no store access here": it must
answer synchronously, so it reads (and, for a clarify answered elsewhere, drops) control rows
on the calling thread. ``Store`` is thread-safe; there is still no network I/O or loop hop.

Out-of-scope write approval (RET-119 spec §2.1 steps 3-6, §2.5) reads and writes the store
the same way: for a gated write with a stored approval request, ``pre_tool_call`` arms it and
returns Hermes's ``approve`` directive, and ``post_tool_call`` disarms it. A denial or timeout
leaves Hermes's own block result: Hermes does not run ``transform_tool_result`` for pre-hook
blocks (Ruling R9). Every failure gives None: without a directive the handler just refuses
again with ``APPROVAL_REQUIRED``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from gateway.session_context import get_session_env

from . import adapter as adapter_mod
from . import tool_approval
from .questions import APPROVAL_PENDING_TEXT, PENDING_NOTICE

log = logging.getLogger(__name__)

PLATFORM = "linear"
_SILENT_TOOLS = frozenset({"clarify"})  # rendered as an elicitation by outbound.clarify
_BRIDGE_TOOL = "tool_call"  # tools/tool_search_catalog.py TOOL_CALL_NAME


def _silent(tool_name: Any, args: Any) -> bool:
    """True for ``clarify``, directly or wrapped in a ``tool_call`` bridge call."""
    if tool_name in _SILENT_TOOLS:
        return True
    if tool_name != _BRIDGE_TOOL or not isinstance(args, dict):
        return False
    calls = args.get("calls")
    if calls is None:
        calls = [args]
    elif isinstance(calls, str):
        try:
            calls = json.loads(calls)
        except ValueError:
            return False
    if isinstance(calls, dict):
        calls = [calls]
    names = [str(c.get("name") or "").strip() for c in calls if isinstance(c, dict)] \
        if isinstance(calls, list) else []
    return bool(names) and all(n in _SILENT_TOOLS for n in names)


def _bridged_call(tool_name: Any, args: Any) -> tuple[Any, Any]:
    """``(name, args)`` of the real call: a ``tool_call`` bridge wrapping exactly one call
    (``calls`` parsed as in :func:`_silent`) is unwrapped; a wrapped batch gives
    ``(None, None)``; anything else is returned as is."""
    if tool_name != _BRIDGE_TOOL or not isinstance(args, dict):
        return tool_name, args
    calls = args.get("calls")
    if calls is None:
        calls = [args]
    elif isinstance(calls, str):
        try:
            calls = json.loads(calls)
        except ValueError:
            return None, None
    if isinstance(calls, dict):
        calls = [calls]
    if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
        return None, None
    inner = calls[0].get("arguments")
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except ValueError:
            return None, None
    return str(calls[0].get("name") or "").strip(), inner if inner is not None else {}


def _gated_request(tool_name: Any, args: Any) -> tuple | None:
    """``(store, session_key, tool, digest, request)`` for a gated tool call whose approval
    request is stored in some adapter's store under the bound session key, else None.

    Any platform (RET-148 spec §2 item 5): a request is found only by ``(session key,
    digest)``, and Hermes session keys of different platforms never collide, so a Slack
    request can't be armed from a Linear session or the reverse."""
    session_key = get_session_env("HERMES_SESSION_KEY") or None
    if not session_key:
        return None
    tool, raw = _bridged_call(tool_name, args)
    if tool not in tool_approval.GATED_TOOLS:
        return None
    try:
        d = tool_approval.digest(tool, raw)
    except TypeError:  # args that are not JSON: the handler never stored a request
        return None
    for ad in list(adapter_mod.ADAPTERS.values()):
        store = getattr(ad, "_store", None)
        if store is None:
            continue
        req = store.request(session_key, d)
        if req is not None:
            return store, session_key, tool, d, req
    return None


def _approve_directive(tool_name: Any, args: Any) -> dict | None:
    """Arm the stored request and return Hermes's ``approve`` directive (spec §2.1 step 3)."""
    found = _gated_request(tool_name, args)
    if found is None:
        return None
    store, session_key, tool, d, _req = found
    req = store.arm_request(session_key, d)
    if req is None:  # dropped meanwhile
        return None
    return {"action": "approve", "message": req.message,
            "rule_key": tool_approval.rule_key(tool, d)}


def _session(kw: dict) -> dict | None:
    """The bound Linear session vars, or None when this is not a Linear session."""
    if get_session_env("HERMES_SESSION_PLATFORM") != PLATFORM:
        return None
    return {
        "session_key": get_session_env("HERMES_SESSION_KEY") or None,
        "message_id": get_session_env("HERMES_SESSION_MESSAGE_ID") or None,
        "chat_id": get_session_env("HERMES_SESSION_CHAT_ID") or None,
        "thread_id": get_session_env("HERMES_SESSION_THREAD_ID") or None,
        "session_id": kw.get("session_id") or get_session_env("HERMES_SESSION_ID") or None,
    }


def _copy_args(args: Any) -> Any:
    return dict(args) if isinstance(args, dict) else args


def _route(adapters: list, phase: str, env: dict, ev: dict) -> None:
    """On the loop: pick the owning adapter (session key, then message id, then chat)."""
    session_key = env.get("session_key")
    if session_key:
        for ad in adapters:
            try:
                target = ad.outbound.tool_target_for_key(session_key)
            except Exception as exc:  # noqa: BLE001
                log.debug("linear: tool hook key lookup failed: %s", type(exc).__name__)
                continue
            if target is not None:
                ad.outbound.tool_event(phase, target, ev)
                return
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


def on_pre_tool_call(tool_name: str, args: Any = None, task_id: Any = None,
                     **kw: Any) -> dict | None:
    """Schedules the start action; for a gated write with a stored approval request, arms
    it and returns Hermes's ``approve`` directive so a human is asked. Otherwise None.
    Any failure gives None (fail-open is safe: the handler then refuses again with
    ``APPROVAL_REQUIRED``)."""
    try:
        env = None if _silent(tool_name, args) else _session(kw)
        if env is not None:
            _schedule("start", env, {"tool_name": tool_name, "args": _copy_args(args),
                                     "task_id": task_id, "tool_call_id": kw.get("tool_call_id"),
                                     "session_id": env["session_id"]})
    except Exception as exc:  # noqa: BLE001 - a hook must never break a tool call
        log.debug("linear: pre_tool_call hook failed: %s", type(exc).__name__)
    try:
        return _approve_directive(tool_name, args)
    except Exception as exc:  # noqa: BLE001
        log.debug("linear: pre_tool_call approval lookup failed: %s", type(exc).__name__)
        return None


def on_post_tool_call(tool_name: str, args: Any = None, result: Any = None, task_id: Any = None,
                      duration_ms: int = 0, **kw: Any) -> None:
    try:
        env = None if _silent(tool_name, args) else _session(kw)
        if env is not None:
            _schedule("finish", env, {
                "tool_name": tool_name, "args": _copy_args(args),
                "result": result if isinstance(result, str) or result is None else str(result),
                "task_id": task_id, "tool_call_id": kw.get("tool_call_id"),
                "status": kw.get("status"), "duration_ms": duration_ms,
                "session_id": env["session_id"]})
    except Exception as exc:  # noqa: BLE001
        log.debug("linear: post_tool_call hook failed: %s", type(exc).__name__)
    try:
        found = _gated_request(tool_name, args)
        if found is not None:  # spec §2.1 step 6: a later hook-less call can't use it
            store, session_key, _tool, d, _req = found
            store.disarm_request(session_key, d)
    except Exception as exc:  # noqa: BLE001
        log.debug("linear: post_tool_call disarm failed: %s", type(exc).__name__)
    return None


# --------------------------------------------------- transform_tool_result (RET-94 spec §3)

_APPROVAL_TIMEOUT_MARK = "timed out without user response"  # tools/approval.py cli_timeout
_NOT_ANSWERED = frozenset({"timed_out", "cancelled", "undelivered"})


def _turn_controls(session_key: str, kind: str) -> list:
    """``(store, row)`` for every ``kind`` control row asked during THIS turn: same session
    key and ``row.turn_mid == HERMES_SESSION_MESSAGE_ID`` (outbound stores the asking turn's
    mid; the turn's MessageEvent binds the same id). Several Linear agent sessions can share
    one Hermes key, so the key alone would touch another session's question. Unbound
    message id -> no rows (act on nothing)."""
    mid = get_session_env("HERMES_SESSION_MESSAGE_ID") or None
    if not mid:
        return []
    found: list = []
    for ad in list(adapter_mod.ADAPTERS.values()):
        store = getattr(ad, "_store", None)
        if store is None:
            continue
        for row in store.controls_for_session_key(session_key):
            if row.kind == kind and row.turn_mid == mid:
                found.append((store, row))
    return found


def _strings(value: Any):
    """Every string inside ``value`` (dict values and list items, recursively)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def _this_question(rows: list, data: dict) -> tuple | None:
    """The ``(store, row)`` this clarify result answers: the row whose question is one of
    the result's ``responses[*].question``; else the newest of this turn's rows."""
    if not rows:
        return None
    responses = data.get("responses")
    asked = {r.get("question") for r in responses if isinstance(r, dict)} \
        if isinstance(responses, list) else set()
    for pair in rows:
        if pair[1].question is not None and pair[1].question in asked:
            return pair
    return max(rows, key=lambda pair: pair[1].created_at)


def _clarify_result(session_key: str, result: Any) -> str | None:
    if not isinstance(result, str):
        return None
    try:
        data = json.loads(result)
    except ValueError:
        return None
    if not isinstance(data, dict) or "outcome" not in data:
        return None
    outcome = data.get("outcome")
    if outcome in _NOT_ANSWERED and outcome != "timed_out":
        return None
    pair = _this_question(_turn_controls(session_key, "clarify"), data)
    if pair is None:
        return None
    if outcome != "timed_out":
        # Answered outside the plugin's own path (e.g. the gateway text intercept): the
        # kept row would stay pending forever. Drop only this question's row.
        store, row = pair
        store.drop_control(row.request_id)
        return None
    data["outcome"] = "pending"
    data["notice"] = PENDING_NOTICE
    return json.dumps(data, ensure_ascii=False)


def _approval_result(session_key: str, args: Any, result: Any) -> str | None:
    if not isinstance(result, str) or _APPROVAL_TIMEOUT_MARK not in result:
        return None
    values = list(_strings(args))
    for _store, row in _turn_controls(session_key, "approval"):
        if row.command and any(row.command in v for v in values):
            return APPROVAL_PENDING_TEXT
    return None


def on_transform_tool_result(tool_name: str, args: Any = None, result: Any = None,
                             **kw: Any) -> str | None:
    """In a Linear session, a timed-out clarify / approval whose question is still stored reads
    as pending, not as a timeout; a clarify answered elsewhere drops its stale row. Only
    rows asked during this turn (``turn_mid``) are considered. Runs on
    the agent thread: store access only, no network I/O or loop hop; any failure leaves the
    result untouched (None)."""
    try:
        if get_session_env("HERMES_SESSION_PLATFORM") != PLATFORM:
            return None
        session_key = get_session_env("HERMES_SESSION_KEY") or None
        if not session_key:
            return None
        if _silent(tool_name, args):
            return _clarify_result(session_key, result)
        return _approval_result(session_key, args, result)
    except Exception as exc:  # noqa: BLE001 - fail open: never alter a result on error
        log.debug("linear: transform_tool_result hook failed: %s", type(exc).__name__)
        return None


__all__ = ["on_post_tool_call", "on_pre_tool_call", "on_transform_tool_result"]
