"""Webhook router: turn a verified Linear webhook payload into decisions.

Spec §6.1–6.4. ``route`` is pure apart from ``ctx.allowed`` and store calls:
no network, no Hermes imports. It records sessions and ticket placeholders
(events may arrive before ``Issue create``) and fills name caches, but never
changes a ticket's mode itself — that is what ``SetMode`` decisions are for;
the adapter applies decisions in order before routing the next event.

Hermes session keys (spec §6.1) derive only from the store and the payload,
so they survive a gateway restart (Review Focus 3):

* delegated ticket: ``chat_id = <issueId>:<generation>``, no thread;
* @mention on a non-delegated ticket: ``chat_id = <issueId>``,
  ``thread_id = <agentSessionId>``.

``route`` dispatches on payload ``type`` via ``_HANDLERS``. Task 8 adds the
data-change handlers (Issue-update context, Comment, Project, ...) there and
in ``_issue``.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .access import REFUSAL_TEXT
from .constants import DELEGATION_WAIT_S
from .models import (
    AwaitDelegation,
    Decision,
    Ignore,
    Interrupt,
    Refuse,
    ResolveControl,
    SetMode,
    StartTurn,
)
from .store import SessionRow, Store, Ticket

ACK_TEXT = "On it"
DEFAULT_AUTHOR = "Linear user"
_TERMINAL_STATES = ("completed", "canceled")


@dataclass
class RouterContext:
    store: Store
    app_user_id: str
    allowed: Callable[[str | None], Awaitable[bool]]


# ------------------------------------------------------------------ keys


def ticket_chat_id(issue_id: str, generation: int) -> str:
    return f"{issue_id}:{generation}"


def turn_header(identifier: str, root_comment_id: str | None, author: str) -> str:
    return f"[Linear {identifier} · thread {root_comment_id} · {author}]"


def message_id(agent_session_id: str, activity_id: str) -> str:
    return f"{agent_session_id}:{activity_id}"


# ------------------------------------------------------------- dispatcher


async def route(event: dict, ctx: RouterContext) -> list[Decision]:
    handler = _HANDLERS.get(event.get("type") or "")
    if handler is None:
        return [Ignore("unhandled type")]
    return await handler(event, ctx)


# ------------------------------------------------------- small extractors


def _d(obj: Any) -> dict:
    return obj if isinstance(obj, dict) else {}


_THREAD_RE = re.compile(
    r"<(primary-directive-thread|user-request)\b[^>]*>.*?</\1>", re.DOTALL
)
_USER_REQUEST_RE = re.compile(r"<user-request\b[^>]*>(.*?)</user-request>", re.DOTALL)


def _thread_portion(prompt_context: str) -> str:
    """The thread's part of ``promptContext`` (no ``<issue>`` element).

    Prefers the ``<primary-directive-thread>`` and ``<user-request>`` elements;
    falls back to the ``<user-request>`` content, then the raw context.
    """
    parts = [m.group(0) for m in _THREAD_RE.finditer(prompt_context)]
    if parts:
        return "\n\n".join(parts)
    m = _USER_REQUEST_RE.search(prompt_context)
    if m:
        return m.group(1).strip()
    return prompt_context


def _delegation_generation(ticket: Ticket | None, state_type: str | None) -> int:
    """Generation for a (re-)delegation, spec §6.4."""
    if ticket is None or ticket.generation < 1:
        return 1  # never delegated before
    if ticket.mode == "delegated":
        return ticket.generation  # already decided (e.g. by the Issue update)
    if state_type in _TERMINAL_STATES:
        return ticket.generation + 1
    return ticket.generation


def _is_plugin_created(store: Store, sid: str, root_comment_id: str | None) -> bool:
    row = store.session(sid)
    if row is not None and row.plugin_created:
        return True
    if root_comment_id:
        row = store.session_by_root_comment(root_comment_id)
        if row is not None and row.plugin_created:
            return True
    return False


def _session_key(row: SessionRow | None, ticket: Ticket) -> tuple[str, str | None]:
    """Hermes ``(chat_id, thread_id)`` for an existing agent session."""
    if row is not None and row.kind == "mention" and ticket.mode != "delegated":
        return ticket.issue_id, row.agent_session_id
    return ticket_chat_id(ticket.issue_id, max(ticket.generation, 1)), None


# --------------------------------------------------- AgentSessionEvent


async def _agent_session_event(event: dict, ctx: RouterContext) -> list[Decision]:
    action = event.get("action")
    if action not in ("created", "prompted"):
        return [Ignore(f"unhandled agent session action {action!r}")]

    store = ctx.store
    session = _d(event.get("agentSession"))
    activity = _d(event.get("agentActivity"))
    issue = _d(session.get("issue"))
    sid = session.get("id") or activity.get("agentSessionId")
    issue_id = session.get("issueId") or issue.get("id")
    if not sid or not issue_id:
        return [Ignore("agent session event without session or issue")]
    root_comment_id = session.get("commentId") or _d(session.get("comment")).get("id")
    is_mention = _d(session.get("sourceMetadata")).get("type") == "comment"

    if action == "created" and _is_plugin_created(store, sid, root_comment_id):
        return [Ignore("plugin-created")]

    # Record the session and a ticket placeholder before anything else.
    existing = store.session(sid)
    kind = existing.kind if existing else ("mention" if is_mention else "delegation")
    store.record_session(sid, issue_id, root_comment_id, kind)
    ticket = store.upsert_ticket(
        issue_id,
        identifier=issue.get("identifier"),
        team_id=issue.get("teamId") or _d(issue.get("team")).get("id"),
    )

    user = _d(activity.get("user"))
    if action == "created":
        creator = _d(session.get("creator"))
        actor = session.get("creatorId") or creator.get("id")
        author = creator.get("name") or user.get("name")
    else:
        actor = activity.get("userId") or user.get("id")
        author = user.get("name")
    author = author or DEFAULT_AUTHOR
    if not actor or not await ctx.allowed(actor):
        return [Refuse(sid, REFUSAL_TEXT)]

    header = turn_header(ticket.identifier or issue_id, root_comment_id, author)

    def turn(chat_id: str, thread_id: str | None, body: str, first: bool) -> StartTurn:
        return StartTurn(
            agent_session_id=sid,
            activity_id=activity.get("id") or "",
            issue_id=issue_id,
            chat_id=chat_id,
            thread_id=thread_id,
            user_id=actor,
            user_name=author,
            text=f"{header}\n\n{body}",
            first_turn=first,
            ack=ACK_TEXT,
        )

    if action == "created":
        prompt_context = event.get("promptContext") or ""
        if not is_mention:  # §6.2.1 delegation; adapter appends the snapshot
            gen = _delegation_generation(ticket, ticket.state_type)
            return [
                SetMode(issue_id, "delegated", gen),
                turn(ticket_chat_id(issue_id, gen), None, prompt_context, True),
            ]
        if ticket.mode == "delegated":  # §6.2.3 mention joins the ticket session
            body = _thread_portion(prompt_context)
            return [turn(ticket_chat_id(issue_id, ticket.generation), None, body, False)]
        return [
            SetMode(issue_id, "mention", ticket.generation),
            turn(issue_id, sid, prompt_context, True),
        ]

    # prompted, §6.2.4 in order
    if activity.get("signal") == "stop":
        return [Interrupt(sid)]
    body = _d(activity.get("content")).get("body") or ""
    control = store.control_for_session(sid)
    if control is not None:
        return [ResolveControl(sid, control.request_id, control.kind, actor, body)]
    chat_id, thread_id = _session_key(store.session(sid), ticket)
    return [turn(chat_id, thread_id, body, False)]


# ---------------------------------------------------------------- Issue


async def _issue(event: dict, ctx: RouterContext) -> list[Decision]:
    store = ctx.store
    data = _d(event.get("data"))
    issue_id = data.get("id")
    if not issue_id:
        return [Ignore("issue event without id")]
    state = _d(data.get("state"))
    if state.get("id") and state.get("name"):
        store.cache_state(state["id"], state["name"])
    for label in data.get("labels") or []:
        label = _d(label)
        if label.get("id") and label.get("name"):
            store.cache_label(label["id"], label["name"])
    ticket = store.upsert_ticket(
        issue_id,
        identifier=data.get("identifier"),
        team_id=data.get("teamId"),
        project_id=data.get("projectId"),
        state_type=state.get("type"),
    )

    if event.get("action") != "update":
        return []  # Task 8: Issue create / remove
    decisions: list[Decision] = []
    updated_from = _d(event.get("updatedFrom"))
    if "delegateId" in updated_from:
        decisions += _delegate_change(
            ctx, ticket, updated_from.get("delegateId"), data.get("delegateId"),
            state.get("type"),
        )
    # Task 8: AddContext for the other changed fields goes here.
    return decisions


def _delegate_change(
    ctx: RouterContext,
    ticket: Ticket,
    old: str | None,
    new: str | None,
    state_type: str | None,
) -> list[Decision]:
    me = ctx.app_user_id
    issue_id = ticket.issue_id
    if new == me and old != me:  # §6.2.2
        if ticket.mode == "delegated":
            # The delegation `created` arrived first and already started the turn.
            return [Ignore("already delegated")]
        gen = _delegation_generation(ticket, state_type or ticket.state_type)
        return [SetMode(issue_id, "delegated", gen), AwaitDelegation(issue_id, DELEGATION_WAIT_S)]
    if old == me and new != me:  # §6.4 un-delegated
        interrupts: list[Decision] = [
            Interrupt(s.agent_session_id) for s in ctx.store.sessions_for_issue(issue_id)
        ]
        return interrupts + [SetMode(issue_id, "untouched", ticket.generation)]
    return []


_HANDLERS: dict[str, Callable[[dict, RouterContext], Awaitable[list[Decision]]]] = {
    "AgentSessionEvent": _agent_session_event,
    "Issue": _issue,
    # Task 8: "Comment", "Project", "ProjectUpdate", ...
}

__all__ = ["ACK_TEXT", "RouterContext", "message_id", "route", "ticket_chat_id", "turn_header"]
