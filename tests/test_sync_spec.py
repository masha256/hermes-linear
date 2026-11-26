"""linear_sync_spec (RET-77; RET-168 spec §6): the pure spec helpers in ``sync.py`` and the
tool itself.

The tool runs against ``FakeWorkspace`` on a real background loop, as in
``test_agent_tools_generic.py``; context resolution is pinned with ``pin_ctx``. Since
RET-168 the whole spec goes to a ``Spec: <H1>`` document on the header ticket (found by
the prefix among the app's own documents) and the description becomes the agent's
summary plus a link line, keeping the plan part after it.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
from pathlib import Path

import pytest

from fake_workspace import HUMAN, FakeWorkspace
from test_agent_tools_runtime import new_adapter

from hermes_linear import agent_tools, sync
from hermes_linear.agent_tools import TOOL_HANDLERS, TOOL_SCHEMAS, linear_sync_spec
from hermes_linear.linear_client import LinearError
from hermes_linear.store import Store
from hermes_linear.sync import (PLAN_PART_MARKER, SPEC_PART_MARKER, SYNC_DOCUMENT_MAX,
                                SYNC_SUMMARY_MAX, TRUNCATED, doc_title, document_content,
                                footer, git_short_commit, read_linear_header, source_info,
                                source_line, spec_part, split_plan_part)

SELF = "uuid-self"
FOOTER_TAIL = " · edit by asking in this thread"
SUMMARY = "Builds the thing. Key decision: do it simply."
TITLE = "Spec: Thing — Design"

SPEC = """# Thing — Design

Linear: RET-1

Some preamble.

## 1. Purpose

Why we build the thing.

Second paragraph of purpose.

## 2. Design

How it works.
"""

PLAN_PART = ("---\n" + PLAN_PART_MARKER + "\nPlan summary.\n\n"
             "**Plan:** [Plan: Thing](https://linear.app/x/document/doc-p) · source "
             "[docs/plan.md @ abc1234](https://github.com/acme/widgets/blob/abc1234/"
             "docs/plan.md)\n\n**Tasks:**\n- T1 · Alpha — RET-5\n- T2 · Beta — RET-6")


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
    w.add_issue("RET-1", title="Session issue", uuid=SELF, description="old")
    w.add_issue("RET-2", title="Child", uuid="uuid-child", parent=SELF)
    w.add_issue("OTH-9", title="Unrelated", uuid="uuid-other", project=None)
    return w


@pytest.fixture
def adapter(loop, ws):
    a = new_adapter(loop, client=ws)
    a._store = Store.open(":memory:")
    return a


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


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", "-C", str(repo), *args],
        check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A git repo with a github origin and one committed spec at docs/spec.md."""
    r = tmp_path / "repo"
    (r / "docs").mkdir(parents=True)
    _git(r, "init", "-q")
    _git(r, "remote", "add", "origin", "git@github.com:acme/widgets.git")
    (r / "docs" / "spec.md").write_text(SPEC, encoding="utf-8")
    _git(r, "add", "docs/spec.md")
    _git(r, "commit", "-q", "-m", "spec")
    return r


def sync_spec(path, summary=SUMMARY):
    return linear_sync_spec({"path": str(path), "summary": summary})


def _names(ws, start=0):
    return [c[0] for c in ws.writes()[start:]]


def _docs(ws, issue=SELF):
    return [d for d in ws.documents.values() if d["issue"] == issue]


# ----------------------------------------------------------------- sync.py (pure)


def test_constants():
    assert SYNC_SUMMARY_MAX == 5_000 and SYNC_DOCUMENT_MAX == 250_000
    assert SPEC_PART_MARKER == "<!-- hermes-linear:spec -->"
    assert PLAN_PART_MARKER == "<!-- hermes-linear:plan -->"
    for gone in ("render_spec", "spec_summary", "spec_description"):
        assert not hasattr(sync, gone)


def test_header_parse():
    assert read_linear_header(SPEC) == "RET-1"
    assert read_linear_header("Linear:RET-7\nLinear: RET-8\n") == "RET-7"  # first wins
    assert read_linear_header("# T\n\nLinear: \t ABC-12 \t\n") == "ABC-12"
    assert read_linear_header("# T\n\nLinear:   ABC-12  trailing\n") is None  # whole line
    assert read_linear_header("no header\n  Linear: RET-1\n") is None  # must start the line
    assert read_linear_header("Linear:\n") is None
    assert read_linear_header("Linear: ret-1\n") is None
    assert read_linear_header("") is None


def test_header_newline_does_not_capture_next_line():
    assert read_linear_header("Linear:\nRET-5\n") is None
    assert read_linear_header("Linear: \nRET-5\n\nLinear: RET-6\n") == "RET-6"


def test_header_only_in_header_block():
    later = "# T\n\n## 1. Purpose\n\nLinear: RET-99\n"
    assert read_linear_header(later) is None  # after the first ## heading
    fenced = "# T\n\n```\nLinear: RET-99\n```\n\nprose\n"
    assert read_linear_header(fenced) is None  # inside a code fence
    assert read_linear_header(fenced + "Linear: RET-3\n") == "RET-3"  # after the fence
    assert read_linear_header("# T\n\n```md\n## not a heading\n```\nLinear: RET-4\n") == "RET-4"
    assert read_linear_header("# T\n\nLinear: RET-1\n\n## 1.\n\n```\nLinear: RET-99\n```\n") \
        == "RET-1"


def test_header_uuid_accepted():
    u = "0f8e2c1a-3b4d-4e5f-8a9b-1c2d3e4f5a6b"
    assert read_linear_header(f"Linear: {u}\n") == u
    assert read_linear_header(f"Linear: {u.upper()}\n") is None
    assert read_linear_header("Linear: 0f8e2c1a-3b4d\n") is None


def test_footer_text():
    assert footer("docs/spec.md", "abc1234") == (
        "Source: docs/spec.md @ abc1234 · edit by asking in this thread")
    assert footer("docs/spec.md", None) == "Source: docs/spec.md @ uncommitted" + FOOTER_TAIL


def test_doc_title():
    assert doc_title("Spec", SPEC, "/r/docs/spec.md") == TITLE
    assert doc_title("Plan", "#   Spaced H1  \n\nx\n", "/r/p.md") == "Plan: Spaced H1"
    assert doc_title("Spec", "Linear: RET-1\n\n## 1. P\n", "/r/docs/2026-x-design.md") \
        == "Spec: 2026-x-design"  # no H1: the file name without extension
    assert doc_title("Spec", "```sh\n# a shell comment\n```\n# Real\n", "/r/s.md") \
        == "Spec: Real"  # a fenced line is not a heading
    assert doc_title("Spec", "#\n#nospace\n", "/r/s.md") == "Spec: s"


def test_source_line():
    info = sync.SourceInfo("docs/spec.md", "abc1234",
                           "https://github.com/acme/widgets/blob/abc1234/docs/spec.md")
    assert source_line("Spec", TITLE, "https://linear.app/x/document/doc-1", info) == (
        "**Spec:** [Spec: Thing — Design](https://linear.app/x/document/doc-1) · source "
        "[docs/spec.md @ abc1234](https://github.com/acme/widgets/blob/abc1234/docs/spec.md)")
    loose = sync.SourceInfo("/tmp/s.md", None, "/tmp/s.md")
    assert source_line("Plan", "Plan: P", "u", loose) == (
        "**Plan:** [Plan: P](u) · source [/tmp/s.md @ uncommitted](/tmp/s.md)")


def test_spec_part():
    assert spec_part("Sum.", "**Spec:** x") == f"Sum.\n\n**Spec:** x\n{SPEC_PART_MARKER}"


def test_split_plan_part():
    assert split_plan_part(None) == ("", None)
    assert split_plan_part("just a brief") == ("just a brief", None)
    head = f"Sum.\n\n**Spec:** x\n{SPEC_PART_MARKER}\n\n"
    assert split_plan_part(head + PLAN_PART) == (head, PLAN_PART)
    # Linear inserts a blank line after a `---` on save: the bar still belongs to the part
    normalized = PLAN_PART.replace("---\n", "---\n\n", 1)
    assert split_plan_part(head + normalized) == (head, normalized)
    # no bar before the marker: the part starts at the marker
    bare = PLAN_PART[len("---\n"):]
    assert split_plan_part("Brief.\n\n" + bare) == ("Brief.\n\n", bare)
    # a bar further up (not directly before the marker) is not part of it
    assert split_plan_part("a\n---\nb\n" + bare) == ("a\n---\nb\n", bare)


def test_document_content_inline():
    info = sync.SourceInfo("docs/spec.md", "abc1234", "https://e.test/spec.md")
    content, inline = document_content(SPEC, info)
    body = f"{SPEC.rstrip()}\n\n---\n{footer('docs/spec.md', 'abc1234')}"
    assert inline is True and content == sync.with_desc_hash(body)
    assert sync.read_desc_hash(content) == sync.desc_hash(body)


def test_document_content_limit_counts_footer_and_hash():
    info = sync.SourceInfo("s.md", None, "s.md")
    probe, _ = document_content("y", info)
    pad = SYNC_DOCUMENT_MAX - sync.utf16_len(probe) + 1
    content, inline = document_content("y" * pad, info)
    assert inline is True and sync.utf16_len(content) == SYNC_DOCUMENT_MAX
    content, inline = document_content("y" * (pad + 1), info)
    assert inline is False and sync.utf16_len(content) <= SYNC_DOCUMENT_MAX


def test_document_content_cut_is_utf16_and_closes_fence():
    info = sync.SourceInfo("s.md", None, "https://e.test/s.md")
    content, inline = document_content("# E\n\n```\n" + "😀" * 130_000 + "\n```\n", info)
    assert inline is False
    assert SYNC_DOCUMENT_MAX - 20 < sync.utf16_len(content) <= SYNC_DOCUMENT_MAX
    content.encode("utf-8")  # no lone surrogates
    body = content[:content.index("\n\n<!-- hermes-linear:h=")]
    assert body.endswith(f"```{TRUNCATED}\n\nFull file (too long to show here): "
                         f"https://e.test/s.md\n\n---\n{footer('s.md', None)}")
    assert sync.read_desc_hash(content) == sync.desc_hash(body)


def test_git_short_commit_and_source_info(repo):
    f = repo / "docs" / "spec.md"
    head = _git(repo, "rev-parse", "--short", "HEAD")
    assert git_short_commit(f) == head
    info = source_info(f)
    assert info.path == "docs/spec.md" and info.commit == head
    assert info.link == f"https://github.com/acme/widgets/blob/{head}/docs/spec.md"
    # a new, never-committed file
    new = repo / "docs" / "new.md"
    new.write_text("x", encoding="utf-8")
    assert git_short_commit(new) is None
    assert source_info(new).link == "docs/new.md"  # not on GitHub yet: the repo path


def test_dirty_working_copy_is_uncommitted(repo):
    f = repo / "docs" / "spec.md"
    f.write_text(SPEC + "\nlocal edit\n", encoding="utf-8")
    assert git_short_commit(f) is None
    info = source_info(f)
    assert (info.path, info.commit, info.link) == ("docs/spec.md", None, "docs/spec.md")
    assert footer(info.path, info.commit) == "Source: docs/spec.md @ uncommitted" + FOOTER_TAIL
    _git(repo, "add", "docs/spec.md")  # staged but not committed is still dirty
    assert git_short_commit(f) is None
    _git(repo, "commit", "-q", "-m", "edit")
    assert git_short_commit(f) == _git(repo, "rev-parse", "--short", "HEAD")


def test_dirty_check_failure_is_uncommitted(tmp_path):
    def git(args):
        return "abc1234" if "log" in args else None  # status fails
    assert git_short_commit(tmp_path / "x.md", git=git) is None


def test_source_info_outside_git(tmp_path):
    f = tmp_path / "loose.md"
    f.write_text("x", encoding="utf-8")
    info = source_info(f, git=lambda *a: None)
    assert (info.path, info.commit, info.link) == (str(f), None, str(f))


@pytest.mark.parametrize("url", ["git@github.com:acme/widgets.git",
                                 "https://github.com/acme/widgets.git",
                                 "https://token@github.com/acme/widgets",
                                 "ssh://git@github.com/acme/widgets.git"])
def test_github_remote_forms(url):
    assert sync.github_repo(url) == "acme/widgets"


@pytest.mark.parametrize("url", ["git@gitlab.com:acme/widgets.git",
                                 "https://github.com.evil.io/acme/widgets", "", None])
def test_non_github_remote(url):
    assert sync.github_repo(url) is None


def test_git_failure_is_none(tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired("git", 10)
    monkeypatch.setattr(sync.subprocess, "run", boom)
    assert git_short_commit(tmp_path / "x.md") is None


def test_length_is_utf16_units():
    assert sync.utf16_len("a😀é") == 4
    assert sync.utf16_truncate("a😀b", 2) == "a"  # never splits a surrogate pair
    assert sync.utf16_truncate("a😀b", 3) == "a😀"


# ----------------------------------------------------------------- linear_sync_spec


def test_schema_and_handler():
    s = TOOL_SCHEMAS["linear_sync_spec"]
    assert s["name"] == "linear_sync_spec" and "Linear app" in s["description"]
    assert s["parameters"]["required"] == ["path", "summary"]
    props = s["parameters"]["properties"]
    assert list(props) == ["path", "summary"]
    assert props["summary"]["type"] == "string" and "5,000" in props["summary"]["description"]
    for phrase in ("'Spec: <H1>' document", "app's own", "plan part", "AMBIGUOUS"):
        assert phrase in s["description"], phrase
    assert TOOL_HANDLERS["linear_sync_spec"] is linear_sync_spec
    assert "linear_sync_spec" not in agent_tools.tool_approval.GATED_TOOLS


@pytest.mark.parametrize("summary", [None, "   ", "x" * (SYNC_SUMMARY_MAX + 1), 3, ["a"]])
def test_summary_required(pin_ctx, ws, repo, summary):
    args = {"path": str(repo / "docs" / "spec.md")}
    if summary is not None:
        args["summary"] = summary
    assert linear_sync_spec(args) == {"ok": False, "error": "BAD_ARGS"}
    assert ws.writes() == [] and ws.documents == {}


def test_summary_at_limit_and_stripped(pin_ctx, ws, repo):
    out = sync_spec(repo / "docs" / "spec.md", "  " + "s" * SYNC_SUMMARY_MAX + "\n")
    assert out["ok"] is True
    assert ws.issues[SELF]["description"].startswith("s" * SYNC_SUMMARY_MAX + "\n\n**Spec:**")


def test_first_sync_creates_doc_and_replaces_description(pin_ctx, ws, repo, adapter):
    f = repo / "docs" / "spec.md"
    head = _git(repo, "rev-parse", "--short", "HEAD")
    out = sync_spec(f)
    (doc,) = _docs(ws)
    assert out == {"ok": True, "issue": "RET-1", "url": "https://linear.app/x/issue/RET-1",
                   "document_id": doc["id"], "document_url": doc["url"],
                   "document_created": True, "document_skipped": False, "inline": True}
    assert doc["title"] == TITLE and doc["creator"] == "app-user"
    info = source_info(f)
    assert doc["content"] == sync.with_desc_hash(
        f"{SPEC.rstrip()}\n\n---\nSource: docs/spec.md @ {head}{FOOTER_TAIL}")
    link = f"https://github.com/acme/widgets/blob/{head}/docs/spec.md"
    want = spec_part(SUMMARY, source_line("Spec", TITLE, doc["url"], info))
    assert ws.issues[SELF]["description"] == want  # the brief ("old") is replaced
    assert want == (f"{SUMMARY}\n\n**Spec:** [{TITLE}]({doc['url']}) · source "
                    f"[docs/spec.md @ {head}]({link})\n{SPEC_PART_MARKER}")
    assert adapter._store.documents_by(SELF) == {doc["id"]}
    assert _names(ws) == ["document_create", "issue_update"]  # document first
    assert ws.writes()[0][1][2] == SELF  # on the header ticket
    assert set(ws.writes()[1][1][1]) == {"description"}


def test_resync_updates_same_doc(pin_ctx, ws, repo, adapter):
    f = repo / "docs" / "spec.md"
    sync_spec(f)
    (doc,) = _docs(ws)
    f.write_text(SPEC.replace("# Thing — Design", "# Thing v2") + "\n## 3. More\n",
                 encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "v2")
    n = len(ws.writes())
    out = sync_spec(f, "New summary.")
    assert out["ok"] is True and out["document_id"] == doc["id"]
    assert (out["document_created"], out["document_skipped"]) == (False, False)
    assert _docs(ws) == [doc] and doc["title"] == "Spec: Thing v2"
    assert "## 3. More" in doc["content"]
    assert _names(ws, n) == ["document_update", "issue_update"]
    assert set(ws.writes()[n][1][1]) == {"title", "content"}
    assert ws.issues[SELF]["description"].startswith("New summary.\n\n**Spec:** "
                                                     "[Spec: Thing v2](")
    assert adapter._store.documents_by(SELF) == {doc["id"]}
    # the same H1 again: the title is not sent
    f.write_text(f.read_text(encoding="utf-8") + "\nmore\n", encoding="utf-8")
    n = len(ws.writes())
    sync_spec(f, "New summary.")
    assert set(ws.writes()[n][1][1]) == {"content"}


def test_resync_unchanged_skips(pin_ctx, ws, repo):
    f = repo / "docs" / "spec.md"
    first = sync_spec(f)
    n = len(ws.writes())
    out = sync_spec(f)
    assert out["ok"] is True and out["document_skipped"] is True
    assert out["document_created"] is False and out["document_id"] == first["document_id"]
    assert out["document_url"] == first["document_url"]
    assert _names(ws, n) == []  # zero documentUpdate and zero issueUpdate


def test_resync_unchanged_skips_after_linear_normalization(pin_ctx, loop, monkeypatch, repo):
    """Linear rewrites description markdown on save (P8: ``- x`` -> ``* x``, a blank line
    before a list): an unchanged re-sync still makes no issue update."""
    w = FakeWorkspace(normalize=True)
    w.add_issue("RET-1", title="Session issue", uuid=SELF, description="old")
    pin_ctx(ad=new_adapter(loop, client=w))
    f = repo / "docs" / "spec.md"
    summary = "Builds:\n- one\n- two"
    assert sync_spec(f, summary)["ok"] is True
    assert "* one" in w.issues[SELF]["description"]  # stored normalized
    n = len(w.writes())
    assert sync_spec(f, summary)["document_skipped"] is True
    assert [c[0] for c in w.writes()[n:]] == []


def test_resync_new_summary_only_updates_description(pin_ctx, ws, repo):
    f = repo / "docs" / "spec.md"
    sync_spec(f)
    n = len(ws.writes())
    out = sync_spec(f, "Different summary.")
    assert out["document_skipped"] is True
    assert _names(ws, n) == ["issue_update"]


def test_retitled_by_hand_only_title_sent(pin_ctx, ws, repo):
    f = repo / "docs" / "spec.md"
    out = sync_spec(f)
    ws.documents[out["document_id"]]["title"] = "Spec: renamed in Linear"
    n = len(ws.writes())
    again = sync_spec(f)
    assert again["document_skipped"] is False
    assert ws.writes()[n:] == [("document_update", (out["document_id"], {"title": TITLE}))]


def test_human_spec_doc_ignored(pin_ctx, ws, repo, adapter):
    human = ws.add_document("Spec: x", "a human's notes", issue=SELF, creator=HUMAN)
    other = ws.add_document("Notes", "app notes", issue=SELF)  # the app's, no prefix
    out = sync_spec(repo / "docs" / "spec.md")
    assert out["ok"] is True and out["document_created"] is True
    assert out["document_id"] not in (human, other)
    assert ws.documents[human]["title"] == "Spec: x"
    assert ws.documents[human]["content"] == "a human's notes"
    assert ws.documents[other]["content"] == "app notes"
    assert not any(c[0] == "document_update" for c in ws.writes())
    assert not any(c == ("document_get", human) for c in ws.calls)  # never read either
    assert adapter._store.documents_by(SELF) == {out["document_id"]}


def test_two_app_spec_docs_ambiguous(pin_ctx, ws, repo):
    a = ws.add_document("Spec: one", "x", issue=SELF)
    b = ws.add_document("Spec: two", "y", issue=SELF)
    out = sync_spec(repo / "docs" / "spec.md")
    assert out == {"ok": False, "error": "AMBIGUOUS", "documents": [
        {"id": a, "title": "Spec: one", "url": ws.documents[a]["url"]},
        {"id": b, "title": "Spec: two", "url": ws.documents[b]["url"]}]}
    assert ws.writes() == [] and ws.issues[SELF]["description"] == "old"
    assert not any(c[0] == "document_get" for c in ws.calls)


def test_plan_doc_is_not_a_spec_doc(pin_ctx, ws, repo):
    plan = ws.add_document("Plan: Thing", "plan", issue=SELF)
    out = sync_spec(repo / "docs" / "spec.md")
    assert out["document_created"] is True and ws.documents[plan]["content"] == "plan"


@pytest.mark.parametrize("bar", ["---\n", "---\n\n"])
def test_keeps_plan_part(pin_ctx, ws, repo, bar):
    plan_part = bar + PLAN_PART[len("---\n"):]
    ws.issues[SELF]["description"] = "The old brief.\n\n" + plan_part
    out = sync_spec(repo / "docs" / "spec.md")
    head = spec_part(SUMMARY, source_line("Spec", TITLE, out["document_url"],
                                          source_info(repo / "docs" / "spec.md")))
    assert ws.issues[SELF]["description"] == f"{head}\n\n{plan_part}"
    assert ws.issues[SELF]["description"].endswith(plan_part)  # byte for byte
    n = len(ws.writes())
    assert sync_spec(repo / "docs" / "spec.md")["ok"] is True
    assert _names(ws, n) == []  # unchanged: no rewrite, the plan part still intact


def test_out_of_tree_refused(pin_ctx, ws, tmp_path):
    f = tmp_path / "s.md"
    f.write_text("# S\n\nLinear: OTH-9\n", encoding="utf-8")
    assert sync_spec(f) == {"ok": False, "error": "OUT_OF_SCOPE"}
    assert ws.writes() == [] and ws.documents == {}
    assert not any(c[0] == "documents_on" for c in ws.calls)


def test_oversize_file_cut(pin_ctx, ws, repo):
    f = repo / "docs" / "spec.md"
    f.write_text(SPEC + "\n## 3. Appendix\n\n" + "x" * 260_000 + "\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "big")
    head = _git(repo, "rev-parse", "--short", "HEAD")
    out = sync_spec(f)
    assert out["ok"] is True and out["inline"] is False
    (doc,) = _docs(ws)
    assert sync.utf16_len(doc["content"]) <= SYNC_DOCUMENT_MAX
    link = f"https://github.com/acme/widgets/blob/{head}/docs/spec.md"
    assert (f"{TRUNCATED}\n\nFull file (too long to show here): {link}\n\n---\n"
            f"Source: docs/spec.md @ {head}{FOOTER_TAIL}") in doc["content"]
    assert doc["content"].startswith(SPEC.rstrip())
    assert sync.read_desc_hash(doc["content"]) is not None


def test_uncommitted_source_line(pin_ctx, ws, tmp_path):
    f = tmp_path / "s.md"
    f.write_text("# Loose\n\nLinear: RET-1\n", encoding="utf-8")
    info = source_info(f)  # tmp_path may sit inside some git repo: the path is relative then
    assert info.commit is None
    out = sync_spec(f)
    assert out["ok"] is True
    (doc,) = _docs(ws)
    assert doc["title"] == "Spec: Loose"
    assert doc["content"].endswith(f"Source: {info.path} @ uncommitted{FOOTER_TAIL}\n\n"
                                   f"<!-- hermes-linear:h={sync.read_desc_hash(doc['content'])} -->")
    assert ws.issues[SELF]["description"] == (
        f"{SUMMARY}\n\n**Spec:** [Spec: Loose]({doc['url']}) · source "
        f"[{info.path} @ uncommitted]({info.link})\n{SPEC_PART_MARKER}")


def test_summary_markers_dropped(pin_ctx, ws, repo):
    """A summary cannot smuggle a part marker in: it would split the description later."""
    out = sync_spec(repo / "docs" / "spec.md", f"Sum {PLAN_PART_MARKER} end.")
    assert out["ok"] is True
    desc = ws.issues[SELF]["description"]
    assert PLAN_PART_MARKER not in desc and desc.startswith("Sum end.")
    assert split_plan_part(desc)[1] is None


def test_description_failure_reports_document(pin_ctx, ws, repo, monkeypatch):
    async def boom(issue_id, inp):
        raise LinearError("rate limited", code="RATELIMITED")
    monkeypatch.setattr(ws, "issue_update", boom)
    out = sync_spec(repo / "docs" / "spec.md")
    (doc,) = _docs(ws)
    assert out == {"ok": False, "error": "RATELIMITED", "message": "rate limited",
                   "document_id": doc["id"], "document_url": doc["url"]}
    monkeypatch.undo()
    pin_ctx()
    again = sync_spec(repo / "docs" / "spec.md")  # a re-run finds the document and finishes
    assert again["ok"] is True and again["document_skipped"] is True
    assert len(_docs(ws)) == 1 and ws.issues[SELF]["description"].startswith(SUMMARY)


def test_document_failure_writes_nothing_else(pin_ctx, ws, repo, monkeypatch):
    async def boom(*a, **kw):
        raise LinearError("content too long", code="INVALID_INPUT")
    monkeypatch.setattr(ws, "document_create", boom)
    out = sync_spec(repo / "docs" / "spec.md")
    assert out == {"ok": False, "error": "INVALID_INPUT", "message": "content too long"}
    assert ws.writes() == [] and ws.issues[SELF]["description"] == "old"


def test_sync_relative_path_rejected(pin_ctx, ws, repo, monkeypatch):
    monkeypatch.chdir(repo)  # even when the relative path would resolve against the cwd
    for rel in ("docs/spec.md", "./docs/spec.md", "~/spec.md"):
        assert sync_spec(rel) == {"ok": False, "error": "BAD_ARGS"}
    assert ws.writes() == []
    assert "Absolute path" in TOOL_SCHEMAS["linear_sync_spec"]["parameters"]["properties"][
        "path"]["description"]


def test_source_path_helper():
    assert sync.source_path("/a/b.md") == Path("/a/b.md")
    assert sync.source_path(" /a/b.md ") == Path("/a/b.md")
    for bad in ("a/b.md", "", "  ", None, 3, "~/b.md", "/a\0b"):
        assert sync.source_path(bad) is None


def test_sync_child_header_in_scope(pin_ctx, ws, tmp_path):
    f = tmp_path / "s.md"
    f.write_text("Linear: RET-2\n\n## 1. P\n", encoding="utf-8")
    assert sync_spec(f)["issue"] == "RET-2"
    (doc,) = _docs(ws, "uuid-child")
    assert doc["title"] == "Spec: s" and doc["content"].startswith("Linear: RET-2")
    assert ws.issues["uuid-child"]["description"].startswith(SUMMARY)


def test_missing_header_error(pin_ctx, ws, tmp_path):
    f = tmp_path / "s.md"
    f.write_text("# No header\n\n## 1. P\n", encoding="utf-8")
    assert sync_spec(f) == {"ok": False, "error": "NO_LINEAR_HEADER"}
    assert ws.writes() == []


@pytest.mark.parametrize("args", [None, {}, {"summary": SUMMARY}, {"path": "", "summary": SUMMARY},
                                  {"path": 3, "summary": SUMMARY},
                                  {"path": "/nope/x.md", "summary": SUMMARY},
                                  {"path": "x.md", "summary": SUMMARY}])
def test_bad_args(pin_ctx, ws, args):
    assert linear_sync_spec(args) == {"ok": False, "error": "BAD_ARGS"}
    assert ws.writes() == []


def test_unreadable_file_is_bad_args(pin_ctx, ws, tmp_path):
    (tmp_path / "bin.md").write_bytes(b"Linear: RET-1\n\xff\xfe")
    assert sync_spec(tmp_path / "bin.md")["error"] == "BAD_ARGS"
    assert sync_spec(tmp_path)["error"] == "BAD_ARGS"  # a directory
    assert ws.writes() == []


def test_oversize_file_is_bad_args(pin_ctx, ws, tmp_path):
    f = tmp_path / "s.md"
    head = b"Linear: RET-1\n\n## 1. P\n\n"
    f.write_bytes(head + b"x" * (sync.MAX_SOURCE_BYTES - len(head)))
    assert f.stat().st_size == sync.MAX_SOURCE_BYTES
    out = sync_spec(f)
    assert out["ok"] is True and out["inline"] is False  # exactly the limit is read (and cut)
    f.write_bytes(head + b"x" * (sync.MAX_SOURCE_BYTES - len(head) + 1))
    assert sync_spec(f) == {"ok": False, "error": "BAD_ARGS"}
    assert _names(ws) == ["document_create", "issue_update"]
    assert sync.read_source(f, max_bytes=10) is None


def test_refused_outside_linear_session(pin_ctx, ws, tmp_path):
    pin_ctx(linear=False)
    f = tmp_path / "s.md"
    f.write_text("Linear: RET-1\n", encoding="utf-8")
    assert sync_spec(f) == {"ok": False, "error": "NOT_LINEAR_SESSION"}
    assert ws.writes() == []


def test_unknown_header_ticket(pin_ctx, ws, tmp_path):
    f = tmp_path / "s.md"
    f.write_text("Linear: NOPE-1\n", encoding="utf-8")
    assert sync_spec(f) == {"ok": False, "error": "NOT_FOUND",
                            "message": "Entity not found: Issue"}


def test_file_io_happens_off_the_loop(pin_ctx, ws, tmp_path, monkeypatch, loop):
    """File read and git run in the tool thread, never on the gateway loop."""
    seen: list[str] = []
    real = sync.source_info

    def spy(*a, **kw):
        seen.append(threading.current_thread().name)
        return real(*a, **kw)
    monkeypatch.setattr(sync, "source_info", spy)
    f = tmp_path / "s.md"
    f.write_text("Linear: RET-1\n", encoding="utf-8")
    assert sync_spec(f)["ok"] is True
    assert seen and "GATEWAY-LOOP" not in seen


def test_on_loop_call_refused_before_any_io(pin_ctx, ws, tmp_path, monkeypatch, loop):
    """PluginContext.dispatch_tool path: invoked on the adapter loop -> WRONG_THREAD with no
    file read and no git call (they would block the gateway loop)."""
    f = tmp_path / "s.md"
    f.write_text("Linear: RET-1\n", encoding="utf-8")
    calls: list[str] = []
    for name in ("source_path", "read_source", "source_info", "run_git"):
        real = getattr(sync, name)
        monkeypatch.setattr(sync, name,
                            lambda *a, _n=name, _r=real, **kw: (calls.append(_n), _r(*a, **kw))[1])
    real_open, real_run = Path.open, sync.subprocess.run
    monkeypatch.setattr(Path, "open", lambda *a, **kw: calls.append("open") or real_open(*a, **kw))
    monkeypatch.setattr(sync.subprocess, "run",
                        lambda *a, **kw: calls.append("subprocess") or real_run(*a, **kw))

    async def on_loop():
        return sync_spec(f)
    out = asyncio.run_coroutine_threadsafe(on_loop(), loop).result(5)
    assert out == {"ok": False, "error": "WRONG_THREAD"}
    # a plain callback on the loop thread is refused too
    done = threading.Event()
    box: list = []
    loop.call_soon_threadsafe(lambda: (box.append(sync_spec(f)), done.set()))
    assert done.wait(5)
    assert box == [{"ok": False, "error": "WRONG_THREAD"}]
    assert calls == [] and ws.writes() == []


def test_adapter_loop_thread_detected_without_running_loop(monkeypatch, adapter, loop):
    """The thread-identity fallback: a live adapter loop's thread counts as on-loop."""
    monkeypatch.setattr(agent_tools.adapter_mod, "ADAPTERS", {"default": adapter})
    monkeypatch.setattr(agent_tools, "_has_running_loop", lambda: False)
    assert agent_tools._on_any_loop() is False  # this (test) thread
    done = threading.Event()
    box: list = []
    loop.call_soon_threadsafe(lambda: (box.append(agent_tools._on_any_loop()), done.set()))
    assert done.wait(5) and box == [True]
