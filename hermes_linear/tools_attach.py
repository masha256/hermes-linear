"""Attach tools (RET-120 spec §1.3–1.4, §2.4–2.5): the local-file path policy, uploads,
URL links, ``embed`` and ``linear_session_link``.

Built on ``agent_tools``' ``_tool`` machinery. Like ``tools_read``, this module registers
itself at its end (Ruling R3); ``SCHEMAS`` / ``HANDLERS`` stay empty until the first
attach tool lands.
"""

from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass
from typing import Callable

from . import agent_tools
from .agent_tools import _Refusal

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


SCHEMAS: dict[str, dict] = {}

HANDLERS: dict[str, Callable[..., dict]] = {}

# Register with ``agent_tools`` (Ruling R3).
agent_tools.TOOL_SCHEMAS.update(SCHEMAS)
agent_tools.TOOL_HANDLERS.update(HANDLERS)
for _tool_name, _handler in HANDLERS.items():
    setattr(agent_tools, _tool_name, _handler)
