"""Render queued ticket changes as a compact digest (spec §7.5).

Pure module — no Hermes imports. ``fold`` turns the unconsumed
:class:`~hermes_linear.models.ContextItem` list of a ticket (oldest first)
into the text placed at the top of the agent's next turn input::

    Since your last turn:
    - Status: Todo → In Progress (Mike Machado)
    - Labels: +Feature (Mike Machado)
    - [Project Valkyrie] status post: "On track, …" (Val)
    - +3 earlier changes (priority, description)
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field as dc_field

from .constants import DIGEST_CAP_CHARS
from .models import ContextItem

HEADER = "Since your last turn:"
QUOTE_MAX = 200
ELLIPSIS = "…"

_LABEL_OVERRIDES = {"state": "Status", "status post": "status post"}
_QUOTED_FIELDS = {"description", "content", "status post"}


@dataclass
class _Group:
    item: ContextItem          # first item (key fields, earliest old)
    new: str | None
    actor: str
    pos: int                   # index of the latest item folded in
    labels: list[str] = dc_field(default_factory=list)


def _label(field: str) -> str:
    if field in _LABEL_OVERRIDES:
        return _LABEL_OVERRIDES[field]
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", field).replace("_", " ").split()
    return " ".join(w[:1].upper() + w[1:] for w in words) or field


def _flat(text: str | None) -> str:
    return " ".join((text if text is not None else "(none)").split())


def _quote(text: str | None) -> str:
    body = _flat(text)
    if len(body) > QUOTE_MAX:
        body = body[: QUOTE_MAX - 1] + ELLIPSIS
    return f'"{body}"'


def _is_quoted(item: ContextItem) -> bool:
    return item.kind == "comment" or item.field in _QUOTED_FIELDS


def _never_collapses(item: ContextItem) -> bool:
    return item.kind == "comment" or item.field == "status post"


def _merge_labels(tokens: list[str], diff: str | None) -> None:
    for tok in (t.strip() for t in (diff or "").split(",")):
        if len(tok) < 2 or tok[0] not in "+-":
            continue
        opposite = ("-" if tok[0] == "+" else "+") + tok[1:]
        if opposite in tokens:
            tokens.remove(opposite)      # +X then -X (or -X then +X): no net change
        elif tok not in tokens:
            tokens.append(tok)


def _group(items: Sequence[ContextItem]) -> list[_Group]:
    groups: list[_Group] = []
    by_key: dict[tuple, _Group] = {}
    for pos, it in enumerate(items):
        key = (it.issue_id, it.kind, it.field, it.project)
        g = None if _never_collapses(it) else by_key.get(key)
        if g is None:
            g = _Group(item=it, new=it.new, actor=it.actor, pos=pos)
            groups.append(g)
            if not _never_collapses(it):
                by_key[key] = g
        else:
            g.new, g.actor, g.pos = it.new, it.actor, pos
        if it.field == "labels":
            _merge_labels(g.labels, it.new)
    kept = [g for g in groups if g.item.field != "labels" or g.labels]
    return sorted(kept, key=lambda g: g.pos)


def _render(g: _Group) -> str:
    it = g.item
    prefix = f"[Project {_flat(it.project)}] " if it.kind == "project" else ""
    label = _label(it.field)
    if it.field == "labels":
        value = ", ".join(g.labels)
    elif _is_quoted(it):
        value = _quote(g.new)
    elif it.old is not None:
        value = f"{_flat(it.old)} → {_flat(g.new)}"
    else:
        value = _flat(g.new)
    return f"- {prefix}{label}: {value} ({_flat(g.actor)})"


def _summary(dropped: list[_Group]) -> str:
    names: list[str] = []
    for g in dropped:
        name = _label(g.item.field).lower()
        if name not in names:
            names.append(name)
    return f"- +{len(dropped)} earlier changes ({', '.join(names)})"


def _join(lines: list[str]) -> str:
    return "\n".join([HEADER, *lines])


def fold(items: Sequence[ContextItem], cap: int = DIGEST_CAP_CHARS) -> str | None:
    """Digest ``items`` (oldest first) into at most ``cap`` chars; None if empty."""
    groups = _group(items)
    if not groups:
        return None
    lines = [_render(g) for g in groups]
    start = 0
    while True:
        tail = [_summary(groups[:start])] if start else []
        out = _join(lines[start:] + tail)
        if len(out) <= cap or start >= len(lines) - 1:
            break
        start += 1
    if len(out) > cap:
        # Only the newest line is left and it is still too long: truncate it.
        room = cap - len(_join(tail)) - 1  # newline before the line
        last = lines[-1]
        if room > len(ELLIPSIS):
            last = last[: room - len(ELLIPSIS)] + ELLIPSIS
            out = _join([last] + tail)
        if len(out) > cap:
            out = out[: max(cap - len(ELLIPSIS), 0)] + ELLIPSIS
            out = out[:cap]
    return out


__all__ = ["HEADER", "fold"]
