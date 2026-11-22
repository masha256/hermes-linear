"""linear_sync_plan (RET-78): the plan helpers in ``sync.py`` and the tool itself.

Same harness as ``test_sync_spec.py``: ``FakeWorkspace`` on a real background loop, context
pinned with ``pin_ctx``. The session issue ``RET-1`` (``SELF``) is the project ticket.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
from pathlib import Path

import pytest

from fake_workspace import FakeWorkspace
from test_agent_tools_runtime import new_adapter

from hermes_linear import agent_tools, sync
from hermes_linear.agent_tools import TOOL_HANDLERS, TOOL_SCHEMAS, linear_sync_plan
from hermes_linear.linear_client import LinearClient
from hermes_linear.sync import (PLAN_COMMENT_MAX, PlanTask, parse_plan, render_plan,
                                task_title, write_ticket_ids)

SELF = "uuid-self"
FIXTURE = Path(__file__).parent / "fixtures" / "plans" / "channel-plan.md"
MARKER = "<!-- hermes-linear:plan -->"

PLAN = """# Thing — Implementation Plan

Linear: RET-1

**Goal:** build the thing.

### Task 1: Alpha

Do alpha first.

**Files:**
- Create: `a.py`

- [ ] **Step 1:** write it.

### Task 2: Beta

**Files:**
- Modify: `b.py`

Beta summary paragraph
continues here.

### Task 3: Gamma

Gamma things.
"""


@pytest.fixture
def loop():
    lp = asyncio.new_event_loop()
    t = threading.Thread(target=lp.run_forever, name="GATEWAY-LOOP", daemon=True)
    t.start()
    yield lp
    lp.call_soon_threadsafe(lp.stop)
    t.join(5)
    lp.close()


@pytest.fixture
def ws():
    w = FakeWorkspace()
    w.add_issue("RET-1", title="Project ticket", uuid=SELF)
    w.add_issue("OTH-9", title="Unrelated", uuid="uuid-other", project=None)
    return w


@pytest.fixture
def adapter(loop, ws):
    return new_adapter(loop, client=ws)


@pytest.fixture
def pin_ctx(monkeypatch, adapter):
    def pin(*, linear=True, issue=SELF, key="sk", ad=adapter):
        ctx = agent_tools.ToolContext(adapter=ad, client=getattr(ad, "client", None),
                                      session_issue_id=issue if linear else None,
                                      session_key=key if linear else None, is_linear=linear)
        monkeypatch.setattr(agent_tools, "resolve_context", lambda session_id=None: ctx)
        return ctx
    pin()
    return pin


@pytest.fixture
def plan(tmp_path):
    f = tmp_path / "plan.md"
    f.write_text(PLAN, encoding="utf-8")
    return f


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", "-C", str(repo), *args],
        check=True, capture_output=True, text=True).stdout.strip()


def children(ws: FakeWorkspace, parent: str = SELF) -> list[dict]:
    """The parent's children as ``LinearClient.issue_get`` orders them."""
    return sorted((c for c in ws.issues.values() if c["parent"] == parent),
                  key=LinearClient._child_order)


def by_title(ws: FakeWorkspace, prefix: str) -> dict:
    found = [i for i in ws.issues.values() if i["title"].startswith(prefix)]
    assert len(found) == 1, found
    return found[0]


def link(p: Path) -> str:
    """The plan link the tool uses (tmp_path may sit inside some git checkout)."""
    return sync.source_info(p).link


# ----------------------------------------------------------------- sync.py (pure)


def test_parse_fixture_15_tasks():
    tasks = parse_plan(FIXTURE.read_text(encoding="utf-8"))
    assert [t.number for t in tasks] == list(range(15))
    assert [t.ticket for t in tasks] == [
        "RET-46", "RET-63", "RET-64", "RET-49", "RET-47", "RET-48", "RET-55", "RET-50",
        "RET-53", "RET-54", "RET-51", "RET-65", "RET-66", "RET-67", "RET-59"]
    assert tasks[0].name == "Dev environment, skeleton, fixtures"
    assert tasks[7].name == "Router — agent session events and lifecycle"  # inner em dash
    assert tasks[0].line == 70
    assert tasks[1].summary.startswith("Answers spec §12 items 1–2")
    assert "**Files:**" not in tasks[1].summary
    assert all(isinstance(t, PlanTask) for t in tasks)


def test_parse_plan_summary_and_lines():
    tasks = parse_plan(PLAN)
    assert [(t.number, t.name, t.ticket) for t in tasks] == [
        (1, "Alpha", None), (2, "Beta", None), (3, "Gamma", None)]
    assert tasks[0].summary == "Do alpha first."
    assert tasks[1].summary == "Beta summary paragraph\ncontinues here."  # Files skipped
    assert tasks[2].summary == "Gamma things."
    assert PLAN.splitlines()[tasks[0].line - 1] == "### Task 1: Alpha"
    assert parse_plan("### Task 1: Empty\n\n**Files:**\n- x\n")[0].summary == ""


def test_parse_ignores_fenced_and_malformed_headings():
    text = ("### Task 1: Real\n\n```md\n### Task 9: In a fence — RET-9\n```\n\n"
            "#### Task 2: Too deep\n### Task x: Not a number\n### Task 3:\n")
    assert [t.number for t in parse_plan(text)] == [1]


def test_heading_with_trailing_text_keeps_id():
    (t,) = parse_plan("### Task 6: Skill — RET-81 (absorbs RET-82)\n\nBody.\n")
    assert (t.number, t.name, t.ticket) == (6, "Skill", "RET-81")
    (t,) = parse_plan("### Task 6: Skill — RET-81 — RET-99  \n")
    assert t.ticket == "RET-81"  # the first ID token wins
    (t,) = parse_plan("### Task 6: Skill —RET-81\n")
    assert t.ticket is None  # no whitespace: not an ID suffix


def test_write_ids_only_changes_headings():
    text = ("# P\r\n\r\nLinear: RET-1\r\n\r\n### Task 1: Alpha\r\n\r\nBody — RET-7\r\n\r\n"
            "### Task 2: Beta — RET-5 (keep this)\r\n\r\n```\r\n### Task 1: Fenced\r\n```\r\n"
            "### Task 3: Gamma — RET-6\r\n### Task 4: Delta  \r\n")
    out = write_ticket_ids(text, {1: "RET-10", 2: "RET-11", 4: "RET-12"})
    old, new = text.splitlines(keepends=True), out.splitlines(keepends=True)
    assert len(old) == len(new)
    changed = {i: (a, b) for i, (a, b) in enumerate(zip(old, new)) if a != b}
    assert changed == {
        4: ("### Task 1: Alpha\r\n", "### Task 1: Alpha — RET-10\r\n"),
        8: ("### Task 2: Beta — RET-5 (keep this)\r\n",
            "### Task 2: Beta — RET-11 (keep this)\r\n"),
        14: ("### Task 4: Delta  \r\n", "### Task 4: Delta — RET-12  \r\n"),
    }
    assert write_ticket_ids(text, {}) == text
    assert write_ticket_ids(out, {1: "RET-10", 2: "RET-11", 4: "RET-12"}) == out
    assert parse_plan(out)[0].ticket == "RET-10"


def test_task_title():
    assert task_title(PlanTask(3, "Gamma", None, "", 1)) == "T3 · Gamma"


def test_render_plan_has_footer():
    assert render_plan(PLAN, "docs/plan.md", "abc1234") == (
        PLAN.rstrip() + "\n\n---\nSource: docs/plan.md @ abc1234 · edit by asking in this thread")


# ----------------------------------------------------------------- linear_sync_plan


def test_schema_and_handler():
    s = TOOL_SCHEMAS["linear_sync_plan"]
    assert s["name"] == "linear_sync_plan" and "Linear app" in s["description"]
    assert s["parameters"]["required"] == ["path"]
    assert "Absolute path" in s["parameters"]["properties"]["path"]["description"]
    assert TOOL_HANDLERS["linear_sync_plan"] is linear_sync_plan


def test_sync_creates_children_in_order(pin_ctx, ws, plan, adapter):
    out = linear_sync_plan({"path": str(plan)})
    assert out["ok"] is True, out
    kids = children(ws)
    assert [k["title"] for k in kids] == ["T1 · Alpha", "T2 · Beta", "T3 · Gamma"]
    assert [k["subIssueSortOrder"] for k in kids] == [10, 20, 30]
    ids = [k["identifier"] for k in kids]
    assert out["issue"] == "RET-1" and out["url"] == "https://linear.app/x/issue/RET-1"
    assert out["tasks"] == {1: ids[0], 2: ids[1], 3: ids[2]}
    assert out["created"] == ids and out["canceled"] == []
    assert out["file_changed"] is True
    assert kids[0]["description"] == f"[Plan]({link(plan)}) · Task 1\n\nDo alpha first."
    assert kids[1]["description"] == (f"[Plan]({link(plan)}) · Task 2\n\n"
                                      "Beta summary paragraph\ncontinues here.")
    assert all(k["project"] is not None and k["team"] == "team-1" for k in kids)
    creates = [c[1] for c in ws.writes() if c[0] == "issue_create"]
    assert [c["subIssueSortOrder"] for c in creates] == [10, 20, 30]
    assert all(c["parentId"] == SELF for c in creates)
    assert adapter.tool_created["sk"] >= {k["id"] for k in kids}  # UUIDs, on the loop
    text = plan.read_text(encoding="utf-8")
    assert text == write_ticket_ids(PLAN, {1: ids[0], 2: ids[1], 3: ids[2]})
    assert [t.ticket for t in parse_plan(text)] == ids


def test_sync_plan_idempotent(pin_ctx, ws, plan):
    first = linear_sync_plan({"path": str(plan)})
    assert first["ok"] is True
    text = plan.read_text(encoding="utf-8")
    n = len(ws.calls)
    second = linear_sync_plan({"path": str(plan)})
    assert second["ok"] is True
    assert second["created"] == [] and second["canceled"] == []
    assert second["file_changed"] is False and second["tasks"] == first["tasks"]
    assert plan.read_text(encoding="utf-8") == text
    assert [c for c in ws.calls[n:] if c in ws.writes()] == []  # zero writes


def test_sync_plan_crash_before_writeback_no_duplicate(pin_ctx, ws, plan):
    first = linear_sync_plan({"path": str(plan)})
    plan.write_text(PLAN, encoding="utf-8")  # the IDs never reached the file
    n = len(ws.writes())
    again = linear_sync_plan({"path": str(plan)})
    assert again["ok"] is True and again["created"] == [] and again["canceled"] == []
    assert again["tasks"] == first["tasks"]
    assert len(children(ws)) == 3
    assert again["file_changed"] is True
    assert parse_plan(plan.read_text(encoding="utf-8"))[2].ticket == first["tasks"][3]
    assert ws.writes()[n:] == []  # adopting needs no Linear write


def test_heading_with_trailing_text_never_creates(pin_ctx, ws, tmp_path):
    ws.add_issue("RET-81", title="Skill", uuid="uuid-81", parent=SELF)
    f = tmp_path / "plan.md"
    text = "Linear: RET-1\n\n### Task 6: Skill — RET-81 (absorbs RET-82)\n\nThe skill.\n"
    f.write_text(text, encoding="utf-8")
    out = linear_sync_plan({"path": str(f)})
    assert out["ok"] is True and out["created"] == [] and out["tasks"] == {6: "RET-81"}
    assert out["file_changed"] is False and f.read_text(encoding="utf-8") == text
    assert ws.issues["uuid-81"]["title"] == "T6 · Skill"
    assert not [c for c in ws.writes() if c[0] == "issue_create"]


def test_sync_plan_renumber_keeps_ids(pin_ctx, ws, plan):
    first = linear_sync_plan({"path": str(plan)})["tasks"]
    a, b, g = first[1], first[2], first[3]
    plan.write_text(f"""# Thing

Linear: RET-1

### Task 1: Gamma renamed — {g}

Gamma things.

### Task 2: Alpha — {a}

Do alpha first.

### Task 3: Beta — {b}

**Files:**
- Modify: `b.py`

Beta summary paragraph
continues here.
""", encoding="utf-8")
    out = linear_sync_plan({"path": str(plan)})
    assert out["ok"] is True and out["created"] == [] and out["canceled"] == []
    assert out["tasks"] == {1: g, 2: a, 3: b}
    assert [(k["identifier"], k["title"]) for k in children(ws)] == [
        (g, "T1 · Gamma renamed"), (a, "T2 · Alpha"), (b, "T3 · Beta")]
    assert ws.issue(a)["description"].startswith(f"[Plan]({link(plan)}) · Task 2\n\n")
    assert out["file_changed"] is False


def test_renamed_task_without_id_adopted_by_prefix(pin_ctx, ws, plan):
    first = linear_sync_plan({"path": str(plan)})["tasks"]
    plan.write_text(PLAN.replace("### Task 2: Beta", "### Task 2: Beta prime"), encoding="utf-8")
    out = linear_sync_plan({"path": str(plan)})
    assert out["created"] == [] and out["tasks"] == first
    assert ws.issue(first[2])["title"] == "T2 · Beta prime"


def test_removed_task_canceled_with_comment(pin_ctx, ws, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin", "https://github.com/acme/widgets.git")
    f = repo / "plan.md"
    f.write_text(PLAN, encoding="utf-8")
    _git(repo, "add", "plan.md")
    _git(repo, "commit", "-q", "-m", "plan")
    head = _git(repo, "rev-parse", "--short", "HEAD")
    human = ws.add_issue(title="Notes from a human", parent=SELF)  # not a T<n> ticket
    first = linear_sync_plan({"path": str(f)})
    link = f"https://github.com/acme/widgets/blob/{head}/plan.md"
    assert ws.issue(first["tasks"][1])["description"].startswith(f"[Plan]({link}) · Task 1")
    _git(repo, "commit", "-q", "-am", "ids")
    text = f.read_text(encoding="utf-8")
    f.write_text(text[:text.index("### Task 3")], encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "drop gamma")
    head2 = _git(repo, "rev-parse", "--short", "HEAD")
    out = linear_sync_plan({"path": str(f)})
    gamma = ws.issue(first["tasks"][3])
    assert out["canceled"] == [gamma["identifier"]] and out["created"] == []
    assert gamma["state"] == "st-cancel"
    notes = [c["body"] for c in ws.comments.values() if c["issue"] == gamma["id"]]
    assert notes == [f"Removed from the plan in {head2}."]
    assert ws.issues[human]["state"] == "st-todo" and ws.issues[human]["title"] == (
        "Notes from a human")
    assert not [c for c in ws.writes() if c[0] == "issue_update" and c[1][0] == human]
    # a third run: already canceled, nothing more
    n = len(ws.writes())
    assert linear_sync_plan({"path": str(f)})["canceled"] == []
    assert ws.writes()[n:] == []


def test_sync_plan_never_touches_done(pin_ctx, ws, plan):
    first = linear_sync_plan({"path": str(plan)})["tasks"]
    done_kept, done_removed = ws.issue(first[1]), ws.issue(first[3])
    done_kept["state"] = done_removed["state"] = "st-done"
    text = plan.read_text(encoding="utf-8")
    text = text[:text.index("### Task 3")]  # Gamma (done) removed
    text = text.replace("### Task 1: Alpha", "### Task 1: Alpha renamed")  # Alpha (done) edited
    text = text.replace("Do alpha first.", "Changed summary.")
    plan.write_text(text, encoding="utf-8")
    n = len(ws.writes())
    out = linear_sync_plan({"path": str(plan)})
    assert out["ok"] is True and out["canceled"] == [] and out["created"] == []
    assert out["tasks"][1] == first[1]
    touched = {c[1][0] for c in ws.writes()[n:] if c[0] == "issue_update"}
    touched |= {c[1][0] for c in ws.writes()[n:] if c[0] == "comment_create"}
    assert done_kept["id"] not in touched and done_removed["id"] not in touched
    assert done_kept["title"] == "T1 · Alpha" and done_removed["state"] == "st-done"


def test_canceled_or_deleted_ticket_in_plan_is_recreated(pin_ctx, ws, plan):
    first = linear_sync_plan({"path": str(plan)})["tasks"]
    ws.issue(first[1])["state"] = "st-cancel"
    del ws.issues[ws.issue(first[2])["id"]]
    out = linear_sync_plan({"path": str(plan)})
    assert out["ok"] is True and len(out["created"]) == 2
    assert out["tasks"][1] not in (first[1],) and out["tasks"][2] != first[2]
    assert out["tasks"][3] == first[3]
    assert out["recreated"] == [1, 2]
    assert [t.ticket for t in parse_plan(plan.read_text(encoding="utf-8"))] == [
        out["tasks"][1], out["tasks"][2], out["tasks"][3]]
    assert [k["title"] for k in children(ws) if k["state"] != "st-cancel"] == [
        "T1 · Alpha", "T2 · Beta", "T3 · Gamma"]


def test_plan_comment_edited_in_place(pin_ctx, ws, plan):
    ws.add_comment(SELF, f"quoting {MARKER} by hand", user="human-1")
    out = linear_sync_plan({"path": str(plan)})
    assert out["comment"] == "created"
    mine = [c for c in ws.comments.values() if c["issue"] == SELF and c["user"] != "human-1"]
    assert len(mine) == 1
    body = mine[0]["body"]
    synced = plan.read_text(encoding="utf-8")
    assert body == f"{render_plan(synced, sync.source_info(plan).path, None)}\n\n{MARKER}"  # IDs included
    plan.write_text(synced.replace("Gamma things.", "Gamma changed."), encoding="utf-8")
    out = linear_sync_plan({"path": str(plan)})
    assert out["comment"] == "edited"
    mine2 = [c for c in ws.comments.values() if c["issue"] == SELF and c["user"] != "human-1"]
    assert [c["id"] for c in mine2] == [mine[0]["id"]]
    assert "Gamma changed." in mine2[0]["body"] and mine2[0]["body"].count(MARKER) == 1
    human = [c for c in ws.comments.values() if c["user"] == "human-1"]
    assert human[0]["body"] == f"quoting {MARKER} by hand"
    assert linear_sync_plan({"path": str(plan)})["comment"] == "unchanged"


def test_plan_comment_oversize_falls_back_to_task_list(pin_ctx, ws, tmp_path):
    f = tmp_path / "plan.md"
    f.write_text(PLAN + "\n" + "😀" * (PLAN_COMMENT_MAX // 2) + "\n", encoding="utf-8")
    out = linear_sync_plan({"path": str(f)})
    assert out["ok"] is True and out["comment_inline"] is False
    (c,) = [c for c in ws.comments.values() if c["issue"] == SELF]
    body = c["body"]
    assert sync.utf16_len(body) <= PLAN_COMMENT_MAX
    assert "😀" not in body and link(f) in body
    for n, ident in out["tasks"].items():
        assert f"T{n} · " in body and ident in body
    assert body.endswith(MARKER) and "edit by asking in this thread" in body


def test_order_uses_sub_issue_sort_order(pin_ctx, ws, plan):
    first = linear_sync_plan({"path": str(plan)})["tasks"]
    text = plan.read_text(encoding="utf-8")
    parts = text.split("### Task ")
    # move Gamma before Alpha, keep numbers: order follows the file, not the numbers
    reordered = parts[0] + "### Task " + parts[3].rstrip("\n") + "\n\n" + "### Task " + parts[1] \
        + "### Task " + parts[2]
    plan.write_text(reordered, encoding="utf-8")
    n = len(ws.writes())
    out = linear_sync_plan({"path": str(plan)})
    assert [k["identifier"] for k in children(ws)] == [first[3], first[1], first[2]]
    assert [k["subIssueSortOrder"] for k in children(ws)] == [10, 20, 30]
    updates = [c[1] for c in ws.writes()[n:] if c[0] == "issue_update"]
    assert all(set(inp) == {"subIssueSortOrder"} for _, inp in updates)
    assert len(updates) == 3 and out["created"] == []
    raw = asyncio.run(ws.issue_get(SELF))["children"]  # Linear's own order is arbitrary
    assert sorted(raw, key=LinearClient._child_order)[0]["identifier"] == first[3]


def test_unmanaged_and_done_children_never_adopted(pin_ctx, ws, plan):
    done = ws.add_issue(title="T1 · Alpha", parent=SELF, state="st-done")
    other = ws.add_issue(title="Tx · not a task", parent=SELF)
    out = linear_sync_plan({"path": str(plan)})
    assert out["tasks"][1] != ws.issues[done]["identifier"] and len(out["created"]) == 3
    assert ws.issues[other]["state"] == "st-todo"
    assert out["canceled"] == []


def test_heading_id_out_of_scope_refused_before_writes(pin_ctx, ws, tmp_path):
    f = tmp_path / "plan.md"
    f.write_text("Linear: RET-1\n\n### Task 1: A — OTH-9\n\n### Task 2: B\n", encoding="utf-8")
    assert linear_sync_plan({"path": str(f)}) == {"ok": False, "error": "OUT_OF_SCOPE"}
    assert ws.writes() == []


def test_heading_id_elsewhere_in_scope_is_updated(pin_ctx, ws, tmp_path):
    mid = ws.add_issue("RET-30", title="Group", uuid="uuid-mid", parent=SELF)
    ws.add_issue("RET-31", title="Deep", uuid="uuid-deep", parent=mid)
    f = tmp_path / "plan.md"
    f.write_text("Linear: RET-1\n\n### Task 1: A — RET-31\n\nA.\n", encoding="utf-8")
    out = linear_sync_plan({"path": str(f)})
    assert out["ok"] is True and out["tasks"] == {1: "RET-31"}
    assert ws.issues["uuid-deep"]["title"] == "T1 · A"
    assert ws.issues["uuid-deep"]["parent"] == mid  # not re-parented


@pytest.mark.parametrize("body", [
    "### Task 1: A\n### Task 1: B\n",  # duplicate number
    "### Task 1: A — RET-5\n### Task 2: B — RET-5\n",  # duplicate ticket
    "### Task 1: A — RET-1\n",  # the project ticket itself
    "No tasks here.\n",
])
def test_malformed_plans_refused(pin_ctx, ws, tmp_path, body):
    ws.add_issue("RET-5", title="x", parent=SELF)
    f = tmp_path / "plan.md"
    f.write_text("Linear: RET-1\n\n" + body, encoding="utf-8")
    assert linear_sync_plan({"path": str(f)}) == {"ok": False, "error": "BAD_ARGS"}
    assert ws.writes() == []


def test_file_edited_during_sync_not_overwritten(pin_ctx, ws, plan, monkeypatch):
    real = agent_tools._sync_plan

    async def edit_then_sync(ctx, a):
        plan.write_text(PLAN + "\nedited meanwhile\n", encoding="utf-8")
        return await real(ctx, a)
    monkeypatch.setattr(agent_tools, "_sync_plan", edit_then_sync)  # looked up per call
    out = linear_sync_plan({"path": str(plan)})
    assert out["ok"] is True and out["file_changed"] is False and out["file_conflict"] is True
    assert plan.read_text(encoding="utf-8").endswith("edited meanwhile\n")


def test_missing_header_and_bad_args(pin_ctx, ws, tmp_path):
    f = tmp_path / "plan.md"
    f.write_text("### Task 1: A\n", encoding="utf-8")
    assert linear_sync_plan({"path": str(f)}) == {"ok": False, "error": "NO_LINEAR_HEADER"}
    for args in (None, {}, {"path": "plan.md"}, {"path": str(tmp_path / "nope.md")}):
        assert linear_sync_plan(args) == {"ok": False, "error": "BAD_ARGS"}
    assert ws.writes() == []


def test_refused_outside_linear_session(pin_ctx, ws, plan):
    pin_ctx(linear=False)
    assert linear_sync_plan({"path": str(plan)}) == {"ok": False, "error": "NOT_LINEAR_SESSION"}
    assert ws.writes() == [] and plan.read_text(encoding="utf-8") == PLAN


def test_on_loop_call_refused_before_any_io(pin_ctx, ws, plan, monkeypatch, loop):
    calls: list[str] = []
    for name in ("source_path", "read_source", "source_info", "run_git"):
        real = getattr(sync, name)
        monkeypatch.setattr(sync, name,
                            lambda *a, _n=name, _r=real, **kw: (calls.append(_n), _r(*a, **kw))[1])

    async def on_loop():
        return linear_sync_plan({"path": str(plan)})
    out = asyncio.run_coroutine_threadsafe(on_loop(), loop).result(5)
    assert out == {"ok": False, "error": "WRONG_THREAD"}
    assert calls == [] and ws.writes() == []


def test_sync_uses_its_own_timeout(pin_ctx, monkeypatch, plan):
    seen: list[float] = []
    real = agent_tools.run_on_adapter

    def spy(ctx, factory, *, timeout_s=agent_tools.TOOL_TIMEOUT_S):
        seen.append(timeout_s)
        return real(ctx, factory, timeout_s=timeout_s)
    monkeypatch.setattr(agent_tools, "run_on_adapter", spy)
    assert linear_sync_plan({"path": str(plan)})["ok"] is True
    assert seen == [agent_tools.PLAN_SYNC_TIMEOUT_S]
    assert agent_tools.PLAN_SYNC_TIMEOUT_S > agent_tools.TOOL_TIMEOUT_S
