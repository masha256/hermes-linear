"""The name directory (RET-121 spec §2.2): TTL cache, refetch-once on a miss, refusals that
carry candidates."""

from __future__ import annotations

import pytest

from hermes_linear import directory
from hermes_linear.agent_tools import _Refusal, resolve_state
from hermes_linear.directory import (CANDIDATES_MAX, DIRECTORY_TTL_S, LABELS_AVAILABLE_MAX,
                                     Directory, for_client)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class StubClient:
    """Answers each list call with the next queued answer (the last one repeats)."""

    def __init__(self, **answers: list[list[dict]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str]] = []

    def _next(self, kind: str, scope: str = "") -> list[dict]:
        self.calls.append((kind, scope))
        queue = self.answers[kind]
        return [dict(r) for r in (queue.pop(0) if len(queue) > 1 else queue[0])]

    async def teams(self):
        return self._next("teams")

    async def team_states(self, team_id):
        return self._next("states", team_id)

    async def team_labels(self, team_id):
        return self._next("labels", team_id)

    async def users(self):
        return self._next("users")

    async def cycles(self, team_id):
        return self._next("cycles", team_id)

    async def projects(self):
        return self._next("projects")

    async def milestones(self, project_id):
        return self._next("milestones", project_id)

    def fetches(self, kind: str) -> int:
        return sum(1 for k, _ in self.calls if k == kind)


STATES = [{"id": "s1", "name": "Todo", "type": "unstarted"},
          {"id": "s2", "name": "In Progress", "type": "started"},
          {"id": "s3", "name": "Done", "type": "completed"}]


def by_state(rows: list[dict], ref: str) -> dict | None:
    sid = resolve_state(rows, ref)
    return next((r for r in rows if r["id"] == sid), None)


def by_name(rows: list[dict], ref: str) -> dict | list[dict] | None:
    hits = [r for r in rows if r["name"].casefold() == ref.casefold()]
    return hits[0] if len(hits) == 1 else (hits or None)


def test_constants():
    assert (DIRECTORY_TTL_S, CANDIDATES_MAX, LABELS_AVAILABLE_MAX) == (300, 20, 100)


async def test_hit_cached_within_ttl():
    client, clock = StubClient(states=[STATES]), Clock()
    d = Directory(client, clock=clock)
    assert await d.rows("states", "t1") == STATES
    clock.t += DIRECTORY_TTL_S
    assert await d.rows("states", "t1") == STATES
    assert client.calls == [("states", "t1")]


async def test_rows_are_copies():
    d = Directory(StubClient(states=[STATES]), clock=Clock())
    (await d.rows("states", "t1"))[0]["name"] = "mutated"
    assert (await d.rows("states", "t1"))[0]["name"] == "Todo"


async def test_expired_after_ttl():
    client, clock = StubClient(states=[STATES]), Clock()
    d = Directory(client, clock=clock)
    await d.rows("states", "t1")
    clock.t += DIRECTORY_TTL_S + 1
    await d.rows("states", "t1")
    assert client.fetches("states") == 2


async def test_entries_keyed_by_scope():
    client = StubClient(states=[STATES])
    d = Directory(client, clock=Clock())
    await d.rows("states", "t1")
    await d.rows("states", "t2")
    await d.rows("states", "t1")
    assert client.calls == [("states", "t1"), ("states", "t2")]


async def test_miss_refetches_once():
    new = {"id": "s4", "name": "New", "type": "unstarted"}
    client = StubClient(states=[STATES, [*STATES, new]])
    d = Directory(client, clock=Clock())
    await d.rows("states", "t1")  # cached, without "New"
    assert await d.resolve("states", "t1", "new", by_state) == new
    assert client.fetches("states") == 2
    assert await d.rows("states", "t1") == [*STATES, new]  # the refetch is cached
    assert client.fetches("states") == 2


async def test_miss_after_refetch_refuses_with_candidates():
    rows = [{"id": f"s{i}", "name": f"State {i}", "type": "unstarted"} for i in range(30)]
    rows[25]["name"] = "Limbo-ish"
    rows[27]["name"] = "Pre-limbo"
    client = StubClient(states=[rows])
    d = Directory(client, clock=Clock())
    await d.rows("states", "t1")
    with pytest.raises(_Refusal) as ei:
        await d.resolve("states", "t1", "LIMBO", by_state)
    assert ei.value.code == "STATE_NOT_FOUND"
    cands = ei.value.extra["candidates"]
    assert len(cands) == CANDIDATES_MAX
    assert [c["name"] for c in cands[:3]] == ["Limbo-ish", "Pre-limbo", "State 0"]
    assert cands[0] == {"id": "s25", "name": "Limbo-ish", "type": "unstarted"}
    assert client.fetches("states") == 2


async def test_no_double_refetch_after_fresh_fetch():
    client = StubClient(states=[STATES])
    d = Directory(client, clock=Clock())
    with pytest.raises(_Refusal) as ei:
        await d.resolve("states", "t1", "Limbo", by_state)
    assert ei.value.code == "STATE_NOT_FOUND"
    assert client.fetches("states") == 1


async def test_expired_entry_miss_fetches_once():
    client, clock = StubClient(states=[STATES]), Clock()
    d = Directory(client, clock=clock)
    await d.rows("states", "t1")
    clock.t += DIRECTORY_TTL_S + 1
    with pytest.raises(_Refusal):
        await d.resolve("states", "t1", "Limbo", by_state)
    assert client.fetches("states") == 2  # the expiry refetch counts as this call's fetch


async def test_ambiguous():
    users = [{"id": "u1", "name": "Sam", "displayName": "sam", "email": "a@x", "app": False,
              "active": True},
             {"id": "u2", "name": "Sam", "displayName": "sammy", "email": "b@x", "app": True,
              "active": True},
             {"id": "u3", "name": "Kim", "displayName": "kim", "email": "c@x", "app": False,
              "active": True}]
    client = StubClient(users=[users])
    d = Directory(client, clock=Clock())
    with pytest.raises(_Refusal) as ei:
        await d.resolve("users", "", "sam", by_name)
    assert ei.value.code == "AMBIGUOUS"
    assert ei.value.extra == {"candidates": [
        {"id": "u1", "name": "Sam", "displayName": "sam", "app": False},
        {"id": "u2", "name": "Sam", "displayName": "sammy", "app": True}]}
    assert client.fetches("users") == 1  # ambiguous is not a miss: no refetch


@pytest.mark.parametrize("kind,code,scope", [
    ("teams", "TEAM_NOT_FOUND", ""), ("users", "USER_NOT_FOUND", ""),
    ("cycles", "CYCLE_NOT_FOUND", "t1"), ("projects", "PROJECT_NOT_FOUND", ""),
    ("milestones", "MILESTONE_NOT_FOUND", "p1")])
async def test_miss_codes_and_candidate_shapes(kind, code, scope):
    row = {"teams": {"id": "t1", "key": "RET", "name": "Retail"},
           "users": {"id": "u1", "name": "Sam", "displayName": "sam", "email": "s@x",
                     "app": False, "active": True},
           "cycles": {"id": "c1", "number": 4, "name": "", "startsAt": "a", "endsAt": "b",
                      "current": True},
           "projects": {"id": "p1", "name": "Orion", "state": "started"},
           "milestones": {"id": "m1", "name": "Beta"}}[kind]
    shape = {"teams": ["id", "key", "name"], "users": ["id", "name", "displayName", "app"],
             "cycles": ["id", "number", "name"], "projects": ["id", "name"],
             "milestones": ["id", "name"]}[kind]
    d = Directory(StubClient(**{kind: [[row]]}), clock=Clock())
    with pytest.raises(_Refusal) as ei:
        await d.resolve(kind, scope, "zzz", lambda rows, ref: None)
    assert ei.value.code == code
    assert ei.value.extra == {"candidates": [{k: row[k] for k in shape}]}


async def test_substring_matches_other_fields():
    users = [{"id": "u1", "name": "Ann", "displayName": "ann", "email": "ann@x", "app": False,
              "active": True},
             {"id": "u2", "name": "Bob", "displayName": "bobby", "email": "rob@x",
              "app": False, "active": True}]
    d = Directory(StubClient(users=[users]), clock=Clock())
    with pytest.raises(_Refusal) as ei:
        await d.resolve("users", "", "rob@", lambda rows, ref: None)
    assert [c["id"] for c in ei.value.extra["candidates"]] == ["u2", "u1"]


async def test_labels_miss_has_available():
    labels = [{"id": f"l{i}", "name": f"Label {i}"} for i in range(120)]
    client = StubClient(labels=[labels])
    d = Directory(client, clock=Clock())
    with pytest.raises(_Refusal) as ei:
        await d.resolve("labels", "t1", "nope", by_name)
    assert ei.value.code == "LABEL_MISSING"
    assert set(ei.value.extra) == {"available"}
    assert ei.value.extra["available"] == [f"Label {i}" for i in range(LABELS_AVAILABLE_MAX)]


async def test_per_client_isolation():
    a, b = StubClient(states=[STATES]), StubClient(states=[STATES[:1]])
    assert for_client(a) is for_client(a)
    assert for_client(a) is not for_client(b)
    assert await for_client(a).rows("states", "t1") == STATES
    assert await for_client(b).rows("states", "t1") == STATES[:1]
    assert a.fetches("states") == 1 and b.fetches("states") == 1


def test_for_client_holds_clients_weakly():
    c = StubClient(states=[STATES])
    for_client(c)
    assert len(directory._DIRECTORIES) >= 1
    n = len(directory._DIRECTORIES)
    del c
    import gc
    gc.collect()
    assert len(directory._DIRECTORIES) == n - 1
