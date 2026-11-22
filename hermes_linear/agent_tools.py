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


def resolve_context(session_id: str | None = None) -> ToolContext:
    """The adapter and Linear session (if any) of the calling tool.

    In a Linear session the owning adapter is the one whose in-flight turn has the bound
    ``HERMES_SESSION_KEY``, else the one whose store has the message id, else the chat (the
    ``hooks._route`` order). Outside Linear, or when no turn is found, reads still work on the
    first adapter in ``ADAPTERS``, with no session issue (writes are refused).
    """
    env = _session()
    adapters = list(adapter_mod.ADAPTERS.values())
    first = adapters[0] if adapters else None
    is_linear = env["platform"] == PLATFORM
    log.debug("linear: tool context (session_id=%s, key=%s, linear=%s)",
              session_id, env["session_key"], is_linear)

    def ctx(ad: Any, issue: str | None) -> ToolContext:
        return ToolContext(adapter=ad, client=getattr(ad, "client", None) if ad else None,
                           session_issue_id=issue if is_linear else None,
                           session_key=env["session_key"] if is_linear else None,
                           is_linear=is_linear)

    if not is_linear:
        return ctx(first, None)
    found: list[tuple[Any, list[str | None]]] = []
    for ad in adapters:
        loop = _live_loop(ad)
        if loop is None:
            continue
        if _on_loop(loop):
            # Q3 dispatch_tool path: never block the loop; run_on_adapter refuses next.
            return ctx(ad, None)
        fut = asyncio.run_coroutine_threadsafe(_lookup(ad, env), loop)
        try:
            found.append((ad, fut.result(RESOLVE_TIMEOUT_S)))
        except Exception as exc:  # noqa: BLE001 - incl. timeout: that adapter does not claim it
            fut.cancel()
            log.debug("linear: tool context lookup on %r failed: %s", ad, type(exc).__name__)
    for rule in range(3):
        for ad, hits in found:
            if hits[rule]:
                return ctx(ad, hits[rule])
    return ctx(first, None)


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
        return _err("CANCELLED")
    except Exception as exc:  # noqa: BLE001
        log.debug("linear: tool call failed: %s", type(exc).__name__)
        return _err(type(exc).__name__)
    return result if isinstance(result, dict) else {"ok": True, "result": result}


async def check_write_scope(ctx: ToolContext, issue_id: str) -> str | None:
    """On the loop: ``None`` if the session may write ``issue_id``, else the refusal code.

    Allowed: the session issue, an issue this session created through a tool
    (``adapter.tool_created[session_key]``), or an issue with the session issue among its
    first ``SCOPE_PARENT_DEPTH`` ancestors. Ids may be identifiers or UUIDs; the UUIDs Linear
    returns are compared. A ``LinearError`` from the walk propagates (``run_on_adapter`` maps it).
    """
    if not ctx.is_linear or not ctx.session_issue_id:
        return "NOT_LINEAR_SESSION"
    created = getattr(ctx.adapter, "tool_created", {}).get(ctx.session_key or "", set())
    allowed = {ctx.session_issue_id, *created}
    if issue_id in allowed:
        return None
    if ctx.client is None:
        return "OUT_OF_SCOPE"

    async def fetch(ident: str) -> dict | None:
        data = await ctx.client.gql(QUERY_ISSUE_PARENT, {"id": ident})
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
