"""Spec/plan rendering for the Linear sync tools (RET-77; Task 4 extends it for plans).

Pure text helpers, small git helpers whose subprocess runner (``git``) is injectable, and
the bounded source-file reader (``source_path`` / ``read_source``) shared by the spec and
plan tools. Nothing here touches Linear or the event loop: callers run the file and git
helpers in the tool thread, never on a running loop, before scheduling the Linear write on
the adapter loop. Lengths are measured in UTF-16 code units (``utf16_len``), as Linear
(JavaScript) measures strings.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
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


def write_source(path: Path, text: str) -> None:
    """Atomically replace the file ``path`` (symlinks followed) with UTF-8 ``text``.

    Same directory temp file (fsynced), mode copied, then ``os.replace``: a crash never leaves a
    half-written plan. Raises ``OSError``. Call only off the loop.
    """
    target = Path(path).resolve()
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())  # the data is on disk before the rename makes it visible
        shutil.copymode(target, tmp)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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


# ----------------------------------------------------------------- plans (RET-78)

#: Limit for the plan comment (marker included), in UTF-16 units. Linear documents no
#: comment limit; probe P1 measured 200,000 for descriptions, so the same value is used.
PLAN_COMMENT_MAX = SPEC_INLINE_MAX
#: A task heading; group 3 is the first ``— <ID>`` token, even when text follows it.
TASK_HEADING_RE = re.compile(r"^### Task (\d+): (.+?)(?:\s+—\s+([A-Z]+-\d+)\b.*)?\s*$")
#: Titles of the ticket the plan sync manages: ``T<n> · …`` (U+00B7).
MANAGED_TITLE_RE = re.compile(r"^T\d+ ·")
#: The content-hash marker ending every task ticket description the plan sync writes
#: (Ruling S16). Probe P8: Linear normalizes descriptions (``- x`` -> ``* x``, blank line
#: before lists) but keeps this trailing comment, so the hash, not the text, is compared.
DESC_HASH_RE = re.compile(r"<!--\s*hermes-linear:h=([0-9a-f]{12})\s*-->\s*\Z")
DESC_HASH_LEN = 12
_PLAN_FENCE_RE = re.compile(r"^ {0,3}(```|~~~)")
#: A heading that ends a task section (levels 1-3); ``####`` and deeper stay inside it.
_SECTION_END_RE = re.compile(r"^#{1,3}(?:\s|$)")
#: A thematic break line (``---`` between tasks).
_RULE_RE = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$")
#: Limit for a task ticket description, hash marker included (probe P1).
TASK_DESC_MAX = SPEC_INLINE_MAX


@dataclass(frozen=True)
class PlanTask:
    number: int
    name: str
    ticket: str | None
    body: str  # the task section after its heading: Files, Interfaces, steps
    line: int  # 1-based line of the heading


def _plan_lines(text: str):
    """``(index, line_without_eol, line_with_eol, fenced)`` for every line of ``text``."""
    fenced = False
    for i, raw in enumerate(text.splitlines(keepends=True)):
        line = raw.rstrip("\r\n")
        if _PLAN_FENCE_RE.match(line):
            fenced = not fenced
            yield i, line, raw, True
            continue
        yield i, line, raw, fenced


def _section(lines: list[str]) -> str:
    """``lines`` joined with ``\n``, without leading blank lines or trailing rules/blanks."""
    lo, hi = 0, len(lines)
    while lo < hi and not lines[lo].strip():
        lo += 1
    while hi > lo and (not lines[hi - 1].strip() or _RULE_RE.match(lines[hi - 1])):
        hi -= 1
    return "\n".join(lines[lo:hi]).rstrip()


def parse_plan(text: str) -> list[PlanTask]:
    """The ``### Task N: <name>[ — <ID>]`` headings outside code fences, in file order.

    A task's body runs to the next level 1-3 heading outside a fence (or the end of file).
    """
    heads: list[tuple[int, re.Match]] = []
    lines: list[tuple[str, bool]] = []
    for i, line, _raw, fenced in _plan_lines(text or ""):
        lines.append((line, fenced))
        if not fenced and (m := TASK_HEADING_RE.match(line)):
            heads.append((i, m))
    out: list[PlanTask] = []
    for i, m in heads:
        end = len(lines)
        for j in range(i + 1, len(lines)):
            if not lines[j][1] and _SECTION_END_RE.match(lines[j][0]):
                end = j
                break
        out.append(PlanTask(number=int(m.group(1)), name=m.group(2).strip(), ticket=m.group(3),
                            body=_section([ln for ln, _f in lines[i + 1:end]]), line=i + 1))
    return out


def plan_header(text: str) -> str:
    """Everything before the first task heading (outside fences), trailing rules dropped."""
    out: list[str] = []
    for _i, line, _raw, fenced in _plan_lines(text or ""):
        if not fenced and TASK_HEADING_RE.match(line):
            break
        out.append(line)
    return _section(out)


def write_ticket_ids(text: str, mapping: dict[int, str]) -> str:
    """``text`` with each mapped heading's ``— <ID>`` added or replaced; nothing else changes."""
    out: list[str] = []
    for _i, line, raw, fenced in _plan_lines(text or ""):
        m = None if fenced else TASK_HEADING_RE.match(line)
        ident = mapping.get(int(m.group(1))) if m else None
        if not m or not ident or m.group(3) == ident:
            out.append(raw)
            continue
        eol = raw[len(line):]
        if m.group(3):
            new = line[:m.start(3)] + ident + line[m.end(3):]
        else:
            new = line[:m.end(2)] + f" — {ident}" + line[m.end(2):]
        out.append(new + eol)
    return "".join(out)


def task_title(t: PlanTask) -> str:
    return f"T{t.number} · {t.name}"


def _cut(text: str, units: int) -> str:
    """``text`` cut to fit ``units`` (``…`` and a closing fence included when cut)."""
    if utf16_len(text) <= units:
        return text
    cut = utf16_truncate(text, units - utf16_len(TRUNCATED) - 4).rstrip()
    open_fence = False
    for _i, _line, _raw, fenced in _plan_lines(cut + "\n."):
        open_fence = fenced  # the probe line "." is fenced only if a fence is left open
    if open_fence:
        cut += "\n```"  # never leave the rest of the ticket inside an open code block
    return cut + TRUNCATED


def _anchor(info: SourceInfo, line: int) -> str:
    return f"{info.link}#L{line}" if info.link.startswith("https://") else info.link


#: Length of the hash marker ``with_desc_hash`` appends.
_DESC_HASH_UNITS = len("\n\n<!-- hermes-linear:h= -->") + DESC_HASH_LEN


def task_description(t: PlanTask, info: SourceInfo) -> str:
    """``[Plan](link) · Task N`` then the task's whole section, without the hash marker.

    Over ``TASK_DESC_MAX`` (marker included) the section is cut and a link to the task in
    the file follows.
    """
    head = f"[Plan]({info.link}) · Task {t.number}"
    if not t.body:
        return head
    full = f"{head}\n\n{t.body}"
    if utf16_len(full) + _DESC_HASH_UNITS <= TASK_DESC_MAX:
        return full
    tail = f"\n\nFull task (too long to show here): {_anchor(info, t.line)}"
    room = TASK_DESC_MAX - _DESC_HASH_UNITS - utf16_len(head) - 2 - utf16_len(tail)
    return f"{head}\n\n{_cut(t.body, room)}{tail}"


def desc_hash(rendered: str) -> str:
    """``sha256(rendered)[:12]`` (hex) of a description without its marker."""
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:DESC_HASH_LEN]


def with_desc_hash(rendered: str) -> str:
    """``rendered`` with ``<!-- hermes-linear:h=<desc_hash> -->`` as its last line."""
    return f"{rendered}\n\n<!-- hermes-linear:h={desc_hash(rendered)} -->"


def read_desc_hash(description: str | None) -> str | None:
    """The ``h=`` of the hash marker ending ``description`` (as Linear returns it), else None."""
    m = DESC_HASH_RE.search(description or "")
    return m.group(1) if m else None


def plan_comment(text: str, info: SourceInfo, tasks: list[PlanTask], marker_tag: str,
                 tickets: dict[int, str]) -> tuple[str, bool]:
    """The plan comment body ending in ``marker_tag`` and whether the header is whole.

    The plan header (``plan_header``), the task list (``T<n> · <name> — <ID>``), then the
    footer: task bodies live in the task tickets. A header that would not fit
    ``PLAN_COMMENT_MAX`` UTF-16 units is cut and followed by a link to the file.
    """
    items = "\n".join(f"- {task_title(t)}" + (f" — {tickets[t.number]}"
                                              if tickets.get(t.number) else "")
                      for t in tasks)
    tail = f"\n\n---\n{footer(info.path, info.commit)}\n\n{marker_tag}"
    header = plan_header(text)
    full = f"{header}\n\n## Tasks\n\n{items}{tail}".lstrip()
    if utf16_len(full) <= PLAN_COMMENT_MAX:
        return full, True
    note = f"\n\nFull plan (too long to show here): {info.link}\n\n## Tasks\n\n"
    room = PLAN_COMMENT_MAX - utf16_len(note) - utf16_len(tail)
    items = _cut(items, room - min(utf16_len(header), room // 2))  # the list keeps half
    header = _cut(header, room - utf16_len(items))
    return f"{header}{note}{items}{tail}".lstrip(), False


# ----------------------------------------------------------------- documents (RET-168, §6)

#: Longest ``summary`` the sync tools take (after ``strip()``), in UTF-16 units.
SYNC_SUMMARY_MAX = 5_000
#: Linear refuses document content over 250,000 characters (probed: ``INVALID_INPUT``,
#: "content must be shorter than or equal to 250000 characters"); in UTF-16 units, the
#: footer and hash marker included.
SYNC_DOCUMENT_MAX = 250_000
#: Hidden markers closing the description's spec part and opening its plan part (HTML
#: comments survive Linear's markdown normalization, probe P8).
SPEC_PART_MARKER = "<!-- hermes-linear:spec -->"
PLAN_PART_MARKER = "<!-- hermes-linear:plan -->"
_H1_RE = re.compile(r"^# (.*)$")
#: A ``---`` bar ending ``text`` (blank lines after it allowed: Linear inserts one on save).
_BAR_BEFORE_RE = re.compile(r"(?:^|(?<=\n))[ \t]*-{3,}[ \t]*\n(?:[ \t]*\n)*[ \t]*\Z")
_LIST_ITEM_RE = re.compile(r"^(\s*)[-*+] ")


def doc_title(kind: str, text: str, path: str) -> str:
    """``<kind>: <H1>``: the first ``# `` heading outside code fences (stripped), else
    the file name without its extension. ``kind`` is ``"Spec"`` or ``"Plan"``."""
    for _i, line, _raw, fenced in _plan_lines(text or ""):
        m = None if fenced else _H1_RE.match(line)
        if m and m.group(1).strip():
            return f"{kind}: {m.group(1).strip()}"
    return f"{kind}: {Path(path).stem}"


def document_content(text: str, info: SourceInfo) -> tuple[str, bool]:
    """The document content (hash marker last) and whether the whole file is in it.

    The file text, ``\\n\\n---\\n`` and the footer, then ``with_desc_hash``. Over
    ``SYNC_DOCUMENT_MAX`` the text is cut (``TRUNCATED``; an open code fence is closed)
    and followed by ``Full file (too long to show here): <link>`` before the footer.
    """
    foot = f"\n\n---\n{footer(info.path, info.commit)}"
    full = with_desc_hash(f"{text.rstrip()}{foot}")
    if utf16_len(full) <= SYNC_DOCUMENT_MAX:
        return full, True
    tail = f"\n\nFull file (too long to show here): {info.link}{foot}"
    room = SYNC_DOCUMENT_MAX - _DESC_HASH_UNITS - utf16_len(tail)
    return with_desc_hash(f"{_cut(text.rstrip(), room)}{tail}"), False


def source_line(kind: str, title: str, doc_url: str, info: SourceInfo) -> str:
    """``**<kind>:** [<title>](<doc_url>) · source [<path> @ <commit>](<link>)``."""
    return (f"**{kind}:** [{title}]({doc_url}) · source "
            f"[{info.path} @ {info.commit or UNCOMMITTED}]({info.link})")


def split_plan_part(description: str | None) -> tuple[str, str | None]:
    """``(before, plan_part)`` of a ticket description.

    ``plan_part`` runs from the ``---`` line directly before ``PLAN_PART_MARKER`` (blank
    lines between allowed), or from the marker itself when no bar precedes it, to the end;
    ``None`` (and the whole description as ``before``) when there is no marker.
    """
    desc = description or ""
    at = desc.find(PLAN_PART_MARKER)
    if at < 0:
        return desc, None
    bar = _BAR_BEFORE_RE.search(desc, 0, at)
    start = bar.start() if bar else at
    return desc[:start], desc[start:]


def spec_part(summary: str, source: str) -> str:
    """The description's spec part: the summary, the source line, then the marker."""
    return f"{summary}\n\n{source}\n{SPEC_PART_MARKER}"


def _markdown_key(text: str | None) -> str:
    """``text`` with Linear's description normalizations undone on both sides (probe P8
    and RET-148: list bullets, blank lines, ``_x_`` -> ``*x*``, trailing spaces)."""
    lines = (_LIST_ITEM_RE.sub(r"\1* ", ln.rstrip()).replace("_", "*")
             for ln in (text or "").replace("\r\n", "\n").split("\n"))
    return "\n".join(ln for ln in lines if ln)


def same_description(current: str | None, new: str) -> bool:
    """True when ``new`` would not change the stored description ``current`` (as Linear
    returns it, normalized on save): compared after ``_markdown_key`` on both."""
    return _markdown_key(current) == _markdown_key(new)


__all__ = ["DESC_HASH_RE", "MANAGED_TITLE_RE", "desc_hash", "read_desc_hash",
           "with_desc_hash", "MAX_SOURCE_BYTES", "PLAN_COMMENT_MAX", "PlanTask",
           "PLAN_PART_MARKER", "SPEC_INLINE_MAX", "SPEC_PART_MARKER", "SYNC_DOCUMENT_MAX",
           "SYNC_SUMMARY_MAX", "SourceInfo", "TASK_DESC_MAX", "TASK_HEADING_RE", "TRUNCATED",
           "doc_title", "document_content", "footer", "git_short_commit", "github_repo",
           "parse_plan", "plan_comment", "read_linear_header", "read_source", "plan_header",
           "run_git", "same_description", "source_info", "source_line", "source_path",
           "spec_part", "split_plan_part", "task_description", "task_title", "utf16_len",
           "utf16_truncate", "write_source", "write_ticket_ids"]
