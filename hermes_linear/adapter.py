"""Linear platform adapter: lifecycle, access policy, event worker (spec §6, §7, §8.1, §8.3).

Contracts: ``docs/hermes-integration-notes.md`` (Q1–Q10). Every source is ``chat_type="dm"``
(Q4). Turns: ``turns.TurnEngine`` (``self.turns``; FIFO Q5, stop Q6). Sessions the plugin opens
and control answers: ``sessions_open.SessionOpener`` (``self.opener``). This module keeps the
lifecycle, the access-policy host contract, decision dispatch and the base hook overrides.

Webhook events for one profile are processed strictly in order by one worker
task (route -> execute every decision -> next event). Linear calls on the
worker are bounded by ``ACK_TIMEOUT_S`` or spawned, so the next event's ack is
never held past Linear's 10 s window.

Seams for Task 12 (outbound): ``self._writer`` (its ``degraded`` flag is the
adapter status), ``self.turns.current_turn_for`` / ``last_outcome_for``, and
``self.stop_message_ids`` (recognise the stop notice by ``reply_to``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Callable, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms._shared import get_scoped_secret
from gateway.platforms.access_policy_mixin import OwnAccessPolicyMixin
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, ProcessingOutcome

from . import renderer
from .access import AccessPolicy, gateway_allow_from
from .constants import ACK_TIMEOUT_S
from .linear_client import LinearClient
from .listener import ProfileEndpoint, get_listener
from .models import (AddContext, AwaitDelegation, Fatal, Ignore, Interrupt, OpenSessionOnComment,
                     Refuse, ResolveControl, SetMode, StartTurn)
from .router import RouterContext, route
from .sessions_open import SessionOpener
from .settings import (
    GLOBAL_ALLOWLIST_WARNING, Settings, SettingsError, load_scoped_settings, only_global_allowlist,
)
from .store import Store
from .turns import QUEUED_ACK, TurnEngine, human_creator, snapshot_block

log = logging.getLogger(__name__)

PLATFORM = "linear"

#: profile name -> connected adapter (registered on connect, removed on disconnect).
ADAPTERS: dict[str, "LinearAdapter"] = {}

_CONFIG_GETTER: Callable[[str, Any], Any] | None = None


def set_config_getter(fn: Callable[[str, Any], Any] | None) -> None:
    """Plugin hook: the ``ctx.get_config`` used when no getter is passed to the adapter."""
    global _CONFIG_GETTER
    _CONFIG_GETTER = fn


class LinearAdapter(OwnAccessPolicyMixin, BasePlatformAdapter):
    ALLOW_ALL_ENV_PREFIX = "LINEAR"
    SUPPORTS_MESSAGE_EDITING = False  # Q1: no streamed previews; outbound is send()-only

    def __init__(self, config: PlatformConfig, *, settings: Settings | None = None,
                 client: LinearClient | None = None, store: Store | None = None,
                 listener: Any = None, get_config: Callable[[str, Any], Any] | None = None) -> None:
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
        self._writer: renderer.ActivityWriter | None = None  # responses (Task 12)
        self._ack_writer: renderer.ActivityWriter | None = None  # acks, refusals, control replies
        self.delegation_wait_s: float | None = None  # override of AwaitDelegation.deadline_s
        self.turns = TurnEngine(  # callables late-bound: tests replace handle_message etc.
            handle_message=lambda ev: self.handle_message(ev),
            build_source=lambda **kw: self.build_source(**kw), spawn=self._spawn,
            store=lambda: self._store, post_ack=self._post_ack, post_response=self._post_response,
            snapshot=lambda i: self.opener.snapshot(i),
            auto_start=lambda d, snap: self.opener.maybe_auto_start(d, snap),
            session_key_of=self._base_session_key, base_owns=self._base_owns,
            base_busy=lambda sk: sk in getattr(self, "_active_sessions", {}))
        self.opener = SessionOpener(
            store=lambda: self._store, client=lambda: self._client,
            settings=lambda: self._settings, post_ack=self._post_ack,
            enqueue=self._enqueue_turn, submit=self._submit,
            wait_override=lambda: self.delegation_wait_s)

    last_outcome = property(lambda self: self.turns.last_outcome)
    stop_message_ids = property(lambda self: self.turns.stop_message_ids)

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

    async def _email_of(self, user_id: str) -> str | None:
        assert self._store is not None and self._client is not None
        row = self._store.cached_user(user_id, time.time())
        if row is not None:
            return row.email
        # Bounded: router lookups run on the event worker. A timeout raises -> denied.
        email, name = await asyncio.wait_for(self._client.user(user_id), ACK_TIMEOUT_S)
        self._store.put_user(user_id, email or None, name or None, time.time())
        return email or None

    async def _allowed(self, user_id: str | None) -> bool:
        if self._policy is None or not user_id:
            return False
        ok = await self._policy.allowed(user_id, self._email_of)
        if ok and not self._policy.open:
            self._allow_from.add(user_id)  # email-resolved UUIDs join the gateway allowlist
        return ok

    # ------------------------------------------------------------ lifecycle

    def _load_settings(self) -> Settings:
        get_config = self._get_config or _CONFIG_GETTER
        if get_config is None:
            raise SettingsError("plugin config is unavailable (no get_config)")
        return load_scoped_settings(get_config, get_scoped_secret)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if self._registered and self._worker is not None and not self._worker.done():
            log.info("linear: connect() while already connected; nothing to do")
            return True
        if self._worker is not None:
            await self._stop_worker()  # left over from a fatal unregister
        # Q3: all settings and secrets are snapshotted here, inside the profile's scope.
        try:
            settings = self._settings_override or self._load_settings()
        except SettingsError as exc:
            log.error("linear: cannot connect: %s", exc)
            self._set_fatal_error("linear_config", str(exc), retryable=False)
            return False
        self._settings = settings
        self._profile = (getattr(self, "_owner_profile", None) or "default").strip() or "default"
        if only_global_allowlist(get_scoped_secret):
            log.warning(GLOBAL_ALLOWLIST_WARNING)
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
        self._allow_from = gateway_allow_from(settings.allowed_users, self._policy.open)
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
        self.opener.cancel_all()
        await self._stop_worker()  # also cancels spawned tasks (acks, dispatches, stops)
        self._events = None  # deliver() is now a logged no-op
        if ADAPTERS.get(self._profile) is self:
            del ADAPTERS[self._profile]
        if self._client is not None:
            await self._client.close()
        if self._store is not None:
            self._store.close()
            self._store = None
        self.turns.reset()
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
        if self._events is None:  # never connected, or disconnected
            log.warning("linear: webhook dropped, adapter not connected")
            return
        self._events.put_nowait(("event", payload))

    async def _run_worker(self) -> None:
        events = self._events
        assert events is not None
        while True:
            kind, item = await events.get()
            try:
                if kind == "event":
                    await self.on_event(item)
                else:
                    await item()
            except Exception as exc:  # noqa: BLE001 - one bad event must not stop the worker
                log.error("linear: event processing failed: %s", type(exc).__name__)
                log.debug("linear: event processing traceback", exc_info=True)
            finally:
                events.task_done()

    def _submit(self, factory) -> bool:
        """Run ``factory()`` on the event worker (in order with webhooks); False if stopped."""
        if self._events is None:
            return False
        self._events.put_nowait(("call", factory))
        return True

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
            if pending or self.turns.draining:  # draining: waiting for the base task to end
                await asyncio.gather(*pending, asyncio.sleep(0.01), return_exceptions=True)
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
        assert store is not None
        if isinstance(d, StartTurn):
            await self._enqueue_turn(d, human=human_creator(event))
        elif isinstance(d, AddContext):
            store.add_context(list(d.items))
        elif isinstance(d, SetMode):
            store.set_mode(d.issue_id, d.mode, d.generation)  # type: ignore[arg-type]
        elif isinstance(d, ResolveControl):
            await self.opener.resolve_control(d)
        elif isinstance(d, Interrupt):
            self.turns.interrupt(d.agent_session_id)
        elif isinstance(d, Refuse):
            await self._post_ack(d.agent_session_id, renderer.error(d.text))
        elif isinstance(d, AwaitDelegation):
            self.opener.await_delegation(d, event)
        elif isinstance(d, OpenSessionOnComment):
            await self.opener.open_session_on_comment(d)
        elif isinstance(d, Fatal):
            await self._fatal(d.reason)
        elif isinstance(d, Ignore):
            log.debug("linear: ignored (%s)", d.reason)
        else:  # pragma: no cover - exhaustive over models.Decision
            log.warning("linear: unknown decision %s", type(d).__name__)

    # ------------------------------------------------------------ turn engine glue

    async def _enqueue_turn(self, d: StartTurn, *, human: bool = False) -> None:
        self.opener.cancel_delegation_timer(d.issue_id)
        self.turns.enqueue(d, human=human)

    async def _post_ack(self, sid: str, content: dict, **kw) -> bool:
        return bool(self._ack_writer) and await self._ack_writer.post(sid, content, **kw)

    async def _post_response(self, sid: str, content: dict, **kw) -> bool:
        return bool(self._writer) and await self._writer.post(sid, content, **kw)

    def _base_session_key(self, event: MessageEvent) -> str | None:
        with contextlib.suppress(Exception):  # unknown key: _base_owns assumes yes (watchdog)
            return self._event_session_key(event)
        return None

    def _base_owns(self, session_key: str | None) -> bool:
        """True while the base holds a guard or a live processing task for ``session_key``."""
        if session_key is None:
            return True
        task = getattr(self, "_session_tasks", {}).get(session_key)
        return (session_key in getattr(self, "_active_sessions", {})
                or (task is not None and not task.done()))

    async def on_processing_start(self, event: MessageEvent) -> None:
        try:
            await super().on_processing_start(event)
        finally:
            self.turns.mark_admitted(event.message_id)

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        found = self.turns.find(event.message_id)
        try:
            await super().on_processing_complete(event, outcome)
            self.turns.record_outcome(found, outcome)
        finally:
            self.turns.complete(found)  # releases after this base task ends (Q5)

    async def _fatal(self, reason: str) -> None:
        log.error("linear: fatal: %s", reason)
        self._set_fatal_error("linear_fatal", reason, retryable=False)
        self._unregister()
        await self._notify_fatal_error()


__all__ = ["ADAPTERS", "LinearAdapter", "QUEUED_ACK", "set_config_getter", "snapshot_block"]
