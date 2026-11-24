"""Pure helpers for out-of-scope tool approval (no I/O, stdlib only).

Used by the gated write handlers, the ``pre_tool_call`` hook, and outbound
approval rendering. A gated call is identified by a digest of its tool name
and normalized arguments (see :func:`normalize_args`); the digest rides in the approval description's last line
so the hook can recover it from Hermes's approval request.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

GATED_TOOLS = frozenset({"linear_comment", "linear_issue_update", "linear_issue_create",
                         "linear_attach", "linear_relation"})

REQUIRED_TEXT = (
    "Not run: {targets} is outside this session's write scope. Call {tool} "
    "again with identical arguments to ask a human for approval in Linear. "
    "If that approval times out, the question stays pending in Linear: end this "
    "turn; an approval arrives as the next message, and re-running the identical "
    "call is then pre-approved once."
)

_PREVIEW_MAX = 1500
_RULE_COMMAND_RE = re.compile(r"^<(linear_[a-z_]+)> \(plugin approval rule\)$")
_DIGEST_LINE_RE = re.compile(r"^digest: ([0-9a-f]{16})$")
_GRANT_RE = re.compile(r"^<([A-Za-z0-9_]+)> \(plugin approval rule\)#([0-9a-f]{16})$")


# JSON type of every property of the gated tools' schemas (agent_tools.TOOL_SCHEMAS),
# suffixed "|null" when the schema's type list allows null. A test keeps this in sync —
# importing agent_tools here would be a cycle.
FIELD_TYPES: dict[str, dict[str, str]] = {
    "linear_attach": {"id": "string", "path": "string", "url": "string", "title": "string",
                      "subtitle": "string", "embed": "boolean"},
    "linear_comment": {"id": "string", "body": "string", "edit_marker": "string"},
    "linear_issue_create": {"title": "string", "description": "string", "parent": "string",
                            "labels": "array", "state": "string", "team": "string",
                            "project": "string", "assignee": "string", "delegate": "string",
                            "priority": "string", "estimate": "integer",
                            "due_date": "string", "cycle": "string", "milestone": "string"},
    "linear_issue_update": {"id": "string", "title": "string", "description": "string",
                            "state": "string", "add_labels": "array",
                            "remove_labels": "array", "parent": "string",
                            "project": "string|null", "assignee": "string|null",
                            "delegate": "string|null", "priority": "string|null",
                            "estimate": "integer|null", "due_date": "string|null",
                            "cycle": "string|null", "milestone": "string|null"},
    "linear_relation": {"id": "string", "type": "string", "other": "string"},
}


def _as_array(value: Any) -> Any:
    """Coerce one non-null array-schema value as Hermes's ``coerce_tool_args`` does."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, list):
            return parsed
    return [value]


def _as_number(value: str, integer_only: bool) -> Any:
    """A finite number: ``int`` when whole; a fraction stays a string when ``integer_only``."""
    try:
        f = float(value)
    except (ValueError, OverflowError):
        return value
    if f != f or f in (float("inf"), float("-inf")):
        return value
    if f == int(f):
        return int(f)
    return value if integer_only else f


def _coerce(field_type: str, value: Any) -> Any:
    """One value coerced for ``field_type`` (a FIELD_TYPES entry) as Hermes does."""
    base, _, null = field_type.partition("|")
    if field_type == "array":
        return _as_array(value)
    if not isinstance(value, str):
        return value
    if null and value.strip().lower() == "null":
        return None
    if base == "array":
        # A nullable array is a type union to Hermes: a JSON list string is parsed,
        # anything else (a bare value included) is kept as is.
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return value
        return parsed if isinstance(parsed, list) else value
    if base in ("integer", "number"):
        return _as_number(value, integer_only=base == "integer")
    if base == "boolean":
        return {"true": True, "false": False}.get(value.strip().lower(), value)
    return value


def normalize_args(tool: str, args: Any) -> dict:
    """``args`` coerced per the tool's FIELD_TYPES; idempotent on coerced args.

    The ``pre_tool_call`` hook sees the model's raw args and the handler sees them
    after Hermes's ``coerce_tool_args``; mirroring that coercion here (arrays, integers,
    numbers, booleans, ``"null"`` for nullable fields) makes both digests agree.
    """
    if not isinstance(args, dict):
        return {}
    types = FIELD_TYPES.get(tool, {})
    return {k: (_coerce(types[k], v) if k in types else v) for k, v in args.items()}


def digest(tool: str, args: Any) -> str:
    """First 16 hex chars of sha256 over the canonical JSON of tool + normalized args."""
    payload = json.dumps(
        {"tool": tool, "args": normalize_args(tool, args)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def rule_key(tool: str, digest: str) -> str:
    return f"{tool}:{digest}"


def _cut(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) > _PREVIEW_MAX:
            return f"{value[:_PREVIEW_MAX]}… [cut, {len(value)} chars]"
        return value
    if isinstance(value, dict):
        return {k: _cut(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_cut(v) for v in value]
    return value


def _one_line(value: object) -> str:
    """``value`` as text with CR/LF replaced by spaces, so it cannot add card lines."""
    return str(value or "").replace("\r", " ").replace("\n", " ")


def approval_message(tool: str, targets: list[dict], args: dict, digest: str) -> str:
    """Human-facing approval description; its last line carries the digest.

    An issue target is ``- RET-n "title"``; a team or project target (one with ``kind``)
    is ``- project "<name>"`` / ``- team "<key>"``, its name in ``identifier``."""
    lines = [f"Approve `{tool}` outside this session's tickets?"]
    for target in targets:
        identifier = _one_line(target.get("identifier", ""))
        if target.get("kind"):
            lines.append(f'- {_one_line(target["kind"])} "{identifier}"')
            continue
        lines.append(f'- {identifier} "{_one_line(target.get("title", ""))}"')
    lines.append("Arguments:")
    lines.append(
        json.dumps(_cut(args), indent=1, sort_keys=True, ensure_ascii=False, default=str)
    )
    lines.append(f"digest: {digest}")
    return "\n".join(lines)


def parse_rule(command: str, description: str | None) -> tuple[str, str] | None:
    """Return ``(tool, digest)`` for one of our plugin approval rules, else None."""
    match = _RULE_COMMAND_RE.match(command or "")
    if not match or not description:
        return None
    non_empty = [line for line in description.splitlines() if line.strip()]
    if not non_empty:
        return None
    digest_match = _DIGEST_LINE_RE.match(non_empty[-1])
    if not digest_match:
        return None
    return match.group(1), digest_match.group(1)


def grant_command(command: str, digest: str) -> str:
    return f"{command}#{digest}"


def split_grant_command(command: str | None) -> tuple[str, str] | None:
    """Inverse of :func:`grant_command` for a plugin-rule command."""
    match = _GRANT_RE.match(command or "")
    if not match:
        return None
    return match.group(1), match.group(2)
