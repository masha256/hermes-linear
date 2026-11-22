#!/usr/bin/env python3
"""Turn raw Linear probe events into scrubbed test fixtures.

Usage: scripts/scrub_fixtures.py [EVENTS_DIR]

EVENTS_DIR defaults to ~/.hermes/profiles/neo/workspace/linear-probe/events.
Each input file is ``{t, path, sig_ok, headers, payload}``; only ``payload``
is kept. Writes ``tests/fixtures/probe/<fixture_name>.json``.

Scrubbing (applied to every string, keys included, structure untouched):
- emails  -> ``user<n>@example.com`` (stable per distinct email)
- UUIDs   -> ``00000000-0000-4000-8000-<n:012d>`` (stable per distinct UUID,
  so cross-references between fixtures still line up)
- the OAuth client id -> stable fake hex
- workspace url key / team name / real person names -> neutral placeholders
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "tests" / "fixtures" / "probe"
DEFAULT_EVENTS_DIR = Path.home() / ".hermes/profiles/neo/workspace/linear-probe/events"

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
HEX32_RE = re.compile(r"\b[0-9a-f]{32}\b")
# Linear comment permalinks carry the first 8 hex digits of the comment UUID.
COMMENT_ANCHOR_RE = re.compile(r"#comment-([0-9a-f]{8})\b", re.I)

# Literal replacements, applied in order after email/UUID scrubbing.
LITERALS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"retrograde-capital", re.I), "example-workspace"),
    (re.compile(r"Retrograde Capital", re.I), "Example Team"),
    (re.compile(r"retrograde", re.I), "example"),
    (re.compile(r"Mike Machado"), "Test User"),
    (re.compile(r"/profiles/mike\b"), "/profiles/testuser"),
    (re.compile(r"machado", re.I), "user"),
    (re.compile(r"\bMike\b", re.I), "Test User"),
]


def _matches(pred: Callable[[dict], bool]) -> Callable[[dict], bool]:
    def safe(p: dict) -> bool:
        try:
            return bool(pred(p))
        except (KeyError, TypeError, AttributeError):
            return False

    return safe


def _ufrom(p: dict) -> dict:
    return p.get("updatedFrom") or {}


def _issue_ident(p: dict) -> str | None:
    return (p.get("data") or {}).get("identifier")


# fixture name -> predicate over payload; first matching event (sorted by file name) wins.
SELECTORS: dict[str, Callable[[dict], bool]] = {
    "agent_created_mention": _matches(lambda p: p["type"] == "AgentSessionEvent"
        and p["action"] == "created"
        and p["agentSession"]["comment"]["body"].startswith("@hermestest this is a top-level comment")),
    "agent_prompted_reply": _matches(lambda p: p["type"] == "AgentSessionEvent"
        and p["action"] == "prompted"),
    "agent_created_delegation": _matches(lambda p: p["type"] == "AgentSessionEvent"
        and p["action"] == "created"
        and p["agentSession"]["issue"]["identifier"] == "RET-62"
        and p["agentSession"]["comment"]["body"].startswith(
            "This thread is for an agent session with hermestest.")),
    "comment_create_mention_root": _matches(lambda p: p["type"] == "Comment"
        and p["action"] == "create"
        and p["data"]["body"].startswith("@hermestest this is a top-level comment")),
    "comment_create_reply": _matches(lambda p: p["type"] == "Comment"
        and p["action"] == "create"
        and p["data"].get("parentId")
        and p["data"]["body"].startswith("second reply in thread")),
    "comment_create_toplevel_plain": _matches(lambda p: p["type"] == "Comment"
        and p["action"] == "create"
        and not p["data"].get("parentId")
        and p["data"]["body"].startswith("top-level comment with no mention")),
    "issue_update_delegated": _matches(lambda p: p["type"] == "Issue"
        and p["action"] == "update" and "delegateId" in _ufrom(p)),
    "issue_update_labels": _matches(lambda p: p["type"] == "Issue"
        and p["action"] == "update" and "labelIds" in _ufrom(p)),
    "issue_update_state": _matches(lambda p: p["type"] == "Issue"
        and p["action"] == "update" and "stateId" in _ufrom(p)
        and _issue_ident(p) == "RET-62"),
    "issue_update_description": _matches(lambda p: p["type"] == "Issue"
        and p["action"] == "update" and "description" in _ufrom(p)
        and _issue_ident(p) == "RET-62"),
    "issue_create_delegated": _matches(lambda p: p["type"] == "Issue"
        and p["action"] == "create" and _issue_ident(p) == "RET-62"
        and p["data"].get("delegateId")),
    "project_update": _matches(lambda p: p["type"] == "Project" and p["action"] == "update"),
}


class Scrubber:
    def __init__(self) -> None:
        self.uuids: dict[str, str] = {}
        self.emails: dict[str, str] = {}
        self.hexes: dict[str, str] = {}

    def _uuid(self, m: re.Match[str]) -> str:
        key = m.group(0).lower()
        if key not in self.uuids:
            self.uuids[key] = f"00000000-0000-4000-8000-{len(self.uuids) + 1:012d}"
        return self.uuids[key]

    def register_uuids(self, obj) -> None:
        """Pre-assign fake ids for every UUID in ``obj`` (in encounter order)."""
        for u in UUID_RE.findall(json.dumps(obj)):
            self._uuid(re.match(UUID_RE, u))

    def _anchor(self, m: re.Match[str]) -> str:
        prefix = m.group(1).lower()
        for real, fake in self.uuids.items():
            if real.startswith(prefix):
                return f"#comment-{fake[-8:]}"
        return f"#comment-{self._uuid(re.match(UUID_RE, prefix + '-0000-0000-0000-000000000000'))[-8:]}"

    def _email(self, m: re.Match[str]) -> str:
        key = m.group(0).lower()
        if key not in self.emails:
            self.emails[key] = f"user{len(self.emails) + 1}@example.com"
        return self.emails[key]

    def _hex(self, m: re.Match[str]) -> str:
        key = m.group(0)
        if key not in self.hexes:
            self.hexes[key] = f"{len(self.hexes) + 1:032x}"
        return self.hexes[key]

    def text(self, s: str) -> str:
        s = EMAIL_RE.sub(self._email, s)  # before UUIDs: oauth-app emails embed one
        s = UUID_RE.sub(self._uuid, s)
        s = COMMENT_ANCHOR_RE.sub(self._anchor, s)
        for pat, repl in LITERALS:
            s = pat.sub(repl, s)
        return s

    def value(self, v):
        if isinstance(v, dict):
            return {self.text(k): self.value(x) for k, x in v.items()}
        if isinstance(v, list):
            return [self.value(x) for x in v]
        if isinstance(v, str):
            return self.text(v)
        return v

    def payload(self, p: dict) -> dict:
        p = dict(p)
        if isinstance(p.get("oauthClientId"), str):
            p["oauthClientId"] = HEX32_RE.sub(self._hex, p["oauthClientId"])
        return self.value(p)


def main(argv: list[str]) -> int:
    events_dir = Path(argv[1]).expanduser() if len(argv) > 1 else DEFAULT_EVENTS_DIR
    files = sorted(events_dir.glob("*.json"))
    if not files:
        print(f"no events in {events_dir}", file=sys.stderr)
        return 1
    events = []
    for f in files:
        raw = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(raw.get("payload"), dict):
            events.append((f.name, raw["payload"]))

    chosen: dict[str, tuple[str, dict]] = {}
    for name, pred in SELECTORS.items():
        for fname, payload in events:
            if pred(payload):
                chosen[name] = (fname, payload)
                break
    missing = [n for n in SELECTORS if n not in chosen]
    if missing:
        print(f"no matching event for: {', '.join(missing)}", file=sys.stderr)
        return 1

    # Assign fake ids in chronological (file-name) order so the mapping is stable.
    scrub = Scrubber()
    ordered = sorted(chosen.items(), key=lambda kv: kv[1][0])
    for _, (_, payload) in ordered:
        scrub.register_uuids(payload)  # so comment anchors resolve to their UUID
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, (fname, payload) in ordered:
        out = OUT_DIR / f"{name}.json"
        out.write_text(json.dumps(scrub.payload(payload), indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        print(f"{fname} -> {out.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
