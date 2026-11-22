"""Outbound path: Hermes -> Linear activities (spec §8.2, §8.4, §8.5; integration notes Q1, Q6,
Q8, Q10).

* ``send``: the Q1 send contract. Target resolution: ``reply_to`` -> metadata
  ``reply_to_message_id`` -> ``store.latest_turn_for_chat(chat_id, thread_id)`` (exact thread)
  -> drop + log. Classification: ``_interim_send`` / ``expect_edits`` -> ``thought``; the
  turn-error notice -> ``error``; an anchorless send with no turn in flight -> ``thought``;
  anything else -> ``response`` (at most one per turn; later ones become ``thought``).
* Stop (Q6): the gateway's ``/stop`` acknowledgement is recognised by ``reply_to`` and never
  forwarded; the stopped turn gets ``Stopped.`` as its ``response`` once it completes CANCELLED.
* Typing / heartbeat: spawned ephemeral thoughts, bounded by ``ACK_TIMEOUT_S``; never awaited by
  the caller.
* Approvals / clarify: ``elicitation`` (+ ``select``) and a ``pending_controls`` row; expiry
  posts a fixed ``thought`` and drops the row (Q10).
* Tool events from ``hooks.py`` (already on the loop): start/finish actions with the fast-tool
  skip, todo -> plan, and the session link.

Writers: ``responses`` go through ``adapter._writer`` (its failure marks the adapter degraded,
§8.6); everything else goes through ``adapter._activity_writer`` and is only logged on failure.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from gateway.platforms.base import SendResult
from gateway.platforms.event import ProcessingOutcome

from . import renderer
from .constants import ACK_TIMEOUT_S, FAST_TOOL_SKIP_S, HEARTBEAT_S, TYPING_MIN_INTERVAL_S
from .turns import STOPPED_TEXT, Key, Turn

if TYPE_CHECKING:  # pragma: no cover
    from .adapter import LinearAdapter

log = logging.getLogger(__name__)

NO_SESSION = "no linear session"
WORKING_TEXT = "Working…"
HEARTBEAT_TEXT = "Still working…"
EXPIRED_TEXT = "This request expired."
APPROVAL_EXPIRED_TEXT = "This request expired. The command was not run."
SESSION_LINK_LABEL = "Hermes session"
_NOTICE_KEYS = frozenset({"thread_id", "hermes_profile"})  # turn-error notice metadata (Q1)
_APPROVAL_CHOICES = ("once", "session", "deny")  # spec §8.4 omits "always" (Q8)
_TODO_TOOLS = frozenset({"todo", "todo_list"})  # tools/todo_tool.py: name + legacy alias
_REMEMBER = 1024  # bounded memory of answered turns / retired stop ids
__all__ = ["Outbound", "Target"]


@dataclass(frozen=True)
class Target:
    sid: str  # Linear agent session id
    mid: str  # the turn's message id
    key: Key  # Hermes session (chat_id, thread_id)
    via: str  # "reply_to" | "reply_to_message_id" | "chat"


class _Bounded(OrderedDict):
    def add(self, item: str) -> None:
        self[item] = None
        while len(self) > _REMEMBER:
            self.popitem(last=False)


def _generation(chat_id: str) -> int:
    tail = str(chat_id).rpartition(":")[2]
    return int(tail) if tail.isdigit() else 0


class Outbound:
    def __init__(self, adapter: "LinearAdapter") -> None:
        self.a = adapter
        self.clock = time.monotonic  # typing rate limit (tests inject a fake clock)
        self.sleep = asyncio.sleep  # heartbeat interval (tests inject a fake)
        self.responded = _Bounded()  # turn mids that already have their response
        self.last_thought: dict[str, str] = {}  # turn mid -> last thought (turn-end fallback)
        self.typing_at: dict[str, float] = {}  # agent session -> last "Working…"
        self.heartbeats: dict[str, asyncio.Task] = {}  # turn mid -> heartbeat task
        self.pending_starts: dict[tuple, asyncio.TimerHandle] = {}  # fast-tool skip timers
        self.url_set: set[str] = set()  # agent sessions whose link was set
        self.retired_stops = _Bounded()  # synthesized stop ids already handled

    def reset(self) -> None:
        """Disconnect: cancel timers and forget state (tasks are cancelled by the adapter)."""
        for handle in self.pending_starts.values():
            handle.cancel()
        self.pending_starts.clear()
        self.heartbeats.clear()
        self.last_thought.clear()
        self.typing_at.clear()

    # ------------------------------------------------------------ writers

    async def _post(self, sid: str, content: dict, **kw) -> bool:
        writer = self.a._activity_writer
        return bool(writer) and await writer.post(sid, content, **kw)

    async def _respond(self, sid: str, content: dict, **kw) -> bool:
        return await self.a._post_response(sid, content, **kw)

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
        if t.via == "chat" and self.a.turns.current_turn_for(*t.key) is None:
            # T1 deferral: the first anchorless send after a turn consumes its outcome.
            outcome = self.a.turns.last_outcome.pop(t.key, None)
            if (reply_to is None and set(md) <= _NOTICE_KEYS
                    and outcome is ProcessingOutcome.FAILURE):
                return "error"  # _notify_turn_error, right after on_processing_complete(FAILURE)
            log.info("linear: unmarked send with no turn in flight posted as a thought "
                     "(session %s)", t.sid)
            return "thought"
        if t.mid in self.responded:
            log.info("linear: turn %s already has its response; posting as a thought", t.mid)
            return "thought"
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
        if kind == "response":
            self.responded.add(target.mid)
            ok = await self._respond(target.sid, renderer.response(body))
        else:
            if kind == "thought":
                self.last_thought[target.mid] = body
            builder = renderer.error if kind == "error" else renderer.thought
            ok = await self._post(target.sid, builder(body))
        if not ok:
            return SendResult(success=False, error="linear write failed")
        return SendResult(success=True, message_id=f"{target.sid}:{kind}:{target.mid}")

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

    def turn_finished(self, found: tuple[Key, Turn] | None, outcome: ProcessingOutcome) -> None:
        """``on_processing_complete`` (sync): stop reply, turn-end fallback, heartbeat."""
        if found is None:
            return
        _key, turn = found
        mid, sid = turn.mid, turn.decision.agent_session_id
        self.stop_heartbeat(mid)
        stops = self.a.turns.stop_message_ids
        stop_ids = [m for m, s in stops.items() if s == sid]
        for m in stop_ids:
            del stops[m]
            self.retired_stops.add(m)
        last = self.last_thought.pop(mid, None)
        if mid in self.responded:
            return
        if outcome is ProcessingOutcome.CANCELLED and stop_ids:
            self.responded.add(mid)
            self.a._spawn(self._respond(sid, renderer.response(STOPPED_TEXT),
                                        timeout_s=ACK_TIMEOUT_S))
        elif outcome is ProcessingOutcome.SUCCESS and last:
            # Q1 caveat: the final send was deduplicated against commentary; promote it.
            self.responded.add(mid)
            self.a._spawn(self._respond(sid, renderer.response(last)))

    # ------------------------------------------------------------ typing / heartbeat

    def typing(self, chat_id: str, metadata: Optional[dict]) -> None:
        turn = self.a.turns.current_turn_for(str(chat_id), (metadata or {}).get("thread_id"))
        if turn is None:
            return
        sid, now = turn.decision.agent_session_id, self.clock()
        last = self.typing_at.get(sid)
        if last is not None and now - last < TYPING_MIN_INTERVAL_S:
            return
        self.typing_at[sid] = now
        self.a._spawn(self._post(sid, renderer.thought(WORKING_TEXT), ephemeral=True,
                                 timeout_s=ACK_TIMEOUT_S))

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
                if self.a.turns.find(mid) is None:  # completed, stopped or timed out
                    return
                await self._post(sid, renderer.thought(HEARTBEAT_TEXT), ephemeral=True,
                                 timeout_s=ACK_TIMEOUT_S)
        finally:
            if self.heartbeats.get(mid) is asyncio.current_task():
                del self.heartbeats[mid]

    # ------------------------------------------------------------ approvals / clarify

    def _put_control(self, rid: str, kind: str, t: Target, choices: list, session_key: str):
        store = self.a._store
        assert store is not None
        store.put_control(rid, kind, t.sid, _generation(t.key[0]), choices,
                          session_key=session_key)

    def _drop_control(self, rid: str) -> None:
        if self.a._store is not None:
            self.a._store.drop_control(rid)

    async def approval_prompt(self, prompt: Any) -> SendResult:
        target = self.resolve(prompt.chat_id, None, dict(prompt.metadata or {}))
        if target is None or not prompt.request_id:
            log.warning("linear: approval prompt dropped (no linear session or request id)")
            return SendResult(success=False, error=NO_SESSION)
        choices = [c for c in _APPROVAL_CHOICES if c in prompt.choices]  # Q8: keep narrowing
        options = [o for o in renderer.approval_options() if o["value"] in choices]
        rid = str(prompt.request_id)
        self._put_control(rid, "approval", target, choices, prompt.session_key)
        ok = await self._post(target.sid, renderer.elicitation(prompt.text), signal="select",
                              signal_metadata={"options": options}, timeout_s=ACK_TIMEOUT_S)
        if not ok:
            self._drop_control(rid)
            return SendResult(success=False, error="linear write failed")
        return SendResult(success=True, message_id=f"approval:{rid}")

    async def clarify(self, chat_id: str, question: str, choices: Optional[list], clarify_id: str,
                      session_key: str, metadata: Optional[dict]) -> SendResult:
        target = self.resolve(chat_id, None, dict(metadata or {}))
        if target is None:
            log.warning("linear: clarify %s dropped, no linear session", clarify_id)
            return SendResult(success=False, error=NO_SESSION)
        values = [str(c) for c in (choices or [])]
        self._put_control(str(clarify_id), "clarify", target, values, session_key)
        kw: dict[str, Any] = {}
        if values:
            kw = {"signal": "select",
                  "signal_metadata": {"options": [{"label": v, "value": v} for v in values]}}
        ok = await self._post(target.sid, renderer.elicitation(str(question)),
                              timeout_s=ACK_TIMEOUT_S, **kw)
        if not ok:
            self._drop_control(str(clarify_id))
            return SendResult(success=False, error="linear write failed")
        return SendResult(success=True, message_id=f"clarify:{clarify_id}")

    async def _expire(self, rid: str, kind: str, text: str) -> bool:
        store = self.a._store
        row = store.control(rid) if store is not None else None
        if row is None or row.kind != kind:
            return False  # already answered or dropped
        self._drop_control(rid)
        await self._post(row.agent_session_id, renderer.thought(text), timeout_s=ACK_TIMEOUT_S)
        return True

    async def edit_message(self, message_id: str) -> SendResult:
        """Q10: only the approval-timeout edit; its body is never echoed."""
        if not str(message_id).startswith("approval:"):
            return SendResult(success=False, error="Not supported")
        await self._expire(str(message_id).split(":", 1)[1], "approval", APPROVAL_EXPIRED_TEXT)
        return SendResult(success=True, message_id=message_id)

    async def retire_clarify(self, clarify_id: str) -> None:
        await self._expire(str(clarify_id), "clarify", EXPIRED_TEXT)

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
        return Target(row.agent_session_id, row.message_id, (row.hermes_chat_id, row.thread_id),
                      "hook")

    def tool_event(self, phase: str, t: Target, ev: dict) -> None:
        """One pre/post tool call for agent session ``t.sid`` (sync, on the loop)."""
        self._session_link(t.sid, ev.get("session_id"))
        name = str(ev.get("tool_name") or "tool")
        ident = (ev.get("task_id"), ev.get("tool_call_id") or name)
        if name in _TODO_TOOLS:
            if phase == "finish":
                plan = renderer.plan_from_todos(_todos(ev.get("args"), ev.get("result")))
                self.a._spawn(self._update_plan(t.sid, plan))
            return
        loop = asyncio.get_running_loop()
        if phase == "start":
            old = self.pending_starts.pop(ident, None)
            if old is not None:
                old.cancel()
            self.pending_starts[ident] = loop.call_later(
                FAST_TOOL_SKIP_S, self._post_start, ident, t.sid, name,
                ev.get("args"))
            return
        pending = self.pending_starts.pop(ident, None)
        if pending is not None:
            pending.cancel()  # finished within FAST_TOOL_SKIP_S: the start is never shown
        result = renderer.result_preview(ev.get("result"))
        if ev.get("status") == "error":
            result = f"Failed: {result}" if result else "Failed"
        self.a._spawn(self._post(t.sid, renderer.action(
            name, renderer.tool_parameter(name, ev.get("args")), result)))

    def _post_start(self, ident: tuple, sid: str, name: str, args: Any) -> None:
        self.pending_starts.pop(ident, None)
        self.a._spawn(self._post(sid, renderer.action(name, renderer.tool_parameter(name, args)),
                                 ephemeral=True))

    async def _update_plan(self, sid: str, plan: list[dict] | None) -> None:
        writer = self.a._activity_writer
        if writer is not None:
            await writer.update_plan(sid, plan)

    def _session_link(self, sid: str, hermes_session_id: str | None) -> None:
        settings = self.a._settings
        template = settings.session_url if settings is not None else ""
        writer = self.a._activity_writer
        if not template or not hermes_session_id or sid in self.url_set or writer is None:
            return
        self.url_set.add(sid)
        url = template.replace("{session_id}", str(hermes_session_id))
        self.a._spawn(writer.set_external_url(sid, SESSION_LINK_LABEL, url))


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
