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


def test_sync_relative_path_rejected(pin_ctx, ws, repo, monkeypatch):
    monkeypatch.chdir(repo)  # even when the relative path would resolve against the cwd
    for rel in ("docs/spec.md", "./docs/spec.md", "~/spec.md"):
        assert linear_sync_spec({"path": rel}) == {"ok": False, "error": "BAD_ARGS"}
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
    pad = SPEC_INLINE_MAX - sync.utf16_len(probe) + 1
    f.write_text(head + "y" * pad, encoding="utf-8")
    assert linear_sync_spec({"path": str(f)})["inline"] is True
    assert sync.utf16_len(ws.issues[SELF]["description"]) == SPEC_INLINE_MAX
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


def test_length_is_utf16_units(pin_ctx, ws, tmp_path):
    """Emoji are 2 UTF-16 units (JavaScript length): 120k of them do not fit inline."""
    assert sync.utf16_len("a😀é") == 4
    assert sync.utf16_truncate("a😀b", 2) == "a"  # never splits a surrogate pair
    assert sync.utf16_truncate("a😀b", 3) == "a😀"
    f = tmp_path / "s.md"
    f.write_text("Linear: RET-1\n\n## 1. Emoji\n\n" + "😀" * 120_000 + "\n", encoding="utf-8")
    assert f.stat().st_size < sync.MAX_SOURCE_BYTES
    out = linear_sync_spec({"path": str(f)})
    assert out["ok"] is True and out["inline"] is False  # len() 120k, but 240k units
    desc = ws.issues[SELF]["description"]
    assert sync.utf16_len(desc) <= SPEC_INLINE_MAX
    assert sync.utf16_len(desc) > SPEC_INLINE_MAX - 10  # truncated to the limit, not len()
    assert desc.startswith("## 1. Emoji") and desc.endswith(FOOTER_TAIL)
    assert "\n\n…\n\nFull spec" in desc
    desc.encode("utf-8")  # no lone surrogates


def test_missing_header_error(pin_ctx, ws, tmp_path):
    f = tmp_path / "s.md"
    f.write_text("# No header\n\n## 1. P\n", encoding="utf-8")
    assert linear_sync_spec({"path": str(f)}) == {"ok": False, "error": "NO_LINEAR_HEADER"}
    assert ws.writes() == []


@pytest.mark.parametrize("args", [None, {}, {"path": ""}, {"path": 3}, {"path": "/nope/x.md"},
                                  {"path": "x.md"}])
def test_bad_args(pin_ctx, ws, args):
    assert linear_sync_spec(args) == {"ok": False, "error": "BAD_ARGS"}
    assert ws.writes() == []


def test_unreadable_file_is_bad_args(pin_ctx, ws, tmp_path):
    (tmp_path / "bin.md").write_bytes(b"Linear: RET-1\n\xff\xfe")
    assert linear_sync_spec({"path": str(tmp_path / "bin.md")})["error"] == "BAD_ARGS"
    assert linear_sync_spec({"path": str(tmp_path)})["error"] == "BAD_ARGS"  # a directory
    assert ws.writes() == []


def test_oversize_file_is_bad_args(pin_ctx, ws, tmp_path):
    f = tmp_path / "s.md"
    head = b"Linear: RET-1\n\n## 1. P\n\n"
    f.write_bytes(head + b"x" * (sync.MAX_SOURCE_BYTES - len(head)))
    assert f.stat().st_size == sync.MAX_SOURCE_BYTES
    assert linear_sync_spec({"path": str(f)})["ok"] is True  # exactly the limit is read
    f.write_bytes(head + b"x" * (sync.MAX_SOURCE_BYTES - len(head) + 1))
    assert linear_sync_spec({"path": str(f)}) == {"ok": False, "error": "BAD_ARGS"}
    assert len(ws.writes()) == 1
    assert sync.read_source(f, max_bytes=10) is None


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
        return linear_sync_spec({"path": str(f)})
    out = asyncio.run_coroutine_threadsafe(on_loop(), loop).result(5)
    assert out == {"ok": False, "error": "WRONG_THREAD"}
    # a plain callback on the loop thread is refused too
    done = threading.Event()
    box: list = []
    loop.call_soon_threadsafe(lambda: (box.append(linear_sync_spec({"path": str(f)})),
                                       done.set()))
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
