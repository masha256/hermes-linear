"""``linear_relation`` (RET-121 spec §1.4; RET-141) and ``ensure_relation``, which
``linear_task_state``'s ``blocked_by`` reuses (§1.6).

Built on ``agent_tools``' ``_tool`` machinery. Like ``tools_read``, this module registers
itself at its end (Ruling R3).
"""

from __future__ import annotations

from typing import Callable

from . import agent_tools
from .agent_tools import _Refusal
from .linear_client import LinearError

#: The agent's relation types, in schema order.
RELATION_TYPES = ("blocks", "blocked_by", "related", "duplicate_of")

#: Agent type -> (Linear ``IssueRelationType``, ends swapped). ``blocked_by`` is ``other
#: blocks id``; ``duplicate_of`` is ``id`` duplicate of ``other`` (``issueId`` is the
#: duplicate; live probe RET-141).
_LINEAR_TYPE: dict[str, tuple[str, bool]] = {
    "blocks": ("blocks", False),
    "blocked_by": ("blocks", True),
    "related": ("related", False),
    "duplicate_of": ("duplicate", False),
}


def _is_match(rel: dict, linear_type: str, src: str, dst: str) -> bool:
    """Same type and direction; ``related`` in either direction (spec §1.4)."""
    if rel.get("type") != linear_type:
        return False
    ends = (rel.get("issue_id"), rel.get("related_id"))
    return ends == (src, dst) or (linear_type == "related" and ends == (dst, src))


async def ensure_relation(ctx: agent_tools.ToolContext, issue: dict, other: dict,
                          type: str) -> tuple[str, bool]:  # noqa: A002
    """``issue`` <type> ``other`` (agent types, ``RELATION_TYPES``): ``(relation_id, created)``.

    Reads ``issue``'s relations (both directions) first; a match returns its id with
    ``created=False`` and writes nothing. Otherwise ``_check_gate`` (a gated first pass
    stops here, so it writes nothing) and then the create. Both ends must already be
    scope-checked by the caller. ``issue`` equal to ``other`` is ``BAD_ARGS``.
    """
    if issue["id"] == other["id"]:
        raise _Refusal("BAD_ARGS")
    linear_type, swapped = _LINEAR_TYPE[type]
    src, dst = (other["id"], issue["id"]) if swapped else (issue["id"], other["id"])
    for rel in await ctx.client.issue_relations(issue["id"]):
        if _is_match(rel, linear_type, src, dst) and rel.get("id"):
            return rel["id"], False
    agent_tools._check_gate(ctx)
    return await ctx.client.relation_create(src, dst, linear_type), True


def _parse_relation(args: dict) -> dict:
    ref = agent_tools._req_str(args, "id")
    other = agent_tools._req_str(args, "other").strip()
    type_ = args.get("type")
    if type_ not in RELATION_TYPES:
        raise _Refusal("BAD_ARGS")
    return {"id": ref, "type": type_, "other": other}


async def _relation(ctx: agent_tools.ToolContext, a: dict) -> dict:
    issue = await agent_tools._writable(ctx, a["id"])
    other = await agent_tools._writable(ctx, a["other"])
    try:
        rid, created = await ensure_relation(ctx, issue, other, a["type"])
    except LinearError as exc:
        return {"ok": False, "error": exc.code or "LINEAR_ERROR",
                "message": exc.detail or str(exc)}
    if not created:
        return {"ok": False, "error": "ALREADY_RELATED", "relation_id": rid}
    return {"ok": True, "relation_id": rid, "issue": issue.get("identifier"),
            "other": other.get("identifier"), "type": a["type"]}


linear_relation = agent_tools._tool(_parse_relation, _relation, gated="linear_relation")


SCHEMAS: dict[str, dict] = {
    "linear_relation": {
        "name": "linear_relation",
        "description": (
            "Relate two Linear issues as the Linear app. type: blocks (id blocks other), "
            "blocked_by (other blocks id), related, duplicate_of (id is a duplicate of "
            "other; Linear moves id to its Duplicate state). ALREADY_RELATED (with "
            "relation_id) if that relation exists. Linear keeps one relation per pair of "
            "issues: another type or direction replaces the existing one. "
            + agent_tools._GATED_NOTE),
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string", "description": agent_tools._ISSUE_REF},
            "type": {"type": "string", "enum": list(RELATION_TYPES),
                     "description": "How id relates to other."},
            "other": {"type": "string", "description": "The other issue: "
                                                       + agent_tools._ISSUE_REF},
        }, "required": ["id", "type", "other"]},
    },
}

HANDLERS: dict[str, Callable[..., dict]] = {"linear_relation": linear_relation}

# Register with ``agent_tools`` (Ruling R3). ``update`` keeps the placeholder slot, so the
# roster order does not depend on which module was imported first.
agent_tools.TOOL_SCHEMAS.update(SCHEMAS)
agent_tools.TOOL_HANDLERS.update(HANDLERS)
for _tool_name, _handler in HANDLERS.items():
    setattr(agent_tools, _tool_name, _handler)
