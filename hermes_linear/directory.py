"""The name directory (RET-121 spec §2.2): teams, states, labels, users, cycles, projects and
milestones, cached per ``LinearClient`` for ``DIRECTORY_TTL_S``.

``resolve`` turns an agent-supplied reference into one row with the caller's ``match``. A
miss refetches the entry once (unless this call already fetched it) and matches again; a
miss after that raises the kind's refusal carrying ``candidates`` (labels: ``available``).
Everything runs on the adapter loop; two concurrent misses may fetch twice, which is
harmless, so there is no lock.
"""

from __future__ import annotations

import time
import weakref
from typing import Any, Awaitable, Callable

DIRECTORY_TTL_S = 300
CANDIDATES_MAX = 20
LABELS_AVAILABLE_MAX = 100

#: kind -> the client call answering it (``scope_id`` is the team or project UUID, or "").
_FETCH: dict[str, Callable[[Any, str], Awaitable[list[dict]]]] = {
    "teams": lambda c, scope: c.teams(),
    "states": lambda c, scope: c.team_states(scope),
    "labels": lambda c, scope: c.team_labels(scope),
    "users": lambda c, scope: c.users(),
    "cycles": lambda c, scope: c.cycles(scope),
    "projects": lambda c, scope: c.projects(),
    "milestones": lambda c, scope: c.milestones(scope),
}

#: kind -> the refusal code of a miss.
MISS_CODES = {"teams": "TEAM_NOT_FOUND", "states": "STATE_NOT_FOUND",
              "labels": "LABEL_MISSING", "users": "USER_NOT_FOUND",
              "cycles": "CYCLE_NOT_FOUND", "projects": "PROJECT_NOT_FOUND",
              "milestones": "MILESTONE_NOT_FOUND"}

#: kind -> the row keys a candidate carries.
_CANDIDATE_KEYS = {"teams": ("id", "key", "name"), "states": ("id", "name", "type"),
                   "labels": ("id", "name"), "users": ("id", "name", "displayName", "app"),
                   "cycles": ("id", "number", "name"), "projects": ("id", "name"),
                   "milestones": ("id", "name")}

#: Row keys a reference is substring-matched on to order candidates.
_TEXT_KEYS = ("name", "key", "displayName", "email")

Match = Callable[[list[dict], Any], "dict | list[dict] | None"]


def _refusal(code: str, extra: dict) -> Exception:
    from .agent_tools import _Refusal  # lazy: agent_tools imports the adapter, which
    return _Refusal(code, extra=extra)  # imports questions, which imports this module


def _candidates(kind: str, rows: list[dict], ref: Any) -> list[dict]:
    """At most ``CANDIDATES_MAX`` rows: substring matches on ``ref`` first, then the rest,
    each in row order, cut to the kind's candidate keys."""
    want = str(ref if ref is not None else "").strip().casefold()

    def hit(row: dict) -> bool:
        return bool(want) and any(want in str(row.get(k) or "").casefold()
                                  for k in _TEXT_KEYS)

    ordered = [r for r in rows if hit(r)] + [r for r in rows if not hit(r)]
    keys = _CANDIDATE_KEYS[kind]
    return [{k: r.get(k) for k in keys} for r in ordered[:CANDIDATES_MAX]]


def miss(kind: str, rows: list[dict], ref: Any) -> Exception:
    """The refusal for ``ref`` missing from ``rows`` (already refetched): the kind's code
    with ``candidates``; for labels ``LABEL_MISSING`` with ``available`` names."""
    if kind == "labels":
        names = list(dict.fromkeys(r.get("name") or "" for r in rows if r.get("name")))
        return _refusal(MISS_CODES[kind], {"available": names[:LABELS_AVAILABLE_MAX]})
    return _refusal(MISS_CODES[kind], {"candidates": _candidates(kind, rows, ref)})


class Directory:
    """One client's cached lists, keyed ``(kind, scope_id)``, each with its fetch time."""

    def __init__(self, client: Any, clock: Callable[[], float] = time.monotonic) -> None:
        self._client = client
        self._clock = clock
        self._entries: dict[tuple[str, str], tuple[float, list[dict]]] = {}

    async def _fetch(self, kind: str, scope_id: str) -> list[dict]:
        rows = [dict(r) for r in await _FETCH[kind](self._client, scope_id)]
        self._entries[(kind, scope_id)] = (self._clock(), rows)
        return rows

    async def _cached(self, kind: str, scope_id: str) -> tuple[list[dict], bool]:
        """The entry's rows and whether they were fetched just now (missing or expired)."""
        entry = self._entries.get((kind, scope_id))
        if entry is not None and self._clock() - entry[0] <= DIRECTORY_TTL_S:
            return entry[1], False
        return await self._fetch(kind, scope_id), True

    async def rows(self, kind: str, scope_id: str = "") -> list[dict]:
        """The cached rows (copies); fetched when missing or older than the TTL."""
        rows, _ = await self._cached(kind, scope_id)
        return [dict(r) for r in rows]

    async def resolve(self, kind: str, scope_id: str, ref: Any, match: Match) -> dict:
        """The one row ``match(rows, ref)`` picks. ``None`` (a miss) refetches once, unless
        this call fetched already; still a miss: the kind's refusal (``miss``). A list
        (several rows): ``AMBIGUOUS`` with those rows as candidates."""
        rows, fresh = await self._cached(kind, scope_id)
        found = match([dict(r) for r in rows], ref)
        if found is None and not fresh:
            rows = await self._fetch(kind, scope_id)
            found = match([dict(r) for r in rows], ref)
        if found is None:
            raise miss(kind, rows, ref)
        if isinstance(found, list):
            raise _refusal("AMBIGUOUS", {"candidates": _candidates(kind, found, ref)})
        return found


_DIRECTORIES: weakref.WeakKeyDictionary[Any, Directory] = weakref.WeakKeyDictionary()


def for_client(client: Any) -> Directory:
    """The client's directory (one per client, dropped with it: the directory holds only a
    weak proxy, since a value referencing its key would keep the key alive)."""
    d = _DIRECTORIES.get(client)
    if d is None:
        d = _DIRECTORIES[client] = Directory(weakref.proxy(client))
    return d
