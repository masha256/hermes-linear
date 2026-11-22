"""Stateful in-memory Linear workspace for agent-tool tests (Ruling S11).

``FakeWorkspace`` implements the ``LinearClient`` methods the agent tools call, with the
same names, return shapes and ``LinearError`` failures, so a test puts it on an adapter as
the client and runs real tool handlers against it. ``tests/fake_linear.py`` (HTTP, scripted
per operation) is what checks the GraphQL documents themselves; this fake checks behaviour.

Model (all ids are opaque strings; issue ids are UUID-like, identifiers are ``TEAM-n``):

- issues: ``id identifier title description state team project parent labels
  subIssueSortOrder``; ``children`` are derived from ``parent`` and returned in insertion
  order, *not* sorted (real Linear does not sort them either; ``LinearClient.issue_get``
  does, and the fake's callers must not rely on it).
- comments: ``id issue body user`` (``user`` is ``VIEWER`` for the app's own comments,
  any other id for a human); ``app_comments`` is oldest first, as the client returns it.
- states / labels: per team, ``{"id", "name", "type"}`` / ``{"id", "name"}``, plus
  workspace-level labels (``workspace_labels``, no team) that every team can apply.
- ``issue_create`` without ``stateId`` uses the team's first ``backlog``/``unstarted``
  state in workflow order. Assumption: that is the team's default issue state, which it
  is for a stock Linear team (we do not model ``Team.defaultIssueState``).

Every client call is appended to ``calls`` as ``(method, args)``; ``writes()`` filters the
mutating ones. Return shapes are the client's normalized ones (connections flattened to
lists: ``labels``, ``children``, ``project.externalLinks``). Inputs are checked against the
SDL input field names (unknown keys raise ``AssertionError``), and an unknown issue/comment
raises what ``LinearClient`` raises for Linear's ``Entity not found`` error:
``LinearError("Entity not found: Issue", code="NOT_FOUND", status=200)``. Everything
returned is a copy; mutating a result never changes the workspace.

Usage::

    ws = FakeWorkspace()
    self_id = ws.add_issue("RET-1", title="Session issue")
    ws.add_comment(self_id, "hi", user="human-1")
    adapter = LinearAdapter(..., client=ws)

Later tasks extend it (relations, documents, …); keep each addition small and listed here.
"""

from __future__ import annotations

import copy
import itertools
from typing import Any

from hermes_linear.linear_client import LinearError

VIEWER = "app-user"
TEAM = "team-1"
PROJECT = {"id": "proj-1", "name": "Hermes",
           "externalLinks": [{"label": "Spec", "url": "https://example.test/spec"}]}

DEFAULT_STATES = [
    {"id": "st-backlog", "name": "Backlog", "type": "backlog"},
    {"id": "st-todo", "name": "Todo", "type": "unstarted"},
    {"id": "st-progress", "name": "In Progress", "type": "started"},
    {"id": "st-review", "name": "In Review", "type": "started"},
    {"id": "st-done", "name": "Done", "type": "completed"},
    {"id": "st-cancel", "name": "Canceled", "type": "canceled"},
]
DEFAULT_LABELS = [
    {"id": "lb-bug", "name": "Bug"},
    {"id": "lb-feature", "name": "Feature"},
    {"id": "lb-plan", "name": "superpowers:plan"},
]
WORKSPACE_LABELS = [{"id": "lb-ws-blocked", "name": "Blocked"}]

# SDL input field names (schema.graphql): IssueCreateInput / IssueUpdateInput subsets we use.
CREATE_FIELDS = {"title", "description", "teamId", "projectId", "parentId", "labelIds",
                 "stateId", "subIssueSortOrder"}
UPDATE_FIELDS = {"title", "description", "stateId", "addedLabelIds", "removedLabelIds",
                 "labelIds", "parentId", "projectId", "subIssueSortOrder"}


def _not_found(what: str) -> LinearError:
    return LinearError(f"Entity not found: {what}", code="NOT_FOUND", status=200)


class FakeWorkspace:
    def __init__(self) -> None:
        self.issues: dict[str, dict] = {}
        self.comments: dict[str, dict] = {}
        self.states: dict[str, list[dict]] = {TEAM: [dict(s) for s in DEFAULT_STATES]}
        self.labels: dict[str, list[dict]] = {TEAM: [dict(lb) for lb in DEFAULT_LABELS]}
        self.workspace_labels: list[dict] = [dict(lb) for lb in WORKSPACE_LABELS]
        self.calls: list[tuple[str, Any]] = []
        self._ids = itertools.count(1)
        self._numbers = itertools.count(100)

    # -- seeding -------------------------------------------------------
    def add_issue(self, identifier: str | None = None, *, title: str = "", description: str = "",
                  team: str = TEAM, project: dict | None = PROJECT, parent: str | None = None,
                  labels: tuple[str, ...] = (), state: str = "st-todo",
                  sort_order: float = 0.0, uuid: str | None = None) -> str:
        """Add an issue; ``labels`` are label ids. Returns its id."""
        iid = uuid or f"uuid-{next(self._ids)}"
        self.issues[iid] = {
            "id": iid, "identifier": identifier or f"RET-{next(self._numbers)}",
            "title": title, "description": description, "team": team,
            "project": copy.deepcopy(project),
            "parent": parent, "labels": list(labels), "state": state,
            "subIssueSortOrder": sort_order,
        }
        return iid

    def add_comment(self, issue_id: str, body: str, *, user: str = VIEWER) -> str:
        cid = f"comment-{next(self._ids)}"
        self.comments[cid] = {"id": cid, "issue": self.issue(issue_id)["id"], "body": body,
                              "user": user}
        return cid

    # -- inspection ----------------------------------------------------
    def issue(self, ref: str) -> dict:
        """The stored issue for a UUID or identifier (raises NOT_FOUND)."""
        if ref in self.issues:
            return self.issues[ref]
        for i in self.issues.values():
            if i["identifier"] == ref:
                return i
        raise _not_found("Issue")

    def writes(self) -> list[tuple[str, Any]]:
        return [c for c in self.calls if c[0] in {"issue_create", "issue_update",
                                                  "comment_create", "comment_update"}]

    def default_state(self, team: str) -> str:
        """The team's first ``backlog``/``unstarted`` state (see the module docstring)."""
        return next(s["id"] for s in self.states[team] if s["type"] in {"backlog", "unstarted"})

    def _state(self, team: str, sid: str) -> dict:
        return next(s for s in self.states[team] if s["id"] == sid)

    def _label(self, team: str, lid: str) -> dict:
        found = next((lb for lb in [*self.labels.get(team, []), *self.workspace_labels]
                      if lb["id"] == lid), None)
        if found is None:
            raise LinearError("label not found", code="INVALID_INPUT")
        return found

    # -- LinearClient surface -------------------------------------------
    async def gql(self, query: str, variables: dict | None = None) -> dict:
        """Only the scope walk's ``issue(id) { id parent { id } }`` (unknown: NOT_FOUND)."""
        self.calls.append(("gql", variables))
        if "IssueParent" not in query:
            raise AssertionError(f"FakeWorkspace.gql: unsupported document {query[:40]!r}")
        i = self.issue((variables or {})["id"])
        return {"issue": {"id": i["id"], "parent": {"id": i["parent"]} if i["parent"] else None}}

    async def viewer_id(self) -> str:
        return VIEWER

    async def issue_get(self, issue_id: str) -> dict:
        self.calls.append(("issue_get", issue_id))
        i = self.issue(issue_id)
        parent = self.issues.get(i["parent"]) if i["parent"] else None
        children = [c for c in self.issues.values() if c["parent"] == i["id"]]  # unsorted
        st = self._state(i["team"], i["state"])
        return {
            "id": i["id"], "identifier": i["identifier"], "title": i["title"],
            "description": i["description"], "subIssueSortOrder": i["subIssueSortOrder"],
            "url": f"https://linear.app/x/issue/{i['identifier']}",
            "state": dict(st), "team": {"id": i["team"]}, "project": copy.deepcopy(i["project"]),
            "parent": {"id": parent["id"], "identifier": parent["identifier"]} if parent else None,
            "labels": [dict(self._label(i["team"], lid)) for lid in i["labels"]],
            "children": [
                {"id": c["id"], "identifier": c["identifier"], "title": c["title"],
                 "subIssueSortOrder": c["subIssueSortOrder"],
                 "state": {"type": self._state(c["team"], c["state"])["type"]}}
                for c in children],
        }

    def _summary(self, i: dict) -> dict:
        return {"id": i["id"], "identifier": i["identifier"], "title": i["title"],
                "url": f"https://linear.app/x/issue/{i['identifier']}"}

    async def issue_create(self, inp: dict) -> dict:
        self.calls.append(("issue_create", dict(inp)))
        assert set(inp) <= CREATE_FIELDS, set(inp) - CREATE_FIELDS
        team = inp["teamId"]
        if inp.get("parentId"):
            self.issue(inp["parentId"])
        for lid in inp.get("labelIds") or []:
            self._label(team, lid)
        project = PROJECT if inp.get("projectId") == PROJECT["id"] else None
        state = inp.get("stateId") or self.default_state(team)
        self._state(team, state)
        iid = self.add_issue(title=inp.get("title") or "", description=inp.get("description") or "",
                             team=team, project=project, parent=inp.get("parentId"),
                             labels=tuple(inp.get("labelIds") or ()), state=state,
                             sort_order=inp.get("subIssueSortOrder") or 0.0)
        return self._summary(self.issues[iid])

    async def issue_update(self, issue_id: str, inp: dict) -> dict:
        self.calls.append(("issue_update", (issue_id, dict(inp))))
        assert set(inp) <= UPDATE_FIELDS, set(inp) - UPDATE_FIELDS
        i = self.issue(issue_id)
        for key in ("title", "description"):
            if key in inp:
                i[key] = inp[key]
        if "stateId" in inp:
            self._state(i["team"], inp["stateId"])
            i["state"] = inp["stateId"]
        for lid in inp.get("addedLabelIds") or []:
            self._label(i["team"], lid)
            if lid not in i["labels"]:
                i["labels"].append(lid)
        for lid in inp.get("removedLabelIds") or []:
            if lid in i["labels"]:
                i["labels"].remove(lid)
        if "subIssueSortOrder" in inp:
            i["subIssueSortOrder"] = inp["subIssueSortOrder"]
        if "parentId" in inp:
            i["parent"] = self.issue(inp["parentId"])["id"] if inp["parentId"] else None
        return self._summary(i)

    async def comment_create(self, issue_id: str, body: str) -> str:
        self.calls.append(("comment_create", (issue_id, body)))
        return self.add_comment(issue_id, body, user=VIEWER)

    async def comment_update(self, comment_id: str, body: str) -> None:
        self.calls.append(("comment_update", (comment_id, body)))
        if comment_id not in self.comments:
            raise _not_found("Comment")
        self.comments[comment_id]["body"] = body

    async def app_comments(self, issue_id: str) -> list[dict]:
        self.calls.append(("app_comments", issue_id))
        iid = self.issue(issue_id)["id"]
        return [{"id": c["id"], "body": c["body"]} for c in self.comments.values()
                if c["issue"] == iid and c["user"] == VIEWER]

    async def team_states(self, team_id: str) -> list[dict]:
        self.calls.append(("team_states", team_id))
        return [dict(s) for s in self.states.get(team_id, [])]

    async def team_labels(self, team_id: str) -> list[dict]:
        self.calls.append(("team_labels", team_id))
        return [dict(lb) for lb in [*self.labels.get(team_id, []), *self.workspace_labels]]
