"""Shared create/update fields (RET-121 spec §1.1): ``parse`` (sync, BAD_ARGS before any
I/O) and ``resolve`` (names to Linear ids through the directory)."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from hermes_linear import issue_fields
from hermes_linear.agent_tools import _Refusal
from hermes_linear.issue_fields import FIELDS, Resolved, parse, resolve

APP = "00000000-0000-4000-8000-0000000000a1"
OTHER_APP = "00000000-0000-4000-8000-0000000000a2"
MIKE = "00000000-0000-4000-8000-0000000000b1"
MIKA = "00000000-0000-4000-8000-0000000000b2"
TEAM = "00000000-0000-4000-8000-0000000000c1"
TEAM2 = "00000000-0000-4000-8000-0000000000c2"
PROJ = "00000000-0000-4000-8000-0000000000d1"
PROJ2 = "00000000-0000-4000-8000-0000000000d2"

USERS = [
    {"id": APP, "name": "Neo", "displayName": "neo", "email": "neo@app.linear",
     "app": True, "active": True},
    {"id": OTHER_APP, "name": "Codex", "displayName": "codex", "email": "codex@app.linear",
     "app": True, "active": True},
    {"id": MIKE, "name": "Mike Smith", "displayName": "mike", "email": "Mike@Example.com",
     "app": False, "active": True},
    {"id": MIKA, "name": "Mika", "displayName": "mike", "email": "mika@example.com",
     "app": False, "active": True},
]
TEAMS = [{"id": TEAM, "key": "RET", "name": "Retail"},
         {"id": TEAM2, "key": "OPS", "name": "Operations"}]
PROJECTS = [{"id": PROJ, "name": "hermes linear", "state": "started"},
            {"id": PROJ2, "name": "Other", "state": "planned"}]
CYCLES = [{"id": "cy1", "number": 1, "name": "Sprint A", "startsAt": "", "endsAt": ""},
          {"id": "cy2", "number": 2, "name": "Sprint B", "startsAt": "", "endsAt": "",
           "current": True},
          {"id": "cy3", "number": 3, "name": None, "startsAt": "", "endsAt": "",
           "next": True}]
MILESTONES = {PROJ: [{"id": "m1", "name": "Alpha"}, {"id": "m2", "name": "Beta"}],
              PROJ2: [{"id": "m9", "name": "Gamma"}]}


class StubClient:
    """Canned directory lists; cycles per team (absent team: cycles off, no rows)."""

    def __init__(self, cycles: dict | None = None) -> None:
        self.cycle_rows = {TEAM: CYCLES} if cycles is None else cycles
        self.calls: list[tuple[str, str]] = []

    async def viewer_id(self):
        return APP

    async def users(self):
        self.calls.append(("users", ""))
        return [dict(u) for u in USERS]

    async def teams(self):
        self.calls.append(("teams", ""))
        return [dict(t) for t in TEAMS]

    async def projects(self):
        self.calls.append(("projects", ""))
        return [dict(p) for p in PROJECTS]

    async def cycles(self, team_id):
        self.calls.append(("cycles", team_id))
        return [dict(c) for c in self.cycle_rows.get(team_id, [])]

    async def milestones(self, project_id):
        self.calls.append(("milestones", project_id))
        return [dict(m) for m in MILESTONES.get(project_id, [])]


@pytest.fixture
def client():
    return StubClient()


@pytest.fixture
def ctx(client):
    return SimpleNamespace(client=client)


async def run(ctx, parsed, team_id: str = TEAM, project_id: str | None = PROJ) -> Resolved:
    return await resolve(ctx, parsed, team_id=team_id, project_id=project_id)


async def refused(coro) -> _Refusal:
    with pytest.raises(_Refusal) as exc:
        await coro
    return exc.value


def bad(args: dict, update: bool) -> None:
    with pytest.raises(_Refusal) as exc:
        parse(args, update=update)
    assert exc.value.code == "BAD_ARGS"
    assert exc.value.extra == {}


# --- parse ---------------------------------------------------------------------------------

def test_fields_roster():
    assert FIELDS == ("assignee", "delegate", "priority", "estimate", "due_date", "cycle",
                      "milestone", "project")


def test_parse_absent_and_unknown_keys_ignored():
    assert parse({}, update=True) == {}
    assert parse({"title": "x", "labels": ["a"], "state": "Todo"}, update=False) == {}


@pytest.mark.parametrize("args, want", [
    ({"assignee": "me"}, {"assignee": "me"}),
    ({"assignee": "  Mike  "}, {"assignee": "Mike"}),
    ({"delegate": "neo"}, {"delegate": "neo"}),
    ({"project": "Other"}, {"project": "Other"}),
    ({"milestone": "Alpha"}, {"milestone": "Alpha"}),
    ({"priority": "High"}, {"priority": 2}),
    ({"priority": "none"}, {"priority": 0}),
    ({"priority": "URGENT"}, {"priority": 1}),
    ({"priority": "medium"}, {"priority": 3}),
    ({"priority": "low"}, {"priority": 4}),
    ({"priority": 4}, {"priority": 4}),
    ({"priority": 0}, {"priority": 0}),
    ({"priority": "3"}, {"priority": 3}),
    ({"estimate": 0}, {"estimate": 0}),
    ({"estimate": 8}, {"estimate": 8}),
    ({"due_date": "2026-02-28"}, {"due_date": "2026-02-28"}),
    ({"due_date": "2028-02-29"}, {"due_date": "2028-02-29"}),
    ({"cycle": 3}, {"cycle": 3}),
    ({"cycle": "3"}, {"cycle": 3}),
    ({"cycle": "Current"}, {"cycle": "Current"}),
    ({"cycle": "Sprint A"}, {"cycle": "Sprint A"}),
])
@pytest.mark.parametrize("update", [False, True])
def test_parse_good(args, want, update):
    assert parse(args, update=update) == want


@pytest.mark.parametrize("args", [
    {"assignee": 5}, {"delegate": ["neo"]}, {"project": True}, {"milestone": 1.0},
    {"priority": "5"}, {"priority": 5}, {"priority": -1}, {"priority": "highest"},
    {"priority": True}, {"priority": 2.0}, {"priority": ["high"]},
    {"estimate": -1}, {"estimate": 1.5}, {"estimate": 2.0}, {"estimate": "3"},
    {"estimate": True}, {"estimate": False},
    {"due_date": "2026-02-30"}, {"due_date": "2026-13-01"}, {"due_date": "2026-2-3"},
    {"due_date": "20260203"}, {"due_date": "2026-02-03T00:00:00"}, {"due_date": 20260203},
    {"due_date": "2026-W05-1"},
    {"cycle": True}, {"cycle": 1.0}, {"cycle": ["1"]},
])
@pytest.mark.parametrize("update", [False, True])
def test_parse_bad(args, update):
    bad(args, update)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_parse_empty_clears_on_update(value):
    args = {f: value for f in FIELDS}
    assert parse(args, update=True) == {f: None for f in FIELDS}


@pytest.mark.parametrize("value", [None, "", "   "])
def test_parse_empty_is_absent_on_create(value):
    args = {f: value for f in FIELDS + ("team",)}
    assert parse(args, update=False) == {}


def test_parse_team_on_create():
    assert parse({"team": "RET"}, update=False) == {"team": "RET"}
    bad({"team": 3}, update=False)


@pytest.mark.parametrize("value", ["RET", None, ""])
def test_parse_team_on_update_bad_args(value):
    bad({"team": value}, update=True)


# --- resolve: users ------------------------------------------------------------------------

SELF_MSG = "Only a human can delegate to this app; ask in the thread."
APP_ASSIGNEE_MSG = ("Linear turns an app assignee into a delegation; use delegate (another "
                    "agent) or ask a human.")


async def test_assignee_me_refused(ctx, client):
    exc = await refused(run(ctx, {"assignee": "me"}))
    assert (exc.code, exc.extra) == ("ASSIGNEE_IS_APP", {"message": APP_ASSIGNEE_MSG})
    assert ("users", "") not in client.calls  # `me` needs no lookup


@pytest.mark.parametrize("ref", ["Neo", "neo@app.linear", APP, "Codex", "codex", OTHER_APP])
async def test_assignee_app_user_refused(ctx, ref):
    exc = await refused(run(ctx, {"assignee": ref}))
    assert (exc.code, exc.extra) == ("ASSIGNEE_IS_APP", {"message": APP_ASSIGNEE_MSG})


async def test_assignee_human_ok(ctx):
    assert (await run(ctx, {"assignee": "Mike Smith"})).input == {"assigneeId": MIKE}


async def test_user_by_email_case_insensitive(ctx):
    assert (await run(ctx, {"assignee": "mike@EXAMPLE.com"})).input == {"assigneeId": MIKE}
    assert (await run(ctx, {"assignee": "MIKE SMITH"})).input == {"assigneeId": MIKE}
    assert (await run(ctx, {"assignee": MIKA.upper()})).input == {"assigneeId": MIKA}


async def test_user_ambiguous(ctx):
    exc = await refused(run(ctx, {"assignee": "Mike"}))  # displayName of two users
    assert exc.code == "AMBIGUOUS"
    assert {c["id"] for c in exc.extra["candidates"]} == {MIKE, MIKA}
    assert set(exc.extra["candidates"][0]) == {"id", "name", "displayName", "app"}


async def test_user_missing_candidates(ctx, client):
    exc = await refused(run(ctx, {"assignee": "Mik"}))
    assert exc.code == "USER_NOT_FOUND"
    ids = [c["id"] for c in exc.extra["candidates"]]
    assert ids[:2] == [MIKE, MIKA] and set(ids) == {APP, OTHER_APP, MIKE, MIKA}
    assert client.calls.count(("users", "")) == 1  # fetched in this call: no refetch


async def test_delegate_human_refused(ctx):
    exc = await refused(run(ctx, {"delegate": "Mike Smith"}))
    assert exc.code == "DELEGATE_NOT_AGENT"


@pytest.mark.parametrize("ref", ["me", "ME", "Neo", "neo", "neo@app.linear", APP, APP.upper()])
async def test_self_delegation_refused(ctx, ref):
    exc = await refused(run(ctx, {"delegate": ref}))
    assert (exc.code, exc.extra) == ("SELF_DELEGATION", {"message": SELF_MSG})


async def test_other_agent_delegate_ok(ctx):
    for ref in ("codex@app.linear", "Codex", OTHER_APP):
        r = await run(ctx, {"delegate": ref})
        assert r.input == {"delegateId": OTHER_APP}


def test_self_delegation_success_path_removed():
    assert not hasattr(issue_fields, "SELF_DELEGATION_WARNING")
    names = {f.name for f in dataclasses.fields(Resolved)}
    assert not names & {"starts_session", "warnings"}


# --- resolve: scalars ----------------------------------------------------------------------

async def test_scalars_pass_through(ctx, client):
    r = await run(ctx, {"priority": 2, "estimate": 0, "due_date": "2026-03-01"})
    assert r.input == {"priority": 2, "estimate": 0, "dueDate": "2026-03-01"}
    assert client.calls == []


# --- resolve: cycles -----------------------------------------------------------------------

async def test_cycle_current_next_number_name(ctx, client):
    assert (await run(ctx, {"cycle": "current"})).input == {"cycleId": "cy2"}
    assert (await run(ctx, {"cycle": "NEXT"})).input == {"cycleId": "cy3"}
    assert (await run(ctx, {"cycle": 1})).input == {"cycleId": "cy1"}
    assert (await run(ctx, {"cycle": "sprint b"})).input == {"cycleId": "cy2"}
    assert (await run(ctx, {"cycle": "cy3"})).input == {"cycleId": "cy3"}
    assert {s for k, s in client.calls if k == "cycles"} == {TEAM}


async def test_cycle_miss_candidates(ctx):
    exc = await refused(run(ctx, {"cycle": 7}))
    assert exc.code == "CYCLE_NOT_FOUND"
    assert exc.extra["candidates"][0] == {"id": "cy1", "number": 1, "name": "Sprint A"}


async def test_cycle_none_when_cycles_off(ctx):
    exc = await refused(run(ctx, {"cycle": "current"}, team_id=TEAM2))
    assert (exc.code, exc.extra) == ("CYCLE_NOT_FOUND", {"candidates": []})


# --- resolve: project, milestone, team -----------------------------------------------------

async def test_project_resolution_sets_project_id(ctx):
    r = await run(ctx, {"project": "OTHER"})
    assert r.input == {"projectId": PROJ2}
    assert r.project_id == PROJ2 and r.project == PROJECTS[1]
    assert r.team_id == TEAM and r.team is None
    same = await run(ctx, {"project": PROJ}, project_id=None)
    assert same.project_id == PROJ and same.input == {"projectId": PROJ}


async def test_project_miss(ctx):
    exc = await refused(run(ctx, {"project": "nope"}))
    assert exc.code == "PROJECT_NOT_FOUND"
    assert exc.extra["candidates"] == [{"id": PROJ, "name": "hermes linear"},
                                       {"id": PROJ2, "name": "Other"}]


async def test_milestone_in_resulting_project(ctx, client):
    r = await run(ctx, {"milestone": "beta"})
    assert r.input == {"projectMilestoneId": "m2"} and r.project_id == PROJ
    r = await run(ctx, {"project": "Other", "milestone": "gamma"})
    assert r.input == {"projectId": PROJ2, "projectMilestoneId": "m9"}
    exc = await refused(run(ctx, {"project": "Other", "milestone": "Alpha"}))
    assert exc.code == "MILESTONE_NOT_FOUND"
    assert exc.extra["candidates"] == [{"id": "m9", "name": "Gamma"}]


async def test_milestone_without_project(ctx, client):
    exc = await refused(run(ctx, {"milestone": "Alpha"}, project_id=None))
    assert (exc.code, exc.extra) == ("MILESTONE_NOT_FOUND", {"candidates": []})
    exc = await refused(run(ctx, {"project": None, "milestone": "Alpha"}))
    assert (exc.code, exc.extra) == ("MILESTONE_NOT_FOUND", {"candidates": []})
    assert not any(k == "milestones" for k, _ in client.calls)


async def test_team_by_key_name_uuid(ctx):
    for ref in ("ops", "Operations", TEAM2):
        r = await run(ctx, {"team": ref})
        assert r.input == {"teamId": TEAM2}
        assert r.team_id == TEAM2 and r.team == TEAMS[1]


async def test_team_miss(ctx):
    exc = await refused(run(ctx, {"team": "XYZ"}))
    assert exc.code == "TEAM_NOT_FOUND"
    assert exc.extra["candidates"][0] == {"id": TEAM, "key": "RET", "name": "Retail"}


async def test_cycle_resolves_in_resulting_team(ctx, client):
    client.cycle_rows = {TEAM2: [{"id": "ox", "number": 1, "name": "Ops 1", "current": True}]}
    r = await run(ctx, {"team": "OPS", "cycle": "current"})
    assert r.input == {"teamId": TEAM2, "cycleId": "ox"}


# --- resolve: clearing ---------------------------------------------------------------------

async def test_clear_on_update(ctx, client):
    parsed = parse({f: None for f in FIELDS}, update=True)
    r = await run(ctx, parsed)
    assert r.input == {"assigneeId": None, "delegateId": None, "priority": 0,
                       "estimate": None, "dueDate": None, "cycleId": None,
                       "projectMilestoneId": None, "projectId": None}
    assert r.project_id is None and r.project is None
    assert client.calls == []  # clearing reads nothing


async def test_parse_then_resolve_end_to_end(ctx):
    parsed = parse({"assignee": "mike@example.com", "delegate": "Codex", "priority": "Low",
                    "estimate": 3, "due_date": "2026-12-01", "cycle": "2",
                    "project": "hermes linear", "milestone": "Alpha", "title": "t"},
                   update=False)
    r = await run(ctx, parsed, project_id=None)
    assert r.input == {"assigneeId": MIKE, "delegateId": OTHER_APP, "priority": 4,
                       "estimate": 3, "dueDate": "2026-12-01", "cycleId": "cy2",
                       "projectId": PROJ, "projectMilestoneId": "m1"}
