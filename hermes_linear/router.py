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

``route`` dispatches on payload ``type`` via ``_HANDLERS``. Data-change
types (``Issue``, ``Comment``, ``Attachment``, ``Project``, ``ProjectUpdate``)
pass the echo filter first and feed the context collector (spec §7.2–7.4):
only tickets in mode ``delegated`` collect context; bots are context-only and
exempt from the allowlist; denied humans are dropped.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .access import REFUSAL_TEXT
from .constants import DELEGATION_WAIT_S, DESCRIPTION_SNIPPET
from .models import (
    AddContext,
    AwaitDelegation,
    ContextItem,
    Decision,
    Fatal,
    Ignore,
    Interrupt,
    OpenSessionOnComment,
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
    # The app user's Linear handle (``displayName``), for spotting @mentions in
    # plain comments (R18). Optional for backward compatibility.
    app_user_handle: str | None = None


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


def _plugin_created_row(
    store: Store, sid: str, root_comment_id: str | None
) -> SessionRow | None:
    """The plugin-created session row this event belongs to, if any."""
    row = store.session(sid)
    if row is not None and row.plugin_created:
        return row
    if root_comment_id:
        row = store.session_by_root_comment(root_comment_id)
        if row is not None and row.plugin_created:
            return row
    return None


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

    if action == "created":
        own = _plugin_created_row(store, sid, root_comment_id)
        if own is not None:
            if own.agent_session_id != sid and store.session(sid) is None:
                # Matched by root comment only: remember the real session id as
                # ours so a later `prompted` on it is classified correctly.
                store.record_session(
                    sid, own.issue_id, own.root_comment_id or root_comment_id,
                    own.kind, plugin_created=True,
                )
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
        return [ResolveControl(sid, control.request_id, control.kind, actor, body,
                               state=control.state, user_name=author,
                               activity_id=activity.get("id") or "")]
    chat_id, thread_id = _session_key(store.session(sid), ticket)
    return [turn(chat_id, thread_id, body, False)]


# ------------------------------------------------------ data-change shared


def _is_echo(event: dict, me: str) -> bool:
    """Spec §7.2: the event's actor / comment author is the app user."""
    data = _d(event.get("data"))
    return me in (
        _d(event.get("actor")).get("id"),
        data.get("userId"),
        _d(data.get("user")).get("id"),
    )


def _actor_name(event: dict) -> str:
    return _d(event.get("actor")).get("name") or "Linear"


def _at(event: dict) -> str:
    data = _d(event.get("data"))
    return event.get("createdAt") or data.get("updatedAt") or data.get("createdAt") or ""


def _snippet(text: Any) -> str | None:
    if text is None:
        return None
    return str(text)[:DESCRIPTION_SNIPPET]


# Explicit bot/automation actor types (compared case-insensitively). SDL:
# ``union EntityActorWebhookPayload = ExternalUserActorWebhookPayload |
# IntegrationActorWebhookPayload | OauthClientActorWebhookPayload |
# UserActorWebhookPayload``; ``ActorBot.type`` is documented as "Source
# category, such as integration, oauthClient, workflow, or ai". External users
# and unknown types are NOT bots: they are humans that must pass the allowlist.
_BOT_ACTOR_TYPES = frozenset({"integration", "oauthclient", "oauth_client", "workflow", "ai"})


def _explicit_bot(event: dict) -> bool:
    """True only on an explicit bot signal: ``botActor`` set or a bot actor type.

    ``data.botActor`` is an entity field, so it only says who triggered the
    event for a Comment (it credits the comment's author). On an Issue it is
    "the bot that created the issue" and on other entities it is likewise not
    the triggering actor, so it is ignored there: a human editing a
    bot-created issue must still pass the allowlist. Event-level ``botActor``
    and ``actor.type`` apply to every event type.
    """
    if event.get("botActor"):
        return True
    if event.get("type") == "Comment" and _d(event.get("data")).get("botActor"):
        return True
    actor_type = _d(event.get("actor")).get("type")
    return isinstance(actor_type, str) and actor_type.lower() in _BOT_ACTOR_TYPES


def _human_actor_id(event: dict) -> str | None:
    """The id a human actor must pass the allowlist with (fail-closed).

    ``user`` actor -> its id; an external/unknown actor type -> its id (it is
    not on the allowlist, so it is dropped); a bot actor -> None. With no actor
    at all (the SDL allows a null actor), a Comment falls back to its author
    ``data.userId`` / ``data.user.id``; anything else -> None.
    """
    actor = _d(event.get("actor"))
    if actor:
        if _explicit_bot(event):
            return None
        return actor.get("id") or None
    if event.get("type") == "Comment":
        data = _d(event.get("data"))
        return data.get("userId") or _d(data.get("user")).get("id") or None
    return None


async def _context_allowed(event: dict, ctx: RouterContext) -> bool:
    """Spec §7.3: explicit bots are exempt; everyone else must be allowlisted."""
    if _explicit_bot(event):
        return True
    actor_id = _human_actor_id(event)
    return bool(actor_id) and await ctx.allowed(actor_id)


def _scalar(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    return str(value)


# updatedFrom keys that are bookkeeping, not meaningful changes.
_NOISE_FIELDS = frozenset({
    "updatedAt", "sortOrder", "subscriberIds", "startedAt", "reactionData",
    "descriptionData", "prioritySortOrder", "subIssueSortOrder", "previousIdentifiers",
    "activitySummary", "aiAnalysis", "lastAppliedTemplateId", "contentData",
    "bodyData", "priorityLabel", "slugId", "documentContentId",
})


def _is_noise(key: str) -> bool:
    return (
        key in _NOISE_FIELDS
        or key.endswith("Order")
        or key.endswith("At")  # timestamps: the state / archive change carries the meaning
        or key.endswith("History")
    )


# Id-valued keys the change collectors turn into names themselves.
_HANDLED_ID_FIELDS = frozenset({"stateId", "labelIds", "delegateId", "projectId", "statusId",
                                "assigneeId"})


def _is_raw_id(key: str) -> bool:
    """Raw-id keys (``cycleId``, ``lastUpdateId``, ...) carry no readable change."""
    return key not in _HANDLED_ID_FIELDS and (key.endswith("Id") or key.endswith("Ids"))


_PRIORITY = {0: "No priority", 1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}


def _priority_name(value: Any) -> str | None:
    if value is None:
        return None
    return _PRIORITY.get(value) if isinstance(value, int) else str(value)


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
    updated_from = _d(event.get("updatedFrom"))
    if "projectId" in data and data["projectId"] is None and "projectId" in updated_from:
        store.clear_project(issue_id)  # moved out of its project
    # Echo filter only after the caches above: an edit by the app itself must
    # still refresh state/project/labels (spec §7.2).
    if _is_echo(event, ctx.app_user_id):
        return [Ignore("echo")]

    if event.get("action") != "update":
        # create (even one already delegated to us, R20) / remove: caches only.
        return []
    if "delegateId" in updated_from:
        # Delegation transitions are turn/lifecycle triggers (Task 7); the
        # other fields of the same edit are covered by the delegation snapshot.
        return await _delegate_change(
            ctx, event, ticket, updated_from.get("delegateId"), data.get("delegateId"),
            state.get("type"),
        )
    if ticket.mode != "delegated":
        return [Ignore("not delegated")] if _issue_changes(store, event) else []
    if not await _context_allowed(event, ctx):
        return [Ignore("denied actor")]
    items = [
        ContextItem(issue_id=issue_id, kind="issue", field=f, old=o, new=n,
                    actor=_actor_name(event), at=_at(event))
        for f, o, n in _issue_changes(store, event)
    ]
    return [AddContext(tuple(items))] if items else []


def _issue_changes(store: Store, event: dict) -> list[tuple[str, str | None, str | None]]:
    """``(field, old, new)`` for each meaningful change of an Issue update."""
    data = _d(event.get("data"))
    updated_from = _d(event.get("updatedFrom"))
    out: list[tuple[str, str | None, str | None]] = []
    for key, old in updated_from.items():
        if key == "delegateId" or _is_noise(key) or _is_raw_id(key):
            continue
        if key == "labelIds":
            diff = _label_diff(store, old or [], data)
            if diff:
                out.append(("labels", None, diff))
        elif key == "stateId":
            old_name = (store.state_name(old) if old else None) or "(previous)"
            out.append(("state", old_name, _d(data.get("state")).get("name")))
        elif key == "description":
            out.append(("description", None, _snippet(data.get("description"))))
        elif key == "priority":
            out.append(("priority", _priority_name(old),
                        data.get("priorityLabel") or _priority_name(data.get("priority"))))
        elif key == "assigneeId":
            new = _d(data.get("assignee")).get("name") if data.get("assigneeId") else None
            out.append(("assignee", None, new or "(unassigned)"))
        elif key == "projectId":
            new = _d(data.get("project")).get("name") if data.get("projectId") else None
            out.append(("project", None, new or "(none)"))
        elif not isinstance(old, (dict, list)) and not isinstance(data.get(key), (dict, list)):
            out.append((key, _scalar(old), _scalar(data.get(key))))
    return out


def _label_diff(store: Store, old_ids: list, data: dict) -> str:
    names = {
        _d(lb).get("id"): _d(lb).get("name") for lb in data.get("labels") or []
    }
    new_ids = list(data.get("labelIds") or [i for i in names if i])
    parts = [
        f"+{names.get(i) or store.label_name(i) or '(label)'}"
        for i in new_ids if i not in old_ids
    ]
    parts += [
        f"-{store.label_name(i) or '(removed label)'}"
        for i in old_ids if i not in new_ids
    ]
    return ", ".join(parts)


async def _delegate_change(
    ctx: RouterContext,
    event: dict,
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
        # Spec §7.3: a delegation is a turn trigger, so the delegator must be a
        # known, allowed human. A bot/automation or a denied user never starts
        # the agent via this path (no SetMode, no await). Note: an automation
        # delegating via a Linear triage rule therefore does not start the agent
        # here; Linear's own `created` event for such a delegation still goes
        # through the created path's access check on `creatorId`.
        # Strict (fail-closed): only an explicit ``user`` actor qualifies.
        actor = _d(event.get("actor"))
        actor_id = actor.get("id") if actor.get("type") == "user" else None
        if not actor_id or _explicit_bot(event) or not await ctx.allowed(actor_id):
            return [Ignore("denied delegator")]
        gen = _delegation_generation(ticket, state_type or ticket.state_type)
        return [SetMode(issue_id, "delegated", gen), AwaitDelegation(issue_id, DELEGATION_WAIT_S)]
    if old == me and new != me:  # §6.4 un-delegated
        interrupts: list[Decision] = [
            Interrupt(s.agent_session_id) for s in ctx.store.sessions_for_issue(issue_id)
        ]
        return interrupts + [SetMode(issue_id, "untouched", ticket.generation)]
    return []


# --------------------------------------------------------------- Comment


def _mentions_app(body: str, ctx: RouterContext) -> bool:
    """R18: the comment @mentions the app (Linear also sends a `created` for it)."""
    if ctx.app_user_id and ctx.app_user_id in body:
        return True
    handle = (ctx.app_user_handle or "").strip().lstrip("@")
    if not handle:
        return False
    h = re.escape(handle)
    if re.search(rf"(?<![\w@.-])@{h}(?![\w-])", body, re.IGNORECASE):
        return True
    return re.search(rf"linear\.app/[^\s/]+/profiles/{h}(?![\w-])", body, re.IGNORECASE) is not None


async def _comment(event: dict, ctx: RouterContext) -> list[Decision]:
    if event.get("action") != "create":
        return [Ignore(f"comment {event.get('action')}")]
    store = ctx.store
    data = _d(event.get("data"))
    issue_id = data.get("issueId") or _d(data.get("issue")).get("id")
    comment_id = data.get("id")
    if not issue_id or not comment_id:
        return [Ignore("comment not on an issue")]
    body = data.get("body") or ""
    if _mentions_app(body, ctx):
        return [Ignore("mention comment")]
    issue = _d(data.get("issue"))
    ticket = store.upsert_ticket(
        issue_id, identifier=issue.get("identifier"),
        team_id=issue.get("teamId") or _d(issue.get("team")).get("id"),
    )
    if ticket.mode != "delegated":
        return [Ignore("not delegated")]
    parent_id = data.get("parentId") or _d(data.get("parent")).get("id")
    if parent_id and store.is_session_root(parent_id):
        # Review Focus 2: already delivered as the session's `prompted`.
        return [Ignore("session thread")]

    user = _d(data.get("user"))
    bot = _explicit_bot(event)
    actor_id = _human_actor_id(event)
    if not bot and not (actor_id and await ctx.allowed(actor_id)):
        return [Ignore("denied actor")]
    if not parent_id and not bot:  # §6.2 (5): top-level human comment
        return [OpenSessionOnComment(
            issue_id=issue_id, comment_id=comment_id, user_id=actor_id,
            user_name=user.get("name") or _d(event.get("actor")).get("name") or DEFAULT_AUTHOR,
            body=body,
        )]
    bot_name = _d(data.get("botActor")).get("name") if isinstance(data.get("botActor"), dict) else None
    return [AddContext((ContextItem(
        issue_id=issue_id, kind="comment", field="reply" if parent_id else "comment",
        old=None, new=_snippet(body),
        actor=user.get("name") or bot_name or _actor_name(event), at=_at(event),
    ),))]


# ------------------------------------------------------------ Attachment


async def _attachment(event: dict, ctx: RouterContext) -> list[Decision]:
    if event.get("action") not in ("create", "update"):
        return [Ignore(f"attachment {event.get('action')}")]
    data = _d(event.get("data"))
    issue_id = data.get("issueId")
    ticket = ctx.store.ticket(issue_id) if issue_id else None
    if ticket is None or ticket.mode != "delegated":
        return [Ignore("not delegated")]
    if not await _context_allowed(event, ctx):
        return [Ignore("denied actor")]
    link = " ".join(x for x in (data.get("title"), data.get("url")) if x)
    return [AddContext((ContextItem(
        issue_id=issue_id, kind="attachment", field="link", old=None, new=link,
        actor=_actor_name(event), at=_at(event),
    ),))]


# --------------------------------------------------------------- Project


def _project_changes(event: dict) -> list[tuple[str, str | None, str | None]]:
    data = _d(event.get("data"))
    updated_from = _d(event.get("updatedFrom"))
    out: list[tuple[str, str | None, str | None]] = []
    for key, old in updated_from.items():
        if _is_noise(key) or _is_raw_id(key) or key in ("labelIds", "teamIds", "memberIds"):
            continue
        if key == "health" and "lastUpdateId" in updated_from:
            continue  # a status post: its ProjectUpdate event carries it
        if key == "statusId":
            out.append(("status", None, _d(data.get("status")).get("name")))
        elif key in ("description", "content"):
            out.append((key, None, _snippet(data.get(key))))
        elif key == "priority":
            out.append(("priority", _priority_name(old), _priority_name(data.get("priority"))))
        elif not isinstance(old, (dict, list)) and not isinstance(data.get(key), (dict, list)):
            out.append((key, _scalar(old), _scalar(data.get(key))))
    return out


async def _fan_out(
    event: dict, ctx: RouterContext, project_id: str | None, project_name: str | None,
    changes: list[tuple[str, str | None, str | None]],
) -> list[Decision]:
    issues = ctx.store.delegated_in_project(project_id) if project_id else []
    if not issues:
        return [Ignore("no delegated tickets in project")]
    if not changes:
        return [Ignore("no meaningful project change")]
    if not await _context_allowed(event, ctx):
        return [Ignore("denied actor")]
    actor, at = _actor_name(event), _at(event)
    return [AddContext(tuple(
        ContextItem(issue_id=i, kind="project", field=f, old=o, new=n,
                    actor=actor, at=at, project=project_name)
        for i in issues for f, o, n in changes
    ))]


async def _project(event: dict, ctx: RouterContext) -> list[Decision]:
    if event.get("action") != "update":
        return [Ignore(f"project {event.get('action')}")]
    data = _d(event.get("data"))
    return await _fan_out(event, ctx, data.get("id"), data.get("name"),
                          _project_changes(event))


async def _project_update(event: dict, ctx: RouterContext) -> list[Decision]:
    """A project status post (``ProjectUpdate``)."""
    if event.get("action") not in ("create", "update"):
        return [Ignore(f"project update {event.get('action')}")]
    data = _d(event.get("data"))
    project = _d(data.get("project"))
    return await _fan_out(event, ctx, project.get("id") or data.get("projectId"),
                          project.get("name"),
                          [("status post", None, _snippet(data.get("body") or ""))])


# ------------------------------------------------------------- app-level


async def _oauth_app(event: dict, ctx: RouterContext) -> list[Decision]:
    if event.get("action") == "revoked":
        return [Fatal("oauth app revoked")]
    return [Ignore(f"oauth app {event.get('action')}")]


async def _log_only(event: dict, ctx: RouterContext) -> list[Decision]:
    return [Ignore(f"{event.get('type')} ignored")]


_Handler = Callable[[dict, RouterContext], Awaitable[list[Decision]]]


def _echo_filtered(handler: _Handler) -> _Handler:
    """Spec §7.2: drop data-change events authored by the app user itself."""

    async def wrapped(event: dict, ctx: RouterContext) -> list[Decision]:
        if _is_echo(event, ctx.app_user_id):
            return [Ignore("echo")]
        return await handler(event, ctx)

    return wrapped


_HANDLERS: dict[str, _Handler] = {
    "AgentSessionEvent": _agent_session_event,
    "Issue": _issue,  # echo-filtered inside, after the cache refresh
    "Comment": _echo_filtered(_comment),
    "Attachment": _echo_filtered(_attachment),
    "Project": _echo_filtered(_project),
    "ProjectUpdate": _echo_filtered(_project_update),
    "OAuthApp": _oauth_app,
    "AppUserNotification": _log_only,
    "PermissionChange": _log_only,
}

__all__ = ["ACK_TEXT", "RouterContext", "message_id", "route", "ticket_chat_id", "turn_header"]
