"""linear_document (RET-122 spec §1.2, §2; RET-164) through the real registry.

``FakeWorkspace`` (``ws``): RET-1 is the session issue SELF (project ``Hermes``), RET-2
(``CHILD``) its child, OTH-9 (``OTHER``) unrelated and without a project. Documents are
seeded with ``ws.add_document`` (UUID-shaped ids, 12-hex slug ids, ``/document/doc-<slug>``
URLs).
"""

from __future__ import annotations

import pytest

from fake_workspace import PROJECT, FakeWorkspace
from test_agent_tools_generic import SELF, adapter, deny_mode, loop, pin_ctx  # noqa: F401

from hermes_linear import agent_tools, tool_approval, tools_document
from hermes_linear.agent_tools import TOOL_HANDLERS, TOOL_SCHEMAS, _Refusal
from hermes_linear.linear_client import LinearError
from hermes_linear.store import Store
from hermes_linear.tool_approval import digest

CHILD, OTHER = "uuid-child", "uuid-other"
TOOL = "linear_document"


@pytest.fixture
def ws():
    w = FakeWorkspace()
    w.add_issue("RET-1", title="Session issue", uuid=SELF)
    w.add_issue("RET-2", title="Child", uuid=CHILD, parent=SELF)
    w.add_issue("OTH-9", title="Unrelated", uuid=OTHER, project=None)
    return w


def linear_document(args):
    return TOOL_HANDLERS[TOOL](args)


def _calls(ws, name):
    return [c[1] for c in ws.calls if c[0] == name]


def _requests(adapter) -> int:
    return adapter._store.conn.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0]


def _request(adapter, args):
    return adapter._store.request("sk", digest(TOOL, args))


def _approve_and_rerun(adapter, args):
    """The human's "Allow once": arm the stored request, then the identical re-call."""
    assert adapter._store.arm_request("sk", digest(TOOL, args)) is not None
    return linear_document(dict(args))


def _outside(adapter, mode="approve"):
    import dataclasses

    adapter._settings = dataclasses.replace(adapter._settings, outside_session_writes=mode)


# -- registration


def test_schema_and_registration():
    s = TOOL_SCHEMAS[TOOL]
    assert s["name"] == TOOL and "Linear app" in s["description"]
    assert "APPROVAL_REQUIRED" in s["description"] and "ALREADY_EXISTS" in s["description"]
    props = s["parameters"]["properties"]
    assert list(props) == ["action", "id", "title", "content", "issue", "project"]
    assert props["action"]["enum"] == ["get", "create", "update"]
    assert all(props[k]["type"] == "string" and props[k].get("description") for k in props)
    assert s["parameters"]["required"] == ["action"]
    assert agent_tools.linear_document is TOOL_HANDLERS[TOOL]
    assert tools_document.HANDLERS == {TOOL: TOOL_HANDLERS[TOOL]}
    assert TOOL in agent_tools.__all__ and TOOL in tool_approval.GATED_TOOLS
    assert tool_approval.FIELD_TYPES[TOOL] == {k: "string" for k in props}
    assert tools_document.DOCUMENT_CONTENT_MAX == 50_000
    assert list(TOOL_SCHEMAS)[-1] == TOOL


@pytest.mark.parametrize("raw, want", [
    ("2c967867-0f2b-4c4a-9d55-0a0b0c0d0e0f", "2c967867-0f2b-4c4a-9d55-0a0b0c0d0e0f"),
    ("2C967867-0F2B-4C4A-9D55-0A0B0C0D0E0F", "2C967867-0F2B-4C4A-9D55-0A0B0C0D0E0F"),
    ("dd7c3ea51183", "dd7c3ea51183"),
    ("  dd7c3ea51183 ", "dd7c3ea51183"),
    ("https://linear.app/ws/document/ret-122-probe-doc-dd7c3ea51183", "dd7c3ea51183"),
    ("https://linear.app/ws/document/ret-122-probe-doc-dd7c3ea51183?x=1#c", "dd7c3ea51183"),
    ("https://linear.app/ws/document/dd7c3ea51183/", "dd7c3ea51183"),
])
def test_doc_ref_accepts(raw, want):
    assert tools_document.doc_ref(raw) == want


@pytest.mark.parametrize("raw", [
    "", "  ", "doc-1", "not a ref", "RET-1",
    "https://linear.app/ws/issue/RET-1/title-dd7c3ea51183",
    "http://linear.app/ws/document/x-dd7c3ea51183",
    "https://evil.test/ws/document/x-dd7c3ea51183",
    "https://linear.app/ws/document/",
    "https://linear.app/ws/document/x-",
])
def test_doc_ref_refuses(raw):
    with pytest.raises(_Refusal) as exc:
        tools_document.doc_ref(raw)
    assert exc.value.code == "BAD_ARGS"


# -- get


def test_get_by_uuid_slug_and_url(pin_ctx, ws, adapter):
    did = ws.add_document("Spec: X", "# X", issue="RET-1")
    d = ws.documents[did]
    want = {"ok": True, "document": {
        "id": did, "title": "Spec: X", "url": d["url"], "content": "# X",
        "issue": {"identifier": "RET-1", "title": "Session issue"}, "project": None,
        "creator": "Neo", "updated_at": d["updated_at"]}}
    for ref in (did, d["slugId"], d["url"], d["url"] + "?a=b#c"):
        assert linear_document({"action": "get", "id": ref}) == want, ref
    pid = ws.add_document("Roadmap", "r", project=PROJECT["id"])
    got = linear_document({"action": "get", "id": pid})["document"]
    assert got["issue"] is None and got["project"] == {"name": "Hermes"}
    assert ws.writes() == [] and _requests(adapter) == 0  # a read never asks


def test_get_works_outside_even_when_writes_are_denied(pin_ctx, ws, adapter):
    """Spec §2.3: reads work everywhere; ``deny`` only refuses writes."""
    did = ws.add_document("Notes", "n", issue="OTH-9")
    pin_ctx(linear=False)
    _outside(adapter, "deny")
    out = linear_document({"action": "get", "id": did})
    assert out["ok"] is True and out["document"]["title"] == "Notes"
    assert linear_document({"action": "create", "title": "x", "content": "",
                            "issue": "OTH-9"}) == {"ok": False, "error": "NOT_LINEAR_SESSION"}
    assert _requests(adapter) == 0


def test_get_not_found(pin_ctx, ws):
    out = linear_document({"action": "get", "id": "0123456789ab"})
    assert out == {"ok": False, "error": "DOCUMENT_NOT_FOUND"}
    out = linear_document({"action": "update", "id": "0123456789ab", "title": "x"})
    assert out == {"ok": False, "error": "DOCUMENT_NOT_FOUND"}
    assert ws.writes() == []


def test_get_cuts_content(pin_ctx, ws):
    """Review Focus 5: a 60,000-char document comes back bounded, with the flags."""
    did = ws.add_document("Big", "x" * 59_999 + "y", issue="RET-1")
    doc = linear_document({"action": "get", "id": did})["document"]
    assert doc["content"] == "x" * 50_000
    assert doc["content_truncated"] is True and doc["content_length"] == 60_000
    small = ws.add_document("Small", "x" * 50_000, issue="RET-1")
    doc = linear_document({"action": "get", "id": small})["document"]
    assert len(doc["content"]) == 50_000
    assert "content_truncated" not in doc and "content_length" not in doc


# -- create


def test_create_on_session_ticket_no_prompt(pin_ctx, ws, adapter):
    out = linear_document({"action": "create", "title": "Notes", "content": "hi"})
    [did] = ws.documents
    assert out == {"ok": True, "document_id": did, "url": ws.documents[did]["url"],
                   "issue": "RET-1"}
    assert _calls(ws, "document_create") == [("Notes", "hi", SELF, None)]
    assert adapter._store.documents_by(SELF) == {did}
    # an in-scope issue named explicitly (a child): no prompt either; empty content is fine
    out = linear_document({"action": "create", "title": "Child notes", "content": "",
                           "issue": "RET-2"})
    assert out["ok"] is True and out["issue"] == "RET-2"
    assert ws.documents[out["document_id"]]["issue"] == CHILD
    assert adapter._store.documents_by(SELF) == {did, out["document_id"]}
    assert _requests(adapter) == 0


@pytest.mark.parametrize("title", ["Spec: Mine", "Plan: Mine", "  Spec:x", "Plan:"])
@pytest.mark.parametrize("target", [{}, {"issue": "OTH-9"}, {"project": "Hermes"}])
def test_create_reserved_sync_title_bad_args(pin_ctx, ws, adapter, title, target):
    """Ruling 11: ``Spec:`` / ``Plan:`` titles (after strip, case-sensitive) belong to the
    sync tools; refused before any read or gate, wherever the document would go."""
    out = linear_document({"action": "create", "title": title, "content": "c", **target})
    assert out["ok"] is False and out["error"] == "BAD_ARGS", out
    assert "linear_sync_spec" in out["message"] and "linear_sync_plan" in out["message"]
    assert ws.calls == [] and _requests(adapter) == 0


@pytest.mark.parametrize("title", ["spec: lower", "Specs: x", "My Spec: x", "Plan x"])
def test_create_title_not_reserved(pin_ctx, ws, adapter, title):
    out = linear_document({"action": "create", "title": title, "content": "c"})
    assert out["ok"] is True, out


def test_create_on_out_of_scope_issue_asks(pin_ctx, ws, adapter):
    args = {"action": "create", "title": "Their notes", "content": "c", "issue": "OTH-9"}
    out = linear_document(dict(args))
    assert out["ok"] is False and out["error"] == "APPROVAL_REQUIRED", out
    assert "OTH-9" in out["message"] and TOOL in out["message"]
    assert _calls(ws, "document_create") == [] and ws.writes() == []
    req = _request(adapter, args)
    assert req.targets == [OTHER] and req.tool == TOOL
    assert '\n- OTH-9 "Unrelated"\n' in req.message
    out = _approve_and_rerun(adapter, args)
    assert out["ok"] is True and out["issue"] == "OTH-9", out
    assert _calls(ws, "document_create") == [("Their notes", "c", OTHER, None)]
    # recorded against the session ticket, whatever it was attached to (spec §2.2)
    assert adapter._store.documents_by(SELF) == {out["document_id"]}
    assert _request(adapter, args) is None


def test_create_on_project_always_asks(pin_ctx, ws, adapter):
    """Q3: a project document always asks, the session ticket's own project included."""
    for n, ref in enumerate(("Hermes", PROJECT["id"])):
        args = {"action": "create", "title": f"Roadmap {ref}", "content": "r", "project": ref}
        out = linear_document(dict(args))
        assert out["error"] == "APPROVAL_REQUIRED", out
        assert len(ws.writes()) == n  # the first pass wrote nothing
        req = _request(adapter, args)
        assert req.targets == [PROJECT["id"]] and '\n- project "Hermes"\n' in req.message
        out = _approve_and_rerun(adapter, args)
        assert out == {"ok": True, "document_id": out["document_id"],
                       "url": ws.documents[out["document_id"]]["url"], "project": "Hermes"}
        assert ws.documents[out["document_id"]]["project"] == PROJECT["id"]
        assert out["document_id"] in adapter._store.documents_by(SELF)
    assert [c[3] for c in _calls(ws, "document_create")] == [PROJECT["id"]] * 2
    miss = linear_document({"action": "create", "title": "x", "content": "", "project": "Nope"})
    assert miss["error"] == "PROJECT_NOT_FOUND" and miss["candidates"]
    assert len(ws.writes()) == 2


def test_create_same_title_already_exists(pin_ctx, ws, adapter):
    """Review Focus 3: a re-run after a create that did succeed answers ALREADY_EXISTS with
    the first id; titles compare stripped and case-sensitive, on the target only."""
    args = {"action": "create", "title": "Notes", "content": "v1"}
    first = linear_document(dict(args))
    assert first["ok"] is True
    again = linear_document({**args, "title": "  Notes\n", "content": "v2"})
    assert again == {"ok": False, "error": "ALREADY_EXISTS",
                     "document_id": first["document_id"], "url": first["url"]}
    assert len(_calls(ws, "document_create")) == 1
    assert linear_document({**args, "title": "notes"})["ok"] is True  # case-sensitive
    # same title on another target is no duplicate
    assert linear_document({**args, "issue": "RET-2"})["ok"] is True
    # out of scope: the duplicate read comes before the gate, so nobody is asked
    theirs = ws.add_document("Theirs", issue="OTH-9")
    out = linear_document({"action": "create", "title": "Theirs", "content": "",
                           "issue": "OTH-9"})
    assert out == {"ok": False, "error": "ALREADY_EXISTS", "document_id": theirs,
                   "url": ws.documents[theirs]["url"]}
    proj = ws.add_document("Plan", project=PROJECT["id"])
    out = linear_document({"action": "create", "title": "Plan", "content": "",
                           "project": "Hermes"})
    assert out["error"] == "ALREADY_EXISTS" and out["document_id"] == proj
    assert len(_calls(ws, "document_create")) == 3 and _requests(adapter) == 0


@pytest.mark.parametrize("args", [
    {"action": "create", "title": "x", "content": "", "issue": "RET-1", "project": "Hermes"},
    {"action": "create", "content": "c"},
    {"action": "create", "title": "  ", "content": "c"},
    {"action": "create", "title": "x"},
    {"action": "create", "title": "x", "content": None},
    {"action": "create", "title": "x", "content": 5},
    {"action": "create", "title": 3, "content": "c"},
    {"action": "create", "title": "x", "content": "", "issue": " "},
    {"action": "create", "title": "x", "content": "", "project": ""},
])
def test_create_issue_and_project_bad_args(pin_ctx, ws, adapter, args):
    assert linear_document(args) == {"ok": False, "error": "BAD_ARGS"}
    assert ws.calls == [] and _requests(adapter) == 0


def test_create_outside_session_without_target_bad_args(pin_ctx, ws, adapter):
    pin_ctx(linear=False)
    for mode in ("approve", "allow"):
        _outside(adapter, mode)
        out = linear_document({"action": "create", "title": "x", "content": ""})
        assert out == {"ok": False, "error": "BAD_ARGS"}
    assert ws.writes() == [] and _requests(adapter) == 0
    # with a target, every outside create asks (nothing is in scope there)...
    _outside(adapter, "approve")
    args = {"action": "create", "title": "From Slack", "content": "", "issue": "RET-1"}
    out = linear_document(dict(args))
    assert out["error"] == "APPROVAL_REQUIRED" and ws.writes() == []
    assert _request(adapter, args).targets == [SELF]
    # ...and an approved one is not recorded: there is no session ticket (Ruling 1)
    out = _approve_and_rerun(adapter, args)
    assert out["ok"] is True
    assert adapter._store.conn.execute(
        "SELECT COUNT(*) FROM created_documents").fetchone()[0] == 0


def test_create_without_any_session_refused(pin_ctx, ws):
    pin_ctx(linear=False, outside=False)
    out = linear_document({"action": "create", "title": "x", "content": ""})
    assert out == {"ok": False, "error": "NOT_LINEAR_SESSION"} and ws.writes() == []


def test_create_unknown_issue_is_linear_code(pin_ctx, ws):
    out = linear_document({"action": "create", "title": "x", "content": "", "issue": "NOPE-1"})
    assert out == {"ok": False, "error": "NOT_FOUND", "message": "Entity not found: Issue"}
    assert ws.writes() == []


def test_create_linear_failure_keeps_code_and_message(pin_ctx, ws, monkeypatch):
    async def boom(*a, **k):
        raise LinearError("content too long", code="INVALID_INPUT")

    monkeypatch.setattr(ws, "document_create", boom)
    out = linear_document({"action": "create", "title": "x", "content": "c"})
    assert out == {"ok": False, "error": "INVALID_INPUT", "message": "content too long"}


def test_deny_mode_refuses_out_of_scope_create(pin_ctx, ws, adapter):
    deny_mode(adapter)
    out = linear_document({"action": "create", "title": "x", "content": "", "issue": "OTH-9"})
    assert out == {"ok": False, "error": "OUT_OF_SCOPE"} and ws.writes() == []


# -- update


def test_update_in_scope_no_prompt(pin_ctx, ws, adapter):
    did = ws.add_document("Child doc", "old", issue="RET-2")  # a human's, on a child
    out = linear_document({"action": "update", "id": did, "content": "new"})
    assert out == {"ok": True, "document_id": did, "url": ws.documents[did]["url"]}
    assert ws.documents[did]["content"] == "new"
    out = linear_document({"action": "update", "id": ws.documents[did]["url"], "title": "T"})
    assert out["ok"] is True and ws.documents[did]["title"] == "T"
    assert _calls(ws, "document_update") == [(did, {"content": "new"}), (did, {"title": "T"})]
    assert _requests(adapter) == 0


@pytest.mark.parametrize("title", ["Spec: Taken", " Plan: Taken "])
def test_update_reserved_sync_title_bad_args(pin_ctx, ws, adapter, title):
    """Ruling 11: retitling a document to ``Spec:`` / ``Plan:`` is refused the same way,
    before the document is read (no gate, no write), with or without content."""
    did = ws.add_document("Child doc", "old", issue="RET-2")
    for extra in ({}, {"content": "new"}):
        out = linear_document({"action": "update", "id": did, "title": title, **extra})
        assert out["ok"] is False and out["error"] == "BAD_ARGS", out
        assert "reserved" in out["message"]
    assert ws.calls == [] and _requests(adapter) == 0
    assert ws.documents[did]["title"] == "Child doc"


def test_update_created_document_after_restart_no_prompt(pin_ctx, ws, adapter, tmp_path):
    """Review Focus 2: a project document this ticket created is editable without a prompt
    in a later turn, after a restart (a fresh ``Store`` on the same file)."""
    path = tmp_path / "linear.db"
    adapter._store = Store.open(path)
    args = {"action": "create", "title": "Roadmap", "content": "v1", "project": "Hermes"}
    assert linear_document(dict(args))["error"] == "APPROVAL_REQUIRED"
    did = _approve_and_rerun(adapter, args)["document_id"]
    adapter._store.close()
    adapter._store = Store.open(path)  # restart
    out = linear_document({"action": "update", "id": did, "content": "v2"})
    assert out == {"ok": True, "document_id": did, "url": ws.documents[did]["url"]}
    assert ws.documents[did]["content"] == "v2" and _requests(adapter) == 0
    # one recorded for another ticket asks
    theirs = ws.add_document("Their roadmap", project=PROJECT["id"])
    adapter._store.record_document("uuid-another-ticket", theirs)
    out = linear_document({"action": "update", "id": theirs, "content": "x"})
    assert out["error"] == "APPROVAL_REQUIRED"
    assert ws.documents[theirs]["content"] == ""


def test_update_other_document_asks_with_document_target(pin_ctx, ws, adapter):
    did = ws.add_document("Their doc", "old", issue="OTH-9")
    args = {"action": "update", "id": did, "content": "new"}
    out = linear_document(dict(args))
    assert out["ok"] is False and out["error"] == "APPROVAL_REQUIRED", out
    assert "Their doc" in out["message"]
    assert _calls(ws, "document_update") == [] and ws.writes() == []
    req = _request(adapter, args)
    assert req.targets == [did]
    assert '\n- document "Their doc"\n' in req.message
    out = _approve_and_rerun(adapter, args)
    assert out == {"ok": True, "document_id": did, "url": ws.documents[did]["url"]}
    assert _calls(ws, "document_update") == [(did, {"content": "new"})]
    assert _request(adapter, args) is None
    # a project document nobody recorded asks too
    proj = ws.add_document("Project doc", project=PROJECT["id"])
    out = linear_document({"action": "update", "id": proj, "title": "x"})
    assert out["error"] == "APPROVAL_REQUIRED"
    # deny mode refuses
    deny_mode(adapter)
    assert linear_document({"action": "update", "id": proj, "title": "y"}) == {
        "ok": False, "error": "OUT_OF_SCOPE"}
    assert len(ws.writes()) == 1


def test_update_outside_every_document_is_a_target(pin_ctx, ws, adapter):
    did = ws.add_document("Session doc", "old", issue="RET-1")
    adapter._store.record_document(SELF, did)
    pin_ctx(linear=False)
    _outside(adapter, "approve")
    args = {"action": "update", "id": did, "content": "from slack"}
    out = linear_document(dict(args))
    assert out["error"] == "APPROVAL_REQUIRED" and ws.writes() == []
    assert _request(adapter, args).targets == [did]
    assert _approve_and_rerun(adapter, args)["ok"] is True
    assert ws.documents[did]["content"] == "from slack"


def test_update_sends_only_title_content(pin_ctx, ws, monkeypatch):
    did = ws.add_document("Doc", "old", issue="RET-1")
    sent = []
    real = ws.document_update

    async def spy(doc_id, fields):
        sent.append(dict(fields))
        return await real(doc_id, fields)

    monkeypatch.setattr(ws, "document_update", spy)
    linear_document({"action": "update", "id": did, "title": "New", "content": "body"})
    linear_document({"action": "update", "id": did, "content": ""})
    assert sent == [{"title": "New", "content": "body"}, {"content": ""}]
    for fields in sent:
        assert not set(fields) & {"trashed", "hiddenAt", "issueId", "projectId"}


@pytest.mark.parametrize("extra", [{}, {"title": "  "}, {"title": None, "content": None},
                                   {"content": 3}, {"title": ["x"]}])
def test_update_needs_a_field(pin_ctx, ws, extra):
    did = ws.add_document("Doc", "old", issue="RET-1")
    ws.calls.clear()
    out = linear_document({"action": "update", "id": did, **extra})
    assert out == {"ok": False, "error": "BAD_ARGS"} and ws.calls == []


@pytest.mark.parametrize("args", [
    {"action": "get", "id": "dd7c3ea51183", "project": "Hermes"},
    {"action": "get", "id": "dd7c3ea51183", "title": "x"},
    {"action": "get", "id": "dd7c3ea51183", "content": "x"},
    {"action": "get", "id": "dd7c3ea51183", "issue": "RET-1"},
    {"action": "get"},
    {"action": "create", "id": "dd7c3ea51183", "title": "x", "content": ""},
    {"action": "update", "id": "dd7c3ea51183", "title": "x", "issue": "RET-1"},
    {"action": "update", "id": "dd7c3ea51183", "title": "x", "project": "Hermes"},
    {"action": "update", "title": "x"},
    {"action": "get", "id": "dd7c3ea51183", "foo": "bar"},
    {"id": "dd7c3ea51183"},
    {"action": "delete", "id": "dd7c3ea51183"},
    {"action": "GET", "id": "dd7c3ea51183"},
    {"action": None, "id": "dd7c3ea51183"},
])
def test_foreign_arg_bad_args(pin_ctx, ws, adapter, args):
    assert linear_document(args) == {"ok": False, "error": "BAD_ARGS"}
    assert ws.calls == [] and _requests(adapter) == 0


@pytest.mark.parametrize("args", [None, "x", [], 5])
def test_non_dict_args_never_raise(pin_ctx, ws, args):
    assert linear_document(args) == {"ok": False, "error": "BAD_ARGS"}
