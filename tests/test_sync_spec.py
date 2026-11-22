"""linear_sync_spec (RET-77): the pure spec helpers in ``sync.py`` and the tool itself.

The tool runs against ``FakeWorkspace`` on a real background loop, as in
``test_agent_tools_generic.py``; context resolution is pinned with ``pin_ctx``.
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
from hermes_linear.agent_tools import TOOL_HANDLERS, TOOL_SCHEMAS, linear_sync_spec
from hermes_linear.sync import (SPEC_INLINE_MAX, footer, git_short_commit, read_linear_header,
                                render_spec, source_info, spec_summary)

SELF = "uuid-self"
FOOTER_TAIL = " · edit by asking in this thread"

SPEC = """# Thing — Design

Linear: RET-1

Some preamble.

## 1. Purpose

Why we build the thing.

Second paragraph of purpose.

## 2. Design

How it works.
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
    w.add_issue("RET-1", title="Session issue", uuid=SELF, description="old")
    w.add_issue("RET-2", title="Child", uuid="uuid-child", parent=SELF)
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


# ----------------------------------------------------------------- sync.py (pure)


def test_header_parse():
    assert read_linear_header(SPEC) == "RET-1"
    assert read_linear_header("Linear:RET-7\nLinear: RET-8\n") == "RET-7"  # first wins
    assert read_linear_header("# T\n\nLinear:   ABC-12  trailing\n") == "ABC-12"
    assert read_linear_header("no header\n  Linear: RET-1\n") is None  # must start the line
    assert read_linear_header("Linear:\n") is None
    assert read_linear_header("") is None


def test_render_has_exact_footer():
    out = render_spec(SPEC, "docs/spec.md", "abc1234")
    assert out == (SPEC.rstrip() + "\n\n---\n"
                   + "Source: docs/spec.md @ abc1234 · edit by asking in this thread")
    assert footer("docs/spec.md", "abc1234") == (
        "Source: docs/spec.md @ abc1234 · edit by asking in this thread")


def test_uncommitted_file_footer_says_uncommitted():
    out = render_spec(SPEC, "docs/spec.md", None)
    assert out.endswith("Source: docs/spec.md @ uncommitted" + FOOTER_TAIL)


def test_spec_summary_is_section_one():
    s = spec_summary(SPEC)
    assert s.startswith("## 1. Purpose")
    assert "Second paragraph of purpose." in s
    assert "## 2." not in s and "How it works" not in s
    assert "### sub" in spec_summary("## 1. A\n\n### sub\n\nx\n## 2. B\n")  # ### is not a stop
    assert spec_summary("## 1. Last\n\nbody\n") == "## 1. Last\n\nbody"
    assert spec_summary("# no sections\n") == ""


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


# ----------------------------------------------------------------- linear_sync_spec


def test_schema_and_handler():
    s = TOOL_SCHEMAS["linear_sync_spec"]
    assert s["name"] == "linear_sync_spec" and "Linear app" in s["description"]
    assert s["parameters"]["required"] == ["path"]
    assert TOOL_HANDLERS["linear_sync_spec"] is linear_sync_spec


def test_sync_overwrites_description(pin_ctx, ws, repo):
    f = repo / "docs" / "spec.md"
    head = _git(repo, "rev-parse", "--short", "HEAD")
    out = linear_sync_spec({"path": str(f)})
    assert out == {"ok": True, "issue": "RET-1", "url": "https://linear.app/x/issue/RET-1",
                   "inline": True}
    want = render_spec(SPEC, "docs/spec.md", head)
    assert ws.issues[SELF]["description"] == want
    # every call overwrites, even with no change to the file
    ws.issues[SELF]["description"] = "edited by a human"
    assert linear_sync_spec({"path": str(f)})["ok"] is True
    assert ws.issues[SELF]["description"] == want
    assert [c[0] for c in ws.writes()] == ["issue_update", "issue_update"]
    assert set(ws.writes()[0][1][1]) == {"description"}


def test_sync_relative_path_uses_cwd(pin_ctx, ws, repo, monkeypatch):
    monkeypatch.chdir(repo)
    assert linear_sync_spec({"path": "docs/spec.md"})["ok"] is True
    assert "Source: docs/spec.md @ " in ws.issues[SELF]["description"]


def test_sync_child_header_in_scope(pin_ctx, ws, tmp_path):
    f = tmp_path / "s.md"
    f.write_text("Linear: RET-2\n\n## 1. P\n", encoding="utf-8")
    assert linear_sync_spec({"path": str(f)})["issue"] == "RET-2"
    assert ws.issues["uuid-child"]["description"].startswith("Linear: RET-2")


def test_oversize_falls_back_to_summary(pin_ctx, ws, repo):
    f = repo / "docs" / "spec.md"
    big = SPEC + "\n## 3. Appendix\n\n" + "x" * 250_000 + "\n"
    f.write_text(big, encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "big")
    head = _git(repo, "rev-parse", "--short", "HEAD")
    out = linear_sync_spec({"path": str(f)})
    assert out["ok"] is True and out["inline"] is False
    desc = ws.issues[SELF]["description"]
    assert len(desc) < SPEC_INLINE_MAX
    assert desc.startswith(spec_summary(big))
    assert f"https://github.com/acme/widgets/blob/{head}/docs/spec.md" in desc
    assert desc.endswith(f"Source: docs/spec.md @ {head}" + FOOTER_TAIL)
    assert "## 2. Design" not in desc


def test_inline_limit_counts_footer(pin_ctx, ws, tmp_path):
    f = tmp_path / "s.md"
    head = "Linear: RET-1\n\n## 1. P\n\n"
    f.write_text(head, encoding="utf-8")
    info = source_info(f)
    probe = render_spec(head + "y", info.path, info.commit)  # "y": no trailing space stripped
    pad = SPEC_INLINE_MAX - len(probe) + 1
    f.write_text(head + "y" * pad, encoding="utf-8")
    assert linear_sync_spec({"path": str(f)})["inline"] is True
    assert len(ws.issues[SELF]["description"]) == SPEC_INLINE_MAX
    f.write_text(head + "y" * (pad + 1), encoding="utf-8")
    assert linear_sync_spec({"path": str(f)})["inline"] is False
    assert len(ws.issues[SELF]["description"]) <= SPEC_INLINE_MAX


def test_oversize_section_one_is_truncated(pin_ctx, ws, tmp_path):
    f = tmp_path / "s.md"
    f.write_text("Linear: RET-1\n\n## 1. Huge\n\n" + "z" * 300_000 + "\n", encoding="utf-8")
    out = linear_sync_spec({"path": str(f)})
    assert out["ok"] is True and out["inline"] is False
    desc = ws.issues[SELF]["description"]
    assert len(desc) <= SPEC_INLINE_MAX
    assert desc.startswith("## 1. Huge") and desc.endswith(FOOTER_TAIL)


def test_missing_header_error(pin_ctx, ws, tmp_path):
    f = tmp_path / "s.md"
    f.write_text("# No header\n\n## 1. P\n", encoding="utf-8")
    assert linear_sync_spec({"path": str(f)}) == {"ok": False, "error": "NO_LINEAR_HEADER"}
    assert ws.writes() == []


@pytest.mark.parametrize("args", [None, {}, {"path": ""}, {"path": 3}, {"path": "/nope/x.md"}])
def test_bad_args(pin_ctx, ws, args):
    assert linear_sync_spec(args) == {"ok": False, "error": "BAD_ARGS"}
    assert ws.writes() == []


def test_unreadable_file_is_bad_args(pin_ctx, ws, tmp_path):
    (tmp_path / "bin.md").write_bytes(b"Linear: RET-1\n\xff\xfe")
    assert linear_sync_spec({"path": str(tmp_path / "bin.md")})["error"] == "BAD_ARGS"
    assert linear_sync_spec({"path": str(tmp_path)})["error"] == "BAD_ARGS"  # a directory
    assert ws.writes() == []


def test_out_of_scope_header_refused(pin_ctx, ws, tmp_path):
    f = tmp_path / "s.md"
    f.write_text("Linear: OTH-9\n", encoding="utf-8")
    assert linear_sync_spec({"path": str(f)}) == {"ok": False, "error": "OUT_OF_SCOPE"}
    assert ws.writes() == []


def test_refused_outside_linear_session(pin_ctx, ws, tmp_path):
    pin_ctx(linear=False)
    f = tmp_path / "s.md"
    f.write_text("Linear: RET-1\n", encoding="utf-8")
    assert linear_sync_spec({"path": str(f)}) == {"ok": False, "error": "NOT_LINEAR_SESSION"}
    assert ws.writes() == []


def test_unknown_header_ticket(pin_ctx, ws, tmp_path):
    f = tmp_path / "s.md"
    f.write_text("Linear: NOPE-1\n", encoding="utf-8")
    assert linear_sync_spec({"path": str(f)}) == {"ok": False, "error": "NOT_FOUND"}


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
    assert linear_sync_spec({"path": str(f)})["ok"] is True
    assert seen and "GATEWAY-LOOP" not in seen
