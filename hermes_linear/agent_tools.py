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
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Coroutine

from gateway.session_context import get_session_env

from . import adapter as adapter_mod
from . import sync
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

    Allowed: the session issue, an issue this session created through a tool
    (``adapter.tool_created[session_key]``), or an issue with the session issue among its
    first ``SCOPE_PARENT_DEPTH`` ancestors. Ids may be identifiers or UUIDs; the UUIDs Linear
    returns are compared. Never raises: a ``LinearError`` from the parent walk returns its
    ``code`` (``LINEAR_ERROR`` if none). Fail-closed: any non-``None`` result is a refusal.

    Descendants: an issue fetched through the ``children`` connection of an issue that
    passed this check is in scope (it has that issue as its parent), so callers such as
    ``linear_sync_plan`` write such children without a per-child check.

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


# ----------------------------------------------------------------- generic tools (RET-76)
#
# Handlers are sync ``handler(args, **kw) -> dict`` and never raise. Arguments are validated
# before anything else (``BAD_ARGS``); every Linear call, scope check and ``tool_created``
# update then runs in one coroutine on the adapter loop (``run_on_adapter``). Issue
# references may be identifiers or UUIDs; each is resolved with ``issue_get`` first so scope
# checks and ``tool_created`` see the UUID Linear returns.

MARKER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
#: Any hermes-linear marker, stripped from agent-supplied bodies before ours is appended.
ANY_MARKER_RE = re.compile(r"[ \t]*<!--\s*hermes-linear:.*?-->", re.DOTALL)


class _Refusal(Exception):
    """Raised inside a tool coroutine to answer ``{"ok": False, "error": code}``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


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


def _tool(parse: Callable[[dict], dict],
          body: Callable[[ToolContext, dict], Coroutine[Any, Any, dict]], *,
          blocking_parse: bool = False, timeout_s: float = TOOL_TIMEOUT_S,
          finish: Callable[[dict, dict], dict] | None = None) -> Callable[..., dict]:
    """Wrap ``parse`` (sync arg validation) and ``body`` (loop coroutine) into a handler.

    With ``blocking_parse`` (``parse`` reads files / runs git), a call on an event-loop
    thread (the ``PluginContext.dispatch_tool`` path) is refused with ``WRONG_THREAD``
    before ``parse`` runs, so no blocking I/O ever runs on the gateway loop. ``finish``
    (``(parsed, result) -> result``) runs in the tool thread after a successful ``body``,
    e.g. to write a file; it is only allowed together with ``blocking_parse``.
    """
    assert finish is None or blocking_parse, "finish does I/O: needs the on-loop guard"

    def handler(args: Any = None, **kw: Any) -> dict:
        try:
            if blocking_parse and _on_any_loop():
                return _err("WRONG_THREAD")
            if not isinstance(args, dict):
                return _err("BAD_ARGS")
            try:
                parsed = parse(args)
            except _Refusal as r:
                return _err(r.code)
            ctx = resolve_context(kw.get("session_id"))
            if ctx.client is None:
                return _err("NOT_CONNECTED")

            async def run() -> dict:
                try:
                    return await body(ctx, parsed)
                except _Refusal as r:
                    return _err(r.code)
            result = run_on_adapter(ctx, run, timeout_s=timeout_s)
            if finish is not None and result.get("ok"):
                result = finish(parsed, result)
            return result
        except Exception as exc:  # noqa: BLE001 - Q3.4: a tool never raises
            return _internal(exc, "tool")
    return handler


async def _writable(ctx: ToolContext, ref: str) -> dict:
    """``issue_get(ref)`` if the session may write it (by its UUID), else ``_Refusal``."""
    if not ctx.is_linear or not ctx.session_issue_id:
        raise _Refusal("NOT_LINEAR_SESSION")
    issue = await ctx.client.issue_get(ref)
    code = await check_write_scope(ctx, issue["id"])
    if code is not None:
        raise _Refusal(code)
    return issue


async def _state_id(ctx: ToolContext, team_id: str, name: str) -> str:
    sid = resolve_state(await ctx.client.team_states(team_id), name)
    if sid is None:
        raise _Refusal("BAD_ARGS")
    return sid


async def _label_ids(ctx: ToolContext, team_id: str, add: list[str],
                     remove: list[str] = ()) -> tuple[list[str], list[str]]:
    """Label ids for names to ``add`` and ``remove`` (case-insensitive).

    An unknown name to add is ``LABEL_MISSING`` (probe P3: the app cannot create labels);
    an unknown name to remove is skipped, since no issue can carry it (Ruling S13).
    """
    if not add and not remove:
        return [], []
    by_name: dict[str, str] = {}
    for lb in await ctx.client.team_labels(team_id):
        by_name.setdefault((lb.get("name") or "").casefold(), lb["id"])
    out: list[list[str]] = []
    for names, strict in ((add, True), (remove, False)):
        ids: list[str] = []
        for n in names:
            lid = by_name.get(n.casefold())
            if lid is None:
                if strict:
                    raise _Refusal("LABEL_MISSING")
                continue
            if lid not in ids:
                ids.append(lid)
        out.append(ids)
    return out[0], out[1]


def _team_of(issue: dict) -> str:
    team = (issue.get("team") or {}).get("id")
    if not team:
        raise _Refusal("LINEAR_ERROR")
    return team


# -- linear_issue_get


def _parse_get(args: dict) -> dict:
    return {"id": _req_str(args, "id")}


async def _get(ctx: ToolContext, a: dict) -> dict:
    return {"ok": True, "issue": await ctx.client.issue_get(a["id"])}


# -- linear_issue_create


def _parse_create(args: dict) -> dict:
    return {"title": _req_str(args, "title"), "description": _opt_str(args, "description"),
            "parent": _opt_str(args, "parent"), "labels": _opt_names(args, "labels"),
            "state": _opt_str(args, "state")}


async def _create(ctx: ToolContext, a: dict) -> dict:
    if not ctx.is_linear or not ctx.session_issue_id:
        raise _Refusal("NOT_LINEAR_SESSION")
    home = await ctx.client.issue_get(ctx.session_issue_id)
    team = _team_of(home)
    inp: dict[str, Any] = {"teamId": team, "title": a["title"]}
    if (home.get("project") or {}).get("id"):
        inp["projectId"] = home["project"]["id"]
    if a["description"] is not None:
        inp["description"] = a["description"]
    if a["parent"] and a["parent"].strip():
        inp["parentId"] = (await _writable(ctx, a["parent"].strip()))["id"]
    if a["state"] is not None:
        inp["stateId"] = await _state_id(ctx, team, a["state"])
    labels, _ = await _label_ids(ctx, team, a["labels"])
    if labels:
        inp["labelIds"] = labels
    issue = await ctx.client.issue_create(inp)
    # On the loop (this coroutine): record the UUID Linear returned, never the identifier.
    ctx.adapter.tool_created.setdefault(ctx.session_key or "", set()).add(issue["id"])
    return {"ok": True, "issue": issue}


# -- linear_issue_update


def _parse_update(args: dict) -> dict:
    a = {"id": _req_str(args, "id"), "title": _opt_str(args, "title"),
         "description": _opt_str(args, "description"), "state": _opt_str(args, "state"),
         "add_labels": _opt_names(args, "add_labels"),
         "remove_labels": _opt_names(args, "remove_labels")}
    if a["title"] is not None and not a["title"].strip():
        raise _Refusal("BAD_ARGS")
    if not (a["title"] is not None or a["description"] is not None or a["state"] is not None
            or a["add_labels"] or a["remove_labels"]):
        raise _Refusal("BAD_ARGS")
    return a


async def _update(ctx: ToolContext, a: dict) -> dict:
    issue = await _writable(ctx, a["id"])
    team = _team_of(issue)
    inp: dict[str, Any] = {}
    for key in ("title", "description"):
        if a[key] is not None:
            inp[key] = a[key]
    if a["state"] is not None:
        inp["stateId"] = await _state_id(ctx, team, a["state"])
    added, removed = await _label_ids(ctx, team, a["add_labels"], a["remove_labels"])
    carried = _carried(issue)
    removed = [lid for lid in removed if lid in carried]  # removing an absent label: no-op
    if added:
        inp["addedLabelIds"] = added
    if removed:
        inp["removedLabelIds"] = removed
    if not inp:  # only no-op removals: never send an empty issueUpdate (Task 2 minor N1)
        return {"ok": True, "issue": _summary(issue)}
    return {"ok": True, "issue": await ctx.client.issue_update(issue["id"], inp)}


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
#: Loop-side budget for one plan sync. A ~30-task plan costs up to ~30 issue reads, ~30
#: writes and a few comment calls, one GraphQL round trip each (0.3–1 s live): more than
#: the generic TOOL_TIMEOUT_S (30 s). Every step is idempotent, so a TIMEOUT is retried
#: safely (children are adopted by their ``T<n> ·`` title on the rerun).
PLAN_SYNC_TIMEOUT_S = 120.0
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
            # On the loop (this coroutine): the UUID Linear returned, never the identifier.
            ctx.adapter.tool_created.setdefault(ctx.session_key or "", set()).add(new["id"])
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
        cancel_state = cancel_state or await _state_id(ctx, team, "canceled")
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
         "summary": _opt_str(args, "summary"), "question": _opt_str(args, "question")}
    if a["state"] == "blocked":
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
    """The id of the team's (or workspace's) ``Blocked`` label, case-insensitive."""
    want = BLOCKED_LABEL.casefold()
    return next((lb["id"] for lb in await ctx.client.team_labels(team)
                 if (lb.get("name") or "").casefold() == want and lb.get("id")), None)


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
        target = _target_state(await client.team_states(team), current, state)
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
    # like every write target, before anything is written.
    project = await _writable(ctx, ctx.session_issue_id or "")
    if label is not None and not carried:
        await client.issue_update(task["id"], {"addedLabelIds": [label]})
    await client.comment_create(
        project["id"], f"**{task.get('identifier')} needs input:** {a['question']}")
    _track_open_task(ctx, task["id"], False)  # posted: blocked, even with LABEL_MISSING
    if label is None:  # Ruling S18: the question is posted, the label could not be
        return {"ok": False, "error": "LABEL_MISSING", "issue": task.get("identifier"),
                "label_missing": True, "question_posted": True}
    out["question_posted"] = True
    return out


linear_issue_get = _tool(_parse_get, _get)
linear_issue_create = _tool(_parse_create, _create)
linear_issue_update = _tool(_parse_update, _update)
linear_comment = _tool(_parse_comment, _comment)
linear_sync_spec = _tool(_parse_sync_spec, _sync_spec, blocking_parse=True)
linear_sync_plan = _tool(_parse_sync_plan, lambda ctx, a: _sync_plan(ctx, a),  # late-bound
                         blocking_parse=True, timeout_s=PLAN_SYNC_TIMEOUT_S,
                         finish=_finish_sync_plan)
linear_task_state = _tool(_parse_task_state, _task_state)

_ISSUE_REF = "Issue identifier (e.g. RET-12) or UUID."

TOOL_SCHEMAS: dict[str, dict] = {
    "linear_issue_get": {
        "name": "linear_issue_get",
        "description": ("Read a Linear issue as the Linear app: state, team, project and links, "
                        "parent, labels and sub-issues."),
        "parameters": {"type": "object", "properties": {"id": {"type": "string",
                                                               "description": _ISSUE_REF}},
                       "required": ["id"]},
    },
    "linear_issue_create": {
        "name": "linear_issue_create",
        "description": ("Create a Linear issue as the Linear app, in the session issue's team "
                        "and project. Set parent to make it a sub-issue (parent must be the "
                        "session issue, a descendant, or an issue you created)."),
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
            "description": {"type": "string", "description": "Markdown."},
            "parent": {"type": "string", "description": _ISSUE_REF},
            "labels": {"type": "array", "items": {"type": "string"},
                       "description": "Existing label names."},
            "state": {"type": "string",
                      "description": "Workflow state name, or a type such as started."},
        }, "required": ["title"]},
    },
    "linear_issue_update": {
        "name": "linear_issue_update",
        "description": ("Update a Linear issue as the Linear app: title, description, state, "
                        "labels. Only the session issue, its descendants, or issues you "
                        "created."),
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
        }, "required": ["id"]},
    },
    "linear_comment": {
        "name": "linear_comment",
        "description": ("Comment on a Linear issue as the Linear app. With edit_marker, edit "
                        "the app's earlier comment carrying that marker instead of adding a "
                        "new one."),
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
        "description": ("Mirror a plan file onto Linear as the Linear app: the plan as one "
                        "comment on the ticket named by its 'Linear: <ID>' header, and one "
                        "sub-ticket 'T<n> · <name>' per '### Task <n>: <name>' heading, in "
                        "plan order. Headings with '— <ID>' update that ticket; others adopt "
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
                        "ticket as '**<ID> needs input:** <question>' (LABEL_MISSING if the "
                        "team has no Blocked label, the question is still posted); "
                        "unblocked: remove the Blocked label. Already-applied changes are "
                        "not rewritten."),
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string", "description": _ISSUE_REF},
            "state": {"type": "string", "enum": list(TASK_STATES)},
            "summary": {"type": "string",
                        "description": "Markdown; with done, posted on the task ticket."},
            "question": {"type": "string",
                         "description": "Required with blocked: what you need answered."},
        }, "required": ["id", "state"]},
    },
}

TOOL_HANDLERS: dict[str, Callable[..., dict]] = {
    "linear_issue_get": linear_issue_get,
    "linear_issue_create": linear_issue_create,
    "linear_issue_update": linear_issue_update,
    "linear_comment": linear_comment,
    "linear_sync_spec": linear_sync_spec,
    "linear_sync_plan": linear_sync_plan,
    "linear_task_state": linear_task_state,
}


__all__ = ["TOOL_HANDLERS", "TOOL_SCHEMAS", "ToolContext", "check_write_scope",
           "linear_comment", "linear_issue_create", "linear_issue_get", "linear_issue_update",
           "linear_sync_plan", "linear_sync_spec", "linear_task_state",
           "resolve_context", "resolve_state", "run_on_adapter"]
