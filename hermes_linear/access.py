"""Allowlist access policy for Linear users.

Pure module (no Hermes imports). Entries come from ``Settings.allowed_users``:

* ``*`` allows everyone;
* an entry containing ``@`` is an email, compared case-insensitively;
* anything else is a Linear user UUID, compared case-insensitively.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

REFUSAL_TEXT = (
    "You're not authorized to use this agent. "
    "Ask the Hermes owner to add you to the Linear allowlist."
)

_STAR = "*"


class AccessPolicy:
    """Decide whether a Linear user may talk to the agent. Default deny."""

    __slots__ = ("_open", "_uuids", "_emails")

    def __init__(self, entries: frozenset[str], allow_all: bool) -> None:
        cleaned = {e.strip().lower() for e in entries if e and e.strip()}
        self._open = bool(allow_all) or _STAR in cleaned
        cleaned.discard(_STAR)
        self._emails = frozenset(e for e in cleaned if "@" in e)
        self._uuids = frozenset(e for e in cleaned if "@" not in e)

    @property
    def open(self) -> bool:
        """True when every user is allowed (``allow_all`` or ``*``)."""
        return self._open

    @property
    def uuid_entries(self) -> frozenset[str]:
        """Lowercased Linear user UUID entries."""
        return self._uuids

    @property
    def email_entries(self) -> frozenset[str]:
        """Lowercased email entries."""
        return self._emails

    async def allowed(
        self,
        user_id: str | None,
        email_of: Callable[[str], Awaitable[str | None]],
    ) -> bool:
        if not user_id:
            return False
        if self._open:
            return True
        if user_id.strip().lower() in self._uuids:
            return True
        if not self._emails:
            return False
        try:
            email = await email_of(user_id)
        except Exception as exc:  # noqa: BLE001 - any lookup failure denies
            logger.debug(
                "linear access: email lookup failed for user %s (%s); denying",
                user_id,
                type(exc).__name__,
            )
            return False
        if not email:
            logger.debug("linear access: no email for user %s; denying", user_id)
            return False
        if email.strip().lower() in self._emails:
            return True
        logger.debug("linear access: user %s not in allowlist; denying", user_id)
        return False
