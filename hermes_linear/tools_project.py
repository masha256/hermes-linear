"""``linear_project`` (RET-122 spec §1.1, §2, §3; RET-165, RET-166): read a Linear project,
post project updates, create and edit milestones and add project links as the app.

- The project is ``project`` (a name or UUID, through the directory's ``projects`` kind);
  without it the session ticket's project, outside a Linear session ``default_project``.
  Neither is ``PROJECT_NOT_FOUND`` with ``candidates``, never an empty success.
- ``get`` is a read: it runs ungated (outside a Linear session too, even when
  ``outside_session_writes`` is ``deny``; spec §2.3) and never records a target. Update
  bodies are cut at ``PROJECT_UPDATE_BODY_MAX`` and the result is capped as
  ``linear_issue_get``'s (``tools_read.trim``: the oldest updates go first).
- ``post_update``, ``milestone`` and ``link`` always record the project as a target
  (``- project "<name>"``), the session ticket's own project included (Q3, spec §2.1).
  ``post_update`` is not idempotent. ``milestone`` resolves an existing one in the project
  (directory ``milestones`` kind); Linear's duplicate-name refusal (seen only on write) is
  ``ALREADY_EXISTS``. ``link`` reads the project's links first: the same URL (both
  stripped) is ``ALREADY_LINKED`` before the gate, so a repeat never asks or writes.

Every read, resolution and scope step comes before ``_check_gate``, so a first pass that
needs approval writes nothing. Like ``tools_document``, this module registers itself at its
end (Ruling R3).
"""

from __future__ import annotations

import re
from typing import Any, Callable

from . import agent_tools, directory, issue_fields, tools_read
from .agent_tools import _Refusal
from .linear_client import LinearError

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

#: ``link``'s URL after ``strip()`` (spec §1.1): http(s) only, no whitespace inside.
_LINK_URL = re.compile(r"^https?://\S+$")

#: ``description: null`` (or blank) on a milestone update: Linear leaves the description as
#: it is (RET-166 probe, spec §7), so the tool refuses rather than report a clear it can't do.
MILESTONE_DESCRIPTION_CLEAR_MESSAGE = (
    "Linear can't clear a milestone description: null or an empty string leaves it "
    "unchanged. Pass new text to replace it.")


def _blank_to_none(v: Any) -> Any:
    return None if isinstance(v, str) and not v.strip() else v


def _date(v: Any) -> str | None:
    """``target_date``: ``YYYY-MM-DD`` (issue_fields' ``due_date`` grammar), blank → None."""
    if isinstance(v, str):
        v = v.strip()
    if v is None or v == "":
        return None
    return issue_fields._due_date(v)


def _parse_milestone(args: dict, out: dict) -> dict:
    ref = agent_tools._opt_str(args, "milestone")
    if ref is not None and not ref.strip():
        raise _Refusal("BAD_ARGS")
    name = agent_tools._opt_str(args, "name")
    if name is not None:
        if not name.strip():
            raise _Refusal("BAD_ARGS")
        name = name.strip()
    description = _blank_to_none(agent_tools._opt_str(args, "description"))
    target_date = _date(args.get("target_date"))
    if ref is None:  # create: a null description / target_date is simply left out
        if name is None:
            raise _Refusal("BAD_ARGS")
        out.update(milestone=None, name=name, description=description,
                   target_date=target_date)
        return out
    fields: dict[str, Any] = {}
    if name is not None:
        fields["name"] = name
    if "description" in args:
        if description is None:
            raise _Refusal("BAD_ARGS", extra={"message": MILESTONE_DESCRIPTION_CLEAR_MESSAGE})
        fields["description"] = description
    if "target_date" in args:  # null clears (probed, spec §7)
        fields["targetDate"] = target_date
    if not fields:
        raise _Refusal("BAD_ARGS")
    out.update(milestone=ref.strip(), fields=fields)
    return out


def _parse_link(args: dict, out: dict) -> dict:
    url = agent_tools._opt_str(args, "url")
    url = url.strip() if url is not None else ""
    if not _LINK_URL.fullmatch(url):
        raise _Refusal("BAD_ARGS")
    label = _blank_to_none(agent_tools._opt_str(args, "label"))
    out.update(url=url, label=label)
    return out


def _parse(args: dict) -> dict:
    action = args.get("action")
    if action not in ACTIONS:
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
    if action == "milestone":
        return _parse_milestone(args, out)
    if action == "link":
        return _parse_link(args, out)
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


async def _session_project(ctx: agent_tools.ToolContext, a: dict) -> dict:
    """A write's project: a Linear call needs its ticket (as ``linear_document`` update)."""
    if not ctx.outside and (not ctx.is_linear or not ctx.session_issue_id):
        raise _Refusal("NOT_LINEAR_SESSION")
    return await _project(ctx, a["project"])


async def _post_update(ctx: agent_tools.ToolContext, a: dict) -> dict:
    project = await _session_project(ctx, a)
    _ask_project(ctx, project)
    agent_tools._check_gate(ctx)
    made = await ctx.client.project_update_create(project["id"], a["body"], a["health"])
    return {"ok": True, "update_id": made["id"], "url": made.get("url")}


def _not_unique(exc: LinearError) -> bool:
    """Linear's duplicate milestone name: ``INPUT_ERROR`` "name not unique" (probed)."""
    return exc.code == "INPUT_ERROR" and "not unique" in str(exc).casefold()


async def _already_exists(ctx: agent_tools.ToolContext, project_id: str, name: str,
                          exc: LinearError) -> _Refusal:
    """``ALREADY_EXISTS`` with Linear's message (userPresentableMessage preferred) and,
    when the directory finds it, the existing milestone's id."""
    extra: dict[str, Any] = {"message": exc.detail or str(exc)}
    try:
        row = await directory.for_client(ctx.client).resolve(
            "milestones", project_id, name, issue_fields._by_name)
        extra["milestone_id"] = row["id"]
    except (_Refusal, LinearError):
        pass
    return _Refusal("ALREADY_EXISTS", extra=extra)


async def _milestone(ctx: agent_tools.ToolContext, a: dict) -> dict:
    project = await _session_project(ctx, a)
    milestone = None
    if a["milestone"] is not None:  # resolved in the project (a miss: MILESTONE_NOT_FOUND)
        milestone = await directory.for_client(ctx.client).resolve(
            "milestones", project["id"], a["milestone"], issue_fields._by_name)
    _ask_project(ctx, project)
    agent_tools._check_gate(ctx)
    # The directory has no invalidate API: a name made or changed here resolves on a later
    # call through its refetch-once on a miss.
    try:
        if milestone is None:
            made = await ctx.client.milestone_create(
                project["id"], a["name"], a["description"], a["target_date"])
        else:
            made = await ctx.client.milestone_update(milestone["id"], a["fields"])
    except LinearError as exc:  # a duplicate name shows only on write, after the gate
        if not _not_unique(exc):
            raise
        name = a["name"] if milestone is None else a["fields"].get("name", milestone["name"])
        raise await _already_exists(ctx, project["id"], name, exc) from None
    return {"ok": True, "milestone_id": made["id"], "name": made.get("name"),
            "target_date": made.get("target_date"), "created": milestone is None}


async def _link(ctx: agent_tools.ToolContext, a: dict) -> dict:
    project = await _session_project(ctx, a)
    # Linear doesn't refuse a duplicate URL (probed): check before the gate, so a repeat
    # stores no request, shows no card and writes nothing.
    detail = await ctx.client.project_detail(project["id"], 0)
    for lk in detail.get("links") or []:
        if (lk.get("url") or "").strip() == a["url"]:
            raise _Refusal("ALREADY_LINKED", extra={"link_id": lk.get("id")})
    _ask_project(ctx, project)
    agent_tools._check_gate(ctx)
    made = await ctx.client.project_link_create(project["id"], a["url"], a["label"])
    return {"ok": True, "link_id": made["id"]}


_BODIES = {"post_update": _post_update, "milestone": _milestone, "link": _link}


async def _write(ctx: agent_tools.ToolContext, a: dict) -> dict:
    return await _BODIES[a["action"]](ctx, a)


_read_tool = agent_tools._tool(_parse, _get)
_write_tool = agent_tools._tool(_parse, _write, gated="linear_project")


def linear_project(args: Any = None, **kw: Any) -> dict:
    """``get`` runs ungated (a read); the writes through the gate."""
    if isinstance(args, dict) and args.get("action") == "get":
        return _read_tool(args, **kw)
    return _write_tool(args, **kw)


SCHEMAS: dict[str, dict] = {
    "linear_project": {
        "name": "linear_project",
        "description": (
            "Read a Linear project, post a project update, create or edit a milestone, or "
            "add a project link, as the Linear app. Without "
            "project: the session ticket's project (outside a Linear session, "
            "default_project); none is PROJECT_NOT_FOUND with candidates. get (updates? "
            "0-20, default 3): state, health, lead, target_date, links, milestones, "
            "documents and the newest updates (bodies cut at 4,000 chars, body_truncated). "
            "post_update (body; health?): posts to the whole project, which sets its "
            "health; a re-run posts again. milestone without milestone creates one (name "
            "required; description?, target_date? YYYY-MM-DD); with milestone (name or "
            "UUID in the project, else MILESTONE_NOT_FOUND with candidates) it edits that "
            "one (at least one of name, description, target_date; target_date null clears "
            "the date; Linear can't clear a description). A taken name is ALREADY_EXISTS. "
            "link (url: http(s) only; label? - the label defaults to the URL) adds a link, "
            "never edits or removes one; a link with the same URL is ALREADY_LINKED with "
            "its link_id, and nothing is asked or written. Every project write needs "
            "approval, the session ticket's own project included. "
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
            "milestone": {"type": "string", "description": (
                "milestone: the milestone to edit (name or UUID); omit to create one.")},
            "name": {"type": "string", "description": "milestone: its name."},
            "description": {"type": ["string", "null"], "description": (
                "milestone: its description (Linear can't clear it; null is refused on "
                "an edit).")},
            "target_date": {"type": ["string", "null"], "description": (
                "milestone: YYYY-MM-DD (null clears on an edit).")},
            "url": {"type": "string", "description": "link: an http(s) URL."},
            "label": {"type": "string",
                      "description": "link: its label (default: the URL)."},
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
