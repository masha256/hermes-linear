"""Shared test helpers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "probe"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def fixture_names() -> list[str]:
    """Names of all scrubbed probe fixtures (file stem == fixture name)."""
    return sorted(p.stem for p in FIXTURE_DIR.glob("*.json"))


def load_fixture(name: str) -> dict:
    """Return the scrubbed webhook ``payload`` of the probe event ``name``."""
    with open(FIXTURE_DIR / f"{name}.json", encoding="utf-8") as fh:
        return json.load(fh)
