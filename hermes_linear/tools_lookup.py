"""``linear_lookup`` (RET-121 spec §1.7; RET-143): list the names the write tools accept,
served from the cached directory (``directory``, §2.2).

A read: no scope check and no gate. Rows come from ``Directory.rows``, so a lookup never
forces a refetch; only an explicit ``team`` / ``project`` that misses goes through the
directory's refetch-once rule before ``TEAM_NOT_FOUND`` / ``PROJECT_NOT_FOUND``.

Built on ``agent_tools``' ``_tool`` machinery. Like ``tools_read``, this module registers
itself at its end (Ruling R3).
"""

from __future__ import annotations

from typing import Callable

from . import agent_tools, directory, issue_fields
from .agent_tools import _Refusal
from .linear_client import LinearError

LOOKUP_KINDS = ("teams", "states", "labels", "users", "cycles", "projects", "milestones")
LOOKUP_MAX_ROWS = 250

_TEAM_KINDS = frozenset({"states", "labels", "cycles"})

#: kind -> the keys a row carries (spec §1.7), in output order.
_ROW_KEYS: dict[str, tuple[str, ...]] = {
    "teams": ("id", "name", "key"),
    "states": ("id", "name", "type"),
    "labels": ("id", "name"),
    "users": ("id", "name", "displayName", "app", "active"),
    "cycles": ("id", "name", "number", "startsAt", "endsAt"),
    "projects": ("id", "name", "state"),
    "milestones": ("id", "name"),
}

#: Row keys ``query`` is matched on (email although rows don't return it).
_QUERY_KEYS = ("name", "key", "displayName", "email")


def _opt_ref(args: dict, key: str) -> str | None:
    """An optional string arg, stripped; blank is absent."""
    val = agent_tools._opt_str(args, key)
    return (val.strip() or None) if val is not None else None


def _parse_lookup(args: dict) -> dict:
    kind = args.get("kind")
    if kind not in LOOKUP_KINDS:
        raise _Refusal("BAD_ARGS")
    return {"kind": kind, "team": _opt_ref(args, "team"),
            "project": _opt_ref(args, "project"), "query": _opt_ref(args, "query")}


async def _session_ticket(ctx: agent_tools.ToolContext) -> dict:
    if not ctx.is_linear or not ctx.session_issue_id:
        raise _Refusal("NOT_LINEAR_SESSION")
    return await ctx.client.issue_get(ctx.session_issue_id)


async def _scope(ctx: agent_tools.ToolContext, d: directory.Directory,
                 a: dict) -> str | None:
    """The team or project UUID ``a["kind"]`` is listed in ("" for workspace-wide kinds;
    ``None`` for milestones of a session ticket without a project)."""
    kind = a["kind"]
    if kind in _TEAM_KINDS:
        if a["team"] is not None:
            return (await d.resolve("teams", "", a["team"], issue_fields._by_team))["id"]
        return agent_tools._team_of(await _session_ticket(ctx))
    if kind == "milestones":
        if a["project"] is not None:
            return (await d.resolve("projects", "", a["project"], issue_fields._by_name))["id"]
        return agent_tools._project_of(await _session_ticket(ctx))
    return ""


def _hit(row: dict, want: str) -> bool:
    return any(want in str(row.get(k) or "").casefold() for k in _QUERY_KEYS)


async def _lookup(ctx: agent_tools.ToolContext, a: dict) -> dict:
    kind = a["kind"]
    d = directory.for_client(ctx.client)
    try:
        scope = await _scope(ctx, d, a)
        rows = [] if scope is None else await d.rows(kind, scope)
    except LinearError as exc:
        return {"ok": False, "error": exc.code or "LINEAR_ERROR",
                "message": exc.detail or str(exc)}
    if a["query"] is not None:
        want = a["query"].casefold()
        rows = [r for r in rows if _hit(r, want)]
    keys = _ROW_KEYS[kind]
    out: dict = {"ok": True, "kind": kind,
                 "rows": [{k: r.get(k) for k in keys} for r in rows[:LOOKUP_MAX_ROWS]]}
    if len(rows) > LOOKUP_MAX_ROWS:
        out["truncated"] = True
    return out


linear_lookup = agent_tools._tool(_parse_lookup, _lookup)


def _str_prop(description: str) -> dict:
    return {"type": "string", "description": description}


SCHEMAS: dict[str, dict] = {
    "linear_lookup": {
        "name": "linear_lookup",
        "description": (
            "List what the Linear write tools accept, as the Linear app: teams (id, name, "
            "key), states (id, name, type), labels (the team's and the workspace's), users "
            "(id, name, displayName, app, active), cycles (id, name, number, startsAt, "
            "endsAt), projects (id, name, state) or milestones. states, labels and cycles "
            "are per team (default: the session ticket's), milestones per project (default: "
            "the session ticket's); outside a Linear session these need team / project, "
            "else NOT_LINEAR_SESSION. Unknown team or project: TEAM_NOT_FOUND / "
            "PROJECT_NOT_FOUND with candidates. At most 250 rows (truncated: true past "
            "that; narrow with query). Cached for up to 5 minutes."),
        "parameters": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": list(LOOKUP_KINDS),
                     "description": "What to list."},
            "team": _str_prop("For states, labels, cycles: team UUID, key (e.g. RET) or "
                              "name (case-insensitive)."),
            "project": _str_prop("For milestones: project UUID or name (case-insensitive)."),
            "query": _str_prop("Case-insensitive substring of the name (users: also "
                               "displayName and email; teams: also key)."),
        }, "required": ["kind"]},
    },
}

HANDLERS: dict[str, Callable[..., dict]] = {"linear_lookup": linear_lookup}

# Register with ``agent_tools`` (Ruling R3). ``update`` keeps the placeholder slot, so the
# roster order does not depend on which module was imported first.
agent_tools.TOOL_SCHEMAS.update(SCHEMAS)
agent_tools.TOOL_HANDLERS.update(HANDLERS)
for _tool_name, _handler in HANDLERS.items():
    setattr(agent_tools, _tool_name, _handler)
