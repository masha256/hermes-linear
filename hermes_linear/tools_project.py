"""``linear_project`` (RET-122 spec §1.1, §2, §3; RET-165): read a Linear project and post
project updates as the app.

- The project is ``project`` (a name or UUID, through the directory's ``projects`` kind);
  without it the session ticket's project, outside a Linear session ``default_project``.
  Neither is ``PROJECT_NOT_FOUND`` with ``candidates``, never an empty success.
- ``get`` is a read: it runs ungated (outside a Linear session too, even when
  ``outside_session_writes`` is ``deny``; spec §2.3) and never records a target. Update
  bodies are cut at ``PROJECT_UPDATE_BODY_MAX`` and the result is capped as
  ``linear_issue_get``'s (``tools_read.trim``: the oldest updates go first).
- ``post_update`` always records the project as a target (``- project "<name>"``), the
  session ticket's own project included (Q3, spec §2.1). Not idempotent.
- ``milestone`` / ``link`` are in the schema but answer ``BAD_ARGS`` until RET-166.

Every read, resolution and scope step comes before ``_check_gate``, so a first pass that
needs approval writes nothing. Like ``tools_document``, this module registers itself at its
end (Ruling R3).
"""

from __future__ import annotations

from typing import Any, Callable

from . import agent_tools, directory, issue_fields, tools_read
from .agent_tools import _Refusal

PROJECT_UPDATE_BODY_MAX = 4_000
PROJECT_UPDATES_DEFAULT = 3
PROJECT_UPDATES_MAX = 20

ACTIONS = ("get", "post_update", "milestone", "link")

#: The agent's health names and Linear's own, to Linear's ``ProjectUpdateHealthType``.
HEALTH = {"on_track": "onTrack", "at_risk": "atRisk", "off_track": "offTrack",
          "onTrack": "onTrack", "atRisk": "atRisk", "offTrack": "offTrack"}

#: The arguments each action takes besides ``action``; any other one is ``BAD_ARGS``.
_ARGS: dict[str, frozenset[str]] = {
    "get": frozenset({"project", "updates"}),
    "post_update": frozenset({"project", "body", "health"}),
    "milestone": frozenset({"project", "milestone", "name", "description", "target_date"}),
    "link": frozenset({"project", "url", "label"}),
}

#: Actions whose bodies land with RET-166; until then their calls are ``BAD_ARGS``.
_NOT_YET = frozenset({"milestone", "link"})


def _parse(args: dict) -> dict:
    action = args.get("action")
    if action not in ACTIONS or action in _NOT_YET:
        raise _Refusal("BAD_ARGS")
    # An argument of another action never silently drops input (spec §1.1, §3). A null one
    # carries none, so it counts as absent (as linear_document).
    if any(v is not None for k, v in args.items() if k != "action" and k not in _ARGS[action]):
        raise _Refusal("BAD_ARGS")
    project = agent_tools._opt_str(args, "project")
    if project is not None and not project.strip():
        raise _Refusal("BAD_ARGS")
    out: dict[str, Any] = {"action": action, "project": project.strip() if project else None}
    if action == "get":
        n = args.get("updates")
        if n is None:
            n = PROJECT_UPDATES_DEFAULT
        if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= PROJECT_UPDATES_MAX:
            raise _Refusal("BAD_ARGS")
        out["updates"] = n
        return out
    body = agent_tools._req_str(args, "body")
    health = agent_tools._opt_str(args, "health")
    if health is not None and health not in HEALTH:
        raise _Refusal("BAD_ARGS")
    out.update(body=body, health=HEALTH[health] if health is not None else None)
    return out


async def _project(ctx: agent_tools.ToolContext, ref: str | None) -> dict:
    """The project ``{id, name}``: ``ref`` resolved through the directory; else the session
    ticket's project; else, outside a Linear session, ``default_project`` (a miss names the
    setting). None of them: ``PROJECT_NOT_FOUND`` with the workspace's projects as
    ``candidates`` (spec §1.1)."""
    names = directory.for_client(ctx.client)
    if ref is not None:
        row = await names.resolve("projects", "", ref, issue_fields._by_name)
        return {"id": row["id"], "name": row.get("name") or row["id"]}
    if ctx.session_issue_id and not ctx.outside:
        issue = await ctx.client.issue_get(ctx.session_issue_id)
        project = issue.get("project") or {}
        if project.get("id"):
            return {"id": project["id"], "name": project.get("name") or project["id"]}
    elif ctx.outside:
        settings = getattr(ctx.adapter, "_settings", None)
        default = (getattr(settings, "default_project", "") or "").strip()
        if default:
            try:
                row = await names.resolve("projects", "", default, issue_fields._by_name)
            except _Refusal as exc:
                raise agent_tools._setting_miss(exc, "default_project", default) from None
            return {"id": row["id"], "name": row.get("name") or row["id"]}
    raise directory.miss("projects", await names.rows("projects"), "")


def _ask_project(ctx: agent_tools.ToolContext, project: dict) -> None:
    """A project write always asks (Q3): approved on this pass, recorded on a first pass,
    else ``OUT_OF_SCOPE`` (Ruling 4: ``agent_tools._scope_target``)."""
    agent_tools._scope_target(ctx, project, "project")


def _cut(update: dict) -> dict:
    body = update.get("body") or ""
    if len(body) > PROJECT_UPDATE_BODY_MAX:
        return {**update, "body": body[:PROJECT_UPDATE_BODY_MAX], "body_truncated": True}
    return {**update, "body": body}


async def _get(ctx: agent_tools.ToolContext, a: dict) -> dict:
    project = await _project(ctx, a["project"])
    detail = await ctx.client.project_detail(project["id"], a["updates"])
    detail["updates"] = [_cut(u) for u in detail.get("updates") or []]
    return tools_read.trim({"ok": True, "project": detail}, node="project", items="updates",
                           flag="updates_truncated")


async def _post_update(ctx: agent_tools.ToolContext, a: dict) -> dict:
    if not ctx.outside and (not ctx.is_linear or not ctx.session_issue_id):
        raise _Refusal("NOT_LINEAR_SESSION")
    project = await _project(ctx, a["project"])
    _ask_project(ctx, project)
    agent_tools._check_gate(ctx)
    made = await ctx.client.project_update_create(project["id"], a["body"], a["health"])
    return {"ok": True, "update_id": made["id"], "url": made.get("url")}


_read_tool = agent_tools._tool(_parse, _get)
_write_tool = agent_tools._tool(_parse, _post_update, gated="linear_project")


def linear_project(args: Any = None, **kw: Any) -> dict:
    """``get`` runs ungated (a read); the writes through the gate."""
    if isinstance(args, dict) and args.get("action") == "get":
        return _read_tool(args, **kw)
    return _write_tool(args, **kw)


SCHEMAS: dict[str, dict] = {
    "linear_project": {
        "name": "linear_project",
        "description": (
            "Read a Linear project or post a project update as the Linear app. Without "
            "project: the session ticket's project (outside a Linear session, "
            "default_project); none is PROJECT_NOT_FOUND with candidates. get (updates? "
            "0-20, default 3): state, health, lead, target_date, links, milestones, "
            "documents and the newest updates (bodies cut at 4,000 chars, body_truncated). "
            "post_update (body; health?): posts to the whole project, which sets its "
            "health; a re-run posts again. Every project write needs approval, the session "
            "ticket's own project included. milestone and link are not available yet. "
            + agent_tools._GATED_NOTE),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": list(ACTIONS),
                       "description": "get, post_update, milestone or link."},
            "project": {"type": "string", "description": "Project name or UUID."},
            "updates": {"type": "integer", "description": (
                "get: how many of the newest project updates (0-20, default 3).")},
            "body": {"type": "string", "description": "post_update (required): markdown."},
            "health": {"type": "string", "enum": list(HEALTH),
                       "description": "post_update: the project's health."},
            "milestone": {"type": "string",
                          "description": "milestone: the milestone to edit (name or UUID)."},
            "name": {"type": "string", "description": "milestone: its name."},
            "description": {"type": ["string", "null"],
                            "description": "milestone: its description (null clears)."},
            "target_date": {"type": ["string", "null"], "description": (
                "milestone: YYYY-MM-DD (null clears).")},
            "url": {"type": "string", "description": "link: an http(s) URL."},
            "label": {"type": "string", "description": "link: its label."},
        }, "required": ["action"]},
    },
}

HANDLERS: dict[str, Callable[..., dict]] = {"linear_project": linear_project}

# Register with ``agent_tools`` (Ruling R3). ``update`` keeps the placeholder slot, so the
# roster order does not depend on which module was imported first.
agent_tools.TOOL_SCHEMAS.update(SCHEMAS)
agent_tools.TOOL_HANDLERS.update(HANDLERS)
for _tool_name, _handler in HANDLERS.items():
    setattr(agent_tools, _tool_name, _handler)
