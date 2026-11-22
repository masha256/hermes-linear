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

from fake_workspace import HUMAN, VIEWER, FakeWorkspace, normalize_markdown
from test_agent_tools_runtime import new_adapter

from hermes_linear import agent_tools, sync
from hermes_linear.store import Store
from hermes_linear.agent_tools import TOOL_HANDLERS, TOOL_SCHEMAS, linear_sync_plan
from hermes_linear.linear_client import LinearClient
from hermes_linear.sync import (PLAN_COMMENT_MAX, TASK_DESC_MAX, PlanTask, footer, parse_plan,
                                plan_header, read_desc_hash, task_title, with_desc_hash,
                                write_ticket_ids)

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


LIST_PLAN = """Linear: RET-1

### Task 1: Listy

Steps:
- one
- two

### Task 2: Plain

Just text.
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
    return new_adapter(loop, client=ws, store=Store.open(":memory:"))


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
    assert tasks[1].body.startswith("Answers spec §12 items 1–2")
    assert "**Files:**" in tasks[1].body and "**Interfaces:**" in tasks[1].body
    assert tasks[1].body.endswith("in the same commit.")  # up to the next task heading
    assert all(isinstance(t, PlanTask) for t in tasks)


def test_parse_plan_body_and_lines():
    tasks = parse_plan(PLAN)
    assert [(t.number, t.name, t.ticket) for t in tasks] == [
        (1, "Alpha", None), (2, "Beta", None), (3, "Gamma", None)]
    assert tasks[0].body == "Do alpha first.\n\n**Files:**\n- Create: `a.py`\n\n- [ ] **Step 1:** write it."
    assert tasks[1].body == ("**Files:**\n- Modify: `b.py`\n\n"
                             "Beta summary paragraph\ncontinues here.")
    assert tasks[2].body == "Gamma things."
    assert PLAN.splitlines()[tasks[0].line - 1] == "### Task 1: Alpha"
    assert parse_plan("### Task 1: Empty\n")[0].body == ""


def test_task_body_bounds():
    text = ("### Task 1: A\r\n\r\nIntro.\r\n\r\n#### Detail\r\n\r\n```sh\r\n# a comment\r\n"
            "## not a heading\r\n```\r\n\r\n---\r\n\r\n## Appendix\r\n\r\nNot task 1.\r\n"
            "### Task 2: B\n\nB.\n\n---\n")
    a, b = parse_plan(text)
    assert a.body == ("Intro.\n\n#### Detail\n\n```sh\n# a comment\n## not a heading\n```")
    assert b.body == "B."  # the trailing rule is dropped


def test_plan_header():
    assert plan_header(PLAN) == "# Thing — Implementation Plan\n\nLinear: RET-1\n\n**Goal:** build the thing."
    fixture = plan_header(FIXTURE.read_text(encoding="utf-8"))
    assert fixture.startswith("# Hermes Linear Channel Plugin") and "## File Structure" in fixture
    assert "### Task" not in fixture and not fixture.endswith("---")
    assert plan_header("```\n### Task 1: Fenced\n```\nHead\n### Task 1: Real\n") == (
        "```\n### Task 1: Fenced\n```\nHead")
    assert plan_header("### Task 1: A\n") == ""


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


def expected_comment(text: str, path: str, tickets: dict[int, str], commit=None) -> str:
    items = "\n".join(f"- {task_title(t)} — {tickets[t.number]}" for t in parse_plan(text))
    return (f"{plan_header(text)}\n\n## Tasks\n\n{items}\n\n---\n{footer(path, commit)}"
            f"\n\n{MARKER}")


def desc(p: Path, n: int, body: str) -> str:
    return with_desc_hash(f"[Plan]({link(p)}) · Task {n}\n\n{body}")


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
    tasks = parse_plan(PLAN)
    assert [k["description"] for k in kids] == [desc(plan, t.number, t.body) for t in tasks]
    assert "- [ ] **Step 1:** write it." in kids[0]["description"]  # the whole task section
    assert kids[0]["description"].splitlines()[-1].startswith("<!-- hermes-linear:h=")
    assert all(k["creator"] == VIEWER for k in kids)
    assert all(k["project"] is not None and k["team"] == "team-1" for k in kids)
    creates = [c[1] for c in ws.writes() if c[0] == "issue_create"]
    assert [c["subIssueSortOrder"] for c in creates] == [10, 20, 30]
    assert all(c["parentId"] == SELF for c in creates)
    assert adapter._store.created_by(SELF) >= {k["id"] for k in kids}  # UUIDs
    text = plan.read_text(encoding="utf-8")
    assert text == write_ticket_ids(PLAN, {1: ids[0], 2: ids[1], 3: ids[2]})
    assert [t.ticket for t in parse_plan(text)] == ids


def test_sync_plan_idempotent(pin_ctx, ws, plan):
    ws.normalize = True  # probe P8: Linear rewrites descriptions on save
    plan.write_text(LIST_PLAN, encoding="utf-8")
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
    stored = ws.issue(first["tasks"][1])["description"]
    assert "* one" in stored and "- one" not in stored  # normalized, yet not rewritten


def test_summary_change_rewrites_description_once(pin_ctx, ws, plan):
    ws.normalize = True
    plan.write_text(LIST_PLAN, encoding="utf-8")
    first = linear_sync_plan({"path": str(plan)})["tasks"]
    text = plan.read_text(encoding="utf-8")
    plan.write_text(text.replace("- two", "- two\n- three"), encoding="utf-8")
    n = len(ws.writes())
    assert linear_sync_plan({"path": str(plan)})["ok"] is True
    updates = [c[1] for c in ws.writes()[n:] if c[0] == "issue_update"]
    assert updates == [(ws.issue(first[1])["id"], {"description": updates[0][1]["description"]})]
    assert "- three" in updates[0][1]["description"]
    assert read_desc_hash(ws.issue(first[1])["description"]) is not None
    n = len(ws.writes())
    linear_sync_plan({"path": str(plan)})
    assert ws.writes()[n:] == []


def test_normalize_markdown_mimics_p8():
    assert normalize_markdown("Do:\n- a\n- b") == "Do:\n\n* a\n* b"
    assert normalize_markdown("x\n\n<!-- hermes-linear:h=abc123abc123 -->") == (
        "x\n\n<!-- hermes-linear:h=abc123abc123 -->")
    assert read_desc_hash(normalize_markdown(with_desc_hash("Do:\n- a"))) == read_desc_hash(
        with_desc_hash("Do:\n- a"))


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


def test_adoption_needs_the_exact_title(pin_ctx, ws, plan):
    """M3: adopt only an open, app-made child titled exactly ``T<n> · <name>``."""
    first = linear_sync_plan({"path": str(plan)})["tasks"]
    plan.write_text(PLAN.replace("### Task 2: Beta", "### Task 2: Beta prime"), encoding="utf-8")
    out = linear_sync_plan({"path": str(plan)})  # IDs lost and renamed: a new ticket
    assert out["tasks"][1] == first[1] and out["tasks"][3] == first[3]
    assert len(out["created"]) == 1 and out["tasks"][2] == out["created"][0]
    assert out["canceled"] == [first[2]]
    assert ws.issue(first[2])["title"] == "T2 · Beta"


def test_replaced_task_number_cancels_old_creates_new(pin_ctx, ws, tmp_path):
    f = tmp_path / "plan.md"
    f.write_text("Linear: RET-1\n\n### Task 4: Keep\n\nK.\n\n### Task 5: Old thing\n\nO.\n",
                 encoding="utf-8")
    first = linear_sync_plan({"path": str(f)})["tasks"]
    text = f.read_text(encoding="utf-8")
    f.write_text(text[:text.index("### Task 5")] + "### Task 5: New thing\n\nN.\n",
                 encoding="utf-8")
    out = linear_sync_plan({"path": str(f)})
    assert out["ok"] is True and out["canceled"] == [first[5]]
    assert out["created"] == [out["tasks"][5]] and out["tasks"][5] != first[5]
    assert out["tasks"][4] == first[4]
    assert ws.issue(first[5])["state"] == "st-cancel"
    assert ws.issue(first[5])["title"] == "T5 · Old thing"  # never retitled
    assert ws.issue(out["tasks"][5])["title"] == "T5 · New thing"


def test_human_children_never_canceled_or_adopted(pin_ctx, ws, plan):
    """I2 / Ruling S17: only children the app created are adopted or canceled."""
    human_t1 = ws.add_issue(title="T1 · Alpha", parent=SELF, creator=HUMAN)
    human_t9 = ws.add_issue(title="T9 · Human task", parent=SELF, creator=HUMAN)
    no_creator = ws.add_issue(title="T8 · Gone user", parent=SELF, creator=None)
    out = linear_sync_plan({"path": str(plan)})
    assert out["ok"] is True and len(out["created"]) == 3
    assert out["tasks"][1] != ws.issues[human_t1]["identifier"]  # not adopted
    assert out["canceled"] == []
    assert sorted(out["unmanaged"]) == sorted(ws.issues[i]["identifier"]
                                              for i in (human_t1, human_t9, no_creator))
    for i in (human_t1, human_t9, no_creator):
        assert ws.issues[i]["state"] == "st-todo"
        assert not [c for c in ws.writes() if c[0] in {"issue_update", "comment_create"}
                    and c[1][0] == i]
    assert ws.issues[human_t1]["title"] == "T1 · Alpha"


def test_app_child_of_removed_task_canceled_but_human_kept(pin_ctx, ws, plan):
    app_t7 = ws.add_issue(title="T7 · Stale", parent=SELF, creator=VIEWER)
    human_t7 = ws.add_issue(title="T7 · Stale", parent=SELF, creator=HUMAN)
    out = linear_sync_plan({"path": str(plan)})
    assert out["canceled"] == [ws.issues[app_t7]["identifier"]]
    assert out["unmanaged"] == [ws.issues[human_t7]["identifier"]]
    assert ws.issues[human_t7]["state"] == "st-todo"


def test_heading_naming_session_ticket_refused_for_that_task(pin_ctx, ws, tmp_path):
    """M7: the session ticket (not the project ticket) is never a task ticket."""
    ws.add_issue("RET-50", title="Session ticket", uuid="uuid-sess", creator=VIEWER)
    ws.issues[SELF]["parent"] = "uuid-sess"  # the project ticket is in the session's scope
    pin_ctx(issue="uuid-sess")
    f = tmp_path / "plan.md"
    text = "Linear: RET-1\n\n### Task 1: A — RET-50\n\nA.\n\n### Task 2: B\n\nB.\n"
    f.write_text(text, encoding="utf-8")
    out = linear_sync_plan({"path": str(f)})
    assert out["ok"] is True and out["errors"] == {1: "BAD_ARGS"}
    assert 1 not in out["tasks"] and len(out["created"]) == 1
    assert ws.issues["uuid-sess"]["title"] == "Session ticket"
    assert not [c for c in ws.writes() if c[0] == "issue_update" and c[1][0] == "uuid-sess"]
    assert parse_plan(f.read_text(encoding="utf-8"))[0].ticket == "RET-50"


def test_writeback_oserror_reports_file_error(pin_ctx, ws, plan, monkeypatch):
    """M9: the Linear writes are done; a failed write-back still answers ok."""
    def boom(path, text):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(sync, "write_source", boom)
    out = linear_sync_plan({"path": str(plan)})
    assert out["ok"] is True and out["file_error"] == "Permission denied"
    assert len(out["created"]) == 3 and out["canceled"] == []
    assert out["file_changed"] is False and plan.read_text(encoding="utf-8") == PLAN


def test_write_source_fsyncs_before_replace(tmp_path, monkeypatch):
    order: list[str] = []
    real_fsync, real_replace = sync.os.fsync, sync.os.replace
    monkeypatch.setattr(sync.os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd)))
    monkeypatch.setattr(sync.os, "replace",
                        lambda a, b: (order.append("replace"), real_replace(a, b)))
    f = tmp_path / "p.md"
    f.write_text("old", encoding="utf-8")
    sync.write_source(f, "new")
    assert order == ["fsync", "replace"] and f.read_text(encoding="utf-8") == "new"


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
    assert out["comment_inline"] is True
    assert body == expected_comment(synced, sync.source_info(plan).path, out["tasks"])
    assert "Do alpha first." not in body and "Gamma things." not in body  # no task bodies
    plan.write_text(synced.replace("Gamma things.", "Gamma changed."), encoding="utf-8")
    assert linear_sync_plan({"path": str(plan)})["comment"] == "unchanged"  # a task body only
    plan.write_text(synced.replace("build the thing", "build it"), encoding="utf-8")
    out = linear_sync_plan({"path": str(plan)})
    assert out["comment"] == "edited"
    mine2 = [c for c in ws.comments.values() if c["issue"] == SELF and c["user"] != "human-1"]
    assert [c["id"] for c in mine2] == [mine[0]["id"]]
    assert "build it" in mine2[0]["body"] and mine2[0]["body"].count(MARKER) == 1
    human = [c for c in ws.comments.values() if c["user"] == "human-1"]
    assert human[0]["body"] == f"quoting {MARKER} by hand"
    assert linear_sync_plan({"path": str(plan)})["comment"] == "unchanged"


def test_plan_comment_oversize_header_truncated(pin_ctx, ws, tmp_path):
    f = tmp_path / "plan.md"
    big = PLAN.replace("**Goal:** build the thing.", "😀" * (PLAN_COMMENT_MAX // 2))
    f.write_text(big, encoding="utf-8")
    out = linear_sync_plan({"path": str(f)})
    assert out["ok"] is True and out["comment_inline"] is False
    (c,) = [c for c in ws.comments.values() if c["issue"] == SELF]
    body = c["body"]
    assert sync.utf16_len(body) <= PLAN_COMMENT_MAX
    assert body.startswith("# Thing — Implementation Plan") and "😀" in body
    assert f"…\n\nFull plan (too long to show here): {link(f)}\n\n## Tasks\n\n" in body
    for n, ident in out["tasks"].items():
        assert f"- T{n} · " in body and ident in body
    assert body.endswith(MARKER) and "edit by asking in this thread" in body


def test_task_description_oversize_truncated_with_link(pin_ctx, ws, tmp_path):
    f = tmp_path / "plan.md"
    big = PLAN.replace("Gamma things.", "```\n" + "😀" * (TASK_DESC_MAX // 2) + "\n```")
    f.write_text(big, encoding="utf-8")
    out = linear_sync_plan({"path": str(f)})
    d = ws.issue(out["tasks"][3])["description"]
    assert sync.utf16_len(d) <= TASK_DESC_MAX
    assert d.startswith(f"[Plan]({link(f)}) · Task 3\n\n```\n😀")
    assert f"\n```\n\n…\n\nFull task (too long to show here): {link(f)}" in d  # fence closed
    assert read_desc_hash(d) is not None
    n = len(ws.writes())
    linear_sync_plan({"path": str(f)})
    assert ws.writes()[n:] == []  # stable


def test_task_description_truncation_links_the_task_line():
    info = sync.SourceInfo("docs/plan.md", "abc1234",
                           "https://github.com/acme/w/blob/abc1234/docs/plan.md")
    t = PlanTask(2, "B", None, "x" * TASK_DESC_MAX, 40)
    d = sync.task_description(t, info)
    assert sync.utf16_len(with_desc_hash(d)) <= TASK_DESC_MAX
    assert d.endswith("x\n\n…\n\nFull task (too long to show here): "
                      "https://github.com/acme/w/blob/abc1234/docs/plan.md#L40")
    t2 = PlanTask(2, "B", None, "x" * (TASK_DESC_MAX - 200), 40)
    assert sync.utf16_len(with_desc_hash(sync.task_description(t2, info))) <= TASK_DESC_MAX
    assert sync.task_description(PlanTask(1, "A", None, "", 3), info) == f"[Plan]({info.link}) · Task 1"


def test_resync_migrates_old_shapes_once(pin_ctx, ws, plan):
    """Tickets and the comment written by the first-paragraph/whole-plan sync are rewritten
    once on the next sync; Done tickets keep the old description."""
    first = linear_sync_plan({"path": str(plan)})["tasks"]
    synced = plan.read_text(encoding="utf-8")
    old = {1: "Do alpha first.", 2: "Beta summary paragraph\ncontinues here.", 3: "Gamma things."}
    for n, summary in old.items():
        ws.issue(first[n])["description"] = desc(plan, n, summary)
    ws.issue(first[1])["state"] = "st-done"
    (mine,) = [c for c in ws.comments.values() if c["issue"] == SELF]
    path = sync.source_info(plan).path
    mine["body"] = f"{synced.rstrip()}\n\n---\n{footer(path, None)}\n\n{MARKER}"
    n = len(ws.writes())
    out = linear_sync_plan({"path": str(plan)})
    assert out["comment"] == "edited" and out["created"] == [] and out["canceled"] == []
    updates = {c[1][0]: c[1][1] for c in ws.writes()[n:] if c[0] == "issue_update"}
    # Task 3's first paragraph is its whole section: already current, no write.
    assert set(updates) == {ws.issue(first[2])["id"]}
    assert all(set(inp) == {"description"} for inp in updates.values())
    tasks = parse_plan(PLAN)
    assert ws.issue(first[2])["description"] == desc(plan, 2, tasks[1].body)
    assert ws.issue(first[1])["description"] == desc(plan, 1, old[1])  # Done: untouched
    (mine,) = [c for c in ws.comments.values() if c["issue"] == SELF]
    assert mine["body"] == expected_comment(synced, path, first)
    n = len(ws.writes())
    linear_sync_plan({"path": str(plan)})
    assert ws.writes()[n:] == []


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
    done = ws.add_issue(title="T1 · Alpha", parent=SELF, state="st-done", creator=VIEWER)
    other = ws.add_issue(title="Tx · not a task", parent=SELF, creator=VIEWER)
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
