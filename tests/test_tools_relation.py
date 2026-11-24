"""linear_relation (RET-121 spec §1.4; RET-141) through the real registry.

``FakeWorkspace`` (``ws``): RET-1 is the session issue SELF, RET-2 (``CHILD``) and RET-3
(``CHILD2``) its children, OTH-9 (``OTHER``) and OTH-8 (``OTHER2``) unrelated. Its relation
model follows the live probe on RET-92 (2026-11-24): one relation per issue pair; an
identical create returns the existing relation, another type or direction replaces it.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from conftest import REPO_ROOT
from test_agent_tools_generic import SELF, adapter, deny_mode, loop, pin_ctx  # noqa: F401
from fake_workspace import FakeWorkspace

from hermes_linear import agent_tools, tool_approval, tools_relation
from hermes_linear.agent_tools import TOOL_HANDLERS, TOOL_SCHEMAS
from hermes_linear.linear_client import LinearError
from hermes_linear.tool_approval import digest

CHILD, CHILD2, OTHER, OTHER2 = "uuid-child", "uuid-child2", "uuid-other", "uuid-other2"
ROSTER = ["linear_issue_get", "linear_issue_create", "linear_issue_update", "linear_comment",
          "linear_sync_spec", "linear_sync_plan", "linear_task_state", "linear_query",
          "linear_search", "linear_attach", "linear_session_link", "linear_relation",
          "linear_lookup"]


@pytest.fixture
def ws():
    w = FakeWorkspace()
    w.add_issue("RET-1", title="Session issue", uuid=SELF)
    w.add_issue("RET-2", title="Child", uuid=CHILD, parent=SELF)
    w.add_issue("RET-3", title="Child two", uuid=CHILD2, parent=SELF)
    w.add_issue("OTH-9", title="Unrelated", uuid=OTHER, project=None)
    w.add_issue("OTH-8", title="Unrelated two", uuid=OTHER2, project=None)
    return w


def linear_relation(args):
    return TOOL_HANDLERS["linear_relation"](args)


def _creates(ws):
    return [c[1] for c in ws.calls if c[0] == "relation_create"]


def _rels(ws):
    return sorted((r["issue"], r["related"], r["type"]) for r in ws.relations.values())


# -- registration


def test_schema_and_registration():
    s = TOOL_SCHEMAS["linear_relation"]
    assert s["name"] == "linear_relation" and "Linear app" in s["description"]
    assert "RELATION_CONFLICT" in s["description"] and "replaces" not in s["description"]
    assert "APPROVAL_REQUIRED" in s["description"]
    props = s["parameters"]["properties"]
    assert list(props) == ["id", "type", "other"]
    assert props["type"]["enum"] == list(tools_relation.RELATION_TYPES)
    assert all(props[k]["type"] == "string" and props[k].get("description") for k in props)
    assert s["parameters"]["required"] == ["id", "type", "other"]
    assert tools_relation.RELATION_TYPES == ("blocks", "blocked_by", "related", "duplicate_of")
    assert agent_tools.linear_relation is TOOL_HANDLERS["linear_relation"]
    assert tools_relation.HANDLERS == {"linear_relation": TOOL_HANDLERS["linear_relation"]}
    assert "linear_relation" in agent_tools.__all__
    assert "linear_relation" in tool_approval.GATED_TOOLS
    assert list(TOOL_SCHEMAS) == ROSTER and list(TOOL_HANDLERS) == ROSTER


@pytest.mark.parametrize("imports", [
    "import hermes_linear.tools_relation; from hermes_linear import agent_tools",
    "import hermes_linear.tools_attach; from hermes_linear import agent_tools",
    "import hermes_linear.tools_read; from hermes_linear import agent_tools",
    "import hermes_linear.tools_lookup; from hermes_linear import agent_tools",
    "from hermes_linear import agent_tools; import hermes_linear.tools_relation",
])
def test_roster_order_any_import_order(imports):
    """Ruling R3: whichever module comes first, the roster order is fixed and complete."""
    code = (f"{imports}; print(list(agent_tools.TOOL_SCHEMAS) == list(agent_tools.TOOL_HANDLERS),"
            " ','.join(agent_tools.TOOL_SCHEMAS), all(agent_tools.TOOL_HANDLERS.values()),"
            " all(agent_tools.TOOL_SCHEMAS.values()))")
    proc = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["True", ",".join(ROSTER), "True", "True"]


# -- each type's direction and Linear type


@pytest.mark.parametrize("type_, expect", [
    ("blocks", (CHILD, CHILD2, "blocks")),
    ("blocked_by", (CHILD2, CHILD, "blocks")),
    ("related", (CHILD, CHILD2, "related")),
    ("duplicate_of", (CHILD, CHILD2, "duplicate")),
])
def test_each_type_direction(pin_ctx, ws, type_, expect):
    out = linear_relation({"id": "RET-2", "type": type_, "other": "RET-3"})
    [rid] = ws.relations
    assert out == {"ok": True, "relation_id": rid, "issue": "RET-2", "other": "RET-3",
                   "type": type_}
    assert _creates(ws) == [expect] and _rels(ws) == [expect]


def test_already_related_same_direction(pin_ctx, ws):
    rid = ws.add_relation(CHILD, CHILD2, "blocks")
    out = linear_relation({"id": "RET-2", "type": "blocks", "other": "RET-3"})
    assert out == {"ok": False, "error": "ALREADY_RELATED", "relation_id": rid}
    out = linear_relation({"id": "RET-3", "type": "blocked_by", "other": "RET-2"})
    assert out == {"ok": False, "error": "ALREADY_RELATED", "relation_id": rid}
    assert ws.writes() == []


def _conflict(rid, type_, direction):
    return {"ok": False, "error": "RELATION_CONFLICT",
            "existing": {"relation_id": rid, "type": type_, "direction": direction}}


def test_already_related_blocks_direction(pin_ctx, ws):
    """Ruling R9: the reverse ``blocks`` is not a match. Linear would replace it, so it is
    RELATION_CONFLICT (direction relative to ``id``) and nothing is written."""
    old = ws.add_relation(CHILD2, CHILD, "blocks")
    out = linear_relation({"id": "RET-2", "type": "blocks", "other": "RET-3"})
    assert out == _conflict(old, "blocks", "inbound")
    out = linear_relation({"id": "RET-3", "type": "blocked_by", "other": "RET-2"})
    assert out == _conflict(old, "blocks", "outbound")
    assert ws.writes() == [] and _rels(ws) == [(CHILD2, CHILD, "blocks")]


def test_already_related_related_either_way(pin_ctx, ws):
    rid = ws.add_relation(CHILD2, CHILD, "related")
    for a, b in (("RET-2", "RET-3"), ("RET-3", "RET-2")):
        out = linear_relation({"id": a, "type": "related", "other": b})
        assert out == {"ok": False, "error": "ALREADY_RELATED", "relation_id": rid}
    assert ws.writes() == []


def test_already_related_duplicate_direction(pin_ctx, ws):
    old = ws.add_relation(CHILD2, CHILD, "duplicate")  # RET-3 is the duplicate of RET-2
    out = linear_relation({"id": "RET-2", "type": "duplicate_of", "other": "RET-3"})
    assert out == _conflict(old, "duplicate", "inbound")
    assert ws.writes() == [] and _rels(ws) == [(CHILD2, CHILD, "duplicate")]


@pytest.mark.parametrize("seed, type_, direction", [
    ((CHILD, CHILD2, "related"), "blocks", "outbound"),
    ((CHILD2, CHILD, "related"), "duplicate_of", "inbound"),
    ((CHILD, CHILD2, "blocks"), "related", "outbound"),
    ((CHILD, CHILD2, "blocks"), "duplicate_of", "outbound"),
    ((CHILD2, CHILD, "duplicate"), "blocked_by", "inbound"),
    ((CHILD, CHILD2, "similar"), "related", "outbound"),  # Linear's own type
    ((CHILD2, CHILD, "similar"), "blocks", "inbound"),
])
def test_other_type_is_a_conflict(pin_ctx, ws, seed, type_, direction):
    old = ws.add_relation(*seed)
    out = linear_relation({"id": "RET-2", "type": type_, "other": "RET-3"})
    assert out == _conflict(old, seed[2], direction)
    assert ws.writes() == [] and _rels(ws) == [seed]


def test_relation_with_a_third_issue_is_no_conflict(pin_ctx, ws):
    ws.add_relation(CHILD, SELF, "related")
    out = linear_relation({"id": "RET-2", "type": "blocks", "other": "RET-3"})
    assert out["ok"] is True and _creates(ws) == [(CHILD, CHILD2, "blocks")]


def test_existing_relations_read_from_id(pin_ctx, ws):
    linear_relation({"id": "RET-2", "type": "blocks", "other": "RET-3"})
    assert [c[1] for c in ws.calls if c[0] == "issue_relations"] == [CHILD]


# -- refusals


def test_self_bad_args(pin_ctx, ws):
    for other in ("RET-2", CHILD):
        out = linear_relation({"id": "RET-2", "type": "related", "other": other})
        assert out == {"ok": False, "error": "BAD_ARGS"}
    assert ws.writes() == []


@pytest.mark.parametrize("args", [
    None, {}, {"id": "RET-2", "type": "blocks"}, {"id": "RET-2", "other": "RET-3"},
    {"type": "blocks", "other": "RET-3"}, {"id": "RET-2", "type": "blocks", "other": " "},
    {"id": 3, "type": "blocks", "other": "RET-3"}, {"id": "RET-2", "type": 1, "other": "RET-3"},
])
def test_bad_args(pin_ctx, ws, args):
    assert linear_relation(args) == {"ok": False, "error": "BAD_ARGS"}
    assert ws.calls == []


@pytest.mark.parametrize("type_", ["duplicate", "blocked-by", "Blocks", "similar", ""])
def test_unknown_type_bad_args(pin_ctx, ws, type_):
    out = linear_relation({"id": "RET-2", "type": type_, "other": "RET-3"})
    assert out == {"ok": False, "error": "BAD_ARGS"}
    assert ws.calls == []


def test_unknown_issue_is_linear_code(pin_ctx, ws):
    out = linear_relation({"id": "RET-2", "type": "blocks", "other": "NOPE-1"})
    assert out == {"ok": False, "error": "NOT_FOUND", "message": "Entity not found: Issue"}
    assert ws.writes() == []


def test_needs_linear_session(pin_ctx, ws):
    pin_ctx(linear=False)
    out = linear_relation({"id": "RET-2", "type": "blocks", "other": "RET-3"})
    assert out == {"ok": False, "error": "NOT_LINEAR_SESSION"} and ws.writes() == []


def test_linear_failure_keeps_code_and_message(pin_ctx, ws, monkeypatch):
    async def boom(*a):
        raise LinearError("relation refused", code="INPUT_ERROR")

    monkeypatch.setattr(ws, "relation_create", boom)
    out = linear_relation({"id": "RET-2", "type": "blocks", "other": "RET-3"})
    assert out == {"ok": False, "error": "INPUT_ERROR", "message": "relation refused"}


# -- the gate: both ends are approval targets


@pytest.mark.parametrize("args, targets", [
    ({"id": "RET-2", "type": "blocks", "other": "OTH-9"}, [OTHER]),
    ({"id": "OTH-9", "type": "blocked_by", "other": "RET-2"}, [OTHER]),
    ({"id": "OTH-9", "type": "related", "other": "OTH-8"}, [OTHER, OTHER2]),
])
def test_out_of_scope_other_needs_approval_writes_nothing(pin_ctx, ws, adapter, args, targets):
    out = linear_relation(dict(args))
    assert out["ok"] is False and out["error"] == "APPROVAL_REQUIRED", out
    assert "linear_relation" in out["message"]
    assert ws.writes() == []
    req = adapter._store.request("sk", digest("linear_relation", args))
    assert req is not None and req.targets == targets and req.tool == "linear_relation"


@pytest.mark.parametrize("args, direction", [
    ({"id": "RET-2", "type": "blocks", "other": "OTH-9"}, "inbound"),
    ({"id": "OTH-9", "type": "related", "other": "RET-2"}, "outbound"),
])
def test_out_of_scope_conflict_needs_no_approval(pin_ctx, ws, adapter, args, direction):
    """The conflict is a read before ``_check_gate``: refused without asking anyone."""
    old = ws.add_relation(OTHER, CHILD, "blocks")
    assert linear_relation(dict(args)) == _conflict(old, "blocks", direction)
    assert ws.writes() == []
    assert adapter._store.request("sk", digest("linear_relation", args)) is None


def test_approved_rerun_writes_once(pin_ctx, ws, adapter):
    args = {"id": "RET-2", "type": "blocks", "other": "OTH-9"}
    assert linear_relation(dict(args))["error"] == "APPROVAL_REQUIRED"
    dg = digest("linear_relation", args)
    assert adapter._store.arm_request("sk", dg) is not None
    out = linear_relation(dict(args))
    assert out["ok"] is True and out["other"] == "OTH-9"
    assert _creates(ws) == [(CHILD, OTHER, "blocks")]
    assert adapter._store.request("sk", dg) is None
    # an identical call afterwards: the relation exists (a read), so nothing is written
    # and nobody is asked
    assert linear_relation(dict(args)) == {"ok": False, "error": "ALREADY_RELATED",
                                           "relation_id": out["relation_id"]}
    assert len(_creates(ws)) == 1 and adapter._store.request("sk", dg) is None


def test_approval_covers_only_the_recorded_targets(pin_ctx, ws, adapter):
    """An approved pass whose args now resolve to another out-of-scope issue is refused."""
    args = {"id": "RET-2", "type": "blocks", "other": "OTH-9"}
    linear_relation(dict(args))
    adapter._store.arm_request("sk", digest("linear_relation", args))
    ws.issues[OTHER]["identifier"], ws.issues[OTHER2]["identifier"] = "OTH-X", "OTH-9"
    assert linear_relation(dict(args)) == {"ok": False, "error": "OUT_OF_SCOPE"}
    assert ws.writes() == []


def test_deny_mode_refuses_out_of_scope(pin_ctx, ws, adapter):
    deny_mode(adapter)
    out = linear_relation({"id": "OTH-9", "type": "related", "other": "RET-2"})
    assert out == {"ok": False, "error": "OUT_OF_SCOPE"} and ws.writes() == []


def test_in_scope_never_asks(pin_ctx, ws, adapter):
    out = linear_relation({"id": "RET-3", "type": "related", "other": "RET-1"})
    assert out["ok"] is True
    assert adapter._store.request("sk", digest("linear_relation", {
        "id": "RET-3", "type": "related", "other": "RET-1"})) is None
