"""Linear platform adapter: inbound path and turns (spec §6, §7, §8.1, §8.3).

Wires settings, store, client, listener, router, digest and renderer into a
Hermes ``BasePlatformAdapter``. Hermes contracts follow
``docs/hermes-integration-notes.md`` (Q1–Q10), which override the plan:

* every source is ``chat_type="dm"`` (Q4); keys are
  ``<ns>:linear:dm:<issueId>:<gen>`` / ``<ns>:linear:dm:<issueId>:<agentSessionId>``;
* the gateway interrupts busy sessions (Q5), so the adapter keeps its own
  per-Hermes-session FIFO and hands the next turn to ``handle_message`` only
  after ``on_processing_complete`` for the previous one;
* stop is a synthesized ``/stop`` with ``allow_gateway_control=True`` (Q6);
  every user-originated event uses ``allow_gateway_control=False``
  (Review Focus 1).

Webhook events for one profile are processed strictly in order by one worker
task: route -> execute every decision -> next event.

Outbound (``send``, typing, approval prompts, tool hooks) is Task 12; ``send``
is a stub here. Seams for it: ``self._writer`` (response writer whose
``degraded`` flag is the adapter status), ``self._inflight`` /
``self.last_outcome`` (turn-error classification), ``self.stop_message_ids``
(recognise the stop notice by ``reply_to``).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms._shared import get_scoped_secret
from gateway.platforms.access_policy_mixin import OwnAccessPolicyMixin
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, ProcessingOutcome

from . import renderer
from .access import AccessPolicy
from .constants import SNAPSHOT_DESCRIPTION
from .digest import fold
from .linear_client import LinearClient
from .listener import ProfileEndpoint, get_listener
from .models import (
    AddContext, AwaitDelegation, Fatal, Ignore, Interrupt, OpenSessionOnComment, Refuse,
    ResolveControl, SetMode, StartTurn,
)
from .router import (
    ACK_TEXT, DEFAULT_AUTHOR, RouterContext, message_id as make_message_id, route,
    ticket_chat_id, turn_header,
)
from .settings import ALLOW_ALL_USERS_ENV, ALLOWED_USERS_ENV, Settings, SettingsError, load_settings
from .store import Store

log = logging.getLogger(__name__)

PLATFORM = "linear"
QUEUED_ACK = "Queued — finishing current work first"
STOPPED_TEXT = "Stopped."
REASK_TEXT = "Please answer with one of the options."
STALE_APPROVAL_TEXT = "That approval is no longer pending."
STALE_CLARIFY_TEXT = "That question is no longer pending."
ACK_TIMEOUT_S = 8  # acks must not stall the worker past Linear's 10 s window
SNAPSHOT_TIMEOUT_S = 15
AUTO_START_TIMEOUT_S = 20
_AUTO_START_STATES = frozenset({"backlog", "unstarted", "triage"})
_APPROVAL_CHOICES = ("once", "session", "deny")

#: profile name -> connected adapter (registered on connect, removed on disconnect).
ADAPTERS: dict[str, "LinearAdapter"] = {}

_CONFIG_GETTER: Callable[[str, Any], Any] | None = None


def set_config_getter(fn: Callable[[str, Any], Any] | None) -> None:
    """Plugin hook: the ``ctx.get_config`` used when no getter is passed to the adapter."""
    global _CONFIG_GETTER
    _CONFIG_GETTER = fn


# Hermes control-plane calls (public API, imported lazily; module-level so tests can stub).
def _approval_refusal(session_key: str, actor_id: str, *, request_id: str | None) -> str | None:
    from gateway.approval_actor import approval_refusal
    return approval_refusal(session_key, actor_id, request_id=request_id)


def _refusal_text(requester: str) -> str:
    from gateway.approval_actor import refusal_text
    return refusal_text(requester)


def _resolve_approval_card(session_key: str, choice: str, request_id: str, *, actor_id: str) -> int:
    from tools.approval import resolve_gateway_approval_card
    return resolve_gateway_approval_card(session_key, choice, request_id, actor_id=actor_id)


def _resolve_clarify(request_id: str, answer: str) -> bool:
    from tools.clarify_gateway import resolve_gateway_clarify
    return resolve_gateway_clarify(request_id, answer)


Key = tuple[str, Optional[str]]  # (hermes chat_id, thread_id) == one Hermes session


@dataclass
class _Turn:
    decision: StartTurn
    mid: str
    human: bool = False  # a human (not an automation) started this delegation


def _human_creator(event: dict) -> bool:
    """Spec §8.3: the agent session was created by a human, not an automation."""
    if event.get("type") != "AgentSessionEvent" or event.get("botActor"):
        return False
    session = event.get("agentSession") or {}
    creator = session.get("creator")
    return isinstance(creator, dict) and bool(creator.get("id")) and not session.get("botActor")


def snapshot_block(snap: dict) -> str:
    """Ticket snapshot for a delegated first turn (spec §6.2.1)."""
    state = (snap.get("state") or {}).get("name") or "(unknown)"
    project = (snap.get("project") or {}).get("name") or "(none)"
    desc = snap.get("description") or ""
    if len(desc) > SNAPSHOT_DESCRIPTION:
        desc = desc[:SNAPSHOT_DESCRIPTION] + "…"
    return "\n".join([
        f"Ticket snapshot ({snap.get('identifier') or ''}):".replace(" ()", ""),
        f"- Title: {snap.get('title') or ''}",
        f"- State: {state}",
        f"- Labels: {', '.join(snap.get('labels') or []) or '(none)'}",
        f"- Assignee: {snap.get('assignee') or '(unassigned)'}",
        f"- Project: {project}",
        "- Description:",
        desc or "(empty)",
    ])


class LinearAdapter(OwnAccessPolicyMixin, BasePlatformAdapter):
    ALLOW_ALL_ENV_PREFIX = "LINEAR"
    SUPPORTS_MESSAGE_EDITING = False  # Q1: no streamed previews; outbound is send()-only

    def __init__(
        self,
        config: PlatformConfig,
        *,
        settings: Settings | None = None,
        client: LinearClient | None = None,
        store: Store | None = None,
        listener: Any = None,
        get_config: Callable[[str, Any], Any] | None = None,
    ) -> None:
        super().__init__(config, Platform(PLATFORM))
        self._settings_override = settings
        self._settings: Settings | None = settings
        self._client = client
        self._store = store
        self._listener = listener
        self._get_config = get_config
        # OwnAccessPolicyMixin host contract (Q4).
        self._dm_policy = self._group_policy = "allowlist"
        self._allow_from: set[str] = set()
        self._group_allow_from: set[str] = set()
        self._policy: AccessPolicy | None = None
        self._router_ctx: RouterContext | None = None
        self._profile = "default"
        self._registered = False
        self._events: asyncio.Queue | None = None
        self._worker: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._queues: dict[Key, deque[_Turn]] = {}
        self._inflight: dict[Key, _Turn] = {}
        self.last_outcome: dict[Key, ProcessingOutcome] = {}
        self.stop_message_ids: dict[str, str] = {}  # synthesized stop message_id -> agent session
        self._delegation_timers: dict[str, asyncio.TimerHandle | None] = {}
        self.delegation_wait_s: float | None = None  # override of AwaitDelegation.deadline_s
        self._writer: renderer.ActivityWriter | None = None  # responses (Task 12)
        self._ack_writer: renderer.ActivityWriter | None = None  # acks, refusals, control replies

    # ------------------------------------------------------------ status

    @property
    def degraded(self) -> bool:
        """True after a failed ``response`` write (spec §8.6); acks never count."""
        return bool(self._writer and self._writer.degraded)

    @property
    def ack_degraded(self) -> bool:
        return bool(self._ack_writer and self._ack_writer.degraded)

    # ------------------------------------------------------------ access (Q4)

    def _entry_matches(self, entries, target: str) -> bool:
        return "*" in entries or target in entries

    def resolved_allowlist_user_ids(self) -> set[str]:
        """Settings UUIDs, email-resolved UUIDs, and ``*`` under allow-all."""
        return set(self._allow_from)

    def _init_allow_from(self, settings: Settings) -> None:
        allow: set[str] = set()
        for entry in settings.allowed_users:
            if entry == "*":
                allow.add("*")
            elif "@" not in entry:
                allow.update({entry, entry.lower()})
        if self._policy is not None and self._policy.open:
            allow.add("*")
        self._allow_from = allow

    async def _email_of(self, user_id: str) -> str | None:
        assert self._store is not None and self._client is not None
        row = self._store.cached_user(user_id, time.time())
        if row is not None:
            return row.email
        email, name = await self._client.user(user_id)
        self._store.put_user(user_id, email or None, name or None, time.time())
        return email or None

    async def _allowed(self, user_id: str | None) -> bool:
        if self._policy is None or not user_id:
            return False
        ok = await self._policy.allowed(user_id, self._email_of)
        if ok and not self._policy.open:
            self._allow_from.add(user_id)  # email-resolved UUIDs join the gateway allowlist
        return ok

    def _warn_global_allowlist(self) -> None:
        if get_scoped_secret("GATEWAY_ALLOWED_USERS") and not get_scoped_secret(ALLOWED_USERS_ENV):
            log.warning(
                "linear: GATEWAY_ALLOWED_USERS is set but %s is not; the gateway will admit only "
                "Linear UUIDs listed in GATEWAY_ALLOWED_USERS (email/settings entries are denied). "
                "Set %s to enable the Linear allowlist.", ALLOWED_USERS_ENV, ALLOWED_USERS_ENV)

    # ------------------------------------------------------------ lifecycle

    def _load_settings(self) -> Settings:
        get_config = self._get_config or _CONFIG_GETTER
        if get_config is None:
            raise SettingsError("plugin config is unavailable (no get_config)")
        env = {k: v for k in (ALLOWED_USERS_ENV, ALLOW_ALL_USERS_ENV) if (v := get_scoped_secret(k))}
        return load_settings(get_config, lambda name: get_scoped_secret(name), env)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        # Q3: all settings and secrets are snapshotted here, inside the profile's scope.
        try:
            settings = self._settings_override or self._load_settings()
        except SettingsError as exc:
            log.error("linear: cannot connect: %s", exc)
            self._set_fatal_error("linear_config", str(exc), retryable=False)
            return False
        self._settings = settings
        self._profile = (getattr(self, "_owner_profile", None) or "default").strip() or "default"
        self._warn_global_allowlist()
        if self._client is None:
            secret = settings.client_secret
            self._client = LinearClient(settings.client_id, lambda: secret)
        try:
            await self._client.token()
            viewer = await self._client.viewer()
        except Exception as exc:  # noqa: BLE001 - never echo exception text (may hold secrets)
            code = getattr(exc, "code", None)
            log.error("linear: token/viewer check failed: %s code=%s", type(exc).__name__, code)
            self._set_fatal_error("linear_auth", "Linear token or viewer request failed",
                                  retryable=code == "NETWORK")
            return False
        if self._store is None:
            self._store = Store.open(None)
        store = self._store
        self._policy = AccessPolicy(settings.allowed_users, settings.allow_all_users)
        self._init_allow_from(settings)
        self._router_ctx = RouterContext(store, str(viewer["id"]), self._allowed,
                                         viewer.get("displayName"))
        self._writer = renderer.ActivityWriter(self._client)
        self._ack_writer = renderer.ActivityWriter(self._client, timeout_s=ACK_TIMEOUT_S)
        self._events = asyncio.Queue()
        self._worker = asyncio.create_task(self._run_worker())

        if self._listener is None:
            self._listener = get_listener(settings.listener_host, settings.listener_port)
        listener = self._listener
        webhook_secret = settings.webhook_secret.encode()
        listener.register(ProfileEndpoint(
            profile=self._profile,
            webhook_secret=lambda: webhook_secret,
            client_id=settings.client_id,
            deliver=self.deliver,
            seen=lambda wid: store.seen_webhook(wid, time.time()),
        ))
        self._registered = True
        await listener.wait_idle()
        if listener.start_error is not None:
            log.error("linear: webhook listener %s:%s failed to start: %s", settings.listener_host,
                      settings.listener_port, type(listener.start_error).__name__)
            self._unregister()
            await self._stop_worker()
            self._set_fatal_error("linear_listener", "Linear webhook listener failed to start",
                                  retryable=False)
            return False
        ADAPTERS[self._profile] = self
        self._mark_connected()
        return True

    def _unregister(self) -> None:
        if self._registered and self._listener is not None:
            self._listener.unregister(self._profile)
        self._registered = False

    async def _stop_worker(self) -> None:
        tasks = [t for t in (self._worker, *self._tasks) if t is not None and not t.done()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._worker = None
        self._tasks.clear()

    async def disconnect(self) -> None:
        self._unregister()
        for handle in self._delegation_timers.values():
            if handle is not None:
                handle.cancel()
        self._delegation_timers.clear()
        await self._stop_worker()
        if ADAPTERS.get(self._profile) is self:
            del ADAPTERS[self._profile]
        if self._client is not None:
            await self._client.close()
        if self._store is not None:
            self._store.close()
            self._store = None
        self._queues.clear()
        self._inflight.clear()
        self._mark_disconnected()

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        issue_id = str(chat_id).split(":", 1)[0]
        ticket = self._store.ticket(issue_id) if self._store is not None else None
        ident = (ticket.identifier if ticket else None) or issue_id
        return {"name": f"Linear {ident}", "type": "dm", "chat_id": chat_id}

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> SendResult:
        # Task 12 implements the Q1 send contract (reply_to -> reply_to_message_id ->
        # latest_turn_for_chat; thought/response/error classification).
        log.warning("linear: outbound send not implemented yet; dropped (chat %s)", chat_id)
        return SendResult(success=False, error="linear outbound not implemented")

    # ------------------------------------------------------------ event worker

    def deliver(self, payload: dict) -> None:
        """Listener callback: queue one verified webhook payload for the worker."""
        if self._events is None:
            log.warning("linear: webhook dropped, adapter not connected")
            return
        self._events.put_nowait(("event", payload))

    async def _run_worker(self) -> None:
        assert self._events is not None
        while True:
            kind, item = await self._events.get()
            try:
                if kind == "event":
                    await self.on_event(item)
                else:
                    await item()
            except Exception as exc:  # noqa: BLE001 - one bad event must not stop the worker
                log.error("linear: event processing failed: %s", type(exc).__name__)
                log.debug("linear: event processing traceback", exc_info=True)
            finally:
                self._events.task_done()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def wait_idle(self) -> None:
        """Wait until queued events, scheduled turn releases and turn tasks are done."""
        for _ in range(1000):
            await asyncio.sleep(0)
            if self._events is not None:
                await self._events.join()
            pending = [t for t in self._tasks if not t.done()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
                continue
            await asyncio.sleep(0)
            if not any(not t.done() for t in self._tasks) and (
                    self._events is None or self._events.empty()):
                return

    async def on_event(self, event: dict) -> None:
        """Route one webhook payload and execute its decisions in order."""
        if self._router_ctx is None:
            log.warning("linear: event dropped, adapter not connected")
            return
        log.debug("linear event type=%s action=%s webhookId=%s",
                  event.get("type"), event.get("action"), event.get("webhookId"))
        for decision in await route(event, self._router_ctx):
            await self._execute(decision, event)

    async def _execute(self, d, event: dict) -> None:
        store = self._store
        assert store is not None and self._ack_writer is not None
        if isinstance(d, StartTurn):
            await self._enqueue_turn(d, human=_human_creator(event))
        elif isinstance(d, AddContext):
            store.add_context(list(d.items))
        elif isinstance(d, SetMode):
            store.set_mode(d.issue_id, d.mode, d.generation)  # type: ignore[arg-type]
        elif isinstance(d, ResolveControl):
            await self._resolve_control(d)
        elif isinstance(d, Interrupt):
            await self._interrupt(d.agent_session_id)
        elif isinstance(d, Refuse):
            await self._ack_writer.post(d.agent_session_id, renderer.error(d.text))
        elif isinstance(d, AwaitDelegation):
            self._await_delegation(d, event)
        elif isinstance(d, OpenSessionOnComment):
            await self._open_session_on_comment(d)
        elif isinstance(d, Fatal):
            await self._fatal(d.reason)
        elif isinstance(d, Ignore):
            log.debug("linear: ignored (%s)", d.reason)
        else:  # pragma: no cover - exhaustive over models.Decision
            log.warning("linear: unknown decision %s", type(d).__name__)

    # ------------------------------------------------------------ turns / FIFO (Q5)

    async def _enqueue_turn(self, d: StartTurn, *, human: bool = False) -> None:
        assert self._ack_writer is not None
        self._cancel_delegation_timer(d.issue_id)
        key: Key = (d.chat_id, d.thread_id)
        busy = key in self._inflight or bool(self._queues.get(key))
        # §8.1: the ack goes out before the turn is handed to the gateway. Short timeout;
        # a failed ack never blocks the turn.
        await self._ack_writer.post(d.agent_session_id,
                                    renderer.thought(QUEUED_ACK if busy else d.ack), ephemeral=True)
        mid = make_message_id(d.agent_session_id, d.activity_id)
        self._queues.setdefault(key, deque()).append(_Turn(d, mid, human))
        self._release_next(key)

    def _release_next(self, key: Key) -> None:
        """Start the next queued turn for ``key`` if none is in flight (synchronous)."""
        if key in self._inflight:
            return
        queue = self._queues.get(key)
        if not queue:
            self._queues.pop(key, None)
            return
        turn = queue.popleft()
        if not queue:
            del self._queues[key]
        self._inflight[key] = turn
        self._spawn(self._dispatch(key, turn))

    async def _dispatch(self, key: Key, turn: _Turn) -> None:
        store = self._store
        assert store is not None
        d = turn.decision
        ticket_session = d.thread_id is None
        text = d.text
        items, watermark = ([], None)
        if ticket_session:
            items, watermark = store.pending_context_batch(d.issue_id)
            digest = fold(items) if items else None
            if digest:
                text = f"{digest}\n\n{text}"
        snap = None
        if d.first_turn and ticket_session:
            snap = await self._snapshot(d.issue_id)
            if snap:
                text = f"{text}\n\n{snapshot_block(snap)}"
        try:
            store.record_turn(turn.mid, d.agent_session_id, d.chat_id, d.thread_id)
            self.last_outcome.pop(key, None)
            source = self.build_source(chat_id=d.chat_id, chat_type="dm", user_id=d.user_id,
                                       user_name=d.user_name, thread_id=d.thread_id,
                                       message_id=turn.mid)
            event = MessageEvent(text=text, source=source, message_id=turn.mid, user_id=d.user_id,
                                 user_name=d.user_name, allow_gateway_control=False)
            await self.handle_message(event)
        except Exception as exc:  # noqa: BLE001 - not admitted: free the FIFO, keep context
            log.error("linear: gateway refused turn %s: %s", turn.mid, type(exc).__name__)
            if self._inflight.get(key) is turn:
                del self._inflight[key]
            asyncio.get_running_loop().call_soon(self._release_next, key)
            return
        if items:
            store.consume_context(d.issue_id, turn.mid, up_to_id=watermark)
        if d.first_turn and ticket_session and turn.human:
            await self._maybe_auto_start(d, snap)

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        key = next((k for k, t in self._inflight.items() if t.mid == event.message_id), None)
        try:
            await super().on_processing_complete(event, outcome)
            if key is not None:
                self.last_outcome[key] = outcome
        finally:
            if key is not None:
                self._inflight.pop(key, None)
                # Never inline: the turn-error notice must reach send() first (Q1/Q5).
                asyncio.get_running_loop().call_soon(self._release_next, key)

    async def _snapshot(self, issue_id: str) -> dict | None:
        assert self._client is not None
        try:
            return await asyncio.wait_for(self._client.issue_snapshot(issue_id), SNAPSHOT_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - proceed without the snapshot
            log.warning("linear: issue snapshot failed for %s: %s", issue_id, type(exc).__name__)
            return None

    async def _maybe_auto_start(self, d: StartTurn, snap: dict | None) -> None:
        """Spec §8.3: move a not-yet-started ticket to the team's first started state."""
        if self._settings is None or not self._settings.auto_start or self._store is None:
            return
        ticket = self._store.ticket(d.issue_id)
        state_type = ((snap or {}).get("state") or {}).get("type") or (ticket and ticket.state_type)
        team_id = ((snap or {}).get("team") or {}).get("id") or (ticket and ticket.team_id)
        if state_type not in _AUTO_START_STATES or not team_id:
            return
        assert self._client is not None
        try:
            await asyncio.wait_for(self._client.move_to_first_started(d.issue_id, team_id),
                                   AUTO_START_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - best effort
            log.warning("linear: auto-start failed for %s: %s", d.issue_id, type(exc).__name__)

    # ------------------------------------------------------------ stop (Q6)

    async def _interrupt(self, agent_session_id: str) -> None:
        dropped = 0
        for key in list(self._queues):
            queue = self._queues[key]
            keep = deque(t for t in queue if t.decision.agent_session_id != agent_session_id)
            dropped += len(queue) - len(keep)
            if keep:
                self._queues[key] = keep
            else:
                del self._queues[key]
        running = [t for t in self._inflight.values()
                   if t.decision.agent_session_id == agent_session_id]
        for turn in running:
            await self._send_stop(turn)
        if dropped and not running and self._writer is not None:
            await self._writer.post(agent_session_id, renderer.response(STOPPED_TEXT))

    async def _send_stop(self, turn: _Turn) -> None:
        d = turn.decision
        mid = f"{d.agent_session_id}:stop:{uuid.uuid4().hex[:12]}"
        self.stop_message_ids[mid] = d.agent_session_id
        source = self.build_source(chat_id=d.chat_id, chat_type="dm", user_id=d.user_id,
                                   user_name=d.user_name, thread_id=d.thread_id, message_id=mid)
        stop = MessageEvent(text="/stop", source=source, message_id=mid, user_id=d.user_id,
                            user_name=d.user_name, allow_gateway_control=True)
        try:
            await self.handle_message(stop)
        except Exception as exc:  # noqa: BLE001
            log.error("linear: stop for %s failed: %s", d.agent_session_id, type(exc).__name__)

    # ------------------------------------------------------------ controls (§8.4)

    async def _resolve_control(self, d: ResolveControl) -> None:
        store, writer = self._store, self._ack_writer
        assert store is not None and writer is not None
        sid = d.agent_session_id
        row = store.control_for_session(sid)
        if row is None or row.request_id != d.request_id:
            log.info("linear: control %s no longer pending", d.request_id)
            return
        if d.kind == "clarify":
            ok = _resolve_clarify(d.request_id, d.answer)
            store.drop_control(d.request_id)
            if not ok:
                await writer.post(sid, renderer.thought(STALE_CLARIFY_TEXT))
            return
        allowed = [c for c in _APPROVAL_CHOICES if c in row.choices] or list(_APPROVAL_CHOICES)
        choice = renderer.parse_approval_answer(d.answer)
        if choice is None or choice not in allowed:
            options = [o for o in renderer.approval_options() if o["value"] in allowed]
            await writer.post(sid, renderer.elicitation(REASK_TEXT), signal="select",
                              signal_metadata={"options": options})
            return
        if not row.session_key:
            log.warning("linear: approval %s has no session key; dropped", d.request_id)
            store.drop_control(d.request_id)
            return
        requester = _approval_refusal(row.session_key, d.actor_id, request_id=d.request_id)
        if requester is not None:
            await writer.post(sid, renderer.thought(_refusal_text(requester)))
            return
        resolved = _resolve_approval_card(row.session_key, choice, d.request_id, actor_id=d.actor_id)
        store.drop_control(d.request_id)
        if not resolved:
            await writer.post(sid, renderer.thought(STALE_APPROVAL_TEXT))

    # ------------------------------------------------------------ sessions we open

    def _cancel_delegation_timer(self, issue_id: str) -> None:
        handle = self._delegation_timers.pop(issue_id, None)
        if handle is not None:
            handle.cancel()

    def _await_delegation(self, d: AwaitDelegation, event: dict) -> None:
        if d.issue_id in self._delegation_timers:
            return  # one timer per issue
        actor = event.get("actor") or {}
        info = (actor.get("id"), actor.get("name"))
        wait = self.delegation_wait_s if self.delegation_wait_s is not None else d.deadline_s
        self._delegation_timers[d.issue_id] = asyncio.get_running_loop().call_later(
            wait, self._delegation_expired, d.issue_id, info)

    def _delegation_expired(self, issue_id: str, info: tuple) -> None:
        if issue_id not in self._delegation_timers or self._events is None:
            return
        self._delegation_timers[issue_id] = None  # still pending until the worker runs it
        self._events.put_nowait(("call", lambda: self._open_delegated_session(issue_id, *info)))

    async def _open_delegated_session(self, issue_id: str, actor_id: str | None,
                                      actor_name: str | None) -> None:
        if issue_id not in self._delegation_timers:
            return  # a `created` arrived meanwhile
        del self._delegation_timers[issue_id]
        store, client = self._store, self._client
        assert store is not None and client is not None
        ticket = store.ticket(issue_id)
        if ticket is None or ticket.mode != "delegated" or not actor_id:
            return
        try:
            existing = await client.app_sessions_on_issue(issue_id)
            sid = existing[0]["id"] if existing else await client.create_session_on_issue(issue_id)
        except Exception as exc:  # noqa: BLE001
            log.error("linear: opening a session on %s failed: %s", issue_id, type(exc).__name__)
            return
        store.record_session(sid, issue_id, None, "delegation", plugin_created=True)
        ident = ticket.identifier or issue_id
        author = actor_name or DEFAULT_AUTHOR
        await self._enqueue_turn(StartTurn(
            agent_session_id=sid, activity_id=f"delegated-{uuid.uuid4().hex[:12]}",
            issue_id=issue_id, chat_id=ticket_chat_id(issue_id, max(ticket.generation, 1)),
            thread_id=None, user_id=actor_id, user_name=author,
            text=f"{turn_header(ident, None, author)}\n\nYou have been delegated {ident}.",
            first_turn=True, ack=ACK_TEXT,
        ), human=True)

    async def _open_session_on_comment(self, d: OpenSessionOnComment) -> None:
        store, client = self._store, self._client
        assert store is not None and client is not None
        if store.session_by_root_comment(d.comment_id) is not None:
            return  # R18: a session already hangs off this comment
        ticket = store.ticket(d.issue_id)
        if ticket is None or ticket.mode != "delegated":
            return
        # Placeholder first, so a `created` for it is ignored whatever the arrival order.
        store.record_session(f"pending:{d.comment_id}", d.issue_id, d.comment_id, "comment",
                             plugin_created=True)
        try:
            sid = await client.create_session_on_comment(d.comment_id)
        except Exception as exc:  # noqa: BLE001
            log.error("linear: session on comment %s failed: %s", d.comment_id, type(exc).__name__)
            return
        store.record_session(sid, d.issue_id, d.comment_id, "comment", plugin_created=True)
        ident = ticket.identifier or d.issue_id
        await self._enqueue_turn(StartTurn(
            agent_session_id=sid, activity_id=d.comment_id, issue_id=d.issue_id,
            chat_id=ticket_chat_id(d.issue_id, max(ticket.generation, 1)), thread_id=None,
            user_id=d.user_id, user_name=d.user_name,
            text=f"{turn_header(ident, d.comment_id, d.user_name)}\n\n{d.body}",
            first_turn=False, ack=ACK_TEXT,
        ))

    async def _fatal(self, reason: str) -> None:
        log.error("linear: fatal: %s", reason)
        self._set_fatal_error("linear_fatal", reason, retryable=False)
        self._unregister()
        await self._notify_fatal_error()


__all__ = ["ADAPTERS", "LinearAdapter", "QUEUED_ACK", "set_config_getter", "snapshot_block"]
