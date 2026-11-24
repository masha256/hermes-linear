"""Tests for hermes_linear.tools_attach: the attach path policy (RET-120 spec §2.4).

Runs the REAL ``gateway.platforms.base.validate_media_delivery_path`` (no stub) on tmp
files. ``HOME`` points at ``tmp_path / "home"`` so ``$HOME/.ssh`` is under the test's
control; strict delivery mode is switched off so the default policy is what's judged.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from conftest import REPO_ROOT
from test_agent_tools_generic import SELF, adapter, loop, pin_ctx, ws  # noqa: F401 - fixtures
from test_agent_tools_runtime import put_turn

from hermes_linear import agent_tools, plugin, tool_approval, tools_attach
from hermes_linear.agent_tools import TOOL_HANDLERS, TOOL_SCHEMAS
from hermes_linear.linear_client import LinearError, UploadError
from hermes_linear.tool_approval import digest
from hermes_linear.tools_attach import AttachFile, load_attach_file


def _hermes_refuses_env() -> bool:
    """Ask the REAL policy, once, whether it refuses a project ``.env`` (spec Q1c).

    Hermes versions up to 2026-11-09 (e.g. the CI-pinned 3b42157) have no project-.env
    check: such a file outside the denylisted dirs passes. Behaviour-based: a fresh
    ``.env`` and a plain control file side by side under a temp dir, with ``HOME`` and
    strict mode set as the ``home`` fixture sets them. If even the control is refused the
    answer is True, so the .env tests run and fail loudly instead of being skipped.
    """
    from gateway.platforms.base import validate_media_delivery_path

    root = Path(tempfile.mkdtemp(prefix="hl-env-probe-"))
    saved = {k: os.environ.get(k) for k in ("HOME", "HERMES_MEDIA_DELIVERY_STRICT")}
    try:
        (root / "home" / ".ssh").mkdir(parents=True)
        work = root / "work"
        work.mkdir()
        (work / ".env").write_text("SECRET=1")
        (work / "control.txt").write_text("hi")
        os.environ["HOME"] = str(root / "home")
        os.environ.pop("HERMES_MEDIA_DELIVERY_STRICT", None)
        if not validate_media_delivery_path(str(work / "control.txt")):
            return True
        return not validate_media_delivery_path(str(work / ".env"))
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(root, ignore_errors=True)


HERMES_REFUSES_ENV = _hermes_refuses_env()
needs_env_check = pytest.mark.skipif(
    not HERMES_REFUSES_ENV, reason="installed Hermes's media policy predates the project .env check")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("HERMES_MEDIA_DELIVERY_STRICT", raising=False)
    return h


@pytest.fixture
def work(tmp_path, home):
    d = tmp_path / "work"
    d.mkdir()
    return d


def _code(path, session_key=None) -> str:
    with pytest.raises(agent_tools._Refusal) as ei:
        load_attach_file(str(path), session_key)
    return ei.value.code


def test_allowed_file_loads(work):
    p = work / "x.png"
    p.write_bytes(b"\x89PNG-bytes")
    f = load_attach_file(str(p), "sess-1")
    assert f == AttachFile(name="x.png", content_type="image/png", data=b"\x89PNG-bytes")


def test_relative_path_denied(work, monkeypatch):
    (work / "x.txt").write_text("hi")
    monkeypatch.chdir(work)
    assert _code("x.txt") == "PATH_DENIED"


def test_missing_file_not_found(work):
    assert _code(work / "nope.txt") == "FILE_NOT_FOUND"


def test_directory_not_found(work):
    assert _code(work) == "FILE_NOT_FOUND"


@needs_env_check
def test_env_file_denied(work):
    (work / ".env").write_text("SECRET=1")
    assert _code(work / ".env") == "PATH_DENIED"


def test_policy_accepted_path_is_read(work, monkeypatch):
    """Spec Q1c: the plugin adds no check of its own; whatever the policy accepts is read.

    Pins the older-Hermes behaviour on every Hermes: a policy that accepts a ``.env``
    (as Hermes up to 2026-11-09 does) gets it attached, the same as Slack's MEDIA there.
    """
    import gateway.platforms.base as base

    env = work / ".env"
    env.write_text("SECRET=1")
    monkeypatch.setattr(base, "validate_media_delivery_path",
                        lambda path, session_key="": str(env))
    f = load_attach_file(str(env), None)
    assert (f.name, f.data) == (".env", b"SECRET=1")


def test_ssh_dir_denied(home):
    key = home / ".ssh" / "id_ed25519"
    key.write_text("PRIVATE KEY")
    assert _code(key) == "PATH_DENIED"


def test_symlink_to_denied_target_refused(home, work):
    key = home / ".ssh" / "id_ed25519"
    key.write_text("PRIVATE KEY")
    (work / "innocent.txt").symlink_to(key)
    assert _code(work / "innocent.txt") == "PATH_DENIED"


@needs_env_check
def test_symlink_to_env_refused(work):
    env = work / "sub" / ".env"
    env.parent.mkdir()
    env.write_text("SECRET=1")
    (work / "notes.md").symlink_to(env)
    assert _code(work / "notes.md") == "PATH_DENIED"


def test_symlink_to_allowed_target_reads_target_keeps_given_name(work):
    target = work / "real.bin"
    target.write_bytes(b"abc")
    (work / "link.txt").symlink_to(target)
    f = load_attach_file(str(work / "link.txt"), None)
    assert (f.name, f.content_type, f.data) == ("link.txt", "text/plain", b"abc")


def test_too_large(work, monkeypatch):
    monkeypatch.setattr(tools_attach, "ATTACH_MAX_BYTES", 10)
    p = work / "big.txt"
    p.write_bytes(b"x" * 11)
    assert _code(p) == "FILE_TOO_LARGE"
    p.write_bytes(b"x" * 10)
    assert load_attach_file(str(p), None).data == b"x" * 10


def test_unknown_extension_is_octet_stream(work):
    p = work / "blob.zzqq"
    p.write_bytes(b"\x00\x01")
    assert load_attach_file(str(p), None).content_type == "application/octet-stream"


def test_import_failure_fails_closed(work, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def boom(name, *a, **kw):
        if name == "gateway.platforms.base":
            raise ImportError("no gateway")
        return real_import(name, *a, **kw)

    p = work / "x.txt"
    p.write_text("hi")
    monkeypatch.setattr(builtins, "__import__", boom)
    assert _code(p) == "PATH_DENIED"


def test_max_bytes_is_50_mb():
    assert tools_attach.ATTACH_MAX_BYTES == 50 * 1024 * 1024




# --- linear_attach (RET-134; spec §1.3, §2.4) ------------------------------------------------
#
# Handlers run against ``FakeWorkspace`` (``ws``: RET-1 = the session issue SELF, RET-2 its
# child, OTH-9 unrelated with no parent) on a real background loop, as in
# ``test_agent_tools_generic``; the files are real tmp files the real path policy allows.

linear_attach = TOOL_HANDLERS.get("linear_attach")
OTHER = "uuid-other"
PR = "https://github.com/masha256/hermes-linear/pull/12"


@pytest.fixture
def png(work):
    p = work / "x.png"
    p.write_bytes(b"\x89PNG-bytes")
    return p


def _calls(ws, *names):
    return [c for c in ws.calls if c[0] in names]


def _uploads(ws):
    return _calls(ws, "file_upload")


def _attaches(ws):
    return _calls(ws, "attachment_create", "attachment_link_url", "attachment_link_github_pr")


def test_file_attach_in_scope(pin_ctx, ws, png):
    out = linear_attach({"id": "RET-1", "path": str(png)})
    [(_, (name, ctype, data))] = _uploads(ws)
    assert (name, ctype, data) == ("x.png", "image/png", b"\x89PNG-bytes")
    asset = next(iter(ws.uploads))
    assert _attaches(ws) == [("attachment_create", (SELF, "x.png", None, asset))]
    att = next(iter(ws.attachments))
    assert out == {"ok": True, "attachment_id": att, "asset_url": asset, "issue": "RET-1",
                   "url": asset}


def test_file_attach_passes_title_and_subtitle(pin_ctx, ws, png):
    out = linear_attach({"id": "RET-2", "path": str(png), "title": "Screenshot",
                         "subtitle": "after the fix"})
    assert out["ok"] is True and out["issue"] == "RET-2"
    [(_, args)] = _attaches(ws)
    assert args[:3] == ("uuid-child", "Screenshot", "after the fix")


def test_github_pr_url_uses_link_github_pr(pin_ctx, ws):
    out = linear_attach({"id": "RET-1", "url": PR, "title": "ignored for a PR"})
    assert _uploads(ws) == []
    assert _attaches(ws) == [("attachment_link_github_pr", (SELF, PR))]
    assert out == {"ok": True, "attachment_id": next(iter(ws.attachments)), "issue": "RET-1",
                   "url": PR}


def test_plain_url_uses_link_url(pin_ctx, ws):
    url = "https://example.test/build/42"
    out = linear_attach({"id": "RET-1", "url": url, "title": "Build 42"})
    assert _uploads(ws) == []
    assert _attaches(ws) == [("attachment_link_url", (SELF, url, "Build 42"))]
    assert out["ok"] is True and out["url"] == url and "asset_url" not in out
    # A PR-looking URL off github.com, or a non-PR GitHub URL, is a plain link.
    for other in ("https://github.com/a/b/issues/3", "https://gitlab.com/a/b/pull/3"):
        linear_attach({"id": "RET-1", "url": other})
    assert [c[0] for c in _attaches(ws)][1:] == ["attachment_link_url"] * 2


def test_github_pr_re():
    ok = [PR, PR + "/files", PR + "?x=1", PR + "#discussion"]
    bad = ["https://github.com/a/b/pull/x", "http://github.com/a/b/pull/1",
           "https://github.com/a/b/pull/1x", "https://github.com/a/pull/1"]
    assert all(tools_attach.GITHUB_PR_RE.match(u) for u in ok)
    assert not any(tools_attach.GITHUB_PR_RE.match(u) for u in bad)


def test_embed_uploads_only(pin_ctx, ws, png, work):
    out = linear_attach({"path": str(png), "embed": True})
    asset = next(iter(ws.uploads))
    assert out == {"ok": True, "asset_url": asset, "markdown": f"![x.png]({asset})"}
    txt = work / "notes.txt"
    txt.write_text("hello")
    out = linear_attach({"path": str(txt), "embed": True})
    assert out["markdown"] == f"[notes.txt]({out['asset_url']})"
    assert len(_uploads(ws)) == 2 and _attaches(ws) == []


def test_embed_markdown_escapes_name():
    assert tools_attach.embed_markdown("a]b).png", "image/png", "https://u/1") == "![a\\]b\\).png](https://u/1)"
    assert tools_attach.embed_markdown("[x](y).txt", "text/plain", "https://u/2") == \
        "[\\[x\\]\\(y\\).txt](https://u/2)"
    assert tools_attach.embed_markdown("a\\b\nc.bin", "application/octet-stream", "https://u/3") == \
        "[a\\\\b c.bin](https://u/3)"


BAD_ARGS = [
    {"id": "RET-1", "path": "/x/y.png", "url": "https://example.test"},  # both
    {"id": "RET-1"},  # neither
    {"id": "RET-1", "path": "/x/y.png", "embed": True},  # embed with id
    {"url": "https://example.test", "embed": True},  # embed with url
    {"path": "/x/y.png"},  # no id, no embed
    {"url": "https://example.test"},  # no id
    {"id": "RET-1", "url": "ftp://x"},
    {"id": "RET-1", "url": "javascript:alert(1)"},
    {"id": "RET-1", "url": "https://"},  # no host
    {"id": "RET-1", "path": 3},
    {"id": "RET-1", "url": "https://example.test", "title": 5},
    {"path": "/x/y.png", "embed": "yes"},
    {"id": "  ", "url": "https://example.test"},
]


@pytest.mark.parametrize("args", BAD_ARGS)
def test_bad_combinations(pin_ctx, ws, args):
    assert linear_attach(dict(args)) == {"ok": False, "error": "BAD_ARGS"}
    assert ws.calls == []


@needs_env_check
def test_env_policy_error_before_any_call(pin_ctx, ws, work):
    (work / ".env").write_text("SECRET=1")
    assert linear_attach({"id": "RET-1", "path": str(work / ".env")})["error"] == "PATH_DENIED"
    assert ws.calls == []


def test_file_policy_errors_before_any_call(pin_ctx, ws, work):
    assert linear_attach({"id": "RET-1", "path": "rel.txt"})["error"] == "PATH_DENIED"
    assert linear_attach({"path": str(work / "nope.png"), "embed": True})["error"] == \
        "FILE_NOT_FOUND"
    assert ws.calls == []


def test_parse_passes_the_session_key(pin_ctx, ws, png, monkeypatch):
    from gateway.session_context import clear_session_vars, set_session_vars

    seen = []
    real = tools_attach.load_attach_file
    monkeypatch.setattr(tools_attach, "load_attach_file",
                        lambda path, key: seen.append(key) or real(path, key))
    tokens = set_session_vars(platform="linear", session_key="agent:main:linear:x")
    try:
        assert linear_attach({"path": str(png), "embed": True})["ok"] is True
    finally:
        clear_session_vars(tokens)
    assert seen == ["agent:main:linear:x"]


def test_embed_needs_linear_session(pin_ctx, ws, png):
    pin_ctx(linear=False)
    assert linear_attach({"path": str(png), "embed": True}) == {
        "ok": False, "error": "NOT_LINEAR_SESSION"}
    assert ws.calls == []


def test_outside_attach_url_is_gated(pin_ctx, ws, adapter):
    pin_ctx(linear=False)  # RET-148: the id path goes through the outside gate
    args = {"id": "RET-1", "url": "https://example.com/x"}
    out = linear_attach(dict(args))
    assert out["ok"] is False and out["error"] == "APPROVAL_REQUIRED"
    assert "outside a Linear session" in out["message"]
    assert _uploads(ws) == [] and ws.writes() == []
    req = adapter._store.request("sk", digest("linear_attach", args))
    assert req is not None and req.targets == [SELF] and req.tool == "linear_attach"


def test_out_of_scope_first_pass_uploads_nothing(pin_ctx, ws, adapter, png):
    args = {"id": "OTH-9", "path": str(png)}
    out = linear_attach(dict(args))
    assert out["ok"] is False and out["error"] == "APPROVAL_REQUIRED"
    assert "OTH-9" in out["message"] and "linear_attach" in out["message"]
    assert _uploads(ws) == [] and ws.writes() == []
    req = adapter._store.request("sk", digest("linear_attach", args))
    assert req is not None and req.targets == [OTHER] and req.tool == "linear_attach"


def test_out_of_scope_url_first_pass_links_nothing(pin_ctx, ws):
    out = linear_attach({"id": "OTH-9", "url": PR})
    assert out["error"] == "APPROVAL_REQUIRED" and ws.writes() == []


def test_approved_rerun_uploads_and_attaches_once(pin_ctx, ws, adapter, png):
    args = {"id": "OTH-9", "path": str(png)}
    assert linear_attach(dict(args))["error"] == "APPROVAL_REQUIRED"
    dg = digest("linear_attach", args)
    assert adapter._store.arm_request("sk", dg) is not None
    out = linear_attach(dict(args))
    assert out["ok"] is True and out["issue"] == "OTH-9"
    assert len(_uploads(ws)) == 1
    assert [(c[0], c[1][0]) for c in _attaches(ws)] == [("attachment_create", OTHER)]
    assert adapter._store.request("sk", dg) is None  # consumed by the approved pass


def test_approved_rerun_with_missing_file(pin_ctx, ws, adapter, png):
    """Review Focus 4: the digest binds the path, not the bytes; the re-run reads again."""
    args = {"id": "OTH-9", "path": str(png)}
    assert linear_attach(dict(args))["error"] == "APPROVAL_REQUIRED"
    dg = digest("linear_attach", args)
    adapter._store.arm_request("sk", dg)
    png.unlink()
    assert linear_attach(dict(args)) == {"ok": False, "error": "FILE_NOT_FOUND"}
    assert ws.writes() == []
    assert adapter._store.take_armed_request("sk", dg) is not None  # not consumed


def test_deny_mode_refuses_out_of_scope(pin_ctx, ws, adapter, png):
    from test_agent_tools_generic import deny_mode

    deny_mode(adapter)
    assert linear_attach({"id": "OTH-9", "path": str(png)})["error"] == "OUT_OF_SCOPE"
    assert ws.writes() == []


def test_upload_failed(pin_ctx, ws, png):
    async def boom(name, content_type, data):
        raise UploadError(403)

    ws.file_upload = boom
    assert linear_attach({"id": "RET-1", "path": str(png)}) == {
        "ok": False, "error": "UPLOAD_FAILED", "status": 403}
    assert _attaches(ws) == []
    assert linear_attach({"path": str(png), "embed": True})["status"] == 403


def test_attach_failure_after_upload_returns_asset_url(pin_ctx, ws, png):
    async def boom(*a):
        raise LinearError("Unable to create attachment", code="INPUT_ERROR",
                          detail="Something else went wrong.")

    ws.attachment_create = boom
    out = linear_attach({"id": "RET-1", "path": str(png)})
    asset = next(iter(ws.uploads))
    assert out == {"ok": False, "error": "LINEAR_ERROR", "message": "Something else went wrong.",
                   "code": "INPUT_ERROR", "asset_url": asset}


def test_attach_failure_after_upload_without_code_has_no_code_key(pin_ctx, ws, png):
    async def boom(*a):
        raise LinearError("Unable to create attachment")

    ws.attachment_create = boom
    out = linear_attach({"id": "RET-1", "path": str(png)})
    assert out["error"] == "LINEAR_ERROR" and "code" not in out and out["asset_url"]


@pytest.mark.parametrize("method,url,message", [
    ("attachment_link_github_pr", PR, "This URL has already been linked with RET-92."),
    ("attachment_link_url", "https://example.test/a", "An attachment with the same URL already exists."),
])
def test_relink_is_already_linked(pin_ctx, ws, method, url, message):
    """Ruling R4 (spec §4 item 4): Linear refuses a re-link with INPUT_ERROR."""
    async def boom(*a):
        raise LinearError("Unable to create issue attachment", code="INPUT_ERROR",
                          detail=message)

    setattr(ws, method, boom)
    assert linear_attach({"id": "RET-1", "url": url}) == {
        "ok": False, "error": "ALREADY_LINKED", "message": message}


def test_relink_matches_the_message_text_case_insensitively(pin_ctx, ws):
    async def boom(*a):
        raise LinearError("THIS URL HAS ALREADY BEEN LINKED WITH RET-92.", code="INPUT_ERROR")

    ws.attachment_link_url = boom
    out = linear_attach({"id": "RET-1", "url": "https://example.test/a"})
    assert out["error"] == "ALREADY_LINKED"
    assert out["message"] == "THIS URL HAS ALREADY BEEN LINKED WITH RET-92."


@pytest.mark.parametrize("exc, code", [
    (LinearError("Unable to create issue attachment", code="INPUT_ERROR",
                 detail="Argument Validation Error: url must be a URL."), "INPUT_ERROR"),
    (LinearError("An attachment with the same URL already exists.", code="RATELIMITED"),
     "RATELIMITED"),
    (LinearError("boom"), "LINEAR_ERROR"),  # no code
])
def test_other_url_errors_pass_their_code(pin_ctx, ws, exc, code):
    """Not a re-link: the raw Linear code, as in file mode and the other tools."""
    async def boom(*a):
        raise exc

    ws.attachment_link_url = boom
    out = linear_attach({"id": "RET-1", "url": "https://example.test/a"})
    assert out["ok"] is False and out["error"] == code
    assert out["message"] == (exc.detail or str(exc)) and "asset_url" not in out


def test_unknown_issue_is_not_found(pin_ctx, ws, png):
    assert linear_attach({"id": "NOPE-1", "path": str(png)})["error"] == "NOT_FOUND"
    assert ws.writes() == []


def test_wrong_thread_on_loop(pin_ctx, ws, loop, png):
    async def on_loop():
        return linear_attach({"id": "RET-1", "path": str(png)})

    out = asyncio.run_coroutine_threadsafe(on_loop(), loop).result(10)
    assert out == {"ok": False, "error": "WRONG_THREAD"}
    assert ws.calls == []


def test_never_raises(pin_ctx, ws, png):
    async def boom(*a):
        raise RuntimeError("bug")

    ws.attachment_create = boom
    assert linear_attach({"id": "RET-1", "path": str(png)})["error"] == "INTERNAL_ERROR"
    assert linear_attach(None) == {"ok": False, "error": "BAD_ARGS"}


def test_registry_returns_str(pin_ctx, ws, png):
    raw = plugin.tool_handler("linear_attach")({"id": "RET-1", "path": str(png)})
    assert isinstance(raw, str) and json.loads(raw)["ok"] is True
    raw = plugin.tool_handler("linear_attach")({"id": "RET-1"})
    assert json.loads(raw) == {"ok": False, "error": "BAD_ARGS"}


def test_schema():
    s = TOOL_SCHEMAS["linear_attach"]
    assert s["name"] == "linear_attach" and "Linear app" in s["description"]
    assert agent_tools._GATED_NOTE in s["description"]
    assert "ALREADY_LINKED" in s["description"]
    assert ".env" in s["description"] and "media" in s["description"]
    params = s["parameters"]
    assert "required" not in params
    props = params["properties"]
    assert list(props) == ["id", "path", "url", "title", "subtitle", "embed"]
    assert props["embed"]["type"] == "boolean"
    assert all(props[k]["type"] == "string" for k in ("id", "path", "url", "title", "subtitle"))
    assert all(p.get("description") for p in props.values())
    assert agent_tools.linear_attach is TOOL_HANDLERS["linear_attach"]
    assert tools_attach.HANDLERS == {"linear_attach": TOOL_HANDLERS["linear_attach"],
                                     "linear_session_link": TOOL_HANDLERS["linear_session_link"]}
    assert "linear_attach" in agent_tools.__all__


@pytest.mark.parametrize("imports", [
    "import hermes_linear.tools_attach; from hermes_linear import agent_tools",
    "from hermes_linear import agent_tools; import hermes_linear.tools_attach",
])
def test_roster_merge_any_import_order(imports):
    """Ruling R3 for ``tools_attach``: either module may be imported first."""
    code = (f"{imports}; print(list(agent_tools.TOOL_SCHEMAS)[0], "
            "'linear_attach' in agent_tools.TOOL_HANDLERS, "
            "agent_tools.linear_attach is agent_tools.TOOL_HANDLERS['linear_attach'])")
    proc = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "linear_issue_get True True"


# -- linear_session_link (RET-135; spec §1.4, §2.5)
#
# ``put_turn(adapter, "sk", SELF)`` puts a Linear turn in flight under session key ``sk``
# with agent session ``sid-m1``; ``pin_ctx`` resolves every call to key ``sk``.

def linear_session_link(args):
    return TOOL_HANDLERS["linear_session_link"](args)


@pytest.fixture
def sessions(ws):
    """Record ``update_session`` calls on the fake workspace (it has no session model)."""
    seen: list[tuple] = []

    async def update_session(sid, *, plan=None, external_urls=None, added_external_urls=None):
        seen.append((sid, plan, external_urls, added_external_urls))

    ws.update_session = update_session
    return seen


def test_links_current_turn_session(pin_ctx, adapter, sessions):
    put_turn(adapter, "sk", SELF)
    out = linear_session_link({"url": PR})
    assert sessions == [("sid-m1", None, None, [{"label": "Pull request", "url": PR}])]
    assert out == {"ok": True, "agent_session_id": "sid-m1", "label": "Pull request", "url": PR}


def test_label_defaults_to_host(pin_ctx, adapter, sessions):
    put_turn(adapter, "sk", SELF)
    out = linear_session_link({"url": "https://docs.example.com/x"})
    assert out["ok"] is True and out["label"] == "docs.example.com"
    assert sessions[-1][3] == [{"label": "docs.example.com", "url": "https://docs.example.com/x"}]
    out = linear_session_link({"url": PR, "label": "The fix"})
    assert out["label"] == "The fix"
    assert sessions[-1][3] == [{"label": "The fix", "url": PR}]


def test_repeat_passes_through(pin_ctx, adapter, sessions):
    """Linear de-duplicates a re-added URL (spec §4 item 3), so the tool doesn't track it."""
    put_turn(adapter, "sk", SELF)
    assert linear_session_link({"url": PR})["ok"] is True
    assert linear_session_link({"url": PR})["ok"] is True
    assert len(sessions) == 2


def test_no_turn_is_not_linear_session(pin_ctx, adapter, sessions):
    assert linear_session_link({"url": PR}) == {"ok": False, "error": "NOT_LINEAR_SESSION"}
    pin_ctx(linear=False)  # Slack, cron, a subagent: no session key
    put_turn(adapter, "sk", SELF)
    assert linear_session_link({"url": PR}) == {"ok": False, "error": "NOT_LINEAR_SESSION"}
    assert sessions == []


def test_other_session_key_is_not_linear_session(pin_ctx, adapter, sessions):
    put_turn(adapter, "sk", SELF)
    pin_ctx(key="other")
    assert linear_session_link({"url": PR}) == {"ok": False, "error": "NOT_LINEAR_SESSION"}
    assert sessions == []


@pytest.mark.parametrize("args", [
    {"url": "ftp://example.com/x"}, {"url": "javascript:alert(1)"}, {"url": ""},
    {"url": "https://"}, {}, {"url": 7}, {"url": PR, "label": 3}, {"url": PR, "label": ""},
])
def test_non_http_url_bad_args(pin_ctx, adapter, sessions, args):
    put_turn(adapter, "sk", SELF)
    assert linear_session_link(args) == {"ok": False, "error": "BAD_ARGS"}
    assert sessions == []


def test_linear_error_passthrough(pin_ctx, adapter, ws):
    async def limited(sid, **kw):
        raise LinearError("slow down", code="RATELIMITED")

    ws.update_session = limited
    put_turn(adapter, "sk", SELF)
    assert linear_session_link({"url": PR}) == {"ok": False, "error": "RATELIMITED",
                                                "message": "slow down"}


def test_session_link_registry_returns_str(pin_ctx, adapter, sessions):
    put_turn(adapter, "sk", SELF)
    raw = plugin.tool_handler("linear_session_link")({"url": PR})
    assert isinstance(raw, str) and json.loads(raw)["agent_session_id"] == "sid-m1"
    raw = plugin.tool_handler("linear_session_link")({"url": "ftp://x"})
    assert json.loads(raw) == {"ok": False, "error": "BAD_ARGS"}


def test_session_link_schema():
    s = TOOL_SCHEMAS["linear_session_link"]
    assert s["name"] == "linear_session_link" and "Linear app" in s["description"]
    assert "no-op" in s["description"]  # spec §4 item 3: Linear de-duplicates
    assert agent_tools._GATED_NOTE not in s["description"]
    params = s["parameters"]
    assert params["required"] == ["url"]
    assert list(params["properties"]) == ["url", "label"]
    assert all(p["type"] == "string" and p.get("description")
               for p in params["properties"].values())
    assert agent_tools.linear_session_link is TOOL_HANDLERS["linear_session_link"]
    assert "linear_session_link" in agent_tools.__all__
    assert "linear_session_link" not in tool_approval.GATED_TOOLS
