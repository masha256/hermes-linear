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
import dataclasses
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine

from gateway.session_context import get_session_env

from . import adapter as adapter_mod
from . import directory, graphql_guard, issue_fields, sync, tool_approval
from .constants import PLAN_SYNC_TIMEOUT_S, TOOL_TIMEOUT_S
from .linear_client import LinearError

log = logging.getLogger(__name__)

PLATFORM = "linear"
SCOPE_PARENT_DEPTH = 5  # parent levels walked by check_write_scope
CYCLE_WALK_DEPTH = 50  # ancestor levels walked before a reparent; deeper fails closed
RESOLVE_TIMEOUT_S = 5.0  # per-adapter turn lookup (pure in-memory/store work on the loop)

QUERY_ISSUE_PARENT = """
query IssueParent($id: String!) {
  issue(id: $id) { id parent { id } }
}
"""


class _NeedsApproval(Exception):
    """Raised by ``Gate.check`` inside a gated body: out-of-scope targets, nothing written."""

    def __init__(self, targets: list[dict]) -> None:
        super().__init__("approval needed")
        self.targets = targets


@dataclass
class Gate:
    """One pass of a gated tool (RET-119 spec §2.1).

    ``approved`` empty: a first pass. ``_writable`` records each out-of-scope target
    (``{"id", "identifier", "title"}``) instead of refusing it, and ``check`` (called right
    before the body's first write) raises ``_NeedsApproval`` if anything was recorded.
    ``approved`` set (the UUIDs of a taken armed request, or mode ``allow``): those issues
    are writable; any other out-of-scope target is refused ``OUT_OF_SCOPE``.
    """

    approved: frozenset[str] = frozenset()
    targets: list[dict] = field(default_factory=list)

    def record(self, issue: dict) -> None:
        if any(t["id"] == issue["id"] for t in self.targets):
            return
        self.targets.append({"id": issue["id"], "identifier": issue.get("identifier"),
                             "title": issue.get("title")})

    def record_target(self, id: str, kind: str, name: str) -> None:  # noqa: A002
        """A team or project target (``kind`` ``team`` / ``project``; RET-121 spec §2.4),
        shown on the card as ``- <kind> "<name>"``."""
        if any(t["id"] == id for t in self.targets):
            return
        self.targets.append({"id": id, "kind": kind, "identifier": name, "title": ""})

    def check(self) -> None:
        if self.targets:
            raise _NeedsApproval(list(self.targets))


@dataclass
class ToolContext:
    adapter: Any  # LinearAdapter | None
    client: Any  # LinearClient | None
    session_issue_id: str | None
    session_key: str | None
    is_linear: bool
    gate: Gate | None = None  # set only for a gated tool's pass (``_tool(gated=...)``)


def _err(code: str) -> dict:
    return {"ok": False, "error": code}


def _internal(exc: BaseException, where: str) -> dict:
    """An unexpected (non-Linear) failure: logged by type only, answered ``INTERNAL_ERROR``."""
    log.warning("linear: %s failed: %s", where, type(exc).__name__)
    return _err("INTERNAL_ERROR")


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
        return _internal(exc, "tool call")
    try:
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
    except RuntimeError:  # loop closed between the check and the call
        coro.close()
        return _err("NOT_CONNECTED")
    except Exception as exc:  # noqa: BLE001 - e.g. not a coroutine
        if hasattr(coro, "close"):
            coro.close()
        return _internal(exc, "tool call")
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
        return _internal(exc, "tool call")
    return result if isinstance(result, dict) else {"ok": True, "result": result}


async def check_write_scope(ctx: ToolContext, issue_id: str) -> str | None:
    """On the loop: ``None`` if the session may write ``issue_id``, else the refusal code.

    Allowed: the session issue, an issue the tools created from this session issue (the
    store's ``created_issues``, kept across turns and restarts; RET-110), or an issue with
    one of those among its first ``SCOPE_PARENT_DEPTH`` ancestors. Ids may be identifiers or
    UUIDs; the UUIDs Linear returns are compared. Never raises: a ``LinearError`` from the
    parent walk returns its ``code`` (``LINEAR_ERROR`` if none). Fail-closed: any non-``None``
    result is a refusal.

    Descendants: an issue fetched through the ``children`` connection of an issue that
    passed this check is in scope (it has that issue as its parent), so callers such as
    ``linear_sync_plan`` write such children without a per-child check.

    Contract for write tools:
    - Record the UUID Linear returns for an issue you create (``issueCreate.issue.id``),
      never its identifier (``RET-12``), with ``_record_created``; the walk compares UUIDs.
    - A tool taking a second issue reference (a parent, a relation target, …) must
      scope-check that issue too, not only the issue it writes.
    """
    if not ctx.is_linear or not ctx.session_issue_id:
        return "NOT_LINEAR_SESSION"
    allowed = {ctx.session_issue_id, *_created(ctx)}
    if issue_id in allowed:
        return None
    if ctx.client is None:
        return "OUT_OF_SCOPE"
    try:
        return await _walk_scope(ctx.client, issue_id, allowed)
    except LinearError as exc:
        return exc.code or "LINEAR_ERROR"


def _created(ctx: ToolContext) -> set[str]:
    """The issues the tools created from ``ctx``'s session issue (none without a store)."""
    store = getattr(ctx.adapter, "_store", None)
    return store.created_by(ctx.session_issue_id) if store is not None else set()


def _record_created(ctx: ToolContext, issue_id: str) -> None:
    """Put the new issue ``issue_id`` (a UUID) in the session issue's write scope for good.

    Without a store (a disconnect racing the call) the issue is created but not recorded:
    it stays writable only through the tree scope.
    """
    store = getattr(ctx.adapter, "_store", None)
    if store is None or not ctx.session_issue_id:
        log.warning("linear: created issue %s not recorded (no store)", issue_id)
        return
    store.record_created(ctx.session_issue_id, issue_id)


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


# ----------------------------------------------------------------- generic tools (RET-76)
#
# Handlers are sync ``handler(args, **kw) -> dict`` and never raise. Arguments are validated
# before anything else (``BAD_ARGS``); every Linear call, scope check and created-issue
# record then runs in one coroutine on the adapter loop (``run_on_adapter``). Issue
# references may be identifiers or UUIDs; each is resolved with ``issue_get`` first so scope
# checks and created-issue records see the UUID Linear returns.

MARKER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
#: Any hermes-linear marker, stripped from agent-supplied bodies before ours is appended.
ANY_MARKER_RE = re.compile(r"[ \t]*<!--\s*hermes-linear:.*?-->", re.DOTALL)


class _Refusal(Exception):
    """Raised inside a tool coroutine to answer ``{"ok": False, "error": code, **extra}``
    (``extra``: e.g. a miss's ``candidates``, RET-121 spec §2.2)."""

    def __init__(self, code: str, extra: dict | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.extra: dict = dict(extra or {})

    def result(self) -> dict:
        return {**_err(self.code), **self.extra}


def resolve_state(states: list[dict], name_or_type: str) -> str | None:
    """The state id named ``name_or_type`` (case-insensitive), else the first of that type."""
    want = (name_or_type or "").strip().casefold()
    if not want:
        return None
    for s in states:
        if (s.get("name") or "").casefold() == want:
            return s.get("id")
    for s in states:
        if (s.get("type") or "").casefold() == want:
            return s.get("id")
    return None


def _marker_tag(marker: str) -> str:
    return f"<!-- hermes-linear:{marker} -->"


def _opt_str(args: dict, key: str) -> str | None:
    val = args.get(key)
    if val is None:
        return None
    if not isinstance(val, str):
        raise _Refusal("BAD_ARGS")
    return val


def _req_str(args: dict, key: str) -> str:
    val = _opt_str(args, key)
    if val is None or not val.strip():
        raise _Refusal("BAD_ARGS")
    return val.strip() if key in {"id", "parent"} else val


def _opt_names(args: dict, key: str) -> list[str]:
    val = args.get(key)
    if val is None:
        return []
    if not isinstance(val, list) or not all(isinstance(v, str) and v.strip() for v in val):
        raise _Refusal("BAD_ARGS")
    return [v.strip() for v in val]


def _has_running_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _on_any_loop() -> bool:
    """True on a thread running an event loop (any loop, or a live adapter's loop thread)."""
    if _has_running_loop():
        return True
    me = threading.get_ident()
    for ad in list(adapter_mod.ADAPTERS.values()):
        loop = _live_loop(ad)
        if loop is not None and getattr(loop, "_thread_id", None) == me:
            return True
    return False


_NEEDS_APPROVAL = "_NEEDS_APPROVAL"  # run()'s sentinel error: never returned to a caller


def _tool(parse: Callable[[dict], dict],
          body: Callable[[ToolContext, dict], Coroutine[Any, Any, dict]], *,
          gated: str | None = None, blocking_parse: bool = False,
          timeout_s: float = TOOL_TIMEOUT_S,
          finish: Callable[[dict, dict], dict] | None = None) -> Callable[..., dict]:
    """Wrap ``parse`` (sync arg validation) and ``body`` (loop coroutine) into a handler.

    With ``blocking_parse`` (``parse`` reads files / runs git), a call on an event-loop
    thread (the ``PluginContext.dispatch_tool`` path) is refused with ``WRONG_THREAD``
    before ``parse`` runs, so no blocking I/O ever runs on the gateway loop. ``finish``
    (``(parsed, result) -> result``) runs in the tool thread after a successful ``body``,
    e.g. to write a file; it is only allowed together with ``blocking_parse``.

    ``gated`` (the tool name) makes out-of-scope targets ask for approval instead of being
    refused (``_gated``; RET-119 spec §2.1).
    """
    assert finish is None or blocking_parse, "finish does I/O: needs the on-loop guard"
    assert finish is None or gated is None, "a gated tool has no finish step"

    def handler(args: Any = None, **kw: Any) -> dict:
        try:
            if blocking_parse and _on_any_loop():
                return _err("WRONG_THREAD")
            if not isinstance(args, dict):
                return _err("BAD_ARGS")
            try:
                parsed = parse(args)
            except _Refusal as r:
                return r.result()
            ctx = resolve_context(kw.get("session_id"))
            if ctx.client is None:
                return _err("NOT_CONNECTED")

            def execute(c: ToolContext) -> dict:
                async def run() -> dict:
                    try:
                        return await body(c, parsed)
                    except _Refusal as r:
                        return r.result()
                    except _NeedsApproval as n:
                        return {"ok": False, "error": _NEEDS_APPROVAL, "targets": n.targets}
                return run_on_adapter(c, run, timeout_s=timeout_s)

            if gated is not None:
                return _gated(gated, args, ctx, execute)
            result = execute(ctx)
            if finish is not None and result.get("ok"):
                result = finish(parsed, result)
            return result
        except Exception as exc:  # noqa: BLE001 - Q3.4: a tool never raises
            return _internal(exc, "tool")
    return handler


def _gated(tool: str, args: dict, ctx: ToolContext,
           execute: Callable[[ToolContext], dict]) -> dict:
    """In the tool thread: run a gated tool's body (RET-119 spec §2.1 steps 1, 2 and 5).

    An armed request for this call (``(session key, digest of the normalized args)``) is
    taken first: its stored target UUIDs are the only out-of-scope issues the pass may write.
    Without one, a first pass records out-of-scope targets; then the mode decides: ``deny``
    refuses, ``allow`` runs again with those targets approved, ``approve`` stores a request
    and answers ``APPROVAL_REQUIRED``. Fail-closed: no digest or no store refuses
    ``OUT_OF_SCOPE``, and the run sentinel never reaches the caller.
    """
    store = getattr(ctx.adapter, "_store", None)
    try:
        dg: str | None = tool_approval.digest(tool, args)
    except (TypeError, ValueError):  # args that are not JSON: no request can match
        dg = None
    can_request = (ctx.is_linear and bool(ctx.session_key) and store is not None
                   and dg is not None)
    if can_request:
        req = store.take_armed_request(ctx.session_key, dg)
        if req is not None:
            return _sealed(execute(dataclasses.replace(
                ctx, gate=Gate(approved=frozenset(req.targets)))))
    result = execute(dataclasses.replace(ctx, gate=Gate()))
    if result.get("error") != _NEEDS_APPROVAL:
        return result
    targets: list[dict] = result.get("targets") or []
    settings = getattr(ctx.adapter, "_settings", None)
    mode = getattr(settings, "out_of_scope_writes", None) or "approve"
    if mode == "allow":
        approved = frozenset(t["id"] for t in targets)
        return _sealed(execute(dataclasses.replace(ctx, gate=Gate(approved=approved))))
    if mode == "deny" or not can_request or not targets:
        return _err("OUT_OF_SCOPE")
    store.put_request(ctx.session_key, dg, tool, [t["id"] for t in targets],
                      tool_approval.approval_message(tool, targets, args, dg))
    names = ", ".join(str(t.get("identifier") or t["id"]) for t in targets)
    return {"ok": False, "error": "APPROVAL_REQUIRED",
            "message": tool_approval.REQUIRED_TEXT.format(tool=tool, targets=names)}


def _sealed(result: dict) -> dict:
    """An approved pass's result; a target recorded there anyway is ``OUT_OF_SCOPE``."""
    return _err("OUT_OF_SCOPE") if result.get("error") == _NEEDS_APPROVAL else result


async def _writable(ctx: ToolContext, ref: str) -> dict:
    """``issue_get(ref)`` if the session may write it (by its UUID), else ``_Refusal``.

    For a gated tool (``ctx.gate``): an issue in ``gate.approved`` is writable; on a first
    pass (nothing approved) a plain ``OUT_OF_SCOPE`` verdict is recorded on the gate and the
    issue returned, so the body must call ``gate.check()`` before writing. Any other code,
    or ``OUT_OF_SCOPE`` on an approved pass, is refused.
    """
    if not ctx.is_linear or not ctx.session_issue_id:
        raise _Refusal("NOT_LINEAR_SESSION")
    issue = await ctx.client.issue_get(ref)
    gate = ctx.gate
    if gate is not None and issue["id"] in gate.approved:
        return issue
    code = await check_write_scope(ctx, issue["id"])
    if code == "OUT_OF_SCOPE" and gate is not None and not gate.approved:
        gate.record(issue)
        return issue
    if code is not None:
        raise _Refusal(code)
    return issue


def _check_gate(ctx: ToolContext) -> None:
    """Right before a gated body's first write: ``_NeedsApproval`` if a target is out of
    scope. Every scope check (``_writable``) of the body must come before this."""
    if ctx.gate is not None:
        ctx.gate.check()


def _target_ok(ctx: ToolContext, id: str) -> bool:  # noqa: A002
    """True on a pass whose approved targets hold ``id`` (a team or project UUID)."""
    return ctx.gate is not None and id in ctx.gate.approved


def _scope_target(ctx: ToolContext, row: dict, kind: str) -> None:
    """``row`` (a resolved team or project) is outside the session's scope: approved on
    this pass, recorded on a first pass (the body's ``_check_gate`` then asks), else
    ``OUT_OF_SCOPE`` (RET-121 spec §2.4)."""
    if _target_ok(ctx, row["id"]):
        return
    if ctx.gate is None or ctx.gate.approved:
        raise _Refusal("OUT_OF_SCOPE")
    ctx.gate.record_target(row["id"], kind, row.get("key" if kind == "team" else "name")
                           or row["id"])


def _state_row(states: list[dict], ref: str) -> dict | None:
    """``resolve_state``'s pick as a row: a directory ``match``."""
    sid = resolve_state(states, ref)
    return next((s for s in states if s.get("id") == sid), None) if sid else None


def _label_row(labels: list[dict], name: str) -> dict | None:
    """The first label named ``name`` (case-insensitive): a directory ``match``."""
    want = name.casefold()
    return next((lb for lb in labels
                 if (lb.get("name") or "").casefold() == want and lb.get("id")), None)


def _labels_all_named(labels: list[dict], names: list[str]) -> dict | None:
    """A directory ``match`` for several names at once: ``{"rows": labels}`` when every
    name is a label, else ``None`` (a miss)."""
    return {"rows": labels} if all(_label_row(labels, n) for n in names) else None


async def _state_id(ctx: ToolContext, team_id: str, name: str) -> str:
    """The team's state named (or typed) ``name``; a miss is ``STATE_NOT_FOUND`` with the
    team's states as ``candidates`` (after the directory's refetch-once)."""
    row = await directory.for_client(ctx.client).resolve("states", team_id, name, _state_row)
    return row["id"]


async def _cancel_state(ctx: ToolContext, team_id: str) -> str:
    """The team's ``canceled`` state for plan sync. Not an agent-supplied name, so a team
    without one stays ``BAD_ARGS`` (spec §2.5, as ``_target_state``)."""
    try:
        return await _state_id(ctx, team_id, "canceled")
    except _Refusal:
        raise _Refusal("BAD_ARGS") from None


async def _label_ids(ctx: ToolContext, team_id: str, add: list[str],
                     remove: list[str] = ()) -> tuple[list[str], list[str]]:
    """Label ids for names to ``add`` and ``remove`` (case-insensitive).

    An unknown name to add is ``LABEL_MISSING`` with ``available`` (probe P3: the app
    cannot create labels), after one refetch of the cached labels; an unknown name to
    remove is skipped, since no issue can carry it (Ruling S13).
    """
    if not add and not remove:
        return [], []
    names = directory.for_client(ctx.client)
    if add:  # one resolve: a miss refetches once (unless just fetched), then LABEL_MISSING
        labels = (await names.resolve("labels", team_id, add, _labels_all_named))["rows"]
    else:
        labels = await names.rows("labels", team_id)
    out: list[list[str]] = []
    for group in (add, remove):
        ids: list[str] = []
        for n in group:
            row = _label_row(labels, n)
            if row is not None and row["id"] not in ids:
                ids.append(row["id"])
        out.append(ids)
    return out[0], out[1]


def _team_of(issue: dict) -> str:
    team = (issue.get("team") or {}).get("id")
    if not team:
        raise _Refusal("LINEAR_ERROR")
    return team


def _project_of(issue: dict | None) -> str | None:
    return ((issue or {}).get("project") or {}).get("id")


def _with_fields(out: dict, r: issue_fields.Resolved) -> dict:
    """A write's result plus self-delegation's ``starts_session`` / ``warnings``."""
    if r.starts_session is not None:
        out["starts_session"] = r.starts_session
    if r.warnings:
        out["warnings"] = list(r.warnings)
    return out


# -- linear_issue_create


def _parse_create(args: dict) -> dict:
    return {"title": _req_str(args, "title"), "description": _opt_str(args, "description"),
            "parent": _opt_str(args, "parent"), "labels": _opt_names(args, "labels"),
            "state": _opt_str(args, "state"),
            "fields": issue_fields.parse(args, update=False)}


async def _create(ctx: ToolContext, a: dict) -> dict:
    """Base team and project (RET-121 spec §1.2): the parent's with ``parent``, else the
    session ticket's. An explicit team / project other than the base is a target."""
    if not ctx.is_linear or not ctx.session_issue_id:
        raise _Refusal("NOT_LINEAR_SESSION")
    parent = None
    if a["parent"] and a["parent"].strip():
        parent = await _writable(ctx, a["parent"].strip())
    base = parent if parent is not None else await ctx.client.issue_get(ctx.session_issue_id)
    base_team, base_project = _team_of(base), _project_of(base)
    r = await issue_fields.resolve(ctx, a["fields"], team_id=base_team,
                                   project_id=base_project)
    if r.team is not None and r.team["id"] != base_team:
        _scope_target(ctx, r.team, "team")
    if r.project is not None and r.project["id"] != base_project:
        _scope_target(ctx, r.project, "project")
    team = r.team_id
    inp: dict[str, Any] = {"teamId": team, "title": a["title"]}
    if r.project_id:
        inp["projectId"] = r.project_id
    if a["description"] is not None:
        inp["description"] = a["description"]
    if parent is not None:
        inp["parentId"] = parent["id"]
    if a["state"] is not None:
        inp["stateId"] = await _state_id(ctx, team, a["state"])
    labels, _ = await _label_ids(ctx, team, a["labels"])
    if labels:
        inp["labelIds"] = labels
    inp.update({k: v for k, v in r.input.items() if k not in ("teamId", "projectId")})
    _check_gate(ctx)
    issue = await ctx.client.issue_create(inp)
    _record_created(ctx, issue["id"])  # the UUID Linear returned, never the identifier
    return _with_fields({"ok": True, "issue": issue}, r)


# -- linear_issue_update


def _parse_update(args: dict) -> dict:
    a = {"id": _req_str(args, "id"), "title": _opt_str(args, "title"),
         "description": _opt_str(args, "description"), "state": _opt_str(args, "state"),
         "add_labels": _opt_names(args, "add_labels"),
         "remove_labels": _opt_names(args, "remove_labels"), "parent": _parse_parent(args),
         "fields": issue_fields.parse(args, update=True)}  # ``team``: BAD_ARGS
    if a["title"] is not None and not a["title"].strip():
        raise _Refusal("BAD_ARGS")
    if not (a["title"] is not None or a["description"] is not None or a["state"] is not None
            or a["add_labels"] or a["remove_labels"] or a["parent"] is not None
            or a["fields"]):
        raise _Refusal("BAD_ARGS")
    return a


def _parse_parent(args: dict) -> str | None:
    """``None``: leave the parent alone (key absent). ``""``: detach (null, empty or blank).
    Else the new parent's reference, stripped."""
    if "parent" not in args:
        return None
    val = args["parent"]
    if val is None:
        return ""
    if not isinstance(val, str):
        raise _Refusal("BAD_ARGS")
    return val.strip()


async def _update(ctx: ToolContext, a: dict) -> dict:
    issue = await _writable(ctx, a["id"])
    team = _team_of(issue)
    inp: dict[str, Any] = {}
    for key in ("title", "description"):
        if a[key] is not None:
            inp[key] = a[key]
    if a["state"] is not None:
        inp["stateId"] = await _state_id(ctx, team, a["state"])
    parent: dict | None = None
    if a["parent"] == "":
        inp["parentId"] = None  # detach: top-level
    elif a["parent"] is not None:
        parent = await _writable(ctx, a["parent"])  # both ends in scope (RET-110)
        await _refuse_cycle(ctx, issue["id"], parent)
        inp["parentId"] = parent["id"]
    r = await issue_fields.resolve(ctx, a["fields"], team_id=team,
                                   project_id=_project_of(issue))
    if r.project is not None and not await _project_in_scope(ctx, issue, a["parent"], parent,
                                                             r.project["id"]):
        _scope_target(ctx, r.project, "project")
    inp.update(r.input)
    added, removed = await _label_ids(ctx, team, a["add_labels"], a["remove_labels"])
    carried = _carried(issue)
    removed = [lid for lid in removed if lid in carried]  # removing an absent label: no-op
    if added:
        inp["addedLabelIds"] = added
    if removed:
        inp["removedLabelIds"] = removed
    if not inp:  # only no-op removals: never send an empty issueUpdate (Task 2 minor N1)
        return {"ok": True, "issue": _summary(issue)}
    _check_gate(ctx)  # after both ends' scope checks, the cycle check and the project check
    return _with_fields({"ok": True, "issue": await ctx.client.issue_update(issue["id"], inp)},
                        r)


async def _project_in_scope(ctx: ToolContext, issue: dict, parent_arg: str | None,
                            new_parent: dict | None, project_id: str) -> bool:
    """True if moving ``issue`` to ``project_id`` needs no approval (RET-121 spec §1.3):
    its current project (no change), the session ticket's, or its parent's project, the
    parent being ``new_parent`` when the call sets one (none when it detaches), else the
    issue's current parent. Reads only as far as needed."""
    if project_id == _project_of(issue):
        return True
    if project_id == _project_of(await ctx.client.issue_get(ctx.session_issue_id or "")):
        return True
    if parent_arg is not None:  # the parent changes in this call
        return new_parent is not None and project_id == _project_of(new_parent)
    current = (issue.get("parent") or {}).get("id")
    return bool(current) and project_id == _project_of(await ctx.client.issue_get(current))


async def _refuse_cycle(ctx: ToolContext, issue_id: str, parent: dict) -> None:
    """``PARENT_CYCLE`` if ``parent`` (an ``issue_get`` result) is ``issue_id`` or one of its
    descendants, i.e. ``issue_id`` is among ``parent``'s ancestors. Fail-closed: an ancestry
    deeper than ``CYCLE_WALK_DEPTH`` is refused too."""
    if parent["id"] == issue_id:
        raise _Refusal("PARENT_CYCLE")
    node: dict | None = parent
    for _ in range(CYCLE_WALK_DEPTH):
        pid = ((node or {}).get("parent") or {}).get("id")
        if not pid:
            return  # reached the root: no cycle
        if pid == issue_id:
            raise _Refusal("PARENT_CYCLE")
        data = await ctx.client.gql(QUERY_ISSUE_PARENT, {"id": pid})
        node = data.get("issue") if isinstance(data, dict) else None
    raise _Refusal("PARENT_CYCLE")


def _carried(issue: dict) -> set[str]:
    """The label ids ``issue`` (an ``issue_get`` result) carries."""
    return {lb["id"] for lb in issue.get("labels") or [] if isinstance(lb, dict) and lb.get("id")}


def _summary(issue: dict) -> dict:
    """The ``issue_update`` result shape, from an ``issue_get`` result (no write made)."""
    return {k: issue.get(k) for k in ("id", "identifier", "title", "url")}


# -- linear_comment


def _parse_comment(args: dict) -> dict:
    a = {"id": _req_str(args, "id"), "body": _req_str(args, "body"),
         "edit_marker": _opt_str(args, "edit_marker")}
    if a["edit_marker"] is not None and not MARKER_RE.match(a["edit_marker"]):
        raise _Refusal("BAD_ARGS")
    return a


def _with_marker(body: str, marker: str) -> str:
    """``body`` without any hermes-linear marker, then exactly one ``marker`` tag."""
    return f"{ANY_MARKER_RE.sub('', body).rstrip()}\n\n{_marker_tag(marker)}"


async def _comment(ctx: ToolContext, a: dict) -> dict:
    issue = await _writable(ctx, a["id"])
    _check_gate(ctx)
    body, marker = a["body"], a["edit_marker"]

    def done(comment_id: str, edited: bool) -> dict:
        return {"ok": True, "comment_id": comment_id, "edited": edited,
                "issue": issue.get("identifier"), "url": issue.get("url")}

    if marker is None:
        return done(await ctx.client.comment_create(issue["id"], body), False)
    tag = _marker_tag(marker)
    text = _with_marker(body, marker)
    # Only the app's own comments: a human quoting the marker is never edited.
    for c in await ctx.client.app_comments(issue["id"]):
        if tag in (c.get("body") or ""):
            await ctx.client.comment_update(c["id"], text)
            return done(c["id"], True)
    return done(await ctx.client.comment_create(issue["id"], text), False)


# -- linear_sync_spec (RET-77)


def _read_source(args: dict, key: str = "path") -> tuple[Path, str]:
    """``args[key]`` as an absolute path and its UTF-8 text (≤ MAX_SOURCE_BYTES), or BAD_ARGS.

    Shared by the spec and plan sync tools; call only off the loop (``blocking_parse``).
    """
    p = sync.source_path(args.get(key))
    if p is None:
        raise _Refusal("BAD_ARGS")  # missing, non-str, empty, or relative (Ruling S15)
    text = sync.read_source(p)
    if text is None:
        raise _Refusal("BAD_ARGS")  # not a regular file, too large, unreadable or not UTF-8
    return p, text


def _parse_sync_spec(args: dict) -> dict:
    """In the tool thread, off the loop: read the file, its header, and git (sync.py)."""
    p, text = _read_source(args)
    ticket = sync.read_linear_header(text)
    if ticket is None:
        raise _Refusal("NO_LINEAR_HEADER")
    description, inline = sync.spec_description(text, sync.source_info(p))
    return {"ticket": ticket, "description": description, "inline": inline}


async def _sync_spec(ctx: ToolContext, a: dict) -> dict:
    issue = await _writable(ctx, a["ticket"])
    out = await ctx.client.issue_update(issue["id"], {"description": a["description"]})
    return {"ok": True, "issue": out.get("identifier") or issue.get("identifier"),
            "url": out.get("url") or issue.get("url"), "inline": a["inline"]}


# -- linear_sync_plan (RET-78)

PLAN_MARKER = "plan"
PLAN_SORT_STEP = 10  # subIssueSortOrder = PLAN_SORT_STEP * position (1-based)
_CLOSED = {"completed", "canceled"}


def _state_type(issue: dict) -> str | None:
    return (issue.get("state") or {}).get("type")


def _parse_sync_plan(args: dict) -> dict:
    """In the tool thread, off the loop: read and parse the plan, and run git (sync.py)."""
    p, text = _read_source(args)
    ticket = sync.read_linear_header(text)
    if ticket is None:
        raise _Refusal("NO_LINEAR_HEADER")
    tasks = sync.parse_plan(text)
    numbers = [t.number for t in tasks]
    ids = [t.ticket for t in tasks if t.ticket]
    if not tasks or len(set(numbers)) != len(numbers) or len(set(ids)) != len(ids) \
            or ticket in ids:
        raise _Refusal("BAD_ARGS")  # no tasks, a duplicate number/ID, or the project itself
    return {"path": p, "text": text, "ticket": ticket, "tasks": tasks,
            "info": sync.source_info(p)}


def _is_session(ctx: ToolContext, node: dict) -> bool:
    """True if ``node`` (an issue or child) is the session issue (UUID or identifier)."""
    return bool(ctx.session_issue_id) and ctx.session_issue_id in (node.get("id"),
                                                                   node.get("identifier"))


def _app_made(node: dict, me: str) -> bool:
    """True if the app user (``me``, the viewer) created ``node`` (Ruling S17)."""
    return (node.get("creator") or {}).get("id") == me


async def _resolve_tasks(ctx: ToolContext, project: dict, tasks: list,
                         me: str) -> tuple[dict, list, dict]:
    """Reads only: task number -> existing issue (full ``issue_get``), recreated numbers, and
    per-task errors (``{n: code}``; those tasks are not written at all).

    A heading ID wins; a deleted or canceled ticket is recreated; a heading naming the
    session issue (not the project ticket) is refused for that task (``BAD_ARGS``), so the
    session ticket is never retitled. A task without a usable ID adopts an open child the
    app created (``creator.id == me``) whose title is exactly ``T<n> · <name>`` and that no
    other heading claims (crash between create and write-back). Exact title, not prefix: a
    task number reused for a different task gets a new ticket and the old one is canceled.
    Every heading target is scope-checked before any write.
    """
    pid = project["id"]
    target: dict[int, dict] = {}
    recreated: list[int] = []
    errors: dict[int, str] = {}
    for t in tasks:
        if not t.ticket:
            continue
        try:
            issue = await ctx.client.issue_get(t.ticket)
        except LinearError as exc:
            if exc.code != "NOT_FOUND":
                raise
            recreated.append(t.number)
            continue
        if issue["id"] == pid:
            raise _Refusal("BAD_ARGS")
        if _is_session(ctx, issue):
            errors[t.number] = "BAD_ARGS"  # the session ticket is never a task ticket
            continue
        if _state_type(issue) == "canceled":
            recreated.append(t.number)
            continue
        if (issue.get("parent") or {}).get("id") != pid:  # a child of a checked issue is fine
            code = await check_write_scope(ctx, issue["id"])
            if code is not None:
                raise _Refusal(code)
        target[t.number] = issue
    claimed = {i["id"] for i in target.values()}
    for t in tasks:
        if t.number in target or t.number in errors:
            continue
        title = sync.task_title(t)
        for c in project.get("children") or []:
            if c["id"] in claimed or _state_type(c) in _CLOSED or _is_session(ctx, c) \
                    or not _app_made(c, me) or c.get("title") != title:
                continue
            target[t.number] = await ctx.client.issue_get(c["id"])
            claimed.add(c["id"])
            break
    return target, recreated, errors


async def _sync_plan(ctx: ToolContext, a: dict) -> dict:
    client, info, tasks = ctx.client, a["info"], a["tasks"]
    project = await _writable(ctx, a["ticket"])
    pid, team = project["id"], _team_of(project)
    me = await client.viewer_id()  # cached since connect
    target, recreated, errors = await _resolve_tasks(ctx, project, tasks, me)

    mapping: dict[int, str] = {}
    created: list[str] = []
    keep: set[str] = {i["id"] for i in target.values()}
    for pos, t in enumerate(tasks, 1):
        if t.number in errors:
            continue
        order = PLAN_SORT_STEP * pos
        title, rendered = sync.task_title(t), sync.task_description(t, info)
        issue = target.get(t.number)
        if issue is None:
            inp: dict[str, Any] = {"teamId": team, "title": title,
                                   "description": sync.with_desc_hash(rendered),
                                   "parentId": pid, "subIssueSortOrder": order}
            if (project.get("project") or {}).get("id"):
                inp["projectId"] = project["project"]["id"]
            new = await client.issue_create(inp)
            _record_created(ctx, new["id"])  # the UUID Linear returned, never the identifier
            keep.add(new["id"])
            mapping[t.number] = new["identifier"]
            created.append(new["identifier"])
            continue
        mapping[t.number] = issue["identifier"]
        if _state_type(issue) == "completed":
            continue  # Done tickets are never touched
        inp = {}
        if issue.get("title") != title:  # titles round-trip exactly (probe P8)
            inp["title"] = title
        # Descriptions are normalized by Linear (P8): compare the hash marker, never text.
        if sync.read_desc_hash(issue.get("description")) != sync.desc_hash(rendered):
            inp["description"] = sync.with_desc_hash(rendered)
        if issue.get("subIssueSortOrder") != order:
            inp["subIssueSortOrder"] = order
        if inp:
            await client.issue_update(issue["id"], inp)

    canceled: list[str] = []
    unmanaged: list[str] = []
    cancel_state: str | None = None
    for c in project.get("children") or []:
        if c["id"] in keep or _state_type(c) in _CLOSED or _is_session(ctx, c) \
                or not sync.MANAGED_TITLE_RE.match(c.get("title") or ""):
            continue  # in the plan, closed, the session ticket, or not a plan ticket
        if not _app_made(c, me):
            unmanaged.append(c["identifier"])  # a human's T<n> ticket: reported, never touched
            continue
        cancel_state = cancel_state or await _cancel_state(ctx, team)
        # Reason first: a crash in between repeats the comment rather than losing it.
        await client.comment_create(
            c["id"], f"Removed from the plan in {info.commit or sync.UNCOMMITTED}.")
        await client.issue_update(c["id"], {"stateId": cancel_state})
        canceled.append(c["identifier"])

    # The comment shows the plan as it will be on disk (IDs written back), so a rerun on
    # the written file finds it unchanged. Markers in the plan text are dropped so no
    # other edit_marker lookup can ever match this comment.
    tag = _marker_tag(PLAN_MARKER)
    text = ANY_MARKER_RE.sub("", sync.write_ticket_ids(a["text"], mapping))
    body, inline = sync.plan_comment(text, info, tasks, tag, mapping)
    mine = next((c for c in await client.app_comments(pid) if tag in (c.get("body") or "")),
                None)  # the app's own comments only: a human quoting the marker is never edited
    if mine is None:
        await client.comment_create(pid, body)
        comment = "created"
    elif mine.get("body") != body:
        await client.comment_update(mine["id"], body)
        comment = "edited"
    else:
        comment = "unchanged"
    return {"ok": True, "issue": project.get("identifier"), "url": project.get("url"),
            "tasks": mapping, "created": created, "canceled": canceled,
            "recreated": recreated, "unmanaged": unmanaged, "errors": errors,
            "comment": comment, "comment_inline": inline}


def _finish_sync_plan(a: dict, out: dict) -> dict:
    """In the tool thread: write the IDs back into the headings, unless the file changed.

    The Linear writes are done by now: an ``OSError`` here still answers ``ok`` (with
    ``file_error``) so the caller sees what was created and canceled; a rerun adopts.
    """
    new = sync.write_ticket_ids(a["text"], out["tasks"])
    out.update(file_changed=False, file_conflict=False)
    if new == a["text"]:
        return out
    if sync.read_source(a["path"]) != a["text"]:
        out["file_conflict"] = True  # edited meanwhile: rerun the sync on the new file
        return out
    try:
        sync.write_source(a["path"], new)
    except OSError as exc:
        log.warning("linear: plan write-back failed: %s", type(exc).__name__)
        out["file_error"] = exc.strerror or type(exc).__name__
        return out
    out["file_changed"] = True
    return out


# -- linear_task_state (RET-79)

TASK_STATES = ("in_progress", "in_review", "done", "blocked", "unblocked")
BLOCKED_LABEL = "Blocked"


def _parse_task_state(args: dict) -> dict:
    state = args.get("state")
    if not isinstance(state, str) or state.strip() not in TASK_STATES:
        raise _Refusal("BAD_ARGS")
    a = {"id": _req_str(args, "id"), "state": state.strip(),
         "summary": _opt_str(args, "summary"), "question": _opt_str(args, "question"),
         "blocked_by": None}
    if args.get("blocked_by") is not None:  # RET-121 §1.6: only with blocked
        if a["state"] != "blocked":
            raise _Refusal("BAD_ARGS")
        a["blocked_by"] = _req_str(args, "blocked_by").strip()
    if a["state"] == "blocked" and (a["blocked_by"] is None or a["question"] is not None):
        a["question"] = _req_str(args, "question").strip()  # before any write (or read)
    return a


def _first_of_type(states: list[dict], type_: str) -> dict | None:
    return next((s for s in states if (s.get("type") or "").casefold() == type_), None)


def _named(states: list[dict], name: str) -> dict | None:
    want = name.casefold()
    return next((s for s in states if (s.get("name") or "").casefold() == want), None)


def _target_state(states: list[dict], current: dict, state: str) -> dict:
    """The workflow state ``state`` moves to (may be ``current``), else ``BAD_ARGS``."""
    if state == "in_progress":
        found = _named(states, "In Progress") or _first_of_type(states, "started")
    elif state == "in_review":
        found = _named(states, "In Review")
        if found is None:  # no review state: stay in a started state, else move to one
            found = current if (current.get("type") or "") == "started" \
                else _first_of_type(states, "started")
    else:  # done: any completed state is kept, as for in_review
        found = current if (current.get("type") or "") == "completed" \
            else _first_of_type(states, "completed")
    if found is None or not found.get("id"):
        raise _Refusal("BAD_ARGS")  # the team has no such state, as for _state_id
    return found


async def _blocked_label(ctx: ToolContext, team: str) -> str | None:
    """The id of the team's (or workspace's) ``Blocked`` label, case-insensitive; None when
    missing after the directory's refetch-once (the caller answers ``LABEL_MISSING`` in its
    own shape, Ruling S18)."""
    try:
        row = await directory.for_client(ctx.client).resolve("labels", team, BLOCKED_LABEL,
                                                             _label_row)
    except _Refusal:
        return None
    return row["id"]


def _track_open_task(ctx: ToolContext, task_id: str, open_: bool) -> None:
    """Record (RET-94) whether ``task_id`` is an open task of the session ticket; called
    only after the transition's Linear writes succeeded. No adapter or store: skipped."""
    store = getattr(ctx.adapter, "_store", None)  # None adapter: None
    if store is not None and ctx.session_issue_id:
        store.set_open_task(ctx.session_issue_id, task_id, open_)


async def _task_state(ctx: ToolContext, a: dict) -> dict:
    client, state = ctx.client, a["state"]
    task = await _writable(ctx, a["id"])
    team = _team_of(task)
    current = task.get("state") or {}
    out: dict[str, Any] = {"ok": True, "issue": task.get("identifier"),
                           "state": current.get("name"), "label_missing": False}

    if state in {"in_progress", "in_review", "done"}:
        target = _target_state(await directory.for_client(client).rows("states", team),
                               current, state)
        summary = (a["summary"] or "").strip() if state == "done" else ""
        if state == "done":
            out["summary_posted"] = False
        # First: a crash in between repeats the call, and the retry finds the summary already
        # posted (the app's own comments only; bodies round-trip byte-exact) and skips it.
        if summary and not any((c.get("body") or "").strip() == summary
                               for c in await client.app_comments(task["id"])):
            await client.comment_create(task["id"], a["summary"])
            out["summary_posted"] = True
        if target["id"] != current.get("id"):
            await client.issue_update(task["id"], {"stateId": target["id"]})
        out["state"] = target.get("name")
        _track_open_task(ctx, task["id"], state != "done")
        return out

    label = await _blocked_label(ctx, team)
    carried = label is not None and label in _carried(task)
    if state == "unblocked":
        if label is None:
            out["label_missing"] = True
        elif carried:
            await client.issue_update(task["id"], {"removedLabelIds": [label]})
        _track_open_task(ctx, task["id"], True)  # ruling R12: work resumes, the task is open
        return out  # nothing to remove: zero writes

    # blocked: the question goes to the project ticket (the session issue), scope-checked
    # like every write target, before anything is written. So is the blocker (RET-121
    # §1.6), without a gate: this tool stays tree-only, an out-of-tree blocker is
    # OUT_OF_SCOPE (``linear_relation`` asks for it).
    project = await _writable(ctx, ctx.session_issue_id or "")
    blocker = await _writable(ctx, a["blocked_by"]) if a["blocked_by"] else None
    if blocker is not None and blocker["id"] == task["id"]:
        raise _Refusal("BAD_ARGS")
    rid = None
    if blocker is not None:  # RELATION_CONFLICT refuses before any write (Ruling R9)
        # ``tools_relation`` is bottom-imported (R3): bound by the time any tool runs
        rid = await tools_relation.check_relation(ctx, blocker, task, "blocks")
    if label is not None and not carried:
        await client.issue_update(task["id"], {"addedLabelIds": [label]})
    rel: dict[str, Any] = {}
    if blocker is not None:  # an existing relation is kept: a re-run writes nothing
        created = rid is None
        if created:
            rid = await tools_relation.create_relation(ctx, blocker, task, "blocks")
        rel = {"relation_id": rid, "relation_created": created}
    if a["question"]:
        await client.comment_create(
            project["id"], f"**{task.get('identifier')} needs input:** {a['question']}")
    _track_open_task(ctx, task["id"], False)  # blocked, even with LABEL_MISSING
    if label is None:  # Ruling S18: the rest is done, the label could not be
        return {"ok": False, "error": "LABEL_MISSING", "issue": task.get("identifier"),
                "label_missing": True, "question_posted": bool(a["question"]), **rel}
    return {**out, "question_posted": bool(a["question"]), **rel}


# -- linear_query (RET-119 spec §3)

QUERY_MAX_RESULT_CHARS = 50_000
QUERY_ERROR_MESSAGE_CHARS = 1_000
#: ``LinearError`` codes ``linear_query`` answers as-is (plus ``message``).
_QUERY_KEPT_CODES = {"RATELIMITED", "NOT_FOUND", "UNAUTHENTICATED", "AUTHENTICATION_ERROR",
                     "FORBIDDEN", "NETWORK"}
_TOO_COMPLEX_PREFIX = "Query too complex"  # live probe: INPUT_ERROR with a presentable detail


def _parse_query(args: dict) -> dict:
    """The read-only guard: runs on the tool thread, before any Linear call."""
    query = args.get("query")
    code = graphql_guard.check(query)
    if code is not None:
        raise _Refusal(code)
    variables = args.get("variables")
    if variables is not None and not isinstance(variables, dict):
        raise _Refusal("BAD_ARGS")
    return {"query": query, "variables": variables}


def _query_error(exc: LinearError) -> dict:
    message = str(exc)
    if message.startswith(_TOO_COMPLEX_PREFIX):
        return {"ok": False, "error": "QUERY_TOO_COMPLEX",
                "message": f"{exc.detail or message} Lower first: or the nesting depth."}
    if exc.code in _QUERY_KEPT_CODES:
        return {"ok": False, "error": exc.code, "message": message}
    return {"ok": False, "error": "GRAPHQL_ERROR",
            "message": message[:QUERY_ERROR_MESSAGE_CHARS]}


async def _query(ctx: ToolContext, a: dict) -> dict:
    try:
        data = await ctx.client.gql(a["query"], a["variables"])
    except LinearError as exc:
        return _query_error(exc)
    size = len(json.dumps(data, ensure_ascii=False, default=str))
    if size > QUERY_MAX_RESULT_CHARS:
        return {"ok": False, "error": "RESPONSE_TOO_LARGE", "size": size,
                "limit": QUERY_MAX_RESULT_CHARS,
                "message": "Paginate (first:/after:) or select fewer fields."}
    return {"ok": True, "data": data}


linear_issue_create = _tool(_parse_create, _create, gated="linear_issue_create")
linear_issue_update = _tool(_parse_update, _update, gated="linear_issue_update")
linear_comment = _tool(_parse_comment, _comment, gated="linear_comment")
linear_sync_spec = _tool(_parse_sync_spec, _sync_spec, blocking_parse=True)
linear_sync_plan = _tool(_parse_sync_plan, lambda ctx, a: _sync_plan(ctx, a),  # late-bound
                         blocking_parse=True, timeout_s=PLAN_SYNC_TIMEOUT_S,
                         finish=_finish_sync_plan)
linear_task_state = _tool(_parse_task_state, _task_state)
linear_query = _tool(_parse_query, _query)

_ISSUE_REF = "Issue identifier (e.g. RET-12) or UUID."
_GATED_NOTE = ("Outside the session issue's tree (issues you did not create) the first call "
               "returns APPROVAL_REQUIRED; call again with identical arguments to ask a human.")
_USER_REF = "User UUID, name, display name or email; me is this app."
_PRIORITY = ("none, urgent, high, medium or low, or Linear's number 0-4 "
             "(0 none, 1 urgent, 2 high, 3 medium, 4 low).")


def _field_props(nullable: bool) -> dict[str, dict]:
    """The shared issue fields (RET-121 spec §1.1); on update ``null`` (or "") clears."""
    s: Any = ["string", "null"] if nullable else "string"
    i: Any = ["integer", "null"] if nullable else "integer"
    return {
        "assignee": {"type": s, "description": _USER_REF},
        "delegate": {"type": s, "description": (
            "An agent (app) user: UUID, name, display name or email; me is this app, "
            "which starts no session (starts_session: false).")},
        "priority": {"type": s, "description": _PRIORITY},
        "estimate": {"type": i, "description": "Estimate points, an integer >= 0."},
        "due_date": {"type": s, "description": "YYYY-MM-DD."},
        "cycle": {"type": s, "description": ("Cycle UUID, number, name, current or next, in "
                                             "the issue's team.")},
        "milestone": {"type": s, "description": ("Project milestone UUID or name, in the "
                                                 "issue's resulting project.")},
    }


TOOL_SCHEMAS: dict[str, dict] = {
    "linear_issue_get": {},  # first slot; tools_read registers its schema here (Ruling R3)
    "linear_issue_create": {
        "name": "linear_issue_create",
        "description": ("Create a Linear issue as the Linear app, in the parent's team and "
                        "project (with parent: a sub-issue), else the session issue's. A "
                        "team or project other than that needs a human's approval. "
                        "Delegating to this app starts no session. " + _GATED_NOTE),
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
            "description": {"type": "string", "description": "Markdown."},
            "parent": {"type": "string", "description": _ISSUE_REF},
            "labels": {"type": "array", "items": {"type": "string"},
                       "description": "Existing label names."},
            "state": {"type": "string",
                      "description": "Workflow state name, or a type such as started."},
            "team": {"type": "string", "description": "Team UUID, key (e.g. RET) or name."},
            "project": {"type": "string",
                        "description": ("Project UUID or name; omitted or null: the parent's "
                                        "(else the session issue's).")},
            **_field_props(nullable=False),
        }, "required": ["title"]},
    },
    "linear_issue_update": {
        "name": "linear_issue_update",
        "description": ("Update a Linear issue as the Linear app: title, description, state, "
                        "labels, parent, project, assignee, delegate, priority, estimate, "
                        "due date, cycle, milestone (null clears a field). Another project "
                        "than the session issue's or the parent's needs a human's approval. "
                        + _GATED_NOTE),
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string", "description": _ISSUE_REF},
            "title": {"type": "string"},
            "description": {"type": "string", "description": "Markdown; replaces the body."},
            "state": {"type": "string",
                      "description": "Workflow state name, or a type such as completed."},
            "add_labels": {"type": "array", "items": {"type": "string"},
                           "description": "Existing label names to add."},
            "remove_labels": {"type": "array", "items": {"type": "string"},
                              "description": "Label names to remove."},
            "parent": {"type": "string",
                       "description": ("New parent: identifier (e.g. RET-12) or UUID. Empty "
                                       "string detaches the issue (top-level). Refused "
                                       "(PARENT_CYCLE) if it is the issue or a descendant. A "
                                       "detached issue you did not create leaves your scope.")},
            "project": {"type": ["string", "null"],
                        "description": ("Project UUID or name; null removes the project. "
                                        "Moving to another project clears the milestone "
                                        "unless milestone is given too.")},
            **_field_props(nullable=True),
        }, "required": ["id"]},
    },
    "linear_comment": {
        "name": "linear_comment",
        "description": ("Comment on a Linear issue as the Linear app. With edit_marker, edit "
                        "the app's earlier comment carrying that marker instead of adding a "
                        "new one. " + _GATED_NOTE),
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string", "description": _ISSUE_REF},
            "body": {"type": "string", "description": "Markdown."},
            "edit_marker": {"type": "string",
                            "description": "Stable key, e.g. status (letters, digits, _.:-)."},
        }, "required": ["id", "body"]},
    },
    "linear_sync_spec": {
        "name": "linear_sync_spec",
        "description": ("Mirror a committed spec file onto the Linear ticket named by its "
                        "'Linear: <ID>' header, as the Linear app: overwrites the ticket "
                        "description with the file plus a source footer (only section 1 and "
                        "a link if the spec is over 200,000 chars). Commit the file first; "
                        "uncommitted edits are marked as such."),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string",
                     "description": "Absolute path to the spec file."},
        }, "required": ["path"]},
    },
    "linear_sync_plan": {
        "name": "linear_sync_plan",
        "description": ("Mirror a plan file onto Linear as the Linear app: the plan header "
                        "(before the first task) and the task list as one comment on the "
                        "ticket named by its 'Linear: <ID>' header, and one sub-ticket "
                        "'T<n> · <name>' per '### Task <n>: <name>' heading, in plan order, "
                        "whose description is that task's whole section. Headings with '— <ID>' update that ticket; others adopt "
                        "the app's open child with that exact title or create one. App-made "
                        "tickets of removed tasks are canceled; humans' tickets and Done "
                        "tickets are never touched. Writes the IDs back into "
                        "the headings (commit the file afterwards). Safe to re-run."),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string",
                     "description": "Absolute path to the plan file."},
        }, "required": ["path"]},
    },
    "linear_task_state": {
        "name": "linear_task_state",
        "description": ("Track a plan task sub-ticket as the Linear app. in_progress: move to "
                        "In Progress; in_review: move to In Review (if the team has none, "
                        "stay in or move to a started state); done: move to a completed "
                        "state, posting summary (if given) on the task ticket; blocked: add "
                        "the Blocked label and post question on the session (project) "
                        "ticket as '**<ID> needs input:** <question>'; with blocked_by, also "
                        "record '<blocked_by> blocks <id>' (question then optional; both in "
                        "the session tree, else OUT_OF_SCOPE) (LABEL_MISSING if the team "
                        "has no Blocked label, the rest is still done); "
                        "unblocked: remove the Blocked label. Already-applied changes are "
                        "not rewritten."),
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string", "description": _ISSUE_REF},
            "state": {"type": "string", "enum": list(TASK_STATES)},
            "summary": {"type": "string",
                        "description": "Markdown; with done, posted on the task ticket."},
            "question": {"type": "string",
                         "description": ("With blocked: what you need answered (required "
                                         "without blocked_by).")},
            "blocked_by": {"type": "string",
                           "description": ("Only with blocked: the issue blocking this one "
                                           "(identifier or UUID, in the session tree).")},
        }, "required": ["id", "state"]},
    },
    "linear_query": {
        "name": "linear_query",
        "description": ("Run a read-only GraphQL query against Linear's API as the Linear app "
                        "(no mutations or subscriptions). Results over 50,000 chars are "
                        "refused: paginate with first:/after: or select fewer fields."),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string",
                      "description": "A GraphQL query document (query/fragment only)."},
            "variables": {"type": "object",
                          "description": "Values for the query's variables."},
        }, "required": ["query"]},
    },
    # Placeholders: the tool modules fill these slots (Ruling R3), so the roster order is
    # fixed whichever module is imported first.
    "linear_search": {},
    "linear_attach": {},
    "linear_session_link": {},
    "linear_relation": {},
}

TOOL_HANDLERS: dict[str, Callable[..., dict]] = {
    "linear_issue_get": None,  # type: ignore[dict-item]  # registered by tools_read (R3)
    "linear_issue_create": linear_issue_create,
    "linear_issue_update": linear_issue_update,
    "linear_comment": linear_comment,
    "linear_sync_spec": linear_sync_spec,
    "linear_sync_plan": linear_sync_plan,
    "linear_task_state": linear_task_state,
    "linear_query": linear_query,
    "linear_search": None,  # type: ignore[dict-item]  # registered by tools_read (R3)
    "linear_attach": None,  # type: ignore[dict-item]  # registered by tools_attach (R3)
    "linear_session_link": None,  # type: ignore[dict-item]  # registered by tools_attach
    "linear_relation": None,  # type: ignore[dict-item]  # registered by tools_relation
}


__all__ = ["TOOL_HANDLERS", "TOOL_SCHEMAS", "ToolContext", "check_write_scope",
           "linear_attach", "linear_comment", "linear_issue_create", "linear_issue_get",
           "linear_issue_update", "linear_query", "linear_relation", "linear_search",
           "linear_session_link",
           "linear_sync_plan", "linear_sync_spec", "linear_task_state", "resolve_context",
           "resolve_state", "run_on_adapter"]

# Bottom import: the tool modules import this module's machinery (RET-120 spec §2.1) and
# register themselves into TOOL_SCHEMAS / TOOL_HANDLERS and this module's namespace at
# their own end, so either module can be imported first (Ruling R3). No attribute access
# here: a tool module imported first is still partially initialized at this point.
from . import tools_read  # noqa: E402,F401
from . import tools_attach  # noqa: E402,F401
from . import tools_relation  # noqa: E402,F401
