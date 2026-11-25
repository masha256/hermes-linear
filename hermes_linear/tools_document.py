"""``linear_document`` (RET-122 spec §1.2, §2; RET-164): read, create and edit Linear
documents as the app.

- ``get`` is a read: it runs ungated (outside a Linear session too, even when
  ``outside_session_writes`` is ``deny``; spec §2.3) and never records a target.
- ``create`` attaches to ``issue``, ``project`` or (neither) the session ticket. An issue
  goes through ``_writable``; a project is always a target (Q3, spec §2.1). A document
  with the same title (stripped, case-sensitive) on the target is ``ALREADY_EXISTS``
  before the gate. A document created from a Linear session is recorded in
  ``created_documents`` against the session ticket (spec §2.2).
- ``update`` runs without asking when the document's issue is in scope or this session
  ticket's tools created it; otherwise the document is a target (``- document "<title>"``).
  It sends only ``title`` / ``content`` (never ``issueId``, ``projectId``, ``trashed`` or
  ``hiddenAt``).

Every read, resolution, duplicate check and scope check comes before ``_check_gate``, so a
first pass that needs approval writes nothing. Built on ``agent_tools``' ``_tool``
machinery; like ``tools_relation``, this module registers itself at its end (Ruling R3).
"""

from __future__ import annotations

import re
from typing import Any, Callable

from . import agent_tools, directory, issue_fields
from .agent_tools import _Refusal

#: ``get`` cuts ``content`` here (with ``content_truncated`` / ``content_length``).
DOCUMENT_CONTENT_MAX = 50_000

ACTIONS = ("get", "create", "update")

#: The arguments each action takes besides ``action``; any other one is ``BAD_ARGS``.
_ARGS: dict[str, frozenset[str]] = {
    "get": frozenset({"id"}),
    "create": frozenset({"title", "content", "issue", "project"}),
    "update": frozenset({"id", "title", "content"}),
}

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{12}$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")
_URL_RE = re.compile(r"^https://linear\.app/[^/?#\s]+/document/([^/?#\s]+)/?(?:[?#].*)?$")


def doc_ref(raw: str) -> str:
    """A document reference for ``document(id:)``: a UUID or slug id as given (stripped);
    a ``https://linear.app/<ws>/document/<slug>-<slugId>`` URL gives its last ``-``
    segment (query and fragment dropped). Anything else is ``BAD_ARGS``."""
    ref = raw.strip() if isinstance(raw, str) else ""
    if _UUID_RE.match(ref) or _SLUG_RE.match(ref):
        return ref
    m = _URL_RE.match(ref)
    if m:
        slug = m.group(1).rsplit("-", 1)[-1]
        if _SLUG_RE.match(slug):
            return slug
    raise _Refusal("BAD_ARGS")


def _parse(args: dict) -> dict:
    action = args.get("action")
    if action not in ACTIONS:
        raise _Refusal("BAD_ARGS")
    # An argument of another action never silently drops input (spec §1.2, §3). A null one
    # carries none, so it counts as absent.
    if any(v is not None for k, v in args.items() if k != "action" and k not in _ARGS[action]):
        raise _Refusal("BAD_ARGS")
    out: dict[str, Any] = {"action": action}
    if action in ("get", "update"):
        out["id"] = doc_ref(agent_tools._req_str(args, "id"))
    if action == "get":
        return out
    title = agent_tools._opt_str(args, "title")
    if title is not None and not title.strip():
        raise _Refusal("BAD_ARGS")
    content = agent_tools._opt_str(args, "content")  # may be empty
    if action == "update":
        if title is None and content is None:
            raise _Refusal("BAD_ARGS")
        out["fields"] = {k: v for k, v in (("title", title), ("content", content))
                         if v is not None}
        return out
    if title is None or content is None:
        raise _Refusal("BAD_ARGS")
    issue = agent_tools._opt_str(args, "issue")
    project = agent_tools._opt_str(args, "project")
    if (issue is not None and not issue.strip()) or (project is not None
                                                     and not project.strip()):
        raise _Refusal("BAD_ARGS")
    if issue is not None and project is not None:
        raise _Refusal("BAD_ARGS")
    out.update(title=title.strip(), content=content,
               issue=issue.strip() if issue else None,
               project=project.strip() if project else None)
    return out


async def _document(ctx: agent_tools.ToolContext, ref: str) -> dict:
    doc = await ctx.client.document_get(ref)
    if not doc:
        raise _Refusal("DOCUMENT_NOT_FOUND")
    return doc


async def _get(ctx: agent_tools.ToolContext, a: dict) -> dict:
    doc = await _document(ctx, a["id"])
    issue, project = doc.get("issue"), doc.get("project")
    out = {"id": doc.get("id"), "title": doc.get("title"), "url": doc.get("url"),
           "content": doc.get("content") or "",
           "issue": ({"identifier": issue.get("identifier"), "title": issue.get("title")}
                     if issue else None),
           "project": {"name": project.get("name")} if project else None,
           "creator": doc.get("creator"), "updated_at": doc.get("updated_at")}
    if len(out["content"]) > DOCUMENT_CONTENT_MAX:
        out["content_length"] = len(out["content"])
        out["content"] = out["content"][:DOCUMENT_CONTENT_MAX]
        out["content_truncated"] = True
    return {"ok": True, "document": out}


def _store(ctx: agent_tools.ToolContext) -> Any:
    return getattr(ctx.adapter, "_store", None)


def _document_target(ctx: agent_tools.ToolContext, doc: dict) -> None:
    """``doc`` is outside the session's scope: approved on this pass, recorded on a first
    pass (``- document "<title>"``; the body's ``_check_gate`` then asks), else
    ``OUT_OF_SCOPE`` (spec §2.1; guarded as ``agent_tools._scope_target``)."""
    if agent_tools._target_ok(ctx, doc["id"]):
        return
    if ctx.gate is None or ctx.gate.approved:
        raise _Refusal("OUT_OF_SCOPE")
    ctx.gate.record_target(doc["id"], "document", doc.get("title") or doc["id"])


async def _create(ctx: agent_tools.ToolContext, a: dict) -> dict:
    if a["project"] is not None:
        row = await directory.for_client(ctx.client).resolve(
            "projects", "", a["project"], issue_fields._by_name)
        agent_tools._scope_target(ctx, row, "project")  # always a target (Q3)
        target: dict[str, Any] = {"project_id": row["id"], "issue_id": None}
        shown = {"project": row.get("name")}
        existing = await ctx.client.documents_on(project_id=row["id"])
    else:
        if a["issue"] is None and ctx.outside:
            raise _Refusal("BAD_ARGS")  # no session ticket to attach to
        issue = await agent_tools._writable(ctx, a["issue"] or ctx.session_issue_id or "")
        target = {"issue_id": issue["id"], "project_id": None}
        shown = {"issue": issue.get("identifier")}
        existing = await ctx.client.documents_on(issue_id=issue["id"])
    for d in existing:
        if (d.get("title") or "").strip() == a["title"]:
            raise _Refusal("ALREADY_EXISTS", extra={"document_id": d.get("id"),
                                                    "url": d.get("url")})
    agent_tools._check_gate(ctx)
    made = await ctx.client.document_create(a["title"], a["content"], **target)
    store = _store(ctx)
    if ctx.session_issue_id and not ctx.outside and store is not None:
        store.record_document(ctx.session_issue_id, made["id"])
    return {"ok": True, "document_id": made["id"], "url": made.get("url"), **shown}


async def _in_scope(ctx: agent_tools.ToolContext, doc: dict) -> bool:
    """Linear session only: the document was created from this session ticket, or its
    issue passes ``check_write_scope``. Any refusal code but ``OUT_OF_SCOPE`` refuses."""
    store = _store(ctx)
    if store is not None and doc["id"] in store.documents_by(ctx.session_issue_id):
        return True
    issue = doc.get("issue")
    if not issue or not issue.get("id"):
        return False
    code = await agent_tools.check_write_scope(ctx, issue["id"])
    if code is not None and code != "OUT_OF_SCOPE":
        raise _Refusal(code)
    return code is None


async def _update(ctx: agent_tools.ToolContext, a: dict) -> dict:
    if not ctx.outside and (not ctx.is_linear or not ctx.session_issue_id):
        raise _Refusal("NOT_LINEAR_SESSION")
    doc = await _document(ctx, a["id"])
    # Outside a Linear session nothing is in scope: every update is a target (spec §2.3).
    if ctx.outside or not await _in_scope(ctx, doc):
        _document_target(ctx, doc)
    agent_tools._check_gate(ctx)
    out = await ctx.client.document_update(doc["id"], a["fields"])
    return {"ok": True, "document_id": out["id"], "url": out.get("url")}


async def _write(ctx: agent_tools.ToolContext, a: dict) -> dict:
    return await (_create if a["action"] == "create" else _update)(ctx, a)


_read_tool = agent_tools._tool(_parse, _get)
_write_tool = agent_tools._tool(_parse, _write, gated="linear_document")


def linear_document(args: Any = None, **kw: Any) -> dict:
    """``get`` runs ungated (a read); ``create`` / ``update`` through the gate."""
    if isinstance(args, dict) and args.get("action") == "get":
        return _read_tool(args, **kw)
    return _write_tool(args, **kw)


SCHEMAS: dict[str, dict] = {
    "linear_document": {
        "name": "linear_document",
        "description": (
            "Read, create or edit a Linear document as the Linear app. get (id): title, "
            "url, content (cut at 50,000 chars with content_truncated and content_length), "
            "its issue or project, creator. create (title, content; issue or project, at "
            "most one; neither: the session ticket): a document with the same title there "
            "is ALREADY_EXISTS with its document_id and url, and nothing is written. update "
            "(id; title and/or content): edits in place, never moves or deletes. A document "
            "on an in-scope issue, or one created from this ticket, needs no approval; one "
            "on a project always does. DOCUMENT_NOT_FOUND for an unknown id. "
            + agent_tools._GATED_NOTE),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": list(ACTIONS),
                       "description": "get, create or update."},
            "id": {"type": "string", "description": (
                "get/update: document UUID, slug id, or its linear.app/<ws>/document/ URL.")},
            "title": {"type": "string", "description": "create (required) / update."},
            "content": {"type": "string", "description": (
                "Markdown; create (required, may be empty) / update (replaces the body).")},
            "issue": {"type": "string", "description": "create: attach to this issue. "
                                                       + agent_tools._ISSUE_REF},
            "project": {"type": "string",
                        "description": "create: attach to this project (UUID or name)."},
        }, "required": ["action"]},
    },
}

HANDLERS: dict[str, Callable[..., dict]] = {"linear_document": linear_document}

# Register with ``agent_tools`` (Ruling R3). ``update`` keeps the placeholder slot, so the
# roster order does not depend on which module was imported first.
agent_tools.TOOL_SCHEMAS.update(SCHEMAS)
agent_tools.TOOL_HANDLERS.update(HANDLERS)
for _tool_name, _handler in HANDLERS.items():
    setattr(agent_tools, _tool_name, _handler)
