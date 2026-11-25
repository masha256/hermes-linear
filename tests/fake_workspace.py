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
- issues carry ``creator`` (a user id, ``VIEWER`` for the app; ``add_issue`` defaults to
  ``HUMAN``, ``issue_create`` sets ``VIEWER``); children expose it as ``creator {id}``.
- ``normalize=True`` mimics probe P8: stored *descriptions* are markdown-normalized
  (``- item`` -> ``* item``, a blank line inserted before a list); titles and comment
  bodies are kept byte-exact, as live Linear does.
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

- uploads / attachments (RET-134): ``file_upload`` stores the bytes under a fake asset URL;
  ``attachment_create`` / ``attachment_link_url`` / ``attachment_link_github_pr`` store
  ``{id, issue, url, title, subtitle}`` and return ``{id, url}`` (duplicates not modelled).

- directory lists and issue fields (RET-121): ``teams`` / ``users`` / ``projects`` /
  ``cycles`` / ``milestones`` answer from ``*_rows`` (``add_team`` / ``add_project`` seed
  more); ``issue_create`` / ``issue_update`` store a known ``projectId`` as the issue's
  ``project`` and the other new input keys as given under ``issues[id]["fields"]``.

- relations (RET-141): ``issue_relations`` / ``relation_create`` over ``relations``, as
  Linear answered the live probe: one relation per issue pair; an identical create (or
  ``related`` either way) returns the existing id, any other type or direction replaces it.

- documents and projects (RET-163): ``documents`` (id -> ``{id, slugId, title, content,
  url, issue, project, creator, updated_at}``; ``add_document`` seeds one) behind
  ``document_get`` (UUID or slugId; unknown is ``None``) / ``documents_on`` /
  ``document_create`` / ``document_update`` (``title``/``content`` only, else
  ``AssertionError``); ``issue_details`` lists the issue's documents. ``project_detail``
  reads ``project_meta`` (status, health, lead, target date per project), ``project_links``,
  ``milestones_rows`` + ``milestone_meta`` and ``project_updates``. As probed: a duplicate
  document title or link URL is accepted; a duplicate milestone name on create raises
  Linear's ``INPUT_ERROR`` "name not unique" with its ``userPresentableMessage`` as
  ``detail``. ``project_update_create`` sets the project's health when given.
  ``milestone_update`` as probed (RET-166): ``targetDate: null`` clears, ``description``
  null or ``""`` leaves the description unchanged.

Later tasks extend it; keep each addition small and listed here.
"""

from __future__ import annotations

import copy
import itertools
import re
from typing import Any

from hermes_linear.linear_client import LinearError

VIEWER = "app-user"
HUMAN = "human-1"
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

#: The directory lists (RET-121): ``teams`` / ``users`` / ``projects`` rows as the client
#: returns them; ``cycles`` per team and ``milestones`` per project (empty by default).
DEFAULT_TEAMS = [{"id": TEAM, "key": "RET", "name": "Retail"}]
DEFAULT_USERS = [
    {"id": VIEWER, "name": "Neo", "displayName": "neo", "email": "neo@app.linear",
     "app": True, "active": True},
    {"id": HUMAN, "name": "Mike Smith", "displayName": "mike", "email": "mike@example.com",
     "app": False, "active": True},
]
DEFAULT_PROJECTS = [{"id": PROJECT["id"], "name": PROJECT["name"], "state": "started"}]

#: Issue fields (RET-121) the fake stores as given under ``issues[id]["fields"]``.
_FIELD_KEYS = ("assigneeId", "delegateId", "priority", "estimate", "dueDate", "cycleId",
               "projectMilestoneId")

# SDL input field names (schema.graphql): IssueCreateInput / IssueUpdateInput subsets we use.
CREATE_FIELDS = {"title", "description", "teamId", "projectId", "parentId", "labelIds",
                 "stateId", "subIssueSortOrder", *_FIELD_KEYS}
UPDATE_FIELDS = {"title", "description", "stateId", "addedLabelIds", "removedLabelIds",
                 "labelIds", "parentId", "projectId", "subIssueSortOrder", *_FIELD_KEYS}


_BULLET_RE = re.compile(r"^(\s*)- ", re.MULTILINE)


def normalize_markdown(text: str) -> str:
    """Probe P8's description normalization: ``- x`` -> ``* x``, blank line before lists."""
    out: list[str] = []
    for line in _BULLET_RE.sub(r"\1* ", text).split("\n"):
        is_item = line.lstrip().startswith("* ")
        prev = out[-1] if out else ""
        if is_item and prev.strip() and not prev.lstrip().startswith("* "):
            out.append("")
        out.append(line)
    return "\n".join(out)


def _not_found(what: str) -> LinearError:
    return LinearError(f"Entity not found: {what}", code="NOT_FOUND", status=200)


class FakeWorkspace:
    def __init__(self, *, normalize: bool = False) -> None:
        self.normalize = normalize
        self.issues: dict[str, dict] = {}
        self.comments: dict[str, dict] = {}
        self.states: dict[str, list[dict]] = {TEAM: [dict(s) for s in DEFAULT_STATES]}
        self.labels: dict[str, list[dict]] = {TEAM: [dict(lb) for lb in DEFAULT_LABELS]}
        self.workspace_labels: list[dict] = [dict(lb) for lb in WORKSPACE_LABELS]
        self.teams_rows: list[dict] = [dict(t) for t in DEFAULT_TEAMS]
        self.users_rows: list[dict] = [dict(u) for u in DEFAULT_USERS]
        self.projects_rows: list[dict] = [dict(p) for p in DEFAULT_PROJECTS]
        self.cycles_rows: dict[str, list[dict]] = {}
        self.milestones_rows: dict[str, list[dict]] = {}
        self.uploads: dict[str, dict] = {}  # asset URL -> {name, content_type, data}
        self.attachments: dict[str, dict] = {}  # id -> {id, issue, url, title, subtitle}
        self.relations: dict[str, dict] = {}  # id -> {id, type, issue, related}
        self.documents: dict[str, dict] = {}  # id -> see the module docstring
        self.project_meta: dict[str, dict] = {}  # project id -> {status, health, lead, ...}
        self.project_links: dict[str, list[dict]] = {
            PROJECT["id"]: [{"id": f"link-{n}", **lk}
                            for n, lk in enumerate(PROJECT["externalLinks"])]}
        self.milestone_meta: dict[str, dict] = {}  # id -> {project, description, targetDate}
        self.project_updates: dict[str, list[dict]] = {}  # project id -> oldest first
        self.calls: list[tuple[str, Any]] = []
        self._ids = itertools.count(1)
        self._numbers = itertools.count(100)

    # -- seeding -------------------------------------------------------
    def add_issue(self, identifier: str | None = None, *, title: str = "", description: str = "",
                  team: str = TEAM, project: dict | None = PROJECT, parent: str | None = None,
                  labels: tuple[str, ...] = (), state: str = "st-todo",
                  sort_order: float = 0.0, uuid: str | None = None,
                  creator: str | None = HUMAN) -> str:
        """Add an issue; ``labels`` are label ids. Returns its id."""
        iid = uuid or f"uuid-{next(self._ids)}"
        self.issues[iid] = {
            "id": iid, "identifier": identifier or f"RET-{next(self._numbers)}",
            "title": title, "description": description, "team": team,
            "project": copy.deepcopy(project),
            "parent": parent, "labels": list(labels), "state": state,
            "subIssueSortOrder": sort_order, "creator": creator, "fields": {},
        }
        return iid

    def add_team(self, team_id: str, key: str, name: str = "") -> None:
        """Another team, with the default states and labels."""
        self.teams_rows.append({"id": team_id, "key": key, "name": name or key})
        self.states[team_id] = [dict(s) for s in DEFAULT_STATES]
        self.labels[team_id] = [dict(lb) for lb in DEFAULT_LABELS]

    def add_project(self, project_id: str, name: str) -> dict:
        """Another project; returns the ``{id, name}`` an issue's ``project`` holds."""
        self.projects_rows.append({"id": project_id, "name": name, "state": "planned"})
        return {"id": project_id, "name": name}

    def _project(self, project_id: str | None) -> dict | None:
        if not project_id:
            return None
        if project_id == PROJECT["id"]:
            return copy.deepcopy(PROJECT)
        row = next((p for p in self.projects_rows if p["id"] == project_id), None)
        if row is None:
            raise _not_found("Project")
        return {"id": row["id"], "name": row["name"]}

    def add_comment(self, issue_id: str, body: str, *, user: str = VIEWER,
                    parent: str | None = None, resolved_at: str | None = None) -> str:
        cid = f"comment-{next(self._ids)}"
        self.comments[cid] = {"id": cid, "issue": self.issue(issue_id)["id"], "body": body,
                              "user": user, "parent": parent, "resolved_at": resolved_at}
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
        return [c for c in self.calls if c[0] in {
            "issue_create", "issue_update", "comment_create", "comment_update", "file_upload",
            "attachment_create", "attachment_link_url", "attachment_link_github_pr",
            "relation_create", "comment_resolve", "document_create", "document_update",
            "project_update_create", "milestone_create", "milestone_update",
            "project_link_create"}]

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
            "creator": {"id": i["creator"]} if i.get("creator") else None,
            "url": f"https://linear.app/x/issue/{i['identifier']}",
            "state": dict(st), "team": {"id": i["team"]}, "project": copy.deepcopy(i["project"]),
            "parent": {"id": parent["id"], "identifier": parent["identifier"]} if parent else None,
            "labels": [dict(self._label(i["team"], lid)) for lid in i["labels"]],
            "children": [
                {"id": c["id"], "identifier": c["identifier"], "title": c["title"],
                 "subIssueSortOrder": c["subIssueSortOrder"],
                 "state": {"type": self._state(c["team"], c["state"])["type"]},
                 "creator": {"id": c["creator"]} if c.get("creator") else None}
                for c in children],
        }

    async def issue_details(self, issue_id: str) -> dict:
        """``LinearClient.issue_details`` for an issue with none of those fields set."""
        self.calls.append(("issue_details", issue_id))
        iid = self.issue(issue_id)["id"]
        return {"assignee": None, "delegate": None, "priority": 0,
                "priorityLabel": "No priority", "estimate": None, "dueDate": None,
                "cycle": None, "projectMilestone": None, "relations": {"nodes": []},
                "inverseRelations": {"nodes": []}, "attachments": {"nodes": []},
                "documents": {"nodes": [self._doc_row(d) for d in self.documents.values()
                                        if d["issue"] == iid]}}

    def _desc(self, text: str) -> str:
        return normalize_markdown(text) if self.normalize else text

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
        project = self._project(inp.get("projectId"))
        state = inp.get("stateId") or self.default_state(team)
        self._state(team, state)
        iid = self.add_issue(title=inp.get("title") or "",
                             description=self._desc(inp.get("description") or ""),
                             team=team, project=project, parent=inp.get("parentId"),
                             labels=tuple(inp.get("labelIds") or ()), state=state,
                             sort_order=inp.get("subIssueSortOrder") or 0.0, creator=VIEWER)
        self.issues[iid]["fields"] = {k: inp[k] for k in _FIELD_KEYS if k in inp}
        return self._summary(self.issues[iid])

    async def issue_update(self, issue_id: str, inp: dict) -> dict:
        self.calls.append(("issue_update", (issue_id, dict(inp))))
        assert set(inp) <= UPDATE_FIELDS, set(inp) - UPDATE_FIELDS
        i = self.issue(issue_id)
        if "title" in inp:
            i["title"] = inp["title"]
        if "description" in inp:
            i["description"] = self._desc(inp["description"])
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
        if "projectId" in inp:
            i["project"] = self._project(inp["projectId"])
        i["fields"].update({k: inp[k] for k in _FIELD_KEYS if k in inp})
        return self._summary(i)

    async def comment_create(self, issue_id: str, body: str,
                             parent_id: str | None = None) -> str:
        if parent_id is None:
            self.calls.append(("comment_create", (issue_id, body)))
            return self.add_comment(issue_id, body, user=VIEWER)
        self.calls.append(("comment_create", (issue_id, body, parent_id)))
        parent = self.comments.get(parent_id)
        # Linear refuses a reply's id as parentId (live probe RET-142: "incorrect parent").
        if parent is None or parent["parent"] or parent["issue"] != self.issue(issue_id)["id"]:
            raise LinearError("incorrect parent", code="INPUT_ERROR", status=200)
        return self.add_comment(issue_id, body, user=VIEWER, parent=parent_id)

    async def comment_get(self, comment_id: str) -> dict | None:
        self.calls.append(("comment_get", comment_id))
        c = self.comments.get(comment_id)
        if c is None:
            return None
        return {"id": c["id"], "issue_id": c["issue"], "parent_id": c["parent"],
                "resolved_at": c["resolved_at"]}

    async def comment_resolve(self, comment_id: str, resolving_comment_id: str) -> None:
        self.calls.append(("comment_resolve", (comment_id, resolving_comment_id)))
        if comment_id not in self.comments or resolving_comment_id not in self.comments:
            raise _not_found("Comment")
        self.comments[comment_id]["resolved_at"] = "2026-11-24T00:00:00.000Z"

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

    # -- directory lists (RET-121) ---------------------------------------
    async def teams(self) -> list[dict]:
        self.calls.append(("teams", None))
        return [dict(t) for t in self.teams_rows]

    async def users(self) -> list[dict]:
        self.calls.append(("users", None))
        return [dict(u) for u in self.users_rows]

    async def projects(self) -> list[dict]:
        self.calls.append(("projects", None))
        return [dict(p) for p in self.projects_rows]

    async def cycles(self, team_id: str) -> list[dict]:
        self.calls.append(("cycles", team_id))
        return [dict(c) for c in self.cycles_rows.get(team_id, [])]

    async def milestones(self, project_id: str) -> list[dict]:
        self.calls.append(("milestones", project_id))
        return [dict(m) for m in self.milestones_rows.get(project_id, [])]

    # -- uploads and attachments (RET-134) ------------------------------
    async def file_upload(self, name: str, content_type: str, data: bytes) -> str:
        self.calls.append(("file_upload", (name, content_type, data)))
        url = f"https://uploads.linear.app/fake/{next(self._ids)}/{name}"
        self.uploads[url] = {"name": name, "content_type": content_type, "data": data}
        return url

    def _attach(self, issue_id: str, url: str, title: str | None,
                subtitle: str | None = None) -> dict:
        aid = f"att-{next(self._ids)}"
        self.attachments[aid] = {"id": aid, "issue": self.issue(issue_id)["id"], "url": url,
                                 "title": title, "subtitle": subtitle}
        return {"id": aid, "url": url}

    async def attachment_create(self, issue_id: str, title: str, subtitle: str | None,
                                url: str) -> dict:
        self.calls.append(("attachment_create", (issue_id, title, subtitle, url)))
        return self._attach(issue_id, url, title, subtitle)

    async def attachment_link_url(self, issue_id: str, url: str, title: str | None) -> dict:
        self.calls.append(("attachment_link_url", (issue_id, url, title)))
        return self._attach(issue_id, url, title)

    async def attachment_link_github_pr(self, issue_id: str, url: str) -> dict:
        self.calls.append(("attachment_link_github_pr", (issue_id, url)))
        return self._attach(issue_id, url, None)

    # -- relations (RET-141) --------------------------------------------
    def add_relation(self, issue_id: str, related_id: str, type_: str) -> str:
        """Seed (or, like Linear, replace) the one relation between two issues."""
        a, b = self.issue(issue_id)["id"], self.issue(related_id)["id"]
        for rid, r in list(self.relations.items()):
            if {r["issue"], r["related"]} == {a, b}:
                if (r["issue"], r["related"], r["type"]) == (a, b, type_) or (
                        type_ == "related" and r["type"] == "related"):
                    return rid  # live probe: an identical create returns the same relation
                del self.relations[rid]  # live probe: another type/direction replaces it
        rid = f"rel-{next(self._ids)}"
        self.relations[rid] = {"id": rid, "type": type_, "issue": a, "related": b}
        return rid

    async def issue_relations(self, issue_id: str) -> list[dict]:
        self.calls.append(("issue_relations", issue_id))
        iid = self.issue(issue_id)["id"]
        return [{"id": r["id"], "type": r["type"], "issue_id": r["issue"],
                 "related_id": r["related"]}
                for r in self.relations.values() if iid in (r["issue"], r["related"])]

    async def relation_create(self, issue_id: str, related_id: str, type_: str) -> str:
        self.calls.append(("relation_create", (issue_id, related_id, type_)))
        assert type_ in {"blocks", "related", "duplicate", "similar"}, type_
        return self.add_relation(issue_id, related_id, type_)

    # -- documents and projects (RET-163) -------------------------------
    def add_document(self, title: str, content: str = "", *, issue: str | None = None,
                     project: str | None = None, creator: str = VIEWER) -> str:
        """Seed a document on an issue (UUID or identifier) or a project; returns its id."""
        n = next(self._ids)
        # UUID-shaped like Linear's (``linear_document`` refuses an id that is neither a
        # UUID nor a slug id); the slug id is 12 hex digits, as live ones are.
        did, slug = f"00000000-0000-4000-8000-{n:012x}", f"{n:012x}"
        self.documents[did] = {
            "id": did, "slugId": slug, "title": title, "content": content,
            "url": f"https://linear.app/x/document/doc-{slug}",
            "issue": self.issue(issue)["id"] if issue else None,
            "project": self._proj(project)["id"] if project else None,
            "creator": creator, "updated_at": "2026-11-25T00:00:00.000Z"}
        return did

    @staticmethod
    def _doc_row(d: dict) -> dict:
        return {"id": d["id"], "title": d["title"], "url": d["url"]}

    def _proj(self, project_id: str | None) -> dict:
        """``_project`` for a required project: a missing or unknown one is NOT_FOUND."""
        p = self._project(project_id)
        if p is None:
            raise _not_found("Project")
        return p

    def _user_name(self, uid: str | None) -> str | None:
        return next((u["name"] for u in self.users_rows if u["id"] == uid), None)

    def _doc(self, ref: str) -> dict | None:
        return self.documents.get(ref) or next(
            (d for d in self.documents.values() if d["slugId"] == ref), None)

    async def document_get(self, ref: str) -> dict | None:
        self.calls.append(("document_get", ref))
        d = self._doc(ref)
        if d is None:
            return None
        issue = self.issues[d["issue"]] if d["issue"] else None
        project = self._proj(d["project"]) if d["project"] else None
        return {"id": d["id"], "title": d["title"], "url": d["url"], "content": d["content"],
                "issue": ({"id": issue["id"], "identifier": issue["identifier"],
                           "title": issue["title"]} if issue else None),
                "project": {"id": project["id"], "name": project["name"]} if project else None,
                "creator": self._user_name(d["creator"]), "updated_at": d["updated_at"]}

    async def documents_on(self, issue_id: str | None = None,
                           project_id: str | None = None) -> list[dict]:
        self.calls.append(("documents_on", (issue_id, project_id)))
        if (issue_id is None) == (project_id is None):
            raise ValueError("documents_on needs exactly one of issue_id and project_id")
        if issue_id is not None:
            key, ref = "issue", self.issue(issue_id)["id"]
        else:
            key, ref = "project", self._proj(project_id)["id"]
        return [self._doc_row(d) for d in self.documents.values() if d[key] == ref]

    async def document_create(self, title: str, content: str, *, issue_id: str | None,
                              project_id: str | None) -> dict:
        self.calls.append(("document_create", (title, content, issue_id, project_id)))
        assert not (issue_id and project_id), "issue_id and project_id"
        did = self.add_document(title, content, issue=issue_id, project=project_id)
        return {"id": did, "url": self.documents[did]["url"]}

    async def document_update(self, doc_id: str, fields: dict) -> dict:
        self.calls.append(("document_update", (doc_id, dict(fields))))
        assert fields and set(fields) <= {"title", "content"}, set(fields)
        d = self._doc(doc_id)
        if d is None:
            raise _not_found("Document")
        d.update(fields)
        return {"id": d["id"], "url": d["url"]}

    def _meta(self, project_id: str) -> dict:
        return self.project_meta.setdefault(project_id, {
            "status": "In Progress", "health": None, "lead": None, "targetDate": None})

    async def project_detail(self, project_id: str, updates: int) -> dict:
        self.calls.append(("project_detail", (project_id, updates)))
        p = self._proj(project_id)
        pid, meta = p["id"], self._meta(p["id"])
        ups = list(reversed(self.project_updates.get(pid, [])))[:updates]
        return {
            "id": pid, "name": p["name"], "url": f"https://linear.app/x/project/{pid}",
            "state": meta["status"], "health": meta["health"], "lead": meta["lead"],
            "target_date": meta["targetDate"],
            "links": [dict(lk) for lk in self.project_links.get(pid, [])],
            "milestones": [{"id": m["id"], "name": m["name"],
                            "target_date": self.milestone_meta.get(m["id"], {}).get("targetDate")}
                           for m in self.milestones_rows.get(pid, [])],
            "documents": [self._doc_row(d) for d in self.documents.values()
                          if d["project"] == pid],
            "updates": [{k: u[k] for k in ("id", "created_at", "author", "health", "body")}
                        for u in ups],
        }

    async def project_update_create(self, project_id: str, body: str,
                                    health: str | None) -> dict:
        self.calls.append(("project_update_create", (project_id, body, health)))
        assert health in (None, "onTrack", "atRisk", "offTrack"), health
        pid = self._proj(project_id)["id"]
        uid = f"pu-{next(self._ids)}"
        rows = self.project_updates.setdefault(pid, [])
        # Live probe (RET-165): an update without health takes the project's current health
        # (the update reads it back too); the project's health stays as it was.
        if health is None:
            health = self._meta(pid)["health"]
        rows.append({"id": uid, "created_at": f"2026-11-25T00:00:{len(rows):02d}.000Z",
                     "author": self._user_name(VIEWER), "health": health, "body": body})
        self._meta(pid)["health"] = health
        return {"id": uid, "url": f"https://linear.app/x/project/{pid}/updates#{uid}"}

    @staticmethod
    def _dup_milestone(name: str) -> LinearError:
        return LinearError("name not unique", code="INPUT_ERROR", status=200,
                           detail=f"A project milestone called '{name}' already exists "
                                  "on this project.")

    def _milestone_out(self, m: dict) -> dict:
        return {"id": m["id"], "name": m["name"],
                "target_date": self.milestone_meta[m["id"]].get("targetDate")}

    async def milestone_create(self, project_id: str, name: str, description: str | None,
                               target_date: str | None) -> dict:
        self.calls.append(("milestone_create", (project_id, name, description, target_date)))
        pid = self._proj(project_id)["id"]
        rows = self.milestones_rows.setdefault(pid, [])
        if any(m["name"] == name for m in rows):
            raise self._dup_milestone(name)
        m = {"id": f"ms-{next(self._ids)}", "name": name}
        rows.append(m)
        self.milestone_meta[m["id"]] = {"project": pid, "description": description,
                                        "targetDate": target_date}
        return self._milestone_out(m)

    async def milestone_update(self, milestone_id: str, fields: dict) -> dict:
        self.calls.append(("milestone_update", (milestone_id, dict(fields))))
        assert fields and set(fields) <= {"name", "description", "targetDate"}, set(fields)
        m = next((m for rows in self.milestones_rows.values() for m in rows
                  if m["id"] == milestone_id), None)
        if m is None:
            raise _not_found("ProjectMilestone")
        meta = self.milestone_meta.setdefault(m["id"], {"description": None,
                                                        "targetDate": None})
        if "name" in fields:
            m["name"] = fields["name"]
        if "targetDate" in fields:  # null clears (RET-166 probe)
            meta["targetDate"] = fields["targetDate"]
        if fields.get("description"):  # null or "" leaves it unchanged (RET-166 probe)
            meta["description"] = fields["description"]
        return self._milestone_out(m)

    async def project_link_create(self, project_id: str, url: str, label: str | None) -> dict:
        self.calls.append(("project_link_create", (project_id, url, label)))
        pid = self._proj(project_id)["id"]
        lid = f"link-{next(self._ids)}"
        self.project_links.setdefault(pid, []).append(
            {"id": lid, "label": url if label is None else label, "url": url})
        return {"id": lid}
