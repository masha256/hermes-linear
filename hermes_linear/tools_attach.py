"""Attach tools (RET-120 spec §1.3–1.4, §2.4–2.5): the local-file path policy, uploads,
URL links, ``embed`` and ``linear_session_link``.

Built on ``agent_tools``' ``_tool`` machinery. Like ``tools_read``, this module registers
itself at its end (Ruling R3).
"""

from __future__ import annotations

import mimetypes
import os
import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

from gateway.session_context import get_session_env

from . import agent_tools
from .agent_tools import _Refusal
from .constants import PLAN_SYNC_TIMEOUT_S
from .linear_client import LinearError, UploadError

#: Largest file ``linear_attach`` uploads. Linear's ``fileUpload`` accepted every size
#: probed up to 1 GB (2026-11-24; spec §4.2), so the cap is the plugin's own: a 50 MB PUT
#: fits the 120 s tool budget.
ATTACH_MAX_BYTES = 50 * 1024 * 1024


@dataclass(frozen=True)
class AttachFile:
    name: str
    content_type: str
    data: bytes


def load_attach_file(path: str, session_key: str | None) -> AttachFile:
    """Apply the attach path policy (spec §2.4) to ``path`` and read the file.

    Order: not absolute → ``PATH_DENIED``; not a regular file (symlinks followed) →
    ``FILE_NOT_FOUND``; refused by Hermes's ``validate_media_delivery_path`` (or that
    check can't be imported: fail closed) → ``PATH_DENIED``; over ``ATTACH_MAX_BYTES`` →
    ``FILE_TOO_LARGE``. The bytes come from the path the policy returned (a symlink is
    judged and read by its target); ``name`` is the basename of ``path`` as given.
    """
    if not isinstance(path, str) or not os.path.isabs(path):
        raise _Refusal("PATH_DENIED")
    if not os.path.isfile(path):
        raise _Refusal("FILE_NOT_FOUND")
    try:
        from gateway.platforms.base import validate_media_delivery_path
    except Exception:  # noqa: BLE001 - no policy available: refuse
        raise _Refusal("PATH_DENIED") from None
    try:
        resolved = validate_media_delivery_path(path, session_key=session_key or "")
    except Exception:  # noqa: BLE001 - a crashing policy check is a refusal
        resolved = None
    if not resolved:
        raise _Refusal("PATH_DENIED")
    try:
        if os.path.getsize(resolved) > ATTACH_MAX_BYTES:
            raise _Refusal("FILE_TOO_LARGE")
        with open(resolved, "rb") as fh:
            data = fh.read(ATTACH_MAX_BYTES + 1)
    except OSError:
        raise _Refusal("FILE_NOT_FOUND") from None
    if len(data) > ATTACH_MAX_BYTES:  # grew between the stat and the read
        raise _Refusal("FILE_TOO_LARGE")
    name = os.path.basename(path)
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return AttachFile(name=name, content_type=content_type, data=data)


# -- linear_attach (RET-134; spec §1.3, §2.4)

#: A GitHub pull request URL (spec §1.5): linked with ``attachmentLinkGitHubPR``.
GITHUB_PR_RE = re.compile(r"^https://github\.com/[^/]+/[^/]+/pull/\d+(?:[/?#].*)?$")
#: Linear's re-link refusals (spec §4 item 4; Ruling R4), matched case-insensitively.
_ALREADY_LINKED_RE = re.compile(r"already been linked|already exists", re.IGNORECASE)
_MD_SPECIAL_RE = re.compile(r"([\\\[\]()])")


def embed_markdown(name: str, content_type: str, asset_url: str) -> str:
    """``![name](url)`` for an image, else ``[name](url)``; ``name`` escaped for markdown."""
    text = _MD_SPECIAL_RE.sub(r"\\\1", name).replace("\r", " ").replace("\n", " ")
    bang = "!" if (content_type or "").startswith("image/") else ""
    return f"{bang}[{text}]({asset_url})"


def _http_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme in {"http", "https"} and bool(parts.netloc)


def _parse_attach(args: dict) -> dict:
    """In the tool thread: the argument combination (§1.3), then the path policy (§2.4)."""
    ref, path, url = (agent_tools._opt_str(args, k) for k in ("id", "path", "url"))
    title, subtitle = agent_tools._opt_str(args, "title"), agent_tools._opt_str(args, "subtitle")
    embed = args.get("embed")
    if embed is not None and not isinstance(embed, bool):
        raise _Refusal("BAD_ARGS")
    if ref is not None:
        ref = agent_tools._req_str(args, "id")  # blank → BAD_ARGS
    if (path is None) == (url is None):
        raise _Refusal("BAD_ARGS")  # exactly one of path and url
    if embed and (ref is not None or url is not None):
        raise _Refusal("BAD_ARGS")
    if not embed and ref is None:
        raise _Refusal("BAD_ARGS")
    if url is not None and not _http_url(url):
        raise _Refusal("BAD_ARGS")
    file = None
    if path is not None:
        file = load_attach_file(path, get_session_env("HERMES_SESSION_KEY") or None)
    return {"id": ref, "file": file, "url": url, "title": title, "subtitle": subtitle}


def _message(exc: LinearError) -> str:
    return exc.detail or str(exc)


async def _attach(ctx: agent_tools.ToolContext, a: dict) -> dict:
    if not ctx.is_linear or not ctx.session_issue_id:
        raise _Refusal("NOT_LINEAR_SESSION")  # an embed belongs to a ticket thread too
    issue = None
    if a["id"] is not None:
        issue = await agent_tools._writable(ctx, a["id"])
        agent_tools._check_gate(ctx)  # before the upload: a first pass writes nothing
    client, f = ctx.client, a["file"]
    if f is not None:
        try:
            asset = await client.file_upload(f.name, f.content_type, f.data)
        except UploadError as exc:  # before LinearError: UploadError is one
            return {"ok": False, "error": "UPLOAD_FAILED", "status": exc.status}
        if issue is None:
            return {"ok": True, "asset_url": asset,
                    "markdown": embed_markdown(f.name, f.content_type, asset)}
        try:
            att = await client.attachment_create(issue["id"], a["title"] or f.name,
                                                 a["subtitle"], asset)
        except LinearError as exc:  # the asset stays: the agent can embed it instead
            return {"ok": False, "error": "LINEAR_ERROR", "message": _message(exc),
                    "asset_url": asset}
        return {"ok": True, "attachment_id": att.get("id"), "asset_url": asset,
                "issue": issue.get("identifier"), "url": att.get("url") or asset}
    url = a["url"]
    assert issue is not None  # parse: url mode always has an id
    try:
        if GITHUB_PR_RE.match(url):
            att = await client.attachment_link_github_pr(issue["id"], url)
        else:
            att = await client.attachment_link_url(issue["id"], url, a["title"])
    except LinearError as exc:
        text = f"{exc.detail or ''}\n{exc}"
        if exc.code == "INPUT_ERROR" and _ALREADY_LINKED_RE.search(text):
            return {"ok": False, "error": "ALREADY_LINKED", "message": _message(exc)}
        return {"ok": False, "error": "LINEAR_ERROR", "message": _message(exc)}
    return {"ok": True, "attachment_id": att.get("id"), "issue": issue.get("identifier"),
            "url": att.get("url") or url}


linear_attach = agent_tools._tool(_parse_attach, _attach, gated="linear_attach",
                                  blocking_parse=True, timeout_s=PLAN_SYNC_TIMEOUT_S)


# -- linear_session_link (RET-135; spec §1.4, §2.5)

def _parse_session_link(args: dict) -> dict:
    url = agent_tools._req_str(args, "url").strip()
    if not _http_url(url):
        raise _Refusal("BAD_ARGS")
    label = None
    if args.get("label") is not None:
        label = agent_tools._req_str(args, "label")  # blank → BAD_ARGS
    if label is None:
        label = "Pull request" if GITHUB_PR_RE.match(url) else urlsplit(url).hostname or url
    return {"url": url, "label": label}


async def _session_link(ctx: agent_tools.ToolContext, a: dict) -> dict:
    """Add ``{label, url}`` to the current turn's own agent session. No issue is targeted,
    so there is no scope check. Called directly, not through the activity write queue: an
    external URL is not an activity, so its order doesn't matter. Linear de-duplicates a
    re-added URL (spec §4 item 3)."""
    turns = getattr(ctx.adapter, "turns", None)
    found = turns.turn_for_session_key(ctx.session_key) if turns is not None else None
    if found is None:
        raise _Refusal("NOT_LINEAR_SESSION")  # Slack, cron, a subagent, another session
    sid = found[1].decision.agent_session_id
    await ctx.client.update_session(sid, added_external_urls=[{"label": a["label"],
                                                               "url": a["url"]}])
    return {"ok": True, "agent_session_id": sid, "label": a["label"], "url": a["url"]}


linear_session_link = agent_tools._tool(_parse_session_link, _session_link)


def _prop(type_: str, description: str) -> dict:
    return {"type": type_, "description": description}


SCHEMAS: dict[str, dict] = {
    "linear_attach": {
        "name": "linear_attach",
        "description": (
            "Attach to a Linear issue as the Linear app. path + id: upload a local file "
            "(files Hermes may deliver as media; .env, credential dirs and Hermes state are "
            "refused; max 50 MB) and attach it. url + id: link a URL (a GitHub PR URL as a "
            "PR); ALREADY_LINKED if the issue has it. path + embed: upload only and return "
            "markdown to put in a comment. " + agent_tools._GATED_NOTE),
        "parameters": {"type": "object", "properties": {
            "id": _prop("string", "Target issue (not with embed): " + agent_tools._ISSUE_REF),
            "path": _prop("string", "Absolute path of a local file (not with url)."),
            "url": _prop("string", "http(s) URL to link (not with path or embed)."),
            "title": _prop("string", "Attachment title (default: the file name)."),
            "subtitle": _prop("string", "File attachment subtitle."),
            "embed": _prop("boolean", "With path and no id: upload only, return markdown."),
        }},
    },
    "linear_session_link": {
        "name": "linear_session_link",
        "description": (
            "Add a link (usually the PR) to the current Linear agent session as the Linear "
            "app; it shows on the session in Linear. Only in a Linear ticket thread "
            "(NOT_LINEAR_SESSION otherwise). Re-adding the same URL is a no-op."),
        "parameters": {"type": "object", "properties": {
            "url": _prop("string", "http(s) URL to link."),
            "label": _prop("string", "Link label (default: \"Pull request\" for a GitHub PR "
                                     "URL, else the URL's host)."),
        }, "required": ["url"]},
    },
}

HANDLERS: dict[str, Callable[..., dict]] = {"linear_attach": linear_attach,
                                            "linear_session_link": linear_session_link}

# Register with ``agent_tools`` (Ruling R3).
agent_tools.TOOL_SCHEMAS.update(SCHEMAS)
agent_tools.TOOL_HANDLERS.update(HANDLERS)
for _tool_name, _handler in HANDLERS.items():
    setattr(agent_tools, _tool_name, _handler)
