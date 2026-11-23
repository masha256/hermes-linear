"""Pure helpers for out-of-scope tool approval (no I/O, stdlib only).

Used by the gated write handlers, the ``pre_tool_call`` hook, and outbound
approval rendering. A gated call is identified by a digest of its tool name
and raw arguments; the digest rides in the approval description's last line
so the hook can recover it from Hermes's approval request.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

GATED_TOOLS = frozenset({"linear_comment", "linear_issue_update", "linear_issue_create"})

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


def digest(tool: str, args: Any) -> str:
    """First 16 hex chars of sha256 over the canonical JSON of tool + args."""
    raw_args = args if isinstance(args, dict) else {}
    payload = json.dumps(
        {"tool": tool, "args": raw_args},
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


def approval_message(tool: str, targets: list[dict], args: dict, digest: str) -> str:
    """Human-facing approval description; its last line carries the digest."""
    lines = [f"Approve `{tool}` outside this session's tickets?"]
    for target in targets:
        lines.append(f'- {target.get("identifier", "")} "{target.get("title", "")}"')
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
