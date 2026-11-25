"""The RET-163 additions to ``FakeWorkspace`` answer like ``LinearClient`` (shapes, errors)."""

from __future__ import annotations

import asyncio

import pytest

from fake_workspace import PROJECT, FakeWorkspace

from hermes_linear.linear_client import LinearError


def run(coro):
    return asyncio.run(coro)


def test_documents_roundtrip():
    ws = FakeWorkspace()
    iid = ws.add_issue("RET-1", title="T")
    made = run(ws.document_create("Spec", "# s", issue_id=iid, project_id=None))
    assert run(ws.documents_on(issue_id="RET-1")) == [
        {"id": made["id"], "title": "Spec", "url": made["url"], "creator_id": "app-user"}]
    human = ws.add_document("Notes", issue=iid, creator="human-1")
    assert run(ws.documents_on(issue_id=iid))[-1]["creator_id"] == "human-1"
    del ws.documents[human]
    slug = ws.documents[made["id"]]["slugId"]
    got = run(ws.document_get(slug))
    assert got["issue"] == {"id": iid, "identifier": "RET-1", "title": "T"}
    assert got["project"] is None and got["creator"] == "Neo"
    assert run(ws.document_get("000000000000")) is None
    run(ws.document_update(made["id"], {"content": "new"}))
    assert ws.documents[made["id"]]["content"] == "new"
    with pytest.raises(AssertionError):
        run(ws.document_update(made["id"], {"trashed": True}))


def test_project_writes_show_in_detail():
    ws = FakeWorkspace()
    pid = PROJECT["id"]
    run(ws.project_update_create(pid, "first", None))
    run(ws.project_update_create(pid, "second", "atRisk"))
    m = run(ws.milestone_create(pid, "Beta", None, "2026-12-31"))
    run(ws.project_link_create(pid, "https://e.test/r", None))
    run(ws.project_link_create(pid, "https://e.test/r", "repo"))  # duplicates are accepted
    with pytest.raises(LinearError) as ei:
        run(ws.milestone_create(pid, "Beta", None, None))
    assert ei.value.code == "INPUT_ERROR"
    assert ei.value.detail == "A project milestone called 'Beta' already exists on this project."
    run(ws.milestone_update(m["id"], {"targetDate": None}))
    d = run(ws.project_detail(pid, 1))
    assert [u["body"] for u in d["updates"]] == ["second"] and d["health"] == "atRisk"
    assert d["milestones"] == [{"id": m["id"], "name": "Beta", "target_date": None}]
    assert [lk["label"] for lk in d["links"]] == ["Spec", "https://e.test/r", "repo"]
    assert run(ws.project_detail(pid, 0))["updates"] == []
    assert {c[0] for c in ws.writes()} == {
        "project_update_create", "milestone_create", "milestone_update", "project_link_create"}
