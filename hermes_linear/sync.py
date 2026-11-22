"""Spec/plan rendering for the Linear sync tools (RET-77; Task 4 extends it for plans).

Pure text helpers, plus small git helpers whose subprocess runner (``git``) is injectable.
Nothing here touches Linear or the event loop: callers run the git helpers in the tool
thread, before scheduling the Linear write on the adapter loop.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import quote

log = logging.getLogger(__name__)

#: Probe P1: Linear accepts 200,000-char descriptions (rejects 400,000); the limit applies
#: to the rendered text including the footer.
SPEC_INLINE_MAX = 200_000
GIT_TIMEOUT_S = 10.0
UNCOMMITTED = "uncommitted"

HEADER_RE = re.compile(r"^Linear:\s*(\S+)", re.MULTILINE)
_SECTION_ONE_RE = re.compile(r"^## 1\.", re.MULTILINE)
_NEXT_SECTION_RE = re.compile(r"^## ", re.MULTILINE)
_GITHUB_RE = re.compile(
    r"^(?:(?:https?|ssh|git)://(?:[^@/\s]+@)?github\.com/|(?:[^@/\s]+@)?github\.com:)"
    r"([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")
TRUNCATED = "\n\n…"

Git = Callable[[list[str]], "str | None"]


def run_git(args: list[str]) -> str | None:
    """``git <args>``'s stripped stdout, or ``None`` on any failure (bounded by a timeout)."""
    try:
        res = subprocess.run(["git", *args], capture_output=True, text=True,
                             timeout=GIT_TIMEOUT_S, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.debug("linear: git %s failed: %s", args[:3], type(exc).__name__)
        return None
    return res.stdout.strip() if res.returncode == 0 else None


# ----------------------------------------------------------------- text


def read_linear_header(text: str) -> str | None:
    """The ID on the first line starting ``Linear:``, else ``None``."""
    m = HEADER_RE.search(text or "")
    return m.group(1) if m else None


def footer(path: str, short_commit: str | None) -> str:
    return (f"Source: {path} @ {short_commit or UNCOMMITTED} · "
            "edit by asking in this thread")


def render_spec(text: str, path: str, short_commit: str | None) -> str:
    """The file text (trailing whitespace dropped), a rule, then the footer."""
    return f"{text.rstrip()}\n\n---\n{footer(path, short_commit)}"


def spec_summary(text: str) -> str:
    """From the first ``## 1.`` heading up to the next ``## `` heading (``""`` if none)."""
    m = _SECTION_ONE_RE.search(text or "")
    if not m:
        return ""
    end = _NEXT_SECTION_RE.search(text, m.end())
    return text[m.start():end.start() if end else len(text)].strip()


# ----------------------------------------------------------------- git / source


@dataclass(frozen=True)
class SourceInfo:
    path: str  # repo-relative POSIX path inside a git repo, else the path as given
    commit: str | None  # short commit of the file's last change, None if uncommitted
    link: str  # GitHub blob URL at ``commit`` when possible, else ``path``


def git_short_commit(path: str | Path, git: Git = run_git) -> str | None:
    """``git log -1 --format=%h -- <file>`` in the file's directory; ``None`` if none."""
    p = Path(path)
    return git(["-C", str(p.parent), "log", "-1", "--format=%h", "--", str(p)]) or None


def github_repo(remote_url: str | None) -> str | None:
    """``owner/repo`` for a github.com remote URL (ssh, scp-like or https), else ``None``."""
    m = _GITHUB_RE.match((remote_url or "").strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def source_info(path: str | Path, git: Git = run_git) -> SourceInfo:
    """Footer path, short commit and link for an absolute file path. Never raises."""
    p = Path(path)
    shown = str(p)
    top = git(["-C", str(p.parent), "rev-parse", "--show-toplevel"])
    if top:
        try:
            shown = p.resolve().relative_to(Path(top).resolve()).as_posix()
        except (OSError, ValueError):
            top = None
    commit = git_short_commit(p, git)
    link = shown
    if top and commit:
        repo = github_repo(git(["-C", top, "remote", "get-url", "origin"]))
        if repo:
            link = f"https://github.com/{repo}/blob/{commit}/{quote(shown, safe='/')}"
    return SourceInfo(path=shown, commit=commit, link=link)


# ----------------------------------------------------------------- spec description


def spec_description(text: str, info: SourceInfo) -> tuple[str, bool]:
    """The ticket description for a spec and whether it is the whole spec (``inline``).

    Inline when ``render_spec`` fits ``SPEC_INLINE_MAX``; otherwise §1, a link line to the
    full file, then the same footer, with §1 cut short if even that would not fit.
    """
    full = render_spec(text, info.path, info.commit)
    if len(full) <= SPEC_INLINE_MAX:
        return full, True
    tail = (f"\n\nFull spec (too long to show here): {info.link}"
            f"\n\n---\n{footer(info.path, info.commit)}")
    summary = spec_summary(text)
    room = SPEC_INLINE_MAX - len(tail)
    if len(summary) > room:
        summary = summary[:max(room - len(TRUNCATED), 0)].rstrip() + TRUNCATED
    return (summary + tail).lstrip(), False


__all__ = ["SPEC_INLINE_MAX", "SourceInfo", "footer", "git_short_commit", "github_repo",
           "read_linear_header", "render_spec", "run_git", "source_info", "spec_description",
           "spec_summary"]
