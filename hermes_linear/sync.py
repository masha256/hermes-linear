"""Spec/plan rendering for the Linear sync tools (RET-77; Task 4 extends it for plans).

Pure text helpers, small git helpers whose subprocess runner (``git``) is injectable, and
the bounded source-file reader (``source_path`` / ``read_source``) shared by the spec and
plan tools. Nothing here touches Linear or the event loop: callers run the file and git
helpers in the tool thread, never on a running loop, before scheduling the Linear write on
the adapter loop. Lengths are measured in UTF-16 code units (``utf16_len``), as Linear
(JavaScript) measures strings.
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
#: Largest spec/plan file the tools read (bytes); larger files are refused (BAD_ARGS).
MAX_SOURCE_BYTES = 2 * 1024 * 1024
GIT_TIMEOUT_S = 10.0
UNCOMMITTED = "uncommitted"

#: A whole header line: ``Linear: <identifier | UUID>`` and nothing else.
HEADER_RE = re.compile(
    r"^Linear:[ \t]*([A-Z][A-Z0-9]*-\d+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}"
    r"-[0-9a-f]{12})[ \t]*$", re.MULTILINE)
_FENCE_RE = re.compile(r"^ {0,3}```")
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


# ----------------------------------------------------------------- source files


def source_path(raw: object) -> Path | None:
    """``raw`` as a ``Path`` if it is an absolute path string, else ``None`` (BAD_ARGS).

    Relative paths are refused: the gateway's cwd is not the agent's working directory.
    """
    if not isinstance(raw, str) or not raw.strip() or "\0" in raw:
        return None
    p = Path(raw.strip())
    return p if p.is_absolute() else None


def read_source(path: Path, max_bytes: int = MAX_SOURCE_BYTES) -> str | None:
    """The UTF-8 text of regular file ``path`` if at most ``max_bytes`` long, else ``None``."""
    try:
        if not path.is_file() or path.stat().st_size > max_bytes:
            return None
        with path.open("rb") as fh:
            data = fh.read(max_bytes + 1)  # bounded even if the file grew since the stat
        if len(data) > max_bytes:
            return None
        return data.decode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return None


# ----------------------------------------------------------------- text


def utf16_len(s: str) -> int:
    """``s``'s length in UTF-16 code units (JavaScript ``String.length``)."""
    return len(s.encode("utf-16-le")) // 2


def utf16_truncate(s: str, units: int) -> str:
    """The longest prefix of ``s`` of at most ``units`` UTF-16 code units (no split pairs)."""
    if units <= 0:
        return ""
    return s.encode("utf-16-le")[:2 * units].decode("utf-16-le", errors="ignore")


def _header_block(text: str) -> str:
    """The lines before the first ``## `` heading, without fenced (```) code blocks."""
    out: list[str] = []
    fenced = False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        if line.startswith("## "):
            break
        out.append(line)
    return "\n".join(out)


def read_linear_header(text: str) -> str | None:
    """The ID on the first ``Linear: <ID>`` header line, else ``None``.

    Only whole lines in the header block (before the first ``## `` heading, outside code
    fences) count; the ID must be an issue identifier (``RET-12``) or a lowercase UUID.
    """
    m = HEADER_RE.search(_header_block(text or ""))
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
    """``git log -1 --format=%h -- <file>`` in the file's directory; ``None`` if none.

    Also ``None`` when the working copy differs from HEAD (``git status --porcelain`` not
    empty, or it fails): the commit would not name the content being synced.
    """
    p = Path(path)
    commit = git(["-C", str(p.parent), "log", "-1", "--format=%h", "--", str(p)]) or None
    if commit is None:
        return None
    dirty = git(["-C", str(p.parent), "status", "--porcelain", "--", str(p)])
    return commit if dirty == "" else None


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
    if utf16_len(full) <= SPEC_INLINE_MAX:
        return full, True
    tail = (f"\n\nFull spec (too long to show here): {info.link}"
            f"\n\n---\n{footer(info.path, info.commit)}")
    summary = spec_summary(text)
    room = SPEC_INLINE_MAX - utf16_len(tail)
    if utf16_len(summary) > room:
        summary = utf16_truncate(summary, room - utf16_len(TRUNCATED)).rstrip() + TRUNCATED
    return (summary + tail).lstrip(), False


__all__ = ["MAX_SOURCE_BYTES", "SPEC_INLINE_MAX", "SourceInfo", "footer", "git_short_commit",
           "github_repo",
           "read_linear_header", "read_source", "render_spec", "run_git", "source_info",
           "source_path", "spec_description", "spec_summary", "utf16_len", "utf16_truncate"]
