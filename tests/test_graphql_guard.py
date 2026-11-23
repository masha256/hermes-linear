"""The read-only GraphQL guard of ``linear_query`` (RET-119 spec §3.1)."""

from __future__ import annotations

import pytest

from hermes_linear import graphql_guard
from hermes_linear.graphql_guard import MAX_QUERY_CHARS, check


@pytest.mark.parametrize("query", [
    "{ viewer { id } }",
    "query Q($id: String!) { issue(id: $id) { id } }",
    "fragment F on Issue { id } query { issue(id:\"x\") { ...F } }",
    "# mutation\n{ viewer { id } }",
    '{ issues(filter:{title:{contains:"mutation { x }"}}) { nodes { id } } }',
    'query { a(x: """mutation\n{""") { id } }',
    "query A { a } query B { b }",
])
def test_read_only_documents_pass(query):
    assert check(query) is None


@pytest.mark.parametrize("query", [
    "mutation { issueDelete(id:\"x\") { success } }",
    "subscription { a }",
    "query { a } mutation { b }",
    " \n, mutation M { b }",
])
def test_writes_and_subscriptions_are_read_only_refusals(query):
    assert check(query) == "READ_ONLY"


@pytest.mark.parametrize("query", [
    "",
    "   ",
    None,
    42,
    "type Query { a: Int }",
    "extend type X { a: Int }",
    "{ a ",
    "{ a } }",
    '{ a(x: "unterminated) }',
    "query",
    "x" * 20_001,
])
def test_malformed_or_non_query_documents_are_bad_args(query):
    assert check(query) == "BAD_ARGS"


def test_size_limit():
    assert MAX_QUERY_CHARS == 20_000
    padded = "{ a }" + " " * (MAX_QUERY_CHARS - 5)
    assert len(padded) == MAX_QUERY_CHARS and check(padded) is None
    assert check(padded + " ") == "BAD_ARGS"


def test_escaped_block_string_quote_does_not_end_the_string():
    assert check('query { a(x: """a \\""" mutation { b }""") { id } }') is None


def test_escaped_quote_in_string_does_not_end_the_string():
    assert check('{ a(x: "say \\"mutation { b }\\"") { id } }') is None


def test_string_cannot_span_lines():
    assert check('{ a(x: "line\nbreak") { id } }') == "BAD_ARGS"


def test_bom_and_spread_tokens():
    assert check("\ufeff{ issue(id:\"x\") { ...on Issue { id } } }") is None


def test_module_is_dependency_free():
    src = open(graphql_guard.__file__, encoding="utf-8").read()
    imports = [ln for ln in src.splitlines() if ln.startswith(("import ", "from "))]
    assert all(ln in ("from __future__ import annotations", "from typing import Any")
               for ln in imports), imports
