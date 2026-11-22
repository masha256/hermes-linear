"""Agent tool runtime (RET-80): what every agent-facing Linear tool runs on.

Integration notes Q2/Q3 are the binding host facts:

- Tool handlers are **sync** (``is_async=False``) and run on a tool worker thread with the
  session ContextVars propagated, so ``get_session_env`` reads the turn's session the same way
  ``hooks.py`` does. ``LinearClient`` and its aiohttp session belong to the gateway loop, so
  every Linear call is scheduled onto ``adapter.event_loop`` with
  ``run_coroutine_threadsafe(...).result(timeout)`` (``run_on_adapter``).
- ``PluginContext.dispatch_tool`` can run a handler on the loop thread itself; blocking there
  would deadlock, so ``run_on_adapter`` refuses with ``WRONG_THREAD`` without scheduling.
- Store / TurnEngine state is only touched on the adapter loop: ``resolve_context`` reads
  ``ADAPTERS`` and ``event_loop`` from the tool thread (as ``hooks.py`` does) but asks each
  candidate adapter for the owning turn in a coroutine on that adapter's loop.

Errors never raise out of the runtime; they become ``{"ok": False, "error": CODE}``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from dataclasses import dataclass
from typing import Any, Callable, Coroutine

from gateway.session_context import get_session_env

from . import adapter as adapter_mod
from .constants import TOOL_TIMEOUT_S
from .linear_client import LinearError

log = logging.getLogger(__name__)

PLATFORM = "linear"
SCOPE_PARENT_DEPTH = 5  # parent levels walked by check_write_scope
RESOLVE_TIMEOUT_S = 5.0  # per-adapter turn lookup (pure in-memory/store work on the loop)

QUERY_ISSUE_PARENT = """
query IssueParent($id: String!) {
  issue(id: $id) { id parent { id } }
}
"""


@dataclass
class ToolContext:
    adapter: Any  # LinearAdapter | None
    client: Any  # LinearClient | None
    session_issue_id: str | None
    session_key: str | None
    is_linear: bool


def _err(code: str) -> dict:
    return {"ok": False, "error": code}


def _issue_of(chat_id: Any) -> str | None:
    """The Linear issue id of a Hermes chat id (``<issue>[:<suffix>]``, as adapter.py)."""
    return str(chat_id).split(":", 1)[0] if chat_id else None


def _session() -> dict:
    return {
        "platform": get_session_env("HERMES_SESSION_PLATFORM") or None,
        "session_key": get_session_env("HERMES_SESSION_KEY") or None,
        "message_id": get_session_env("HERMES_SESSION_MESSAGE_ID") or None,
        "chat_id": get_session_env("HERMES_SESSION_CHAT_ID") or None,
        "thread_id": get_session_env("HERMES_SESSION_THREAD_ID") or None,
        "profile": get_session_env("HERMES_SESSION_PROFILE") or None,
    }


def _live_loop(ad: Any) -> asyncio.AbstractEventLoop | None:
    loop = getattr(ad, "event_loop", None) if ad is not None else None
    if loop is None or loop.is_closed() or not loop.is_running():
        return None
    return loop


def _on_loop(loop: asyncio.AbstractEventLoop) -> bool:
    try:
        return asyncio.get_running_loop() is loop
    except RuntimeError:
        return False


async def _lookup(ad: Any, env: dict) -> list[str | None]:
    """On ``ad``'s loop: the issue id this adapter owns for ``env``, by key / message / chat.

    Mirrors ``hooks._route``. Returns one entry per routing rule, ``None`` where it misses.
    """
    out: list[str | None] = [None, None, None]
    key = env.get("session_key")
    if key:
        try:
            t = ad.outbound.tool_target_for_key(key)
            out[0] = _issue_of(t.key[0]) if t is not None else None
        except Exception as exc:  # noqa: BLE001
            log.debug("linear: tool context key lookup failed: %s", type(exc).__name__)
    for i, by_message in ((1, True), (2, False)):
        try:
            t = ad.outbound.tool_target(env, by_message=by_message)
            out[i] = _issue_of(t.key[0]) if t is not None else None
        except Exception as exc:  # noqa: BLE001 - e.g. store closed mid-disconnect
            log.debug("linear: tool context lookup failed: %s", type(exc).__name__)
    return out


def _fallback(registry: dict, profile: str | None) -> Any:
    """The adapter for reads without an owning turn: the session's profile's, else the first.

    The default bot binds ``HERMES_SESSION_PROFILE`` as ``''`` but registers as ``default``.
    """
    profile = (profile or "").strip() or "default"  # as adapter._profile is derived
    if registry.get(profile) is not None:
        return registry[profile]
    return next(iter(registry.values()), None)


def resolve_context(session_id: str | None = None) -> ToolContext:
    """The adapter and Linear session (if any) of the calling tool. Never raises.

    In a Linear session the owning adapter is the one whose in-flight turn has the bound
    ``HERMES_SESSION_KEY``, else the one whose store has the message id, else the chat (the
    ``hooks._route`` order). Outside Linear, or when no turn is found, reads still work on the
    adapter of the session's profile (``HERMES_SESSION_PROFILE``), else the first in
    ``ADAPTERS``, with no session issue (writes are refused). Any unexpected failure yields a
    context with no adapter: ``run_on_adapter`` answers ``NOT_CONNECTED``, writes are refused.
    """
    try:
        return _resolve(session_id)
    except Exception as exc:  # noqa: BLE001 - Q3.4: tools never see a runtime exception
        log.warning("linear: tool context resolution failed: %s", type(exc).__name__)
        return ToolContext(adapter=None, client=None, session_issue_id=None, session_key=None,
                           is_linear=False)


def _resolve(session_id: str | None) -> ToolContext:
    env = _session()
    registry = dict(adapter_mod.ADAPTERS)
    adapters = list(registry.values())
    fallback = _fallback(registry, env["profile"])
    is_linear = env["platform"] == PLATFORM
    log.debug("linear: tool context (session_id=%s, key=%s, linear=%s)",
              session_id, env["session_key"], is_linear)

    def ctx(ad: Any, issue: str | None) -> ToolContext:
        return ToolContext(adapter=ad, client=getattr(ad, "client", None) if ad else None,
                           session_issue_id=issue if is_linear else None,
                           session_key=env["session_key"] if is_linear else None,
                           is_linear=is_linear)

    if not is_linear:
        return ctx(fallback, None)
    live = [(ad, loop) for ad in adapters if (loop := _live_loop(ad)) is not None]
    for ad, loop in live:  # before blocking on any loop
        if _on_loop(loop):
            # Q3 dispatch_tool path: never block the loop; run_on_adapter refuses next.
            return ctx(ad, None)
    found: list[tuple[Any, list[str | None]]] = []
    for ad, loop in live:
        try:
            fut = asyncio.run_coroutine_threadsafe(_lookup(ad, env), loop)
        except Exception as exc:  # noqa: BLE001 - loop closed since the check
            log.debug("linear: tool context lookup on %r not scheduled: %s", ad,
                      type(exc).__name__)
            continue
        try:
            found.append((ad, fut.result(RESOLVE_TIMEOUT_S)))
        except Exception as exc:  # noqa: BLE001 - incl. timeout: that adapter does not claim it
            fut.cancel()
            log.debug("linear: tool context lookup on %r failed: %s", ad, type(exc).__name__)
    for rule in range(3):
        for ad, hits in found:
            if hits[rule]:
                return ctx(ad, hits[rule])
    return ctx(fallback, None)


def run_on_adapter(ctx: ToolContext, coro_factory: Callable[[], Coroutine[Any, Any, Any]], *,
                   timeout_s: float = TOOL_TIMEOUT_S) -> dict:
    """Run ``coro_factory()`` on the adapter's loop from a tool thread; never raises."""
    loop = _live_loop(ctx.adapter)
    if loop is None:
        return _err("NOT_CONNECTED")
    if _on_loop(loop):
        return _err("WRONG_THREAD")  # Q3: .result() here would deadlock the gateway loop
    try:
        coro = coro_factory()
    except Exception as exc:  # noqa: BLE001
        return _err(type(exc).__name__)
    try:
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
    except RuntimeError:  # loop closed between the check and the call
        coro.close()
        return _err("NOT_CONNECTED")
    except Exception as exc:  # noqa: BLE001 - e.g. not a coroutine
        if hasattr(coro, "close"):
            coro.close()
        return _err(type(exc).__name__)
    try:
        result = fut.result(timeout_s)
    except (TimeoutError, concurrent.futures.TimeoutError):
        fut.cancel()
        return _err("TIMEOUT")
    except LinearError as exc:
        return _err(exc.code or "LINEAR_ERROR")
    except concurrent.futures.CancelledError:
        return _err("NOT_CONNECTED")  # task cancelled on the loop: the adapter disconnected
    except Exception as exc:  # noqa: BLE001
        log.debug("linear: tool call failed: %s", type(exc).__name__)
        return _err(type(exc).__name__)
    return result if isinstance(result, dict) else {"ok": True, "result": result}


async def check_write_scope(ctx: ToolContext, issue_id: str) -> str | None:
    """On the loop: ``None`` if the session may write ``issue_id``, else the refusal code.

    Allowed: the session issue, an issue this session created through a tool
    (``adapter.tool_created[session_key]``), or an issue with the session issue among its
    first ``SCOPE_PARENT_DEPTH`` ancestors. Ids may be identifiers or UUIDs; the UUIDs Linear
    returns are compared. Never raises: a ``LinearError`` from the parent walk returns its
    ``code`` (``LINEAR_ERROR`` if none). Fail-closed: any non-``None`` result is a refusal.

    Contract for write tools:
    - Add the UUID Linear returns for an issue you create (``issueCreate.issue.id``), never
      its identifier (``RET-12``), to ``ctx.adapter.tool_created[ctx.session_key]``, on the
      loop; the parent walk compares UUIDs.
    - A tool taking a second issue reference (a parent, a relation target, …) must
      scope-check that issue too, not only the issue it writes.
    """
    if not ctx.is_linear or not ctx.session_issue_id:
        return "NOT_LINEAR_SESSION"
    created = getattr(ctx.adapter, "tool_created", {}).get(ctx.session_key or "", set())
    allowed = {ctx.session_issue_id, *created}
    if issue_id in allowed:
        return None
    if ctx.client is None:
        return "OUT_OF_SCOPE"
    try:
        return await _walk_scope(ctx.client, issue_id, allowed)
    except LinearError as exc:
        return exc.code or "LINEAR_ERROR"


async def _walk_scope(client: Any, issue_id: str, allowed: set[str]) -> str | None:
    async def fetch(ident: str) -> dict | None:
        data = await client.gql(QUERY_ISSUE_PARENT, {"id": ident})
        node = data.get("issue") if isinstance(data, dict) else None
        return node if isinstance(node, dict) and node.get("id") else None

    node = await fetch(issue_id)
    if node is None:
        return "OUT_OF_SCOPE"
    if node["id"] in allowed:  # e.g. the session issue named by its identifier
        return None
    for level in range(1, SCOPE_PARENT_DEPTH + 1):  # at most SCOPE_PARENT_DEPTH queries
        parent = node.get("parent")
        pid = parent.get("id") if isinstance(parent, dict) else None
        if not pid:
            return "OUT_OF_SCOPE"
        if pid in allowed:
            return None
        if level == SCOPE_PARENT_DEPTH:
            break
        node = await fetch(pid)
        if node is None:
            return "OUT_OF_SCOPE"
    return "OUT_OF_SCOPE"


__all__ = ["ToolContext", "check_write_scope", "resolve_context", "run_on_adapter"]
