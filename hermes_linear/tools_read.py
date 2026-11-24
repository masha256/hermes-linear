"""Read tools (RET-120 spec §1.1–1.2, §2.1–2.3): ``linear_issue_get``'s fields, relations,
attachments and comment threads, and ``linear_search``.

Built on ``agent_tools``' ``_tool`` machinery. This module registers itself at its end:
it merges ``SCHEMAS`` / ``HANDLERS`` into ``agent_tools.TOOL_SCHEMAS`` / ``TOOL_HANDLERS``
and sets each handler as an ``agent_tools`` attribute. ``agent_tools`` only imports this
module at its bottom, so either module may be imported first (Ruling R3).
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from . import agent_tools
from .agent_tools import _ISSUE_REF, ToolContext, _opt_str, _Refusal, _req_str, _tool

ISSUE_GET_MAX_CHARS = 50_000  # serialized result size above which the oldest threads drop
COMMENTS_MAX = 50  # most threads one call may ask for
SEARCH_LIMIT_DEFAULT = 25
SEARCH_LIMIT_MAX = 50
STATE_TYPES = ("triage", "backlog", "unstarted", "started", "completed", "canceled")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _nodes(conn: Any) -> list[dict]:
    nodes = conn.get("nodes") if isinstance(conn, dict) else None
    return [n for n in nodes if isinstance(n, dict)] if isinstance(nodes, list) else []


def _ref(v: Any, *keys: str) -> dict | None:
    return {k: v.get(k) for k in keys} if isinstance(v, dict) else None


def _related(i: Any) -> dict | None:
    if not isinstance(i, dict):
        return None
    return {"identifier": i.get("identifier"), "title": i.get("title"),
            "state": (i.get("state") or {}).get("name")}


def shape_details(node: dict) -> dict:
    """The §1.1 keys from a raw ``issue_details`` node."""
    relations = (
        [{"type": r.get("type"), "direction": "outbound", "issue": _related(r.get("relatedIssue"))}
         for r in _nodes(node.get("relations"))]
        + [{"type": r.get("type"), "direction": "inbound", "issue": _related(r.get("issue"))}
           for r in _nodes(node.get("inverseRelations"))])
    return {
        "assignee": _ref(node.get("assignee"), "id", "name"),
        "delegate": _ref(node.get("delegate"), "id", "name"),
        "priority": node.get("priority"),
        "priority_label": node.get("priorityLabel"),
        "estimate": node.get("estimate"),
        "due_date": node.get("dueDate"),
        "cycle": _ref(node.get("cycle"), "id", "number", "name"),
        "milestone": _ref(node.get("projectMilestone"), "id", "name"),
        "relations": relations,
        "attachments": [{"id": a.get("id"), "title": a.get("title"),
                         "subtitle": a.get("subtitle"), "url": a.get("url"),
                         "source_type": a.get("sourceType")}
                        for a in _nodes(node.get("attachments"))],
    }


def _comment(c: dict, app_id: str) -> dict:
    user = c.get("user")
    return {"id": c.get("id"), "author": _ref(user, "id", "name"),
            "author_is_app": isinstance(user, dict) and user.get("id") == app_id,
            "created_at": c.get("createdAt"), "body": c.get("body") or "",
            "resolved": c.get("resolvedAt") is not None}


def _created(c: dict) -> str:
    return c.get("createdAt") or ""


def shape_threads(nodes: list[dict], app_id: str) -> list[dict]:
    """Threads newest first, each thread's replies oldest first (sorted here, whatever
    Linear's order). ``author_is_app``: the author is the app user ``app_id``."""
    out = []
    for t in sorted(nodes, key=_created, reverse=True):
        thread = _comment(t, app_id)
        thread["url"] = t.get("url")
        thread["replies"] = [_comment(r, app_id)
                             for r in sorted(_nodes(t.get("children")), key=_created)]
        out.append(thread)
    return out


def trim(result: dict, limit: int = ISSUE_GET_MAX_CHARS) -> dict:
    """Drop the oldest threads (the end of ``issue.comments``) while ``result`` serializes
    to more than ``limit`` chars, setting ``comments_truncated``. Without threads left, a
    large result is returned as it is."""
    issue = result.get("issue") or {}
    threads = issue.get("comments")
    if not isinstance(threads, list):
        return result
    while threads and len(json.dumps(result, ensure_ascii=False, default=str)) > limit:
        threads.pop()
        issue["comments_truncated"] = True
    return result


def _parse_get(args: dict) -> dict:
    n = args.get("comments")
    if n is None:
        n = 0
    if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= COMMENTS_MAX:
        raise _Refusal("BAD_ARGS")
    return {"id": _req_str(args, "id"), "comments": n}


async def _get(ctx: ToolContext, a: dict) -> dict:
    issue = await ctx.client.issue_get(a["id"])
    issue.update(shape_details(await ctx.client.issue_details(issue["id"])))
    result = {"ok": True, "issue": issue}
    if a["comments"] > 0:
        nodes = await ctx.client.issue_threads(issue["id"], a["comments"])
        issue["comments"] = shape_threads(nodes, await ctx.client.viewer_id())
        issue["comments_truncated"] = False
        trim(result)
    return result


linear_issue_get = _tool(_parse_get, _get)


def _person(value: str) -> dict:
    if value == "me":
        return {"isMe": {"eq": True}}
    if "@" in value:
        return {"email": {"eqIgnoreCase": value}}
    return {"or": [{"name": {"eqIgnoreCase": value}}, {"displayName": {"eqIgnoreCase": value}}]}


def _state(value: str) -> dict:
    if value.casefold() in STATE_TYPES:
        return {"type": {"eq": value.casefold()}}
    return {"name": {"eqIgnoreCase": value}}


def _project(value: str) -> dict:
    if _UUID_RE.match(value):
        return {"id": {"eq": value}}
    return {"name": {"eqIgnoreCase": value}}


#: ``linear_search``'s filter arguments in signature order: (argument, IssueFilter key, map).
_FILTERS: tuple[tuple[str, str, Callable[[str], dict]], ...] = (
    ("team", "team", lambda v: {"key": {"eq": v.upper()}}),
    ("project", "project", _project),
    ("state", "state", _state),
    ("assignee", "assignee", _person),
    ("delegate", "delegate", _person),
    ("label", "labels", lambda v: {"some": {"name": {"eqIgnoreCase": v}}}),
)


def build_issue_filter(args: dict, parent_id: str | None) -> dict:
    """The ``IssueFilter`` for ``linear_search``'s typed arguments (spec §2.3), ANDed in
    signature order; ``parent_id`` is the parent's resolved UUID. One clause is returned
    bare, none is ``{}``."""
    clauses = [{key: build(args[arg])} for arg, key, build in _FILTERS
               if args.get(arg) is not None]
    if parent_id is not None:
        clauses.append({"parent": {"id": {"eq": parent_id}}})
    if not clauses:
        return {}
    return clauses[0] if len(clauses) == 1 else {"and": clauses}


def _parse_search(args: dict) -> dict:
    out: dict[str, Any] = {}
    for key in ("text", "parent", *(arg for arg, _, _ in _FILTERS)):
        out[key] = _req_str(args, key).strip() if args.get(key) is not None else None
    out["cursor"] = _opt_str(args, "cursor") or None
    inc = args.get("include_comments")
    if inc is not None and not isinstance(inc, bool):
        raise _Refusal("BAD_ARGS")
    if inc and out["text"] is None:
        raise _Refusal("BAD_ARGS")  # searching comments needs a search term
    out["include_comments"] = bool(inc)
    n = args.get("limit")
    if n is None:
        n = SEARCH_LIMIT_DEFAULT
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= SEARCH_LIMIT_MAX:
        raise _Refusal("BAD_ARGS")
    out["limit"] = n
    return out


def _name(v: Any) -> str | None:
    return v.get("name") if isinstance(v, dict) else None


def _row(n: dict) -> dict:
    return {"identifier": n.get("identifier"), "title": n.get("title"),
            "state": _name(n.get("state")), "assignee": _name(n.get("assignee")),
            "delegate": _name(n.get("delegate")), "priority": n.get("priority"),
            "project": _name(n.get("project")), "url": n.get("url"),
            "updated_at": n.get("updatedAt")}


async def _search(ctx: ToolContext, a: dict) -> dict:
    parent_id = None
    if a["parent"] is not None:
        parent_id = (await ctx.client.issue_get(a["parent"]))["id"]
    flt = build_issue_filter(a, parent_id)
    if a["text"] is not None:
        conn = await ctx.client.search_issues(a["text"], flt, a["include_comments"],
                                              a["limit"], a["cursor"])
    else:
        conn = await ctx.client.list_issues(flt, a["limit"], a["cursor"])
    info = conn.get("pageInfo") if isinstance(conn.get("pageInfo"), dict) else {}
    return {"ok": True, "issues": [_row(n) for n in _nodes(conn)],
            "next_cursor": info.get("endCursor") if info.get("hasNextPage") else None}


linear_search = _tool(_parse_search, _search)


def _str_prop(description: str) -> dict:
    return {"type": "string", "description": description}

SCHEMAS: dict[str, dict] = {
    "linear_issue_get": {
        "name": "linear_issue_get",
        "description": ("Read a Linear issue as the Linear app: state, team, project and links, "
                        "parent, labels, sub-issues, assignee, delegate, priority, estimate, "
                        "due date, cycle, milestone, relations and attachments."),
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string", "description": _ISSUE_REF},
            "comments": {"type": "integer", "minimum": 0, "maximum": COMMENTS_MAX,
                         "description": ("Also return the newest N comment threads (default "
                                         "0), newest first, each with up to its 50 newest "
                                         "replies, oldest first. Past 50,000 chars the "
                                         "oldest threads are dropped and comments_truncated "
                                         "is true.")},
        }, "required": ["id"]},
    },
    "linear_search": {
        "name": "linear_search",
        "description": ("Find Linear issues as the Linear app. With text: full-text search; "
                        "without: the most recently updated issues. Typed filters are "
                        "ANDed. Rows: identifier, title, state, assignee, delegate, "
                        "priority, project, url, updated_at. For anything more complex "
                        "(OR, dates, nested filters) use linear_query."),
        "parameters": {"type": "object", "properties": {
            "text": _str_prop("Search term (with include_comments, comments too)."),
            "team": _str_prop("Team key, e.g. RET."),
            "project": _str_prop("Project UUID or name (case-insensitive)."),
            "state": _str_prop("Workflow type (triage, backlog, unstarted, started, "
                               "completed, canceled) or a state name (case-insensitive)."),
            "assignee": _str_prop("'me' (the app), an email, or a name (case-insensitive)."),
            "delegate": _str_prop("'me' (the app), an email, or a name (case-insensitive)."),
            "label": _str_prop("One label name (case-insensitive)."),
            "parent": _str_prop("Parent issue: " + _ISSUE_REF),
            "include_comments": {"type": "boolean",
                                 "description": "Also match comments (needs text)."},
            "limit": {"type": "integer", "minimum": 1, "maximum": SEARCH_LIMIT_MAX,
                      "description": "Page size (default 25)."},
            "cursor": _str_prop("next_cursor of a previous page."),
        }},
    },
}

HANDLERS: dict[str, Callable[..., dict]] = {
    "linear_issue_get": linear_issue_get,
    "linear_search": linear_search,
}

# Register with ``agent_tools`` (Ruling R3). ``update`` keeps the existing placeholder
# slot, so ``linear_issue_get`` stays first in both rosters.
agent_tools.TOOL_SCHEMAS.update(SCHEMAS)
agent_tools.TOOL_HANDLERS.update(HANDLERS)
for _tool_name, _handler in HANDLERS.items():
    setattr(agent_tools, _tool_name, _handler)
