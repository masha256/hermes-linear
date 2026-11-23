"""Sessions the plugin opens, control answers, and Linear ticket helpers (spec §6.2, §8.3, §8.4).

* Delegation timers: an ``Issue update`` delegating to the app waits
  ``AwaitDelegation.deadline_s`` for Linear's own ``created``; on expiry the
  plugin reuses or opens an app session and starts the first turn.
* ``ResolveControl``: approval / clarify answers to pending prompts (Q8).
* First-turn snapshot fetch and auto-start (§8.3) for the turn engine.

Everything here runs on the adapter's event worker, so every Linear call is
bounded by ``ACK_TIMEOUT_S`` (or its own timeout) and nothing waits on a turn.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

from . import questions, renderer, tool_approval
from .constants import ACK_TIMEOUT_S
from .models import AwaitDelegation, ResolveControl, StartTurn
from .router import ACK_TEXT, DEFAULT_AUTHOR, _session_key, message_id, ticket_chat_id, turn_header
from .settings import Settings
from .store import ControlRow, Store

log = logging.getLogger(__name__)

REASK_TEXT = "Please answer with one of the options."
STALE_APPROVAL_TEXT = "That approval is no longer pending."
STALE_CLARIFY_TEXT = "That question is no longer pending."
SNAPSHOT_TIMEOUT_S = 15
AUTO_START_TIMEOUT_S = 20
_AUTO_START_STATES = frozenset({"backlog", "unstarted", "triage"})
_APPROVAL_CHOICES = ("once", "session", "deny")
#: How long a declined task sub-ticket delegation suppresses a second reply (RET-107).
DECLINE_TTL_S = 600.0


# Hermes control-plane calls (public API, imported lazily; module-level so tests can stub).
def _approval_refusal(session_key: str, actor_id: str, *, request_id: str | None) -> str | None:
    from gateway.approval_actor import approval_refusal
    return approval_refusal(session_key, actor_id, request_id=request_id)


def _approval_authority(session_key: str, request_id: str) -> dict | None:
    """Who may answer approval ``request_id`` (Hermes's entry: requester, approvers, chat type),
    read while it is still pending in Hermes; None if Hermes has no such entry or fails."""
    try:
        from tools.approval import list_gateway_approval_authority
        for entry in list_gateway_approval_authority(session_key):
            if entry.get("request_id") == request_id:
                return dict(entry)
    except Exception as exc:  # noqa: BLE001 - unknown authority: a pending answer is refused
        log.warning("linear: approval authority for %s unavailable: %s", request_id,
                    type(exc).__name__)
    return None


def _may_answer(authority: dict, actor_id: str) -> bool:
    # The live path's ``approval_refusal`` passes no platform extra / admin ids: same here.
    from gateway.approval_actor import may_answer
    return may_answer(authority, actor_id)


def _requester_of(authority: dict | None) -> str:
    """The name ``refusal_text`` words the refusal with (as ``answerable_request_ids`` does);
    no requester recorded, or no authority at all: the admins/approvers-only wording."""
    try:
        from gateway.approval_actor import _NO_REQUESTER as none
    except ImportError:  # pragma: no cover - older Hermes
        none = "the requester"
    a = authority or {}
    return str(a.get("requester_name") or a.get("requester_id") or none)


def _refusal_text(requester: str) -> str:
    from gateway.approval_actor import refusal_text
    return refusal_text(requester)


def _approval_answer_head(command: str | None) -> tuple[str, str]:
    """The answer turn's head and the approved-text suffix (spec §2.4): a gated tool's
    ``command#digest`` row names the tool and call; any other row keeps the shell form."""
    split = tool_approval.split_grant_command(command)
    if split is not None and split[0] in tool_approval.GATED_TOOLS:
        tool, digest = split
        return (f"Answer to approval for `{tool}` call {digest}:",
                ". Re-run the same call with identical arguments.")
    return f"Answer to approval for `{command}`:", ""


def _resolve_approval_card(session_key: str, choice: str, request_id: str, *, actor_id: str) -> int:
    from tools.approval import resolve_gateway_approval_card
    return resolve_gateway_approval_card(session_key, choice, request_id, actor_id=actor_id)


def _resolve_clarify(request_id: str, answer: str) -> bool:
    from tools.clarify_gateway import resolve_gateway_clarify
    return resolve_gateway_clarify(request_id, answer)


class SessionOpener:
    def __init__(
        self,
        *,
        store: Callable[[], Optional[Store]],
        client: Callable[[], Any],
        settings: Callable[[], Optional[Settings]],
        post_ack: Callable[..., Awaitable[Any]],
        enqueue: Callable[..., Awaitable[None]],
        submit: Callable[[Callable[[], Awaitable[None]]], bool],
        wait_override: Callable[[], Optional[float]],
        spawn: Callable[[Any], Any] | None = None,
    ) -> None:
        self._store = store
        self._client = client
        self._settings = settings
        self._post_ack = post_ack
        self._enqueue = enqueue  # (StartTurn, *, human) -> enqueue on the turn engine
        self._submit = submit  # run a coroutine factory on the event worker; False if stopped
        self._wait_override = wait_override
        self._spawn = spawn  # fire-and-forget label writes (never block the event worker)
        self.delegation_timers: dict[str, asyncio.TimerHandle | None] = {}
        self._declined: dict[str, float] = {}  # issue id -> monotonic time of the decline

    # ------------------------------------------------------------ task sub-ticket declines

    def note_declined(self, issue_id: str) -> None:
        """Record that a delegation of ``issue_id`` was declined (RET-107)."""
        now = time.monotonic()
        self._declined = {k: t for k, t in self._declined.items() if now - t < DECLINE_TTL_S}
        self._declined[issue_id] = now

    def recently_declined(self, issue_id: str) -> bool:
        """One delegation fires a `created` and an ``Issue update``: reply to only one."""
        t = self._declined.get(issue_id)
        return t is not None and time.monotonic() - t < DECLINE_TTL_S

    # ------------------------------------------------------------ delegation timers

    def cancel_delegation_timer(self, issue_id: str) -> None:
        handle = self.delegation_timers.pop(issue_id, None)
        if handle is not None:
            handle.cancel()

    def cancel_all(self) -> None:
        for handle in self.delegation_timers.values():
            if handle is not None:
                handle.cancel()
        self.delegation_timers.clear()

    def await_delegation(self, d: AwaitDelegation, event: dict) -> None:
        if d.issue_id in self.delegation_timers:
            return  # one timer per issue
        if d.decline and self.recently_declined(d.issue_id):
            return  # the delegation's `created` already got the reply
        actor = event.get("actor") or {}
        info = (actor.get("id"), actor.get("name"), d.decline)
        override = self._wait_override()
        wait = override if override is not None else d.deadline_s
        self.delegation_timers[d.issue_id] = asyncio.get_running_loop().call_later(
            wait, self._delegation_expired, d.issue_id, info)

    def _delegation_expired(self, issue_id: str, info: tuple) -> None:
        if issue_id not in self.delegation_timers:
            return
        self.delegation_timers[issue_id] = None  # still pending until the worker runs it
        if not self._submit(lambda: self.open_delegated_session(issue_id, *info)):
            self.delegation_timers.pop(issue_id, None)

    async def open_delegated_session(self, issue_id: str, actor_id: str | None,
                                     actor_name: str | None, decline: str | None = None) -> None:
        if issue_id not in self.delegation_timers:
            return  # a `created` arrived meanwhile
        del self.delegation_timers[issue_id]
        store, client = self._store(), self._client()
        assert store is not None and client is not None
        ticket = store.ticket(issue_id)
        if decline:  # RET-107: a task sub-ticket; reply once, never delegated mode
            if ticket is None or not actor_id or self.recently_declined(issue_id):
                return
        elif ticket is None or ticket.mode != "delegated" or not actor_id:
            return

        async def reuse_or_create() -> str:
            existing = await client.app_sessions_on_issue(issue_id)
            return existing[0]["id"] if existing else await client.create_session_on_issue(
                issue_id)

        try:  # bounded: this runs on the event worker; both calls share one deadline
            sid = await asyncio.wait_for(reuse_or_create(), ACK_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - includes TimeoutError
            log.error("linear: opening a session on %s failed: %s", issue_id, type(exc).__name__)
            return
        # A reused session is one of *our app's* sessions (app_sessions_on_issue), so marking
        # it plugin_created is accurate; the flag is sticky (MAX) if it was already set.
        store.record_session(sid, issue_id, None, "delegation", plugin_created=True)
        if decline:
            self.note_declined(issue_id)
            await self._post_ack(sid, renderer.response(decline))
            return
        ident = ticket.identifier or issue_id
        author = actor_name or DEFAULT_AUTHOR
        await self._enqueue(StartTurn(
            agent_session_id=sid, activity_id=f"delegated-{uuid.uuid4().hex[:12]}",
            issue_id=issue_id, chat_id=ticket_chat_id(issue_id, max(ticket.generation, 1)),
            thread_id=None, user_id=actor_id, user_name=author,
            text=f"{turn_header(ident, None, author)}\n\nYou have been delegated {ident}.",
            first_turn=True, ack=ACK_TEXT,
        ), human=True)

    # ------------------------------------------------------------ controls (§8.4)

    async def resolve_control(self, d: ResolveControl) -> None:
        store, post = self._store(), self._post_ack
        assert store is not None
        sid = d.agent_session_id
        row = store.control_for_session(sid)
        if row is None or row.request_id != d.request_id:
            log.info("linear: control %s no longer pending", d.request_id)
            return
        if row.state == "pending":  # the store row is authoritative over ``d.state``
            await self.answer_pending(d, row)
            return
        if d.kind == "clarify":
            ok = _resolve_clarify(d.request_id, d.answer)
            store.drop_control(d.request_id)
            if not ok:
                await post(sid, renderer.thought(STALE_CLARIFY_TEXT))
            return
        allowed = [c for c in _APPROVAL_CHOICES if c in row.choices] or list(_APPROVAL_CHOICES)
        choice = renderer.parse_approval_answer(d.answer)
        if choice is None or choice not in allowed:
            options = [o for o in renderer.approval_options() if o["value"] in allowed]
            await post(sid, renderer.elicitation(REASK_TEXT), signal="select",
                       signal_metadata={"options": options})
            return
        if not row.session_key:
            log.warning("linear: approval %s has no session key; dropped", d.request_id)
            store.drop_control(d.request_id)
            return
        requester = _approval_refusal(row.session_key, d.actor_id, request_id=d.request_id)
        if requester is not None:
            await post(sid, renderer.thought(_refusal_text(requester)))
            return
        resolved = _resolve_approval_card(row.session_key, choice, d.request_id, actor_id=d.actor_id)
        store.drop_control(d.request_id)
        if not resolved:
            await post(sid, renderer.thought(STALE_APPROVAL_TEXT))

    # ------------------------------------------------------------ pending questions

    async def answer_pending(self, d: ResolveControl, row: ControlRow) -> None:
        """Spec §6: an answer to a pending question drops the row and starts the next turn.

        The row is dropped before the turn is enqueued, so no response of that turn
        re-posts the answered question (outbound ``_repost_pending``). An approval is checked
        against the authority captured when it was asked (Hermes no longer has it); its
        grant is written only once the answer turn is enqueued (the turn engine drops it if
        that turn never starts)."""
        store, post = self._store(), self._post_ack
        assert store is not None
        sid = d.agent_session_id
        if row.kind == "clarify":
            self.drop_pending([row])
            await self._start_answer_turn(d, f'Answer to "{row.question}": {d.answer}')
            return
        allowed = [c for c in _APPROVAL_CHOICES if c in row.choices] or list(_APPROVAL_CHOICES)
        choice = renderer.parse_approval_answer(d.answer)
        if choice is None or choice not in allowed:  # re-ask; the question still stands
            options = [o for o in renderer.approval_options() if o["value"] in allowed]
            await post(sid, renderer.elicitation(REASK_TEXT), signal="select",
                       signal_metadata={"options": options})
            return
        head, rerun = _approval_answer_head(row.command)
        if choice == "deny":
            self.drop_pending([row])
            await self._start_answer_turn(d, f"{head} denied")
            return
        if row.authority is None or not _may_answer(row.authority, d.actor_id):
            await post(sid, renderer.thought(_refusal_text(_requester_of(row.authority))))
            return  # keep the row: someone allowed may still answer
        grantable = bool(row.session_key and row.command)
        if not grantable:  # no grant possible: the re-run is simply asked again
            log.warning("linear: approval %s has no session key or command; no grant",
                        d.request_id)
        self.drop_pending([row])
        if await self._start_answer_turn(d, f"{head} approved ({choice}){rerun}") and grantable:
            # After the enqueue (no await in between, so the turn has not run yet); the turn
            # engine drops it if this turn is freed before hand-off.
            store.put_grant(row.session_key, row.command, choice, d.actor_id,
                            message_id(sid, d.activity_id))

    async def _start_answer_turn(self, d: ResolveControl, body: str) -> bool:
        """Enqueue the answer turn; False (nothing enqueued) without a session or ticket."""
        store = self._store()
        assert store is not None
        sid = d.agent_session_id
        session = store.session(sid)
        ticket = store.ticket(session.issue_id) if session is not None else None
        if session is None or ticket is None:
            log.warning("linear: answer to %s has no session or ticket; no turn", d.request_id)
            return False
        chat_id, thread_id = _session_key(session, ticket)
        author = d.user_name or DEFAULT_AUTHOR
        header = turn_header(ticket.identifier or ticket.issue_id, session.root_comment_id, author)
        await self._enqueue(StartTurn(
            agent_session_id=sid, activity_id=d.activity_id, issue_id=ticket.issue_id,
            chat_id=chat_id, thread_id=thread_id, user_id=d.actor_id, user_name=author,
            text=f"{header}\n\n{body}", first_turn=False, ack=ACK_TEXT,
        ), human=True)
        return True

    def drop_pending(self, rows: list[ControlRow]) -> None:
        """Drop the *pending* rows among ``rows`` and remove their ``Blocked`` label.

        Live rows are left alone (Hermes cancels its own waiters)."""
        store, client = self._store(), self._client()
        assert store is not None
        tickets: list[str] = []
        for row in rows:
            if row.state != "pending":
                continue
            store.drop_control(row.request_id)
            if row.ticket_id and row.ticket_id not in tickets:
                tickets.append(row.ticket_id)
        if client is None or self._spawn is None:
            return
        for ticket_id in tickets:
            self._spawn(questions.set_blocked(client, ticket_id, False))

    # ------------------------------------------------------------ ticket helpers

    async def snapshot(self, issue_id: str) -> dict | None:
        client = self._client()
        assert client is not None
        try:
            return await asyncio.wait_for(client.issue_snapshot(issue_id), SNAPSHOT_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - proceed without the snapshot
            log.warning("linear: issue snapshot failed for %s: %s", issue_id, type(exc).__name__)
            return None

    async def maybe_auto_start(self, d: StartTurn, snap: dict | None) -> None:
        """Spec §8.3: move a not-yet-started ticket to the team's first started state."""
        settings, store, client = self._settings(), self._store(), self._client()
        if settings is None or not settings.auto_start or store is None or client is None:
            return
        ticket = store.ticket(d.issue_id)
        state_type = ((snap or {}).get("state") or {}).get("type") or (ticket and ticket.state_type)
        team_id = ((snap or {}).get("team") or {}).get("id") or (ticket and ticket.team_id)
        if state_type not in _AUTO_START_STATES or not team_id:
            return
        try:
            await asyncio.wait_for(client.move_to_first_started(d.issue_id, team_id),
                                   AUTO_START_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - best effort
            log.warning("linear: auto-start failed for %s: %s", d.issue_id, type(exc).__name__)


__all__ = ["SessionOpener"]
