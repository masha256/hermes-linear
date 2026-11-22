"""Plain data models shared by the Linear channel.

Pure dataclasses only — no Hermes imports, so this module is importable
(and testable) without Hermes installed.

``Decision`` values are produced by the webhook router and consumed by the
adapter; each one describes a single action to take for an incoming event.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union


@dataclass(frozen=True)
class ContextItem:
    """One change on a ticket (or its project) the agent should be told about."""

    issue_id: str
    kind: str
    field: str
    old: str | None
    new: str | None
    actor: str
    at: str
    project: str | None = None


# ---------------------------------------------------------------- decisions


@dataclass(frozen=True)
class StartTurn:
    """Start an agent turn for a Linear agent session activity."""

    agent_session_id: str
    activity_id: str
    issue_id: str
    chat_id: str
    thread_id: str | None
    user_id: str
    user_name: str
    text: str
    first_turn: bool
    ack: str


@dataclass(frozen=True)
class AddContext:
    """Queue ticket changes to be shown to the agent on its next turn."""

    items: tuple[ContextItem, ...]


@dataclass(frozen=True)
class ResolveControl:
    """Answer a pending approval / clarify request from a Linear reply."""

    agent_session_id: str
    request_id: str
    kind: str
    actor_id: str
    answer: str
    state: str = "live"  # the row's state: "live" (a Hermes waiter) or "pending"
    user_name: str | None = None  # the replying user (the answer turn's header)
    activity_id: str = ""  # the prompted activity (the answer turn's message id)


@dataclass(frozen=True)
class Interrupt:
    """Stop the running turn of an agent session."""

    agent_session_id: str


@dataclass(frozen=True)
class Refuse:
    """Reply on the agent session without running the agent."""

    agent_session_id: str
    text: str


@dataclass(frozen=True)
class SetMode:
    """Change the ticket's working mode (e.g. delegated vs mention-only)."""

    issue_id: str
    mode: str
    generation: int


@dataclass(frozen=True)
class AwaitDelegation:
    """Wait up to ``deadline_s`` seconds for a delegation session to arrive."""

    issue_id: str
    deadline_s: float


@dataclass(frozen=True)
class OpenSessionOnComment:
    """Open a Linear agent session in response to a plain comment."""

    issue_id: str
    comment_id: str
    user_id: str
    user_name: str
    body: str


@dataclass(frozen=True)
class Fatal:
    """Unrecoverable condition; the adapter should report and stop."""

    reason: str


@dataclass(frozen=True)
class Ignore:
    """Event intentionally dropped (duplicate, irrelevant, self-authored...)."""

    reason: str


Decision = Union[
    StartTurn,
    AddContext,
    ResolveControl,
    Interrupt,
    Refuse,
    SetMode,
    AwaitDelegation,
    OpenSessionOnComment,
    Fatal,
    Ignore,
]

__all__ = [
    "AddContext",
    "AwaitDelegation",
    "ContextItem",
    "Decision",
    "Fatal",
    "Ignore",
    "Interrupt",
    "OpenSessionOnComment",
    "Refuse",
    "ResolveControl",
    "SetMode",
    "StartTurn",
]
