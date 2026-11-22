"""Tests for the context digest fold (spec §7.5)."""

from __future__ import annotations

from hermes_linear.constants import DIGEST_CAP_CHARS
from hermes_linear.digest import fold
from hermes_linear.models import ContextItem


def _item(field, old=None, new=None, *, kind="issue", actor="Mike Machado",
          at="2026-11-21T10:00:00Z", project=None, issue_id="ISS-1"):
    return ContextItem(issue_id=issue_id, kind=kind, field=field, old=old, new=new,
                       actor=actor, at=at, project=project)


def test_empty_none():
    assert fold([]) is None
    assert fold(()) is None


def test_header_and_line_format():
    items = [
        _item("state", "Todo", "In Progress"),
        _item("labels", None, "+Feature"),
        _item("status post", None, "On track, shipping Friday", kind="project",
              actor="Val", project="Valkyrie"),
    ]
    assert fold(items) == (
        "Since your last turn:\n"
        "- Status: Todo → In Progress (Mike Machado)\n"
        "- Labels: +Feature (Mike Machado)\n"
        '- [Project Valkyrie] status post: "On track, shipping Friday" (Val)'
    )


def test_other_shapes():
    items = [
        _item("assignee", None, "Ann"),
        _item("dueDate", "2026-11-01", "2026-12-01"),
        _item("description", None, "line one\nline two"),
        _item("comment", None, "hi\n\nthere", kind="comment", actor="Bob"),
        _item("reply", None, "ok", kind="comment", actor="Bob"),
        _item("link", None, "PR #4 https://x/4", kind="attachment"),
        _item("status", None, "Paused", kind="project", project="Valkyrie"),
    ]
    assert fold(items) == (
        "Since your last turn:\n"
        "- Assignee: Ann (Mike Machado)\n"
        "- Due Date: 2026-11-01 → 2026-12-01 (Mike Machado)\n"
        '- Description: "line one line two" (Mike Machado)\n'
        '- Comment: "hi there" (Bob)\n'
        '- Reply: "ok" (Bob)\n'
        "- Link: PR #4 https://x/4 (Mike Machado)\n"
        "- [Project Valkyrie] Status: Paused (Mike Machado)"
    )


def test_quoted_body_truncated_to_200():
    out = fold([_item("comment", None, "x" * 500, kind="comment")])
    line = out.splitlines()[1]
    assert line == '- Comment: "' + "x" * 199 + '…" (Mike Machado)'


def test_same_field_collapses_earliest_old_latest_new():
    items = [
        _item("state", "Todo", "In Progress", actor="A"),
        _item("priority", "Low", "High", actor="A"),
        _item("state", "In Progress", "In Review", actor="B"),
        _item("state", "In Review", "Done", actor="C"),
    ]
    assert fold(items) == (
        "Since your last turn:\n"
        "- Priority: Low → High (A)\n"
        "- Status: Todo → Done (C)"
    )


def test_collapse_key_includes_issue_and_project():
    items = [
        _item("status", None, "Paused", kind="project", project="A"),
        _item("status", None, "Active", kind="project", project="B"),
    ]
    out = fold(items)
    assert "[Project A] Status: Paused" in out
    assert "[Project B] Status: Active" in out


def test_comments_and_status_posts_never_collapse():
    items = [
        _item("comment", None, "one", kind="comment"),
        _item("comment", None, "two", kind="comment"),
        _item("status post", None, "p1", kind="project", project="V"),
        _item("status post", None, "p2", kind="project", project="V"),
    ]
    lines = fold(items).splitlines()[1:]
    assert len(lines) == 4


def test_labels_concatenate_and_cancel():
    items = [
        _item("labels", None, "+Feature, -Bug"),
        _item("labels", None, "+Urgent"),
        _item("labels", None, "-Feature", actor="Z"),
    ]
    assert fold(items) == "Since your last turn:\n- Labels: -Bug, +Urgent (Z)"


def test_labels_fully_cancelled_dropped():
    items = [_item("labels", None, "+Feature"), _item("labels", None, "-Feature")]
    assert fold(items) is None


def test_cap_summarises_oldest_first():
    items = [_item("priority", "Low", "High"), _item("description", None, "d" * 300)]
    items += [_item("comment", None, f"comment number {i:03d} " + "y" * 60, kind="comment",
                    actor="Bob") for i in range(100)]
    cap = 1000
    out = fold(items, cap=cap)
    assert len(out) <= cap
    lines = out.splitlines()
    assert lines[0] == "Since your last turn:"
    summary = lines[-1]
    assert summary.startswith("- +") and " earlier changes (" in summary
    n = int(summary[3:].split(" ", 1)[0])
    kept = lines[1:-1]
    assert n + len(kept) == 102
    assert summary.endswith("(priority, description, comment)")
    # newest items intact and in chronological order
    assert kept[-1] == '- Comment: "comment number 099 ' + "y" * 60 + '" (Bob)'
    assert kept[-2].startswith('- Comment: "comment number 098 ')


def test_cap_default_is_constant():
    items = [_item("comment", None, f"c{i} " + "z" * 150, kind="comment") for i in range(100)]
    assert len(fold(items)) <= DIGEST_CAP_CHARS


def test_single_line_over_cap_truncated():
    items = [_item("link", None, "u" * 500, kind="attachment")]
    out = fold(items, cap=120)
    assert len(out) <= 120
    assert out.startswith("Since your last turn:\n- Link: uuu")
    assert out.endswith("…")
