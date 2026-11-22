"""Turn engine: per-Hermes-session FIFO, hand-off, completion and stop (spec §6, §8.1; Q5, Q6).

The gateway interrupts busy sessions (Q5), so turns for one Hermes session key
``(chat_id, thread_id)`` are serialized here and handed to ``handle_message``
one at a time. The engine talks to the adapter only through injected callables
(see ``TurnEngine.__init__``), so it holds no reference to the adapter.

Lifecycle of a turn: queued -> in flight (``_dispatch`` task: wait for the ack,
fold the digest, snapshot) -> handed off (``handle_message``) -> admitted
(``on_processing_start``) -> complete (``on_processing_complete``) -> draining
(until the base's processing task has finished and dropped its session guard)
-> the next turn is released. A turn that is refused, never admitted, fails
before hand-off, is stopped before hand-off, or exceeds
``constants.TURN_SAFETY_TIMEOUT_S`` frees the FIFO too, as does a stopped turn the gateway has
not completed ``constants.STOP_SETTLE_S`` after the stop (Q6 safety net); those two nets drain
like a completed turn (wait for the base's session guard, bounded) before the next release.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from gateway.platforms.event import MessageEvent, ProcessingOutcome

from . import constants, renderer
from .constants import ACK_TIMEOUT_S, SNAPSHOT_DESCRIPTION
from .digest import fold
from .models import StartTurn
from .router import message_id as make_message_id
from .store import Store

log = logging.getLogger(__name__)

QUEUED_ACK = "Queued — finishing current work first"
STOPPED_TEXT = "Stopped."
TURN_FAILED_TEXT = "Hermes could not start this turn. Please try again."
RELEASE_RETRY_S = 0.05  # re-check interval while the base still holds the session guard
RELEASE_MAX_TRIES = 200
DRAIN_DEADLINE_S = 30.0  # a key still draining this long after complete() is released anyway

Key = tuple[str, Optional[str]]  # (hermes chat_id, thread_id) == one Hermes session
Post = Callable[..., Awaitable[Any]]  # (agent_session_id, content, **writer kwargs)


@dataclass(eq=False)
class Turn:
    decision: StartTurn
    mid: str
    human: bool = False  # a human (not an automation) started this delegation
    ack: Optional[asyncio.Task] = None  # the ack post; awaited (bounded) before hand-off
    task: Optional[asyncio.Task] = None  # this turn's _dispatch task
    handed_off: bool = False  # set immediately before handle_message
    admitted: bool = False  # on_processing_start fired for this turn
    session_key: Optional[str] = None  # the base's session key (busy-guard lookups)
    safety: Optional[asyncio.TimerHandle] = None  # TURN_SAFETY_TIMEOUT_S watchdog
    stop_settle: Optional[asyncio.TimerHandle] = None  # STOP_SETTLE_S watchdog after /stop
    finished: bool = False  # on_processing_complete ran (complete()); its agent run is over

    def disarm(self) -> None:
        for handle in (self.safety, self.stop_settle):
            if handle is not None:
                handle.cancel()
        self.safety = self.stop_settle = None


def human_creator(event: dict) -> bool:
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


class TurnEngine:
    def __init__(
        self,
        *,
        handle_message: Callable[[MessageEvent], Awaitable[Any]],
        build_source: Callable[..., Any],
        spawn: Callable[[Awaitable[Any]], asyncio.Task],
        store: Callable[[], Optional[Store]],
        post_ack: Post,
        post_response: Post,
        snapshot: Callable[[str], Awaitable[Optional[dict]]],
        auto_start: Callable[[StartTurn, Optional[dict]], Awaitable[None]],
        session_key_of: Callable[[MessageEvent], Optional[str]],
        base_owns: Callable[[Optional[str]], bool],
        base_busy: Callable[[str], bool],
        on_stop_unsettled: Callable[[Turn], None] = lambda turn: None,
        on_safety_expired: Callable[[Turn], None] = lambda turn: None,
        on_drained: Callable[[], None] = lambda: None,
    ) -> None:
        self._handle_message = handle_message
        self._build_source = build_source
        self._spawn = spawn
        self._store = store
        self._post_ack = post_ack
        self._post_response = post_response
        self._snapshot = snapshot
        self._auto_start = auto_start
        self._session_key_of = session_key_of
        self._base_owns = base_owns
        self._base_busy = base_busy
        self._on_stop_unsettled = on_stop_unsettled  # posts Stopped. (outbound)
        self._on_safety_expired = on_safety_expired  # posts the timeout error (outbound)
        # A drain ended: a turn freed by the stop-settle / safety nets never completes (its
        # late on_processing_complete finds no turn), so per-session sweeps run here too.
        self._on_drained = on_drained
        self.queues: dict[Key, deque[Turn]] = {}
        self.inflight: dict[Key, Turn] = {}
        # Completed turns whose base task has not finished yet; release waits for it (Q5).
        self.draining: set[Key] = set()
        self._drain_timers: dict[Key, asyncio.TimerHandle] = {}
        self._drain_deadlines: dict[Key, tuple[Turn, asyncio.TimerHandle]] = {}
        self.closing = False  # disconnect in progress: nothing is released any more
        self.last_outcome: dict[Key, ProcessingOutcome] = {}
        self.stop_message_ids: dict[str, str] = {}  # synthesized stop message_id -> agent session
        self._unadmitted_logged = False

    # ------------------------------------------------------------ accessors (T12)

    def current_turn_for(self, chat_id: str, thread_id: str | None) -> Turn | None:
        """The in-flight turn of one Hermes session, if any."""
        return self.inflight.get((chat_id, thread_id))

    def last_outcome_for(self, key: Key) -> ProcessingOutcome | None:
        return self.last_outcome.get(key)

    def turn_for_session_key(self, session_key: str | None) -> tuple[Key, Turn] | None:
        """The in-flight turn whose gateway session key (as the base computed it) is
        ``session_key``; tool hooks route by it (``HERMES_SESSION_KEY``)."""
        if not session_key:
            return None
        key = next((k for k, t in self.inflight.items() if t.session_key == session_key), None)
        return None if key is None else (key, self.inflight[key])

    def owns_session_key(self, session_key: str | None) -> bool:
        return self.turn_for_session_key(session_key) is not None

    def _queued_session_key(self, turn: Turn) -> Optional[str]:
        """The gateway session key ``turn`` will get at hand-off (``None`` if unknown)."""
        if turn.session_key:
            return turn.session_key
        d = turn.decision
        try:
            source = self._build_source(chat_id=d.chat_id, chat_type="dm", user_id=d.user_id,
                                        user_name=d.user_name, thread_id=d.thread_id,
                                        message_id=turn.mid)
            return self._session_key_of(MessageEvent(
                text="", source=source, message_id=turn.mid, user_id=d.user_id,
                user_name=d.user_name, allow_gateway_control=False))
        except Exception:  # noqa: BLE001 - unknown: does not count as that session
            return None

    def has_session_turns(self, session_key: str | None, *,
                          exclude: Optional[Turn] = None) -> bool:
        """True while any in-flight, draining (not yet finished) or queued turn, on any
        Linear key, belongs to gateway session ``session_key``.

        Draining turns freed by the stop-settle / safety nets count (the gateway may still be
        running them); draining turns whose ``on_processing_complete`` ran do not. In-flight
        turns still in ``_hand_off`` (ack wait, snapshot) have no ``session_key`` yet; like
        queued turns they count under the key they will get (``_queued_session_key``).
        """
        if not session_key:
            return False
        turns = [*self.inflight.values(),
                 *(t for t, _h in self._drain_deadlines.values() if not t.finished),
                 *(t for q in self.queues.values() for t in q)]
        return any(t is not exclude and self._queued_session_key(t) == session_key
                   for t in turns)

    def busy(self, key: Key) -> bool:
        return key in self.inflight or key in self.draining or bool(self.queues.get(key))

    def close(self) -> None:
        """Disconnect, step 1 (before dispatches are cancelled): stop all releases."""
        self.closing = True
        for queue in self.queues.values():
            for turn in queue:
                self._drop_grants(turn)  # dropped unstarted
        self.queues.clear()

    def reset(self) -> None:
        """Disconnect: cancel timers and forget all state (tasks are cancelled by the owner)."""
        for turn in self.inflight.values():
            turn.disarm()
        for handle in self._drain_timers.values():
            handle.cancel()
        self._drain_timers.clear()
        for _turn, handle in self._drain_deadlines.values():
            handle.cancel()
        self._drain_deadlines.clear()
        self.draining.clear()
        self.queues.clear()
        self.inflight.clear()
        self.last_outcome.clear()
        self.stop_message_ids.clear()
        self.closing = False  # ready for a reconnect

    # ------------------------------------------------------------ queue

    def enqueue(self, d: StartTurn, *, human: bool = False) -> None:
        key: Key = (d.chat_id, d.thread_id)
        text = QUEUED_ACK if self.busy(key) else d.ack
        # §8.1: the ack goes out before the hand-off. Spawned so a slow Linear never stalls
        # the event worker; _dispatch waits for it (bounded). A failed ack never blocks.
        ack = self._spawn(self._post_ack(d.agent_session_id, renderer.thought(text),
                                         ephemeral=True))
        mid = make_message_id(d.agent_session_id, d.activity_id)
        self.queues.setdefault(key, deque()).append(Turn(d, mid, human, ack=ack))
        self._release_next(key)

    def _release_next(self, key: Key) -> None:
        """Start the next queued turn for ``key`` if none is in flight (synchronous)."""
        if self.closing or key in self.inflight or key in self.draining:
            return
        queue = self.queues.get(key)
        if not queue:
            self.queues.pop(key, None)
            return
        turn = queue.popleft()
        if not queue:
            del self.queues[key]
        self.inflight[key] = turn
        turn.task = self._spawn(self._dispatch(key, turn))

    def _free(self, key: Key, turn: Turn) -> None:
        """Drop ``turn`` from in-flight (if it is still the current one); schedule the next."""
        if self.inflight.get(key) is not turn:
            return
        del self.inflight[key]
        turn.disarm()
        if not turn.handed_off:  # it never ran: an approval grant for it must not outlive it
            self._drop_grants(turn)
        asyncio.get_running_loop().call_soon(self._release_next, key)

    def _drop_grants(self, turn: Turn) -> None:
        """RET-94 §7: drop the approval grants written for ``turn`` (its answer turn's mid)
        when it is freed without running; ``outbound.turn_finished`` covers turns that ran."""
        store = self._store()
        if store is None:
            return
        try:
            store.drop_grants_for_turn(turn.mid)
        except Exception as exc:  # noqa: BLE001 - never wedge the FIFO
            log.warning("linear: dropping grants of %s failed: %s", turn.mid, type(exc).__name__)

    # ------------------------------------------------------------ dispatch

    async def _dispatch(self, key: Key, turn: Turn) -> None:
        try:
            await self._hand_off(key, turn)
        except asyncio.CancelledError:
            if not turn.handed_off:
                self._free(key, turn)  # stopped before hand-off (interrupt already freed it)
            raise
        except Exception as exc:  # noqa: BLE001 - a failed turn must never wedge the FIFO
            if turn.handed_off:
                log.error("linear: post-hand-off step failed for %s: %s", turn.mid,
                          type(exc).__name__)
                return
            log.error("linear: turn %s not started: %s", turn.mid, type(exc).__name__)
            log.debug("linear: turn start traceback", exc_info=True)
            self._free(key, turn)  # context stays unconsumed for the next turn
            self._spawn(self._post_ack(turn.decision.agent_session_id,
                                       renderer.error(TURN_FAILED_TEXT)))

    async def _hand_off(self, key: Key, turn: Turn) -> None:
        store = self._store()
        assert store is not None
        d = turn.decision
        if turn.ack is not None:
            await asyncio.wait({turn.ack}, timeout=ACK_TIMEOUT_S)  # ack before hand-off (§8.1)
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
        store.record_turn(turn.mid, d.agent_session_id, d.chat_id, d.thread_id)
        self.last_outcome.pop(key, None)
        source = self._build_source(chat_id=d.chat_id, chat_type="dm", user_id=d.user_id,
                                    user_name=d.user_name, thread_id=d.thread_id,
                                    message_id=turn.mid)
        event = MessageEvent(text=text, source=source, message_id=turn.mid, user_id=d.user_id,
                             user_name=d.user_name, allow_gateway_control=False)
        # The exact key the base derives for this event (``_event_session_key``), captured
        # before hand-off: tool hooks may fire before handle_message returns.
        turn.session_key = self._session_key_of(event)
        turn.handed_off = True  # from here on, interrupt synthesizes /stop
        try:
            await self._handle_message(event)
        except Exception:
            turn.handed_off = False  # the gateway refused it: not admitted
            raise
        # The base spawns the processing task (and registers it) before handle_message
        # returns, so "admitted" is checkable now: hook fired, or the base owns the session.
        if not turn.admitted and not self._base_owns(turn.session_key):
            self._not_admitted(key, turn)
            return
        if self.inflight.get(key) is turn:
            turn.safety = asyncio.get_running_loop().call_later(
                constants.TURN_SAFETY_TIMEOUT_S, self._safety_expired, key, turn)
        if items:
            store.consume_context(d.issue_id, turn.mid, up_to_id=watermark)
        if d.first_turn and ticket_session and turn.human:
            await self._auto_start(d, snap)

    def _not_admitted(self, key: Key, turn: Turn) -> None:
        (log.debug if self._unadmitted_logged else log.warning)(
            "linear: turn %s was not admitted by the gateway (no processing started); "
            "freeing its queue", turn.mid)
        self._unadmitted_logged = True
        self._free(key, turn)

    def _safety_expired(self, key: Key, turn: Turn) -> None:
        if self.inflight.get(key) is not turn:
            return
        log.error("linear: turn %s hit the safety timeout (%ss) without completing; "
                  "freeing its queue", turn.mid, constants.TURN_SAFETY_TIMEOUT_S)
        turn.safety = None
        try:
            self._on_safety_expired(turn)
        finally:
            self._drain(key, turn)

    # ------------------------------------------------------------ base hooks

    def find(self, mid: str | None) -> tuple[Key, Turn] | None:
        key = next((k for k, t in self.inflight.items() if t.mid == mid), None)
        return None if key is None else (key, self.inflight[key])

    def mark_admitted(self, mid: str | None) -> None:
        found = self.find(mid)
        if found is not None:
            found[1].admitted = True

    def record_outcome(self, found: tuple[Key, Turn] | None, outcome: ProcessingOutcome) -> None:
        if found is not None:
            self.last_outcome[found[0]] = outcome

    def complete(self, found: tuple[Key, Turn] | None) -> None:
        """Turn finished: release the next one once the calling (base) task has ended.

        The base fires ``on_processing_complete`` INSIDE its session guard (base.py ~4533;
        the guard drops in ``_finish_session_task`` ~4602). Releasing now would hit the busy
        path, which fires no hooks. Never inline either (Q1/Q5: the turn-error notice must
        reach ``send()`` first).
        """
        if found is None:
            return
        key, turn = found
        turn.finished = True
        self._drain(key, turn, after_current_task=True)

    def _drain(self, key: Key, turn: Turn, *, after_current_task: bool = False) -> None:
        """Drop ``turn`` from in-flight and release the next one only once the base no
        longer holds the session guard (bounded retries, then ``DRAIN_DEADLINE_S``).

        Used by normal completion (after the calling base task ends) and by the stop-settle
        and safety-timeout nets, where the gateway may still own the session.
        """
        if self.inflight.get(key) is not turn:
            return
        del self.inflight[key]
        turn.disarm()
        self.draining.add(key)
        loop = asyncio.get_running_loop()
        self._drain_deadlines[key] = (turn, loop.call_later(
            DRAIN_DEADLINE_S, self._drain_deadline_hit, key, turn))
        task = asyncio.current_task() if after_current_task else None
        if task is None:
            loop.call_soon(self._release_after_drain, key, turn, 0)
        else:
            task.add_done_callback(lambda _t: loop.call_soon(
                self._release_after_drain, key, turn, 0))

    def _release_after_drain(self, key: Key, turn: Turn, tries: int) -> None:
        entry = self._drain_deadlines.get(key)
        if key not in self.draining or entry is None or entry[0] is not turn:
            return  # disconnected, or already released by the drain deadline
        self._drain_timers.pop(key, None)
        session_key = turn.session_key
        if session_key and self._base_busy(session_key):
            if tries < RELEASE_MAX_TRIES:
                self._drain_timers[key] = asyncio.get_running_loop().call_later(
                    RELEASE_RETRY_S, self._release_after_drain, key, turn, tries + 1)
                return
            log.warning("linear: session %s still busy in the gateway after %.0fs; releasing "
                        "the next turn anyway", session_key, RELEASE_RETRY_S * RELEASE_MAX_TRIES)
        self._end_drain(key)
        self._release_next(key)

    def _end_drain(self, key: Key) -> None:
        self.draining.discard(key)
        entry = self._drain_deadlines.pop(key, None)
        if entry is not None:
            entry[1].cancel()
        handle = self._drain_timers.pop(key, None)
        if handle is not None:
            handle.cancel()
        try:
            self._on_drained()
        except Exception as exc:  # noqa: BLE001 - a sweep must never wedge the FIFO
            log.error("linear: post-drain sweep failed: %s", type(exc).__name__)

    def _drain_deadline_hit(self, key: Key, turn: Turn) -> None:
        entry = self._drain_deadlines.get(key)
        if entry is None or entry[0] is not turn or key not in self.draining:
            return
        log.warning("linear: turn %s still draining %.0fs after completion (the gateway's "
                    "processing task has not ended); releasing the next turn", turn.mid,
                    DRAIN_DEADLINE_S)
        self._end_drain(key)
        self._release_next(key)

    # ------------------------------------------------------------ stop (Q6)

    def interrupt(self, agent_session_id: str) -> None:
        """Drop the session's queued turns; stop its in-flight turn. Never awaits Linear."""
        for key in list(self.queues):
            queue = self.queues[key]
            for t in queue:
                if t.decision.agent_session_id == agent_session_id:
                    self._drop_grants(t)  # dropped unstarted
            keep = deque(t for t in queue if t.decision.agent_session_id != agent_session_id)
            if keep:
                self.queues[key] = keep
            else:
                del self.queues[key]
        gateway_stop = False
        for key, turn in list(self.inflight.items()):
            if turn.decision.agent_session_id != agent_session_id:
                continue
            if turn.handed_off:
                self._spawn(self._send_stop(key, turn))  # the gateway posts the stop notice
                gateway_stop = True
            else:  # still preparing: cancel it here, nothing reached the gateway
                if turn.task is not None:
                    turn.task.cancel()
                self._free(key, turn)
        # Design decision: a stop in thread B while thread A's turn runs in the shared ticket
        # session only drops B's queued turns. Otherwise Linear would hang in "stopping".
        if not gateway_stop:
            self._spawn(self._post_response(agent_session_id, renderer.response(STOPPED_TEXT),
                                            timeout_s=ACK_TIMEOUT_S))

    async def _send_stop(self, key: Key, turn: Turn) -> None:
        d = turn.decision
        mid = f"{d.agent_session_id}:stop:{uuid.uuid4().hex[:12]}"
        self.stop_message_ids[mid] = d.agent_session_id
        if turn.stop_settle is None and self.inflight.get(key) is turn:
            turn.stop_settle = asyncio.get_running_loop().call_later(
                constants.STOP_SETTLE_S, self._stop_settle_expired, key, turn)
        source = self._build_source(chat_id=d.chat_id, chat_type="dm", user_id=d.user_id,
                                    user_name=d.user_name, thread_id=d.thread_id, message_id=mid)
        stop = MessageEvent(text="/stop", source=source, message_id=mid, user_id=d.user_id,
                            user_name=d.user_name, allow_gateway_control=True)
        try:
            await self._handle_message(stop)
        except Exception as exc:  # noqa: BLE001
            log.error("linear: stop for %s failed: %s", d.agent_session_id, type(exc).__name__)

    def _stop_settle_expired(self, key: Key, turn: Turn) -> None:
        turn.stop_settle = None
        if self.inflight.get(key) is not turn:
            return
        log.warning("linear: stopped turn %s not completed by the gateway %ss after /stop; "
                    "answering it and freeing its queue", turn.mid, constants.STOP_SETTLE_S)
        try:
            self._on_stop_unsettled(turn)
        finally:
            self._drain(key, turn)


__all__ = ["Key", "QUEUED_ACK", "STOPPED_TEXT", "Turn", "TurnEngine", "human_creator",
           "snapshot_block"]
