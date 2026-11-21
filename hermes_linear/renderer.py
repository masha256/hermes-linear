"""Outbound rendering: Hermes output -> Linear agent activity payloads (spec §8.2, §8.4).

Pure builders produce the ``content`` dicts Linear validates server-side; the
``ActivityWriter`` posts them and never raises, so a Linear outage cannot crash
a running turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .constants import RESULT_PREVIEW
from .linear_client import LinearError

log = logging.getLogger(__name__)

PARAMETER_MAX = 120
ELLIPSIS = "…"

# Only these activity types may be posted ephemerally (Linear rejects the rest).
EPHEMERAL_TYPES = frozenset({"thought", "action"})

_APPROVAL_OPTIONS = (
    ("Allow once", "once"),
    ("Allow for this session", "session"),
    ("Deny", "deny"),
)

_PLAN_STATUS = {
    "pending": "pending",
    "in_progress": "inProgress",
    "completed": "completed",
    "cancelled": "canceled",
}

_PARAM_KEYS = ("command", "path", "file_path", "query", "url", "pattern", "name")


# ---------------------------------------------------------------- content shapes

def thought(body: str) -> dict:
    return {"type": "thought", "body": body}


def response(body: str) -> dict:
    return {"type": "response", "body": body}


def error(body: str) -> dict:
    return {"type": "error", "body": body}


def elicitation(body: str) -> dict:
    return {"type": "elicitation", "body": body}


def action(name: str, parameter: str, result: str | None = None) -> dict:
    content = {"type": "action", "action": name, "parameter": parameter}
    if result is not None:
        content["result"] = result
    return content


# ---------------------------------------------------------------- approvals

def approval_options() -> list[dict]:
    return [{"label": label, "value": value} for label, value in _APPROVAL_OPTIONS]


def parse_approval_answer(text: str) -> str | None:
    """Exact option value or label (case-insensitive, trimmed). Never fuzzy."""
    if not isinstance(text, str):
        return None
    answer = text.strip().casefold()
    if not answer:
        return None
    for label, value in _APPROVAL_OPTIONS:
        if answer == value.casefold() or answer == label.casefold():
            return value
    return None


# ---------------------------------------------------------------- plan

def plan_from_todos(todos: list[dict]) -> list[dict]:
    """Hermes todo list -> Linear ``agentSessionUpdate.plan`` (full replace)."""
    plan = []
    for t in todos or []:
        status = _PLAN_STATUS.get(str(t.get("status") or ""), "pending")
        plan.append({"content": str(t.get("content") or ""), "status": status})
    return plan


# ---------------------------------------------------------------- previews

def _redact(text: str) -> str:
    # Lazy import so this module loads without Hermes on the path.
    from agent.redact import redact_sensitive_text

    return redact_sensitive_text(text, force=True)


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(ELLIPSIS)] + ELLIPSIS


def tool_parameter(tool_name: str, args: Any) -> str:
    """Short single-line, redacted argument preview (≤ 120 chars)."""
    if not args:
        return ""
    raw: str | None = None
    if isinstance(args, dict):
        for key in _PARAM_KEYS:
            val = args.get(key)
            if val not in (None, ""):
                raw = str(val)
                break
    if raw is None:
        try:
            raw = json.dumps(args, separators=(",", ":"), ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            raw = str(args)
    line = " ".join(_redact(raw).split())
    return _cap(line, PARAMETER_MAX)


def result_preview(result: str | None) -> str:
    """Redacted tool result preview, ≤ RESULT_PREVIEW chars."""
    if result is None:
        return ""
    return _cap(_redact(str(result)).strip(), RESULT_PREVIEW)


# ---------------------------------------------------------------- writer

def _short_error(exc: BaseException) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return "timeout"
    msg = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    return _cap(" ".join(msg.split()), 200)


class ActivityWriter:
    """Posts activities / session updates; never raises.

    Retries: ``LinearClient`` already retries (``WRITE_RETRIES``, 429
    ``Retry-After``); the writer deliberately adds no retry loop of its own so
    attempts are not multiplied. A failure after the client's retries marks the
    writer ``degraded`` and returns False.
    """

    def __init__(self, client: Any, *, timeout_s: float | None = None):
        self.client = client
        self.timeout_s = timeout_s
        self.degraded = False
        self.last_error: str | None = None

    def clear_degraded(self) -> None:
        self.degraded = False
        self.last_error = None

    async def _call(self, what: str, sid: str, coro_factory, timeout_s: float | None) -> bool:
        timeout = timeout_s if timeout_s is not None else self.timeout_s
        try:
            coro = coro_factory()
            if timeout is not None:
                await asyncio.wait_for(coro, timeout)
            else:
                await coro
            return True
        except (LinearError, asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001 - never raise
            self.degraded = True
            self.last_error = _short_error(exc)
            log.warning("linear: %s failed for session %s: %s", what, sid, self.last_error)
            return False

    async def post(
        self,
        agent_session_id: str,
        content: dict,
        *,
        ephemeral: bool = False,
        signal: str | None = None,
        signal_metadata: dict | None = None,
        timeout_s: float | None = None,
    ) -> bool:
        kind = content.get("type") if isinstance(content, dict) else None
        if ephemeral and kind not in EPHEMERAL_TYPES:
            log.warning("linear: refusing ephemeral %r activity (only thought/action allowed)", kind)
            return False
        return await self._call(
            f"activity {kind}",
            agent_session_id,
            lambda: self.client.create_activity(
                agent_session_id,
                content,
                ephemeral=ephemeral,
                signal=signal,
                signal_metadata=signal_metadata,
            ),
            timeout_s,
        )

    async def update_plan(
        self, agent_session_id: str, plan: list[dict], *, timeout_s: float | None = None
    ) -> bool:
        return await self._call(
            "plan update",
            agent_session_id,
            lambda: self.client.update_session(agent_session_id, plan=plan),
            timeout_s,
        )

    async def set_external_url(
        self, agent_session_id: str, label: str, url: str, *, timeout_s: float | None = None
    ) -> bool:
        return await self._call(
            "external url update",
            agent_session_id,
            lambda: self.client.update_session(
                agent_session_id, external_urls=[{"label": label, "url": url}]
            ),
            timeout_s,
        )
