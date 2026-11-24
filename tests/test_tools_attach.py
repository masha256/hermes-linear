"""Tests for hermes_linear.tools_attach: the attach path policy (RET-120 spec §2.4).

Runs the REAL ``gateway.platforms.base.validate_media_delivery_path`` (no stub) on tmp
files. ``HOME`` points at ``tmp_path / "home"`` so ``$HOME/.ssh`` is under the test's
control; strict delivery mode is switched off so the default policy is what's judged.
"""

from __future__ import annotations

import os

import pytest

from hermes_linear import agent_tools, tools_attach
from hermes_linear.tools_attach import AttachFile, load_attach_file


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


def test_env_file_denied(work):
    (work / ".env").write_text("SECRET=1")
    assert _code(work / ".env") == "PATH_DENIED"


def test_ssh_dir_denied(home):
    key = home / ".ssh" / "id_ed25519"
    key.write_text("PRIVATE KEY")
    assert _code(key) == "PATH_DENIED"


def test_symlink_to_denied_target_refused(home, work):
    key = home / ".ssh" / "id_ed25519"
    key.write_text("PRIVATE KEY")
    (work / "innocent.txt").symlink_to(key)
    assert _code(work / "innocent.txt") == "PATH_DENIED"
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


def test_registers_nothing_yet():
    assert tools_attach.SCHEMAS == {} and tools_attach.HANDLERS == {}
    assert os.path.basename(tools_attach.__file__) == "tools_attach.py"
