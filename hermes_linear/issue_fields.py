"""The fields ``linear_issue_create`` and ``linear_issue_update`` share (RET-121 spec §1.1).

``parse`` checks the agent's arguments before any I/O (``BAD_ARGS``); ``resolve`` turns the
parsed references into Linear ids through the name directory (``directory.py``), whose
misses refuse with ``candidates``. No tool lives here: create and update call both.

A parsed field is absent (untouched), a value, or ``None``: on update ``None`` and ``""``
clear the field; on create they are the same as absent, so ``parse`` drops them.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from typing import Any

from . import directory

#: The shared fields, in resolution-independent argument order (create adds ``team``).
FIELDS = ("assignee", "delegate", "priority", "estimate", "due_date", "cycle", "milestone",
          "project")

#: Linear's priority numbers by name.
PRIORITIES = {"none": 0, "urgent": 1, "high": 2, "medium": 3, "low": 4}

SELF_DELEGATION_WARNING = (
    "Delegated to this app: no session starts until a human delegates the issue.")

_DIGITS = re.compile(r"[0-9]+")
_YMD = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")  # fromisoformat alone takes "2026-W05-1"


def _refusal(code: str, extra: dict | None = None) -> Exception:
    from .agent_tools import _Refusal  # lazy, as directory.py: agent_tools imports us
    return _Refusal(code, extra=extra)


def _bad() -> Exception:
    return _refusal("BAD_ARGS")


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _priority(v: Any) -> int:
    if _is_int(v):
        n = v
    elif isinstance(v, str) and _DIGITS.fullmatch(v):
        n = int(v)
    elif isinstance(v, str):
        n = PRIORITIES.get(v.casefold(), -1)
    else:
        raise _bad()
    if not 0 <= n <= 4:
        raise _bad()
    return n


def _estimate(v: Any) -> int:
    if not _is_int(v) or v < 0:
        raise _bad()
    return v


def _due_date(v: Any) -> str:
    if not isinstance(v, str) or not _YMD.fullmatch(v):
        raise _bad()
    try:
        return datetime.date.fromisoformat(v).isoformat()
    except ValueError:
        raise _bad() from None


def _cycle(v: Any) -> int | str:
    if _is_int(v):
        if v < 0:
            raise _bad()
        return v
    if not isinstance(v, str):
        raise _bad()
    return int(v) if _DIGITS.fullmatch(v) else v


def _ref(v: Any) -> str:
    if not isinstance(v, str):
        raise _bad()
    return v


_PARSERS = {"assignee": _ref, "delegate": _ref, "priority": _priority,
            "estimate": _estimate, "due_date": _due_date, "cycle": _cycle,
            "milestone": _ref, "project": _ref, "team": _ref}


def parse(args: dict, *, update: bool) -> dict:
    """The shared fields of ``args`` checked and normalized (strings stripped, ``priority``
    to its number, a digit-string ``cycle`` to its number). Absent keys stay absent;
    ``None`` / blank is ``None`` (clear) on update and dropped on create. ``BAD_ARGS`` for a
    bad type or value, and for ``team`` (any value) on update."""
    if update and "team" in args:
        raise _bad()
    out: dict = {}
    for name in FIELDS + (() if update else ("team",)):
        if name not in args:
            continue
        v = args[name]
        if isinstance(v, str):
            v = v.strip()
        if v is None or v == "":
            if update:
                out[name] = None
            continue
        out[name] = _PARSERS[name](v)
    return out


@dataclass
class Resolved:
    """``input``: ``IssueCreateInput`` / ``IssueUpdateInput`` keys for the given fields.
    ``team_id`` / ``project_id``: the resulting team and project. ``team`` / ``project``:
    the rows resolved for an explicit ``team`` / ``project`` (approval targets).
    ``starts_session`` is ``False`` only for a delegation to this app."""

    input: dict
    team_id: str
    project_id: str | None
    warnings: list[str] = field(default_factory=list)
    starts_session: bool | None = None
    team: dict | None = None
    project: dict | None = None


def _match_on(*keys: str):
    """A directory matcher: the row whose ``id`` equals ``ref`` (case-insensitive), else the
    rows whose ``keys`` equal it case-insensitively (one row, a list when several, or None)."""

    def match(rows: list[dict], ref: Any) -> dict | list[dict] | None:
        want = str(ref).casefold()
        by_id = [r for r in rows if str(r.get("id") or "").casefold() == want]
        if by_id:
            return by_id[0]
        hits = [r for r in rows
                if any(str(r.get(k) or "").casefold() == want for k in keys)]
        return hits[0] if len(hits) == 1 else (hits or None)

    return match


_by_user = _match_on("name", "displayName", "email")
_by_team = _match_on("key", "name")
_by_name = _match_on("name")


def _by_cycle(rows: list[dict], ref: int | str) -> dict | list[dict] | None:
    if _is_int(ref):
        hits = [r for r in rows if r.get("number") == ref]
        return hits[0] if len(hits) == 1 else (hits or None)
    flag = str(ref).casefold()
    if flag in ("current", "next"):
        return next((r for r in rows if r.get(flag) is True), None)
    return _by_name(rows, ref)


async def _user(ctx: Any, d: directory.Directory, ref: str) -> tuple[str, bool]:
    """The user id ``ref`` names and whether it is an app user (``me`` is this app)."""
    if ref.casefold() == "me":
        return await ctx.client.viewer_id(), True
    row = await d.resolve("users", "", ref, _by_user)
    return row["id"], bool(row.get("app"))


async def resolve(ctx: Any, parsed: dict, *, team_id: str, project_id: str | None) -> Resolved:
    """``parsed`` (from ``parse``) as Linear input. ``team_id`` / ``project_id`` are the
    issue's base; an explicit ``team`` / ``project`` replaces them (another team without a
    ``project``: no project, Ruling R8), and the cycle resolves in the resulting team, the
    milestone in the resulting project (none: ``MILESTONE_NOT_FOUND`` with no candidates).
    Misses refuse through the directory."""
    d = directory.for_client(ctx.client)
    out = Resolved(input={}, team_id=team_id, project_id=project_id)
    inp = out.input

    if parsed.get("team") is not None:
        out.team = await d.resolve("teams", "", parsed["team"], _by_team)
        out.team_id = inp["teamId"] = out.team["id"]
        if out.team_id != team_id and "project" not in parsed:
            out.project_id = None  # R8: the base project belongs to the base team
    if "project" in parsed:
        if parsed["project"] is None:
            out.project_id = inp["projectId"] = None
        else:
            out.project = await d.resolve("projects", "", parsed["project"], _by_name)
            out.project_id = inp["projectId"] = out.project["id"]

    if "assignee" in parsed:
        ref = parsed["assignee"]
        inp["assigneeId"] = None if ref is None else (await _user(ctx, d, ref))[0]
    if "delegate" in parsed:
        ref = parsed["delegate"]
        if ref is None:
            inp["delegateId"] = None
        else:
            uid, app = await _user(ctx, d, ref)
            if not app:
                raise _refusal("DELEGATE_NOT_AGENT")
            inp["delegateId"] = uid
            if uid == await ctx.client.viewer_id():
                out.starts_session = False
                out.warnings.append(SELF_DELEGATION_WARNING)

    if "priority" in parsed:  # clearing a priority is Linear's 0 ("No priority")
        inp["priority"] = parsed["priority"] or 0
    if "estimate" in parsed:
        inp["estimate"] = parsed["estimate"]
    if "due_date" in parsed:
        inp["dueDate"] = parsed["due_date"]

    if "cycle" in parsed:
        ref = parsed["cycle"]
        inp["cycleId"] = None if ref is None else \
            (await d.resolve("cycles", out.team_id, ref, _by_cycle))["id"]
    if "milestone" in parsed:
        ref = parsed["milestone"]
        if ref is None:
            inp["projectMilestoneId"] = None
        elif out.project_id is None:
            raise directory.miss("milestones", [], ref)
        else:
            row = await d.resolve("milestones", out.project_id, ref, _by_name)
            inp["projectMilestoneId"] = row["id"]
    return out
