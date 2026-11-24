"""Read-only check of a GraphQL document for ``linear_query`` (RET-119 spec §3.1).

A dependency-free single-pass lexer: strings, block strings and ``#`` comments are single
tokens (or skipped), so a keyword inside them never counts. At depth 0 (``{}`` and ``()``
counted together) every definition starts with ``{`` (an anonymous query), ``query`` or
``fragment``; ``mutation`` / ``subscription`` there is a write. Anything else (SDL, stray
names, unbalanced brackets, unterminated strings, no definition, oversize) is malformed.
"""

from __future__ import annotations

from typing import Any

MAX_QUERY_CHARS = 20_000

READ_ONLY = "READ_ONLY"
BAD_ARGS = "BAD_ARGS"

_IGNORED = " \t\r\n,\ufeff"
_NAME_START = "_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_NAME_CHARS = _NAME_START + "0123456789"
_DEFINITIONS = {"query", "fragment"}
_WRITES = {"mutation", "subscription"}
_OPEN = "{("
_CLOSE = "})"


def _tokens(src: str):
    """Yield ``(kind, text)`` tokens: ``name``, ``string`` or ``punct``. ``None`` = bad."""
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in _IGNORED:
            i += 1
        elif c == "#":
            while i < n and src[i] not in "\r\n":
                i += 1
        elif src.startswith('"""', i):
            i += 3
            while not src.startswith('"""', i):
                if i >= n:
                    yield None  # unterminated block string
                    return
                i += 4 if src.startswith('\\"""', i) else 1
            i += 3
            yield "string", ""
        elif c == '"':
            i += 1
            while i < n and src[i] != '"':
                if src[i] in "\r\n":
                    break
                i += 2 if src[i] == "\\" else 1
            if i >= n or src[i] != '"':
                yield None  # unterminated string
                return
            i += 1
            yield "string", ""
        elif c in _NAME_START:
            j = i + 1
            while j < n and src[j] in _NAME_CHARS:
                j += 1
            yield "name", src[i:j]
            i = j
        elif src.startswith("...", i):
            i += 3
            yield "punct", "..."
        else:
            i += 1
            yield "punct", c


def check(query: Any) -> str | None:
    """``None`` if ``query`` is a read-only GraphQL document, else ``READ_ONLY`` / ``BAD_ARGS``."""
    if not isinstance(query, str) or len(query) > MAX_QUERY_CHARS:
        return BAD_ARGS
    depth = 0
    pending = False  # a ``query`` / ``fragment`` header still waiting for its ``{``
    definitions = 0
    for tok in _tokens(query):
        if tok is None:
            return BAD_ARGS
        kind, text = tok
        if depth == 0:
            if kind == "name" and text in _WRITES:
                return READ_ONLY
            if text == "{":
                pending = False
                definitions += 1
            elif not pending:
                if kind == "name" and text in _DEFINITIONS:
                    pending = True
                else:
                    return BAD_ARGS
        if kind == "punct" and text in _OPEN:
            depth += 1
        elif kind == "punct" and text in _CLOSE:
            depth -= 1
            if depth < 0:
                return BAD_ARGS
    if depth != 0 or pending or definitions == 0:
        return BAD_ARGS
    return None
