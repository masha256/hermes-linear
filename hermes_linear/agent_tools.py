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
          blocking_parse: bool = False) -> Callable[..., dict]:
    """Wrap ``parse`` (sync arg validation) and ``body`` (loop coroutine) into a handler.

    With ``blocking_parse`` (``parse`` reads files / runs git), a call on an event-loop
    thread (the ``PluginContext.dispatch_tool`` path) is refused with ``WRONG_THREAD``
    before ``parse`` runs, so no blocking I/O ever runs on the gateway loop.
    """
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
            return run_on_adapter(ctx, run)
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
    if added:
        inp["addedLabelIds"] = added
    if removed:
        inp["removedLabelIds"] = removed
    return {"ok": True, "issue": await ctx.client.issue_update(issue["id"], inp)}


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


linear_issue_get = _tool(_parse_get, _get)
linear_issue_create = _tool(_parse_create, _create)
linear_issue_update = _tool(_parse_update, _update)
linear_comment = _tool(_parse_comment, _comment)
linear_sync_spec = _tool(_parse_sync_spec, _sync_spec, blocking_parse=True)

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
}

TOOL_HANDLERS: dict[str, Callable[..., dict]] = {
    "linear_issue_get": linear_issue_get,
    "linear_issue_create": linear_issue_create,
    "linear_issue_update": linear_issue_update,
    "linear_comment": linear_comment,
    "linear_sync_spec": linear_sync_spec,
}


__all__ = ["TOOL_HANDLERS", "TOOL_SCHEMAS", "ToolContext", "check_write_scope",
           "linear_comment", "linear_issue_create", "linear_issue_get", "linear_issue_update",
           "linear_sync_spec",
           "resolve_context", "resolve_state", "run_on_adapter"]
