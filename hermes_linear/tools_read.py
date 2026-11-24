"""Read tools (RET-120 spec §1.1, §2.1–2.2): ``linear_issue_get``'s fields, relations,
attachments and comment threads.

Built on ``agent_tools``' ``_tool`` machinery. ``agent_tools`` imports this module at its
end and merges ``SCHEMAS`` / ``HANDLERS`` into its roster, so import ``agent_tools`` (not
this module) first.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from .agent_tools import _ISSUE_REF, ToolContext, _Refusal, _req_str, _tool

ISSUE_GET_MAX_CHARS = 50_000  # serialized result size above which the oldest threads drop
COMMENTS_MAX = 50  # most threads one call may ask for


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
                                         "0), newest first, each with its replies oldest "
                                         "first. Past 50,000 chars the oldest threads are "
                                         "dropped and comments_truncated is true.")},
        }, "required": ["id"]},
    },
}

HANDLERS: dict[str, Callable[..., dict]] = {
    "linear_issue_get": linear_issue_get,
}
