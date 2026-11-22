"""Per-profile SQLite state store for the Linear channel (spec §5.1).

``Store.open(None)`` uses Hermes's ``plugins.plugin_storage.plugin_db("linear")``
(imported lazily so this module stays importable without Hermes). Tests and
tools may pass an explicit path or ``":memory:"``.

Thread safety: the store is used from the asyncio loop thread and from Hermes
tool-hook worker threads. Every public method holds one ``RLock`` and each
mutating method commits before returning (no long-lived transactions).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence

from .constants import DEDUP_TTL_S, MAX_PENDING_ITEMS, TEST_WEBHOOK_PREFIX, USER_CACHE_TTL_S
from .models import ContextItem

SCHEMA_VERSION = 1

Mode = Literal["untouched", "mention", "delegated"]
_MODES = ("untouched", "mention", "delegated")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    issue_id    TEXT PRIMARY KEY,
    identifier  TEXT,
    team_id     TEXT,
    project_id  TEXT,
    mode        TEXT NOT NULL DEFAULT 'untouched'
                CHECK (mode IN ('untouched', 'mention', 'delegated')),
    generation  INTEGER NOT NULL DEFAULT 0,
    state_type  TEXT,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tickets_project ON tickets(project_id, mode);

CREATE TABLE IF NOT EXISTS sessions (
    agent_session_id TEXT PRIMARY KEY,
    issue_id         TEXT NOT NULL,
    root_comment_id  TEXT,
    kind             TEXT NOT NULL
                     CHECK (kind IN ('delegation', 'mention', 'comment', 'plugin')),
    plugin_created   INTEGER NOT NULL DEFAULT 0,
    created_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_root ON sessions(root_comment_id);

CREATE TABLE IF NOT EXISTS turns (
    seq              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id       TEXT NOT NULL UNIQUE,
    agent_session_id TEXT NOT NULL,
    hermes_chat_id   TEXT NOT NULL,
    thread_id        TEXT,
    status           TEXT NOT NULL DEFAULT 'started',
    created_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS turns_chat ON turns(hermes_chat_id, thread_id, seq);

CREATE TABLE IF NOT EXISTS pending_context (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id    TEXT NOT NULL,
    kind        TEXT NOT NULL,
    field       TEXT NOT NULL,
    old         TEXT,
    new         TEXT,
    actor       TEXT NOT NULL,
    at          TEXT NOT NULL,
    project     TEXT,
    consumed_by TEXT
);
CREATE INDEX IF NOT EXISTS pending_issue ON pending_context(issue_id, consumed_by, id);

CREATE TABLE IF NOT EXISTS pending_controls (
    request_id           TEXT PRIMARY KEY,
    kind                 TEXT NOT NULL CHECK (kind IN ('approval', 'clarify')),
    agent_session_id     TEXT NOT NULL,
    execution_generation INTEGER NOT NULL,
    choices              TEXT NOT NULL,
    created_at           REAL NOT NULL,
    session_key          TEXT
);
CREATE INDEX IF NOT EXISTS controls_session ON pending_controls(agent_session_id, created_at);

CREATE TABLE IF NOT EXISTS seen_webhooks (
    webhook_id TEXT PRIMARY KEY,
    at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS seen_at ON seen_webhooks(at);

CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY,
    email      TEXT,
    name       TEXT,
    fetched_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS states (
    state_id TEXT PRIMARY KEY,
    name     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS labels (
    label_id TEXT PRIMARY KEY,
    name     TEXT NOT NULL
);
"""


# ------------------------------------------------------------------ rows


@dataclass(frozen=True)
class Ticket:
    issue_id: str
    identifier: str | None
    team_id: str | None
    project_id: str | None
    mode: str
    generation: int
    state_type: str | None
    updated_at: float


@dataclass(frozen=True)
class SessionRow:
    agent_session_id: str
    issue_id: str
    root_comment_id: str | None
    kind: str
    plugin_created: bool
    created_at: float


@dataclass(frozen=True)
class TurnRow:
    message_id: str
    agent_session_id: str
    hermes_chat_id: str
    thread_id: str | None
    status: str
    created_at: float


@dataclass(frozen=True)
class ControlRow:
    request_id: str
    kind: str
    agent_session_id: str
    execution_generation: int
    choices: tuple[Any, ...]
    created_at: float
    session_key: str | None = None


@dataclass(frozen=True)
class UserRow:
    user_id: str
    email: str | None
    name: str | None
    fetched_at: float


# ----------------------------------------------------------------- store


class Store:
    """Thin, thread-safe wrapper over the plugin's SQLite connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._lock = threading.RLock()
        with self._lock:
            conn.executescript(_SCHEMA)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(pending_controls)")}
            if "session_key" not in cols:  # pre-session_key databases
                conn.execute("ALTER TABLE pending_controls ADD COLUMN session_key TEXT")
            if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()

    @classmethod
    def open(cls, path: Path | str | None = None) -> "Store":
        if path is None:
            from plugins.plugin_storage import plugin_db  # lazy: Hermes only

            return cls(plugin_db("linear"))
        conn = sqlite3.connect(str(path), check_same_thread=False)
        if str(path) != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return cls(conn)

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # internal helpers ----------------------------------------------------

    def _one(self, sql: str, args: Sequence[Any] = ()) -> sqlite3.Row | None:
        cur = self.conn.cursor()
        cur.row_factory = sqlite3.Row
        return cur.execute(sql, args).fetchone()

    def _all(self, sql: str, args: Sequence[Any] = ()) -> list[sqlite3.Row]:
        cur = self.conn.cursor()
        cur.row_factory = sqlite3.Row
        return cur.execute(sql, args).fetchall()

    def _write(self, fn):
        with self._lock:
            try:
                result = fn()
                self.conn.commit()
                return result
            except BaseException:
                self.conn.rollback()
                raise

    # tickets -------------------------------------------------------------

    def ticket(self, issue_id: str) -> Ticket | None:
        with self._lock:
            r = self._one("SELECT * FROM tickets WHERE issue_id = ?", (issue_id,))
        return Ticket(**dict(r)) if r else None

    def upsert_ticket(
        self,
        issue_id: str,
        *,
        identifier: str | None = None,
        team_id: str | None = None,
        project_id: str | None = None,
        state_type: str | None = None,
    ) -> Ticket:
        def go() -> None:
            self.conn.execute(
                """
                INSERT INTO tickets (issue_id, identifier, team_id, project_id,
                                     state_type, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(issue_id) DO UPDATE SET
                    identifier = COALESCE(excluded.identifier, identifier),
                    team_id    = COALESCE(excluded.team_id, team_id),
                    project_id = COALESCE(excluded.project_id, project_id),
                    state_type = COALESCE(excluded.state_type, state_type),
                    updated_at = excluded.updated_at
                """,
                (issue_id, identifier, team_id, project_id, state_type, time.time()),
            )

        with self._lock:
            self._write(go)
            t = self.ticket(issue_id)
        assert t is not None
        return t

    def set_mode(self, issue_id: str, mode: Mode, generation: int | None = None) -> None:
        if mode not in _MODES:
            raise ValueError(f"unknown mode {mode!r}")

        def go() -> None:
            now = time.time()
            self.conn.execute(
                "INSERT OR IGNORE INTO tickets (issue_id, updated_at) VALUES (?, ?)",
                (issue_id, now),
            )
            if generation is not None:
                gen_sql, args = "?", [generation]
            elif mode == "delegated":
                # First delegation of a never-delegated ticket starts at 1.
                gen_sql, args = "CASE WHEN generation < 1 THEN 1 ELSE generation END", []
            else:
                gen_sql, args = "generation", []
            self.conn.execute(
                f"UPDATE tickets SET mode = ?, generation = {gen_sql}, updated_at = ? "
                "WHERE issue_id = ?",
                [mode, *args, now, issue_id],
            )

        self._write(go)

    def clear_project(self, issue_id: str) -> None:
        """Forget the ticket's project (issue moved out of it). No-op if unknown."""
        self._write(lambda: self.conn.execute(
            "UPDATE tickets SET project_id = NULL, updated_at = ? WHERE issue_id = ?",
            (time.time(), issue_id)))

    def delegated_in_project(self, project_id: str) -> list[str]:
        with self._lock:
            rows = self._all(
                "SELECT issue_id FROM tickets WHERE project_id = ? AND mode = 'delegated' "
                "ORDER BY issue_id",
                (project_id,),
            )
        return [r["issue_id"] for r in rows]

    # sessions ------------------------------------------------------------

    def record_session(
        self,
        agent_session_id: str,
        issue_id: str,
        root_comment_id: str | None,
        kind: str,
        plugin_created: bool = False,
    ) -> None:
        def go() -> None:
            self.conn.execute(
                """
                INSERT INTO sessions (agent_session_id, issue_id, root_comment_id, kind,
                                      plugin_created, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_session_id) DO UPDATE SET
                    issue_id        = excluded.issue_id,
                    root_comment_id = COALESCE(excluded.root_comment_id, root_comment_id),
                    kind            = excluded.kind,
                    plugin_created  = MAX(plugin_created, excluded.plugin_created)
                """,
                (agent_session_id, issue_id, root_comment_id, kind,
                 int(bool(plugin_created)), time.time()),
            )

        self._write(go)

    def delete_session(self, agent_session_id: str) -> None:
        """Remove one session row (e.g. a ``pending:<comment>`` placeholder)."""
        def go() -> None:
            self.conn.execute("DELETE FROM sessions WHERE agent_session_id = ?",
                              (agent_session_id,))

        self._write(go)

    @staticmethod
    def _session_row(r: sqlite3.Row | None) -> SessionRow | None:
        if r is None:
            return None
        d = dict(r)
        d["plugin_created"] = bool(d["plugin_created"])
        return SessionRow(**d)

    def session(self, agent_session_id: str) -> SessionRow | None:
        with self._lock:
            r = self._one("SELECT * FROM sessions WHERE agent_session_id = ?",
                          (agent_session_id,))
        return self._session_row(r)

    def session_by_root_comment(self, comment_id: str) -> SessionRow | None:
        with self._lock:
            r = self._one(
                "SELECT * FROM sessions WHERE root_comment_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (comment_id,),
            )
        return self._session_row(r)

    def sessions_for_issue(self, issue_id: str) -> list[SessionRow]:
        with self._lock:
            rows = self._all(
                "SELECT * FROM sessions WHERE issue_id = ? ORDER BY created_at, rowid",
                (issue_id,),
            )
        return [self._session_row(r) for r in rows]  # type: ignore[misc]

    def is_session_root(self, comment_id: str) -> bool:
        return self.session_by_root_comment(comment_id) is not None

    # turns ---------------------------------------------------------------

    def record_turn(
        self,
        message_id: str,
        agent_session_id: str,
        chat_id: str,
        thread_id: str | None = None,
    ) -> None:
        def go() -> None:
            self.conn.execute(
                "INSERT OR IGNORE INTO turns (message_id, agent_session_id, hermes_chat_id, "
                "thread_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (message_id, agent_session_id, chat_id, thread_id, time.time()),
            )

        self._write(go)

    @staticmethod
    def _turn_row(r: sqlite3.Row | None) -> TurnRow | None:
        if r is None:
            return None
        d = dict(r)
        d.pop("seq")
        return TurnRow(**d)

    def turn(self, message_id: str) -> TurnRow | None:
        with self._lock:
            r = self._one("SELECT * FROM turns WHERE message_id = ?", (message_id,))
        return self._turn_row(r)

    def latest_turn_for_chat(self, chat_id: str, thread_id: str | None = None) -> TurnRow | None:
        with self._lock:
            r = self._one(
                "SELECT * FROM turns WHERE hermes_chat_id = ? AND thread_id IS ? "
                "ORDER BY seq DESC LIMIT 1",
                (chat_id, thread_id),
            )
        return self._turn_row(r)

    # pending context -----------------------------------------------------

    def add_context(self, items: Sequence[ContextItem]) -> None:
        if not items:
            return

        def go() -> None:
            self.conn.executemany(
                "INSERT INTO pending_context (issue_id, kind, field, old, new, actor, at, "
                "project) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(i.issue_id, i.kind, i.field, i.old, i.new, i.actor, i.at, i.project)
                 for i in items],
            )
            for issue_id in {i.issue_id for i in items}:
                self._enforce_cap(issue_id)

        self._write(go)

    def _pending_count(self, issue_id: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM pending_context WHERE issue_id = ? AND consumed_by IS NULL",
            (issue_id,),
        ).fetchone()[0]

    def _enforce_cap(self, issue_id: str) -> None:
        count = self._pending_count(issue_id)
        if count <= MAX_PENDING_ITEMS:
            return
        # 1) Collapse duplicate (kind, field, project) groups, oldest group first,
        #    into one row: earliest ``old``, latest ``new``/actor/at, latest position.
        #    Keeping the group's MAX id is load-bearing for consume watermarks:
        #    see ``pending_context_batch``.
        groups = self._all(
            "SELECT kind, field, project, MIN(id) AS first_id, MAX(id) AS last_id, "
            "COUNT(*) AS n FROM pending_context "
            "WHERE issue_id = ? AND consumed_by IS NULL "
            "GROUP BY kind, field, project HAVING n > 1 ORDER BY first_id",
            (issue_id,),
        )
        for g in groups:
            if count <= MAX_PENDING_ITEMS:
                break
            first_old = self.conn.execute(
                "SELECT old FROM pending_context WHERE id = ?", (g["first_id"],)
            ).fetchone()[0]
            self.conn.execute(
                "UPDATE pending_context SET old = ? WHERE id = ?", (first_old, g["last_id"])
            )
            self.conn.execute(
                "DELETE FROM pending_context WHERE issue_id = ? AND consumed_by IS NULL "
                "AND kind = ? AND field = ? AND project IS ? AND id <> ?",
                (issue_id, g["kind"], g["field"], g["project"], g["last_id"]),
            )
            count -= g["n"] - 1
        # 2) Still over: drop the oldest.
        if count > MAX_PENDING_ITEMS:
            self.conn.execute(
                "DELETE FROM pending_context WHERE id IN ("
                " SELECT id FROM pending_context WHERE issue_id = ? AND consumed_by IS NULL"
                " ORDER BY id LIMIT ?)",
                (issue_id, count - MAX_PENDING_ITEMS),
            )

    def pending_context(self, issue_id: str) -> list[ContextItem]:
        with self._lock:
            rows = self._all(
                "SELECT issue_id, kind, field, old, new, actor, at, project "
                "FROM pending_context WHERE issue_id = ? AND consumed_by IS NULL ORDER BY id",
                (issue_id,),
            )
        return [ContextItem(**dict(r)) for r in rows]

    def pending_context_batch(self, issue_id: str) -> tuple[list[ContextItem], int | None]:
        """Unconsumed items (same order as ``pending_context``) plus a watermark.

        The watermark is the max row id included (None when empty). Pass it to
        ``consume_context(..., up_to_id=watermark)`` once the gateway admits the
        turn (spec §7.5) so items that arrived after rendering stay pending.

        Interaction with the cap merge (``_enforce_cap``): a merged group always
        keeps its MAX id, so newer unrendered content never lands in a row with
        id <= watermark. If a group straddles the watermark, the merged row has
        id > watermark and stays pending (re-shown next turn as one consolidated
        earliest-old -> latest-new line); if the whole group is <= watermark it
        is consumed. Drop-oldest deletes the lowest ids, i.e. rendered rows first.
        """
        with self._lock:
            rows = self._all(
                "SELECT id, issue_id, kind, field, old, new, actor, at, project "
                "FROM pending_context WHERE issue_id = ? AND consumed_by IS NULL ORDER BY id",
                (issue_id,),
            )
        items: list[ContextItem] = []
        watermark: int | None = None
        for r in rows:
            d = dict(r)
            watermark = d.pop("id")
            items.append(ContextItem(**d))
        return items, watermark

    def consume_context(
        self, issue_id: str, turn_id: str, up_to_id: int | None = None
    ) -> None:
        """Mark unconsumed rows consumed by ``turn_id``.

        With ``up_to_id`` only rows with id <= ``up_to_id`` are marked (the
        watermark from ``pending_context_batch``). ``None`` marks all unconsumed
        rows (legacy behaviour) — callers holding an empty batch (watermark
        None) should skip consumption rather than pass None.
        """

        def go() -> None:
            if up_to_id is None:
                self.conn.execute(
                    "UPDATE pending_context SET consumed_by = ? "
                    "WHERE issue_id = ? AND consumed_by IS NULL",
                    (turn_id, issue_id),
                )
            else:
                self.conn.execute(
                    "UPDATE pending_context SET consumed_by = ? "
                    "WHERE issue_id = ? AND consumed_by IS NULL AND id <= ?",
                    (turn_id, issue_id, up_to_id),
                )

        self._write(go)

    # pending controls ----------------------------------------------------

    def put_control(
        self,
        request_id: str,
        kind: str,
        agent_session_id: str,
        execution_generation: int,
        choices: Sequence[Any],
        *,
        session_key: str | None = None,
    ) -> None:
        """``session_key`` is the Hermes gateway session key the request belongs to
        (needed to resolve an approval card, integration notes Q8)."""

        def go() -> None:
            self.conn.execute(
                "INSERT OR REPLACE INTO pending_controls (request_id, kind, agent_session_id, "
                "execution_generation, choices, created_at, session_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (request_id, kind, agent_session_id, execution_generation,
                 json.dumps(list(choices)), time.time(), session_key),
            )

        self._write(go)

    def control_for_session(self, agent_session_id: str) -> ControlRow | None:
        with self._lock:
            r = self._one(
                "SELECT * FROM pending_controls WHERE agent_session_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (agent_session_id,),
            )
        return self._control_row(r)

    def control(self, request_id: str) -> ControlRow | None:
        """The pending control ``request_id``, or None once answered / dropped."""
        with self._lock:
            r = self._one("SELECT * FROM pending_controls WHERE request_id = ?", (request_id,))
        return self._control_row(r)

    @staticmethod
    def _control_row(r: sqlite3.Row | None) -> ControlRow | None:
        if r is None:
            return None
        d = dict(r)
        d["choices"] = tuple(json.loads(d["choices"]))
        return ControlRow(**d)

    def drop_control(self, request_id: str) -> None:
        self._write(lambda: self.conn.execute(
            "DELETE FROM pending_controls WHERE request_id = ?", (request_id,)))

    # name caches ---------------------------------------------------------

    def cache_state(self, state_id: str, name: str) -> None:
        self._write(lambda: self.conn.execute(
            "INSERT OR REPLACE INTO states (state_id, name) VALUES (?, ?)", (state_id, name)))

    def state_name(self, state_id: str) -> str | None:
        with self._lock:
            r = self._one("SELECT name FROM states WHERE state_id = ?", (state_id,))
        return r["name"] if r else None

    def cache_label(self, label_id: str, name: str) -> None:
        self._write(lambda: self.conn.execute(
            "INSERT OR REPLACE INTO labels (label_id, name) VALUES (?, ?)", (label_id, name)))

    def label_name(self, label_id: str) -> str | None:
        with self._lock:
            r = self._one("SELECT name FROM labels WHERE label_id = ?", (label_id,))
        return r["name"] if r else None

    # webhook dedup -------------------------------------------------------

    def seen_webhook(self, webhook_id: str, now: float) -> bool:
        """Record ``webhook_id``; True if it was already seen within ``DEDUP_TTL_S``."""

        def go() -> bool:
            cutoff = now - DEDUP_TTL_S
            self.conn.execute("DELETE FROM seen_webhooks WHERE at < ?", (cutoff,))
            hit = self.conn.execute(
                "SELECT 1 FROM seen_webhooks WHERE webhook_id = ?", (webhook_id,)
            ).fetchone()
            if hit:
                return True
            self.conn.execute(
                "INSERT INTO seen_webhooks (webhook_id, at) VALUES (?, ?)", (webhook_id, now)
            )
            return False

        return self._write(go)

    def last_event_at(self) -> float | None:
        """Receive time of the newest webhook still in the dedup window (``hermes linear status``)."""
        with self._lock:
            r = self.conn.execute("SELECT MAX(at) FROM seen_webhooks").fetchone()
        return None if r is None or r[0] is None else float(r[0])

    def last_real_event_at(self) -> float | None:
        """Like ``last_event_at`` but ignoring ``hermes linear test`` probes: the newest
        webhook Linear itself delivered."""
        with self._lock:
            r = self.conn.execute(
                "SELECT MAX(at) FROM seen_webhooks WHERE substr(webhook_id, 1, ?) != ?",
                (len(TEST_WEBHOOK_PREFIX), TEST_WEBHOOK_PREFIX)).fetchone()
        return None if r is None or r[0] is None else float(r[0])

    # users ---------------------------------------------------------------

    def cached_user(self, user_id: str, now: float) -> UserRow | None:
        with self._lock:
            r = self._one("SELECT * FROM users WHERE user_id = ?", (user_id,))
        if r is None or now - r["fetched_at"] > USER_CACHE_TTL_S:
            return None
        return UserRow(**dict(r))

    def put_user(self, user_id: str, email: str | None, name: str | None, now: float) -> None:
        self._write(lambda: self.conn.execute(
            "INSERT OR REPLACE INTO users (user_id, email, name, fetched_at) VALUES (?, ?, ?, ?)",
            (user_id, email, name, now)))


__all__ = ["ControlRow", "SessionRow", "Store", "Ticket", "TurnRow", "UserRow"]
