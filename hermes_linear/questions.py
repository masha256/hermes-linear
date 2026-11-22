"""Pending Linear questions (RET-94): the copy, the ``Blocked`` target and the label write.

A ``clarify`` / approval control whose Hermes wait timed out becomes *pending* (spec "States"):
the question still stands in Linear and in the store, and ``Blocked`` marks the issue that
waits on it (spec §4): the session ticket's one open task, otherwise the session ticket.

Label writes are bounded by ``ACK_TIMEOUT_S``, logged on failure and never raise.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .constants import ACK_TIMEOUT_S
from .store import ControlRow, Store

log = logging.getLogger(__name__)

PENDING_NOTICE = (
    "Not answered. The question stays open in the Linear ticket; the answer arrives as the next "
    "message. End this turn now; do not assume an answer.")
APPROVAL_PENDING_TEXT = (
    "Command not run: approval is pending in Linear. End this turn. If it is approved, the next "
    "message says so, and re-running this exact command is pre-approved once.")

_warned_teams: set[str] = set()  # teams already warned about a missing Blocked label


def blocked_target(store: Store, row: ControlRow) -> str | None:
    """The issue that carries ``Blocked`` for ``row``: the session ticket's single open task,
    else the session ticket; None when the agent session is unknown."""
    session = store.session(row.agent_session_id)
    if session is None:
        return None
    tasks = store.open_tasks(session.issue_id)
    return tasks[0] if len(tasks) == 1 else session.issue_id


async def set_blocked(client: Any, issue_id: str, on: bool) -> bool:
    """Add (``on``) or remove ``Blocked`` on ``issue_id``; writes only on a change.

    False on a missing label or any failure (logged, never raised)."""
    try:
        return await asyncio.wait_for(_set_blocked(client, issue_id, on), ACK_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 — a label write never breaks the caller
        log.warning("linear: Blocked label %s on %s failed: %r",
                    "add" if on else "remove", issue_id, e)
        return False


async def _set_blocked(client: Any, issue_id: str, on: bool) -> bool:
    from .agent_tools import BLOCKED_LABEL  # lazy: agent_tools imports the adapter

    issue = await client.issue_get(issue_id)
    team = (issue.get("team") or {}).get("id")
    if not team:
        log.warning("linear: Blocked label skipped, issue %s has no team", issue_id)
        return False
    want = BLOCKED_LABEL.casefold()
    label = next((lb["id"] for lb in await client.team_labels(team)
                  if (lb.get("name") or "").casefold() == want and lb.get("id")), None)
    if label is None:
        level = logging.DEBUG if team in _warned_teams else logging.WARNING
        _warned_teams.add(team)
        log.log(level, "linear: no %r label in team %s; the question still stands",
                BLOCKED_LABEL, team)
        return False
    carried = {lb["id"] for lb in issue.get("labels") or []
               if isinstance(lb, dict) and lb.get("id")}
    target = issue.get("id") or issue_id
    if on and label not in carried:
        await client.issue_update(target, {"addedLabelIds": [label]})
    elif not on and label in carried:
        await client.issue_update(target, {"removedLabelIds": [label]})
    return True
