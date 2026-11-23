"""Outbound path: Hermes -> Linear activities (spec §8.2, §8.4, §8.5; integration notes Q1, Q6,
Q8, Q10).

* ``send``: the Q1 send contract. Target resolution: ``reply_to`` -> metadata
  ``reply_to_message_id`` -> ``store.latest_turn_for_chat(chat_id, thread_id)`` (exact thread)
  -> drop + log. Classification: ``_interim_send`` / ``expect_edits`` -> ``thought``; the
  turn-error notice -> ``error``; an anchorless send with no turn in flight -> ``thought``;
  anything else -> ``response``. For a turn that already answered (Ruling 33): content
  addressed to it by ``reply_to`` / ``reply_to_message_id`` (attachment fallback, late final)
  is another ``response``; a status/interim notice is dropped unless another turn of the
  agent session is in flight. A cancelled caller never unqueues a response write (shielded).
* Media (Ruling 35): ``media_text`` renders attachments as markdown; a remote http(s) URL is
  linked/embedded, a local file is named by its basename only (never uploaded, never its path).
  The base sends every attachment of a final reply with ``notify`` metadata and no reply_to.
* Stop (Q6): the gateway's ``/stop`` acknowledgement is recognised by ``reply_to`` and never
  forwarded; a stopped turn with no response gets ``Stopped.`` as its ``response`` when it
  completes (CANCELLED or FAILURE), or after ``STOP_SETTLE_S`` if the gateway never completes it.
* No activity after the response: Linear derives the session state from the last activity, so
  a thought or tool action for a turn that already answered (background review, a tool
  interrupted by ``/stop``, …) is dropped once nothing is in flight for that session.
* Typing / heartbeat: spawned ephemeral thoughts, bounded by ``ACK_TIMEOUT_S``; never awaited by
  the caller.
* Approvals / clarify: ``elicitation`` (+ ``select``) and a ``pending_controls`` row. When
  Hermes's wait ends unanswered (``retire_clarify``, the approval-timeout ``edit_message``) the
  live row becomes *pending* (RET-94): nothing is posted and ``Blocked`` is added to the
  target issue. A plugin-issued stop for the session or a stale generation keeps the expiry:
  a fixed ``thought`` and the row dropped (Q10). A new elicitation, once posted, drops the
  session's pending rows and removes their ``Blocked`` (a failed post leaves them standing).
* Pending elicitation: Linear shows a select input only while the elicitation is the agent
  session's latest activity. While a control this adapter elicited is pending (row present,
  its turn in flight), ``_post`` drops every ``thought`` / ``action`` for that session
  (typing, heartbeat, interim sends, tool actions). Elicitations, errors and responses still
  post; the expiry thought posts after the row is dropped. Every response of the turn (the
  first, sent or promoted at turn end, and each ``more`` one: attachments, a late final) would
  land after a *pending* question and hide its select, so the question's select is queued
  again right after each (same row, ``_repost_pending``) under a one-line pointer body: the
  original elicitation stays in the thread, so re-sending its text would duplicate it.
* Tool events from ``hooks.py`` (already on the loop): start/finish actions with the fast-tool
  skip, todo -> plan, and the session link.

Writers: ``responses`` go through ``adapter._writer`` (its failure marks the adapter degraded,
§8.6); everything else goes through ``adapter._activity_writer`` and is only logged on failure.
All writes for one agent session go through one ordered queue (``_SessionWrites``), in the order
they were issued, so an action issued before the response is never written after it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from collections import deque
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional, Sequence
from urllib.parse import unquote, urlparse

from gateway.platforms.base import SendResult
from gateway.platforms.event import ProcessingOutcome

from . import questions, renderer
from . import sessions_open as sessions_open_mod  # grant path, stubbable in tests
from .constants import ACK_TIMEOUT_S, FAST_TOOL_SKIP_S, HEARTBEAT_S, TYPING_MIN_INTERVAL_S
from .router import _session_key as router_session_key  # the chat id a session routes to
from .turns import STOPPED_TEXT, Key, Turn

if TYPE_CHECKING:  # pragma: no cover
    from .adapter import LinearAdapter

log = logging.getLogger(__name__)

NO_SESSION = "no linear session"
WORKING_TEXT = "Working…"
HEARTBEAT_TEXT = "Still working…"
EXPIRED_TEXT = "This request expired."
APPROVAL_EXPIRED_TEXT = "This request expired. The command was not run."
TIMED_OUT_TEXT = "This turn timed out."
# RET-116: the body of a re-posted pending question. Linear keeps the original elicitation
# in the thread as a comment; only its select stops rendering once another activity follows.
REPOST_CLARIFY_TEXT = "Still waiting for your answer to the question above."
REPOST_APPROVAL_TEXT = "Still waiting for your approval of the command above."
SESSION_LINK_LABEL = "Hermes session"
_NOTICE_KEYS = frozenset({"thread_id", "hermes_profile"})  # turn-error notice metadata (Q1)
_APPROVAL_CHOICES = ("once", "session", "deny")  # spec §8.4 omits "always" (Q8)
_TODO_TOOLS = frozenset({"todo", "todo_list"})  # tools/todo_tool.py: name + legacy alias
_REMEMBER = 1024  # bound of every per-turn / per-session memory below (LRU)
__all__ = ["Outbound", "Target", "media_text"]


@dataclass(frozen=True)
class Target:
    sid: str  # Linear agent session id
    mid: str  # the turn's message id
    key: Key  # Hermes session (chat_id, thread_id)
    via: str  # "reply_to" | "reply_to_message_id" | "chat"


class _Bounded(OrderedDict):
    """An LRU-capped dict (``put``) / set (``add``) of at most ``_REMEMBER`` entries."""

    def put(self, key: str, value: Any) -> None:
        self[key] = value
        self.move_to_end(key)
        while len(self) > _REMEMBER:
            self.popitem(last=False)

    def add(self, item: str) -> None:
        self.put(item, None)


class _SessionWrites:
    """Per agent session, run writes one at a time in the order they were submitted.

    ``submit`` is synchronous: the write's place in line is fixed when it is issued, not when a
    spawned task first runs. Returns a future with the write's bool result.
    """

    def __init__(self, spawn: Callable[[Awaitable[Any]], asyncio.Task]) -> None:
        self._spawn = spawn
        self.queues: dict[str, deque] = {}

    def submit(self, sid: str, factory: Callable[[], Awaitable[bool]]) -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        queue = self.queues.get(sid)
        if queue is None:
            queue = self.queues[sid] = deque([(factory, fut)])
            self._spawn(self._drain(sid, queue))
        else:
            queue.append((factory, fut))
        return fut

    async def _drain(self, sid: str, queue: deque) -> None:
        try:
            while queue:
                factory, fut = queue[0]
                if not fut.done():  # the caller may have been cancelled
                    try:
                        ok = bool(await factory())
                    except Exception as exc:  # noqa: BLE001 - writers never raise; be safe
                        log.debug("linear: write failed for %s: %s", sid, type(exc).__name__)
                        ok = False
                    if not fut.done():
                        fut.set_result(ok)
                queue.popleft()
        finally:
            _fail(queue)
            if self.queues.get(sid) is queue:
                del self.queues[sid]

    def reset(self) -> None:
        for queue in self.queues.values():
            _fail(queue)
        self.queues.clear()


def _fail(queue: deque) -> None:
    while queue:
        _factory, fut = queue.popleft()
        if not fut.done():
            fut.set_result(False)


def _done(value: bool) -> asyncio.Future:
    fut = asyncio.get_running_loop().create_future()
    fut.set_result(value)
    return fut


def _md_label(text: str) -> str:
    return " ".join(str(text).split()).replace("[", "\\[").replace("]", "\\]")


def _md_url(url: str) -> str:
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def media_text(kind: str, ref: str, caption: Optional[str] = None,
               file_name: Optional[str] = None) -> str:
    """Markdown for an attachment (Ruling 35). ``kind`` is ``"image"`` or ``"file"``.

    Remote http(s): an image is embedded (``![alt](url)``, caption on its own line), a file is a
    link (``[caption or name](url)``). Anything else is a local file Linear never receives: only
    its basename is shown, never the host path."""
    ref = str(ref or "")
    parsed = urlparse(ref)
    remote = parsed.scheme in ("http", "https") and bool(parsed.netloc)
    path = unquote(parsed.path) if (remote or parsed.scheme == "file") else ref
    name = os.path.basename(str(file_name or "").rstrip("/\\")) or os.path.basename(
        path.rstrip("/\\")) or kind
    caption = str(caption).strip() if caption else ""
    if remote and kind == "image":
        text = f"![{_md_label(caption or 'image')}]({_md_url(ref)})"
    elif remote:
        return f"[{_md_label(caption or name)}]({_md_url(ref)})"
    else:
        label = "image" if kind == "image" else "file"
        text = f"Attached {label}: {name} (not uploaded to Linear)"
    return f"{text}\n\n{caption}" if caption else text


def _generation(chat_id: str) -> int:
    tail = str(chat_id).rpartition(":")[2]
    return int(tail) if tail.isdigit() else 0


class Outbound:
    def __init__(self, adapter: "LinearAdapter") -> None:
        self.a = adapter
        self.clock = time.monotonic  # typing rate limit (tests inject a fake clock)
        self.sleep = asyncio.sleep  # heartbeat interval (tests inject a fake)
        self.responded = _Bounded()  # turn mids that already have their response
        self.last_thought = _Bounded()  # turn mid -> last commentary thought (turn-end fallback)
        self.typing_at = _Bounded()  # agent session -> last activity issued (typing fills gaps)
        self.heartbeats: dict[str, asyncio.Task] = {}  # turn mid -> heartbeat task
        self.pending_starts: dict[tuple, asyncio.TimerHandle] = {}  # fast-tool skip timers
        self.url_set = _Bounded()  # agent sessions whose link was set
        self.retired_stops = _Bounded()  # synthesized stop ids already handled
        self.stop_skipped = _Bounded()  # stopped turn mids whose Stopped. a response replaced
        self.writes = _SessionWrites(lambda coro: self.a._spawn(coro))
        self.elicited: dict[str, dict[str, str]] = {}  # agent session -> {request id: turn mid}

    def reset(self) -> None:
        """Disconnect: cancel timers and forget state (tasks are cancelled by the adapter)."""
        for handle in self.pending_starts.values():
            handle.cancel()
        self.pending_starts.clear()
        self.heartbeats.clear()
        self.last_thought.clear()
        self.typing_at.clear()
        self.elicited.clear()
        self.writes.reset()

    # ------------------------------------------------------------ writers

    # Synchronous: the write is queued (in order) when called; await the future for the result.

    def _post(self, sid: str, content: dict, **kw) -> asyncio.Future:
        writer = self.a._activity_writer
        if writer is None:
            return _done(False)
        if self._suppressed(sid, content):
            log.debug("linear: %s for session %s dropped (elicitation pending)",
                      content.get("type"), sid)
            return _done(True)
        self.typing_at.put(sid, self.clock())
        return self.writes.submit(sid, lambda: writer.post(sid, content, **kw))

    def _suppressed(self, sid: str, content: dict) -> bool:
        """True while an elicitation of ``sid`` is pending: a thought or action posted now
        would supersede it and hide Linear's select input (on the loop: store access)."""
        return content.get("type") in ("thought", "action") and bool(self._live_controls(sid))

    def _live_controls(self, sid: str) -> dict[str, str]:
        """The controls elicited for ``sid`` that are still pending (row present in either
        state, ``live`` or ``pending``: not answered, expired or dropped) for a turn still in
        flight; forgets the rest."""
        pending = self.elicited.get(sid)
        if not pending:
            return {}
        store = self.a._store
        for rid, mid in list(pending.items()):
            if store is None or store.control(rid) is None or self.a.turns.find(mid) is None:
                del pending[rid]
        if not pending:
            del self.elicited[sid]
        return pending

    def _respond(self, sid: str, content: dict, **kw) -> asyncio.Future:
        self.typing_at.put(sid, self.clock())
        return self.writes.submit(sid, lambda: self.a._post_response(sid, content, **kw))

    def _other_turn_busy(self, sid: str, mid: str) -> bool:
        """True while a turn of agent session ``sid`` other than ``mid`` is in flight."""
        return any(t.decision.agent_session_id == sid and t.mid != mid
                   for t in self.a.turns.inflight.values())

    def _answered(self, mid: str) -> bool:
        """The turn already has its response, or is no longer in flight (tool events)."""
        return mid in self.responded or self.a.turns.find(mid) is None

    # ------------------------------------------------------------ resolution (Q1)

    def resolve(self, chat_id: str, reply_to: Optional[str], md: dict) -> Target | None:
        store = self.a._store
        if store is None:
            return None
        for via, mid in (("reply_to", reply_to), ("reply_to_message_id",
                                                   md.get("reply_to_message_id"))):
            if mid:
                row = store.turn(str(mid))
                if row is not None:
                    return Target(row.agent_session_id, row.message_id,
                                  (row.hermes_chat_id, row.thread_id), via)
        row = store.latest_turn_for_chat(str(chat_id), md.get("thread_id"))
        if row is None:
            return None
        return Target(row.agent_session_id, row.message_id, (row.hermes_chat_id, row.thread_id),
                      "chat")

    def _classify(self, t: Target, reply_to: Optional[str], md: dict) -> str:
        if md.get("_interim_send"):
            return "thought"
        if md.get("expect_edits"):
            log.warning("linear: streamed preview (expect_edits) posted as a thought; text "
                        "streaming is not supported on Linear")
            return "thought"
        # Ruling 35: ``notify`` marks the base's user-visible final content (final text and
        # every attachment of it, which never carry reply_to); it is anchored like reply_to.
        # Heartbeat / stuck-warning (interim, above) and the turn-error notice never carry it.
        anchored = t.via != "chat" or bool(md.get("notify"))
        if not anchored and self.a.turns.current_turn_for(*t.key) is None:
            # T1 deferral: the first anchorless send after a turn consumes its outcome.
            outcome = self.a.turns.last_outcome.pop(t.key, None)
            if (reply_to is None and set(md) <= _NOTICE_KEYS
                    and outcome is ProcessingOutcome.FAILURE):
                return "error"  # _notify_turn_error, right after on_processing_complete(FAILURE)
            log.info("linear: unmarked send with no turn in flight posted as a thought "
                     "(session %s)", t.sid)
            return "thought"
        if t.mid in self.responded:
            if not anchored:
                return "thought"  # a status notice: dropped by send() (Ruling 33)
            # Rulings 33/35: real content for the answered turn (attachments of the final,
            # a late final) is an additional response, never a thought after it.
            log.info("linear: turn %s already has its response; posting another", t.mid)
            return "more"
        return "response"

    # ------------------------------------------------------------ send

    async def send(self, chat_id: str, content: str, reply_to: Optional[str],
                   metadata: Optional[dict[str, Any]]) -> SendResult:
        md = dict(metadata or {})
        if reply_to and self._is_stop_ack(str(reply_to)):
            return SendResult(success=True, message_id=str(reply_to))  # Q6: never forwarded
        # Everything up to the post is synchronous: in-flight state is snapshotted at entry (Q1).
        target = self.resolve(chat_id, reply_to, md)
        if target is None:
            log.warning("linear: send dropped, no linear session for chat %s (thread %s)",
                        chat_id, md.get("thread_id"))
            return SendResult(success=False, error=NO_SESSION)
        kind = self._classify(target, reply_to, md)
        body = str(content or "")
        if not body.strip():
            return SendResult(success=True, message_id=f"{target.sid}:empty")
        if (kind == "thought" and target.mid in self.responded
                and not self._other_turn_busy(target.sid, target.mid)):
            # I1: a status/interim thought after the response would flip the Linear session
            # back to active, even while the answered turn itself is still finishing.
            log.debug("linear: thought for answered turn %s dropped (no other turn of "
                      "session %s in flight)", target.mid, target.sid)
            return SendResult(success=True, message_id=f"{target.sid}:dropped:{target.mid}")
        if kind in ("response", "more"):
            first = kind == "response"
            if first:
                self.responded.add(target.mid)
            fut = self._shielded_response(target, renderer.response(body), first)
            # Every response of the turn (attachments, a late final): queued now, so the
            # pending question lands right after it and stays the last activity.
            self._repost_pending(target.sid)
            ok = await fut
            kind = "response"
        else:
            if kind == "thought" and target.via == "reply_to_message_id":
                # Only commentary carries reply_to_message_id; gateway status notices
                # (heartbeat, stuck warning) never become the turn-end fallback (I3).
                self.last_thought.put(target.mid, body)
            builder = renderer.error if kind == "error" else renderer.thought
            ok = await self._post(target.sid, builder(body))
        if not ok:
            return SendResult(success=False, error="linear write failed")
        return SendResult(success=True, message_id=f"{target.sid}:{kind}:{target.mid}")

    def _shielded_response(self, t: Target, content: dict, first: bool) -> Awaitable[bool]:
        """Queue a response (synchronously); a cancelled caller (``/stop`` during the final
        send) never unqueues it. If the turn's first response fails, the turn is unanswered
        again."""
        fut = self._respond(t.sid, content)
        if first:
            fut.add_done_callback(lambda f: self._response_done(f, t))
        return asyncio.shield(fut)

    def _response_done(self, fut: asyncio.Future, t: Target) -> None:
        if not fut.cancelled() and fut.result():
            return
        self.responded.pop(t.mid, None)
        if t.mid in self.stop_skipped:  # the stop reply was skipped for this write
            del self.stop_skipped[t.mid]
            self._answer_stopped(t.mid, t.sid)

    # ------------------------------------------------------------ stop (Q6)

    def _is_stop_ack(self, reply_to: str) -> bool:
        stops = self.a.turns.stop_message_ids
        if reply_to in stops:
            sid = stops[reply_to]
            if not any(t.decision.agent_session_id == sid for t in self.a.turns.inflight.values()):
                del stops[reply_to]  # nothing left to cancel ("No active task to stop.")
                self.retired_stops.add(reply_to)
            return True
        return reply_to in self.retired_stops

    def _retire_stops(self, sid: str) -> list[str]:
        stops = self.a.turns.stop_message_ids
        stop_ids = [m for m, s in stops.items() if s == sid]
        for m in stop_ids:
            del stops[m]
            self.retired_stops.add(m)
        return stop_ids

    def _answer_stopped(self, mid: str, sid: str) -> None:
        if mid in self.responded:
            self.stop_skipped.add(mid)  # answered, unless that response write fails
            return
        self.responded.add(mid)
        self._respond(sid, renderer.response(STOPPED_TEXT), timeout_s=ACK_TIMEOUT_S)

    def turn_finished(self, found: tuple[Key, Turn] | None, outcome: ProcessingOutcome) -> None:
        """``on_processing_complete`` (sync): stop reply, turn-end fallback, heartbeat."""
        if found is None:
            return
        key, turn = found
        mid, sid = turn.mid, turn.decision.agent_session_id
        if self.a._store is not None:
            self.a._store.drop_grants_for_turn(mid)  # §7: only this turn's own grants
        self.stop_heartbeat(mid)
        self._live_controls(sid)  # forget controls this (now finished) turn abandoned
        stop_ids = self._retire_stops(sid)
        last = self.last_thought.pop(mid, None)
        if stop_ids:
            # I2: a stopped turn is always answered, whatever the outcome; a stop-induced
            # FAILURE must not turn the next anchorless send into an error activity.
            self.a.turns.last_outcome.pop(key, None)
            self._answer_stopped(mid, sid)
            return
        if mid in self.responded:
            return
        if outcome is ProcessingOutcome.SUCCESS and last:
            # Q1 caveat: the final send was deduplicated against commentary; promote it.
            self.responded.add(mid)
            self._respond(sid, renderer.response(last))
            self._repost_pending(sid)

    def stop_unsettled(self, turn: Turn) -> None:
        """Q6 safety net: the gateway never completed a stopped turn within STOP_SETTLE_S."""
        mid, sid = turn.mid, turn.decision.agent_session_id
        self.stop_heartbeat(mid)
        self._retire_stops(sid)
        self.last_thought.pop(mid, None)
        self._answer_stopped(mid, sid)

    def safety_expired(self, turn: Turn) -> None:
        """TURN_SAFETY_TIMEOUT_S hit: the turn ends with an ``error`` unless it answered."""
        mid, sid = turn.mid, turn.decision.agent_session_id
        self.stop_heartbeat(mid)
        self._retire_stops(sid)
        self.last_thought.pop(mid, None)
        if mid not in self.responded:
            self._post(sid, renderer.error(TIMED_OUT_TEXT), timeout_s=ACK_TIMEOUT_S)

    # ------------------------------------------------------------ typing / heartbeat

    def typing(self, chat_id: str, metadata: Optional[dict]) -> None:
        turn = self.a.turns.current_turn_for(str(chat_id), (metadata or {}).get("thread_id"))
        if turn is None or turn.mid in self.responded:
            return
        sid, now = turn.decision.agent_session_id, self.clock()
        last = self.typing_at.get(sid)
        if last is not None and now - last < TYPING_MIN_INTERVAL_S:
            return
        self._post(sid, renderer.thought(WORKING_TEXT), ephemeral=True, timeout_s=ACK_TIMEOUT_S)

    def start_heartbeat(self, mid: str | None) -> None:
        found = self.a.turns.find(mid)
        if found is None or found[1].mid in self.heartbeats:
            return
        turn = found[1]
        self.heartbeats[turn.mid] = self.a._spawn(
            self._heartbeat(turn.mid, turn.decision.agent_session_id))

    def stop_heartbeat(self, mid: str) -> None:
        task = self.heartbeats.pop(mid, None)
        if task is not None:
            task.cancel()

    async def _heartbeat(self, mid: str, sid: str) -> None:
        try:
            while True:
                await self.sleep(HEARTBEAT_S)
                if self._answered(mid):  # answered, completed, stopped or timed out
                    return
                await self._post(sid, renderer.thought(HEARTBEAT_TEXT), ephemeral=True,
                                 timeout_s=ACK_TIMEOUT_S)
        finally:
            if self.heartbeats.get(mid) is asyncio.current_task():
                del self.heartbeats[mid]

    # ------------------------------------------------------------ approvals / clarify

    def _put_control(self, rid: str, kind: str, t: Target, choices: list, session_key: str,
                     *, question: str, command: Optional[str] = None,
                     authority: Optional[dict] = None):
        store = self.a._store
        assert store is not None
        store.put_control(rid, kind, t.sid, _generation(t.key[0]), choices,
                          session_key=session_key, question=question, command=command,
                          turn_mid=t.mid, authority=authority)
        self.elicited.setdefault(t.sid, {})[rid] = t.mid  # gates _post until resolved

    def _elicit(self, sid: str, kind: str, question: str, choices: Sequence[str]
                ) -> asyncio.Future:
        """Queue the elicitation of a control (the original post and the re-post): approvals
        offer ``renderer.approval_options()`` narrowed to ``choices``; a clarify offers its
        choices, and no select at all when it has none."""
        if kind == "approval":
            options = [o for o in renderer.approval_options() if o["value"] in choices]
        else:
            options = [{"label": v, "value": v} for v in choices]
        kw: dict[str, Any] = {}
        if kind == "approval" or options:
            kw = {"signal": "select", "signal_metadata": {"options": options}}
        return self._post(sid, renderer.elicitation(question), timeout_s=ACK_TIMEOUT_S, **kw)

    def _repost_pending(self, sid: str) -> None:
        """Spec §5: right after each response of the turn is queued, queue the session's
        pending question again (same row and request id) so it is the last activity and
        Linear keeps showing its select. The re-post carries the same select but a one-line
        pointer body: the original elicitation stays in the thread (RET-116). A live row is
        never re-posted. Never raises: the response is already queued, and a raised ``send``
        would invite a second final."""
        try:
            store = self.a._store
            row = store.control_for_session(sid) if store is not None else None
            if row is None or row.state != "pending":
                return
            log.info("linear: re-posting pending %s %s after the response", row.kind,
                     row.request_id)
            body = REPOST_APPROVAL_TEXT if row.kind == "approval" else REPOST_CLARIFY_TEXT
            self._elicit(sid, row.kind, body, row.choices)
        except Exception as exc:  # noqa: BLE001 - store error; the response stands
            log.warning("linear: re-posting the pending question of %s failed: %s", sid,
                        type(exc).__name__)

    def _replace_pending(self, sid: str, rid: str) -> None:
        """A new question ``rid`` of ``sid`` was posted: it replaces the session's pending
        ones. Called only after a successful post (a failed one leaves them standing). Their
        rows are dropped and their ``Blocked`` removed; the new question adds it again if it
        goes pending (final review 3)."""
        store = self.a._store
        session = store.session(sid) if store is not None else None
        if session is None:
            return
        rows = [r for r in store.controls_for_issue(session.issue_id)
                if r.agent_session_id == sid and r.state == "pending" and r.request_id != rid]
        for row in rows:
            for pending in self.elicited.values():
                pending.pop(row.request_id, None)
        if rows:
            self.a.opener.drop_pending(rows)

    def _drop_control(self, rid: str) -> None:
        for pending in self.elicited.values():
            pending.pop(rid, None)
        if self.a._store is not None:
            self.a._store.drop_control(rid)

    async def approval_prompt(self, prompt: Any) -> SendResult:
        target = self.resolve(prompt.chat_id, None, dict(prompt.metadata or {}))
        if target is None or not prompt.request_id:
            log.warning("linear: approval prompt dropped (no linear session or request id)")
            return SendResult(success=False, error=NO_SESSION)
        choices = [c for c in _APPROVAL_CHOICES if c in prompt.choices]  # Q8: keep narrowing
        rid = str(prompt.request_id)
        if self._use_grant(prompt, rid):
            return SendResult(success=True, message_id=f"approval:{rid}")  # no row, no post
        # Who may answer, read now while Hermes still holds the entry (it is queued before
        # notify): a pending answer comes after Hermes timed it out and forgot it.
        authority = sessions_open_mod._approval_authority(str(prompt.session_key), rid)
        self._put_control(rid, "approval", target, choices, prompt.session_key,
                          question=prompt.text, command=prompt.command, authority=authority)
        ok = await self._elicit(target.sid, "approval", prompt.text, choices)
        if not ok:
            self._drop_control(rid)
            return SendResult(success=False, error="linear write failed")
        self._replace_pending(target.sid, rid)
        return SendResult(success=True, message_id=f"approval:{rid}")

    def _use_grant(self, prompt: Any, rid: str) -> bool:
        """Spec §7: the re-run of a command approved while pending resolves at once with the
        stored one-use grant. Exact ``(session_key, command)`` match only. A grant whose choice
        the card does not offer (Deny-only mark kept) stays unused; a refused or failed
        resolve falls back to the normal prompt (the grant is spent)."""
        store = self.a._store
        if store is None or not prompt.command:
            return False
        key, command = str(prompt.session_key), str(prompt.command)
        grant = store.take_grant(key, command, offered=list(prompt.choices))
        if grant is None:
            return False
        choice, actor = grant
        if sessions_open_mod._approval_refusal(key, actor, request_id=rid) is not None:
            log.info("linear: approval grant for %s refused, asking again", rid)
            return False
        try:
            resolved = sessions_open_mod._resolve_approval_card(key, choice, rid, actor_id=actor)
        except Exception:
            log.exception("linear: approval grant for %s failed, asking again", rid)
            return False
        if not resolved:
            log.info("linear: approval grant for %s not accepted, asking again", rid)
            return False
        return True

    async def clarify(self, chat_id: str, question: str, choices: Optional[list], clarify_id: str,
                      session_key: str, metadata: Optional[dict]) -> SendResult:
        target = self.resolve(chat_id, None, dict(metadata or {}))
        if target is None:
            log.warning("linear: clarify %s dropped, no linear session", clarify_id)
            return SendResult(success=False, error=NO_SESSION)
        values = [str(c) for c in (choices or [])]
        self._put_control(str(clarify_id), "clarify", target, values, session_key,
                          question=str(question))
        ok = await self._elicit(target.sid, "clarify", str(question), values)
        if not ok:
            self._drop_control(str(clarify_id))
            return SendResult(success=False, error="linear write failed")
        self._replace_pending(target.sid, str(clarify_id))
        return SendResult(success=True, message_id=f"clarify:{clarify_id}")

    async def _expire(self, rid: str, kind: str, text: str) -> bool:
        store = self.a._store
        row = store.control(rid) if store is not None else None
        if row is None or row.kind != kind or row.state != "live":
            return False  # already answered, dropped or pending
        self._drop_control(rid)
        await self._post(row.agent_session_id, renderer.thought(text), timeout_s=ACK_TIMEOUT_S)
        return True

    async def edit_message(self, message_id: str) -> SendResult:
        """Q10: only the approval-timeout edit; its body is never echoed."""
        if not str(message_id).startswith("approval:"):
            return SendResult(success=False, error="Not supported")
        rid = str(message_id).split(":", 1)[1]
        if not await self.defer(rid, "approval"):
            await self._expire(rid, "approval", APPROVAL_EXPIRED_TEXT)
        return SendResult(success=True, message_id=message_id)

    async def retire_clarify(self, clarify_id: str) -> None:
        if not await self.defer(str(clarify_id), "clarify"):
            await self._expire(str(clarify_id), "clarify", EXPIRED_TEXT)

    def _cancelled(self, row: Any) -> bool:
        """The wait ended because the plugin stopped the session, or the row is stale: its
        generation is not the one the session's chat id carries now (re-delegation, or a
        mention thread now routed to the delegated ticket). Such a question must not stay
        pending."""
        if row.agent_session_id in self.a.turns.stop_message_ids.values():
            return True
        store = self.a._store
        session = store.session(row.agent_session_id) if store is not None else None
        ticket = store.ticket(session.issue_id) if session is not None else None
        if ticket is None:
            return False
        # The generation the session's chat id carries now (a mention thread's: none, 0).
        current = _generation(router_session_key(session, ticket)[0])
        return row.execution_generation != current

    async def defer(self, rid: str, kind: str) -> bool:
        """Hermes's wait for ``rid`` ended unanswered: a live, not cancelled row becomes
        pending (nothing posted) and ``Blocked`` is spawned onto its target. True if deferred."""
        store = self.a._store
        row = store.control(rid) if store is not None else None
        if row is None or row.kind != kind or row.state != "live" or self._cancelled(row):
            return False
        ticket = questions.blocked_target(store, row)
        if not store.mark_control_pending(rid, ticket):
            return False
        client = self.a._client
        if ticket is not None and client is not None:
            self.a._spawn(questions.set_blocked(client, ticket, True))
        log.info("linear: %s %s pending (target %s)", kind, rid, ticket)
        return True

    # ------------------------------------------------------------ tool events (hooks.py)

    def tool_target(self, env: dict, *, by_message: bool) -> Target | None:
        """Adapter selection for a hook event (runs on the loop)."""
        store = self.a._store
        if store is None:
            return None
        if by_message:
            mid = env.get("message_id")
            row = store.turn(mid) if mid else None
        else:
            chat = env.get("chat_id")
            row = store.latest_turn_for_chat(chat, env.get("thread_id")) if chat else None
        if row is None:
            return None
        found = self.a.turns.find(row.message_id)
        session_key = env.get("session_key")
        if (session_key and found is not None and found[1].session_key
                and found[1].session_key != session_key):
            return None  # in flight under another gateway session (another profile's turn)
        return Target(row.agent_session_id, row.message_id, (row.hermes_chat_id, row.thread_id),
                      "hook")

    def tool_target_for_key(self, session_key: str) -> Target | None:
        """The in-flight turn the base keyed as ``session_key`` (hooks.py, on the loop)."""
        found = self.a.turns.turn_for_session_key(session_key)
        if found is None:
            return None
        key, turn = found
        return Target(turn.decision.agent_session_id, turn.mid, key, "hook")

    def tool_event(self, phase: str, t: Target, ev: dict) -> None:
        """One pre/post tool call for agent session ``t.sid`` (sync, on the loop)."""
        name = str(ev.get("tool_name") or "tool")
        ident = (ev.get("task_id"), ev.get("tool_call_id") or name)
        if self._answered(t.mid):
            # I1: background-review forks, a tool interrupted by /stop, … — an action now
            # would flip the answered Linear session back to active.
            pending = self.pending_starts.pop(ident, None)
            if pending is not None:
                pending.cancel()
            log.debug("linear: tool event %s for answered turn %s dropped", name, t.mid)
            return
        self._session_link(t.sid, ev.get("session_id"))
        if name in _TODO_TOOLS:
            if phase == "finish":
                plan = renderer.plan_from_todos(_todos(ev.get("args"), ev.get("result")))
                self._update_plan(t.sid, plan)
            return
        loop = asyncio.get_running_loop()
        if phase == "start":
            old = self.pending_starts.pop(ident, None)
            if old is not None:
                old.cancel()
            self.pending_starts[ident] = loop.call_later(
                FAST_TOOL_SKIP_S, self._post_start, ident, t, name, ev.get("args"))
            return
        pending = self.pending_starts.pop(ident, None)
        if pending is not None:
            pending.cancel()  # finished within FAST_TOOL_SKIP_S: the start is never shown
        result = renderer.result_preview(ev.get("result"))
        if ev.get("status") == "error":
            result = f"Failed: {result}" if result else "Failed"
        self._post(t.sid, renderer.action(name, renderer.tool_parameter(name, ev.get("args")),
                                          result))

    def _post_start(self, ident: tuple, t: Target, name: str, args: Any) -> None:
        self.pending_starts.pop(ident, None)
        if self._answered(t.mid):
            return
        self._post(t.sid, renderer.action(name, renderer.tool_parameter(name, args)),
                   ephemeral=True)

    def _update_plan(self, sid: str, plan: list[dict] | None) -> None:
        writer = self.a._activity_writer
        if writer is not None and plan is not None:
            self.writes.submit(sid, lambda: writer.update_plan(sid, plan))

    def _session_link(self, sid: str, hermes_session_id: str | None) -> None:
        settings = self.a._settings
        template = settings.session_url if settings is not None else ""
        writer = self.a._activity_writer
        if not template or not hermes_session_id or sid in self.url_set or writer is None:
            return
        self.url_set.add(sid)
        url = template.replace("{session_id}", str(hermes_session_id))
        self.writes.submit(sid, lambda: writer.set_external_url(sid, SESSION_LINK_LABEL, url))


def _todos(args: Any, result: Any) -> list | None:
    """The todo list after the call: the tool's JSON result (merged list), else its args."""
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and isinstance(parsed.get("todos"), list):
            return parsed["todos"]
    if isinstance(args, dict) and isinstance(args.get("todos"), list) and not args.get("merge"):
        return args["todos"]
    return None
