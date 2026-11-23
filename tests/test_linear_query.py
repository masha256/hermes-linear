"""``linear_query``: read-only GraphQL as the app (RET-119 spec §3).

Handlers are called from the test thread; the adapter's loop runs in a background thread,
as in ``test_agent_tools_generic``. Context resolution is pinned with ``pin_ctx``.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from test_agent_tools_runtime import new_adapter

from hermes_linear import agent_tools, plugin
from hermes_linear.agent_tools import QUERY_MAX_RESULT_CHARS, TOOL_HANDLERS, TOOL_SCHEMAS
from hermes_linear.linear_client import LinearError

linear_query = TOOL_HANDLERS["linear_query"]
DATA = {"viewer": {"id": "app-user"}}


class StubClient:
    """Records ``gql`` calls; answers ``data`` or raises ``fail``."""

    def __init__(self, data=None, fail: Exception | None = None):
        self.data = DATA if data is None else data
        self.fail = fail
        self.calls: list[tuple[str, dict | None]] = []

    async def gql(self, query, variables=None):
        self.calls.append((query, variables))
        if self.fail is not None:
            raise self.fail
        return self.data


@pytest.fixture
def loop():
    lp = asyncio.new_event_loop()
    t = threading.Thread(target=lp.run_forever, name="GATEWAY-LOOP", daemon=True)
    t.start()
    yield lp
    lp.call_soon_threadsafe(lp.stop)
    t.join(5)
    lp.close()


@pytest.fixture
def stub():
    return StubClient()


@pytest.fixture
def adapter(loop, stub):
    return new_adapter(loop, client=stub)


@pytest.fixture
def pin_ctx(monkeypatch, adapter):
    def pin(*, linear=True, issue="uuid-self", key="sk", ad=adapter):
        ctx = agent_tools.ToolContext(adapter=ad, client=getattr(ad, "client", None),
                                      session_issue_id=issue if linear else None,
                                      session_key=key if linear else None, is_linear=linear)
        monkeypatch.setattr(agent_tools, "resolve_context", lambda session_id=None: ctx)
        return ctx
    pin()
    return pin


def test_schema():
    s = TOOL_SCHEMAS["linear_query"]
    assert s["name"] == "linear_query"
    assert s["description"] == (
        "Run a read-only GraphQL query against Linear's API as the Linear app (no mutations "
        "or subscriptions). Results over 50,000 chars are refused: paginate with "
        "first:/after: or select fewer fields.")
    assert s["parameters"]["required"] == ["query"]
    assert s["parameters"]["properties"]["query"]["type"] == "string"
    assert s["parameters"]["properties"]["variables"]["type"] == "object"


def test_query_returns_data(pin_ctx, stub):
    q = "query Q($id: String!) { issue(id: $id) { id } }"
    out = linear_query({"query": q, "variables": {"id": "RET-1"}})
    assert out == {"ok": True, "data": DATA}
    assert stub.calls == [(q, {"id": "RET-1"})]


def test_variables_absent_or_null_is_none(pin_ctx, stub):
    assert linear_query({"query": "{ viewer { id } }"})["ok"] is True
    assert linear_query({"query": "{ viewer { id } }", "variables": None})["ok"] is True
    assert [v for _, v in stub.calls] == [None, None]


def test_mutation_refused_before_any_call(pin_ctx, stub):
    out = linear_query({"query": 'mutation { issueDelete(id:"x") { success } }'})
    assert out == {"ok": False, "error": "READ_ONLY"}
    assert stub.calls == []


@pytest.mark.parametrize("args", [{}, {"query": 5}, {"query": "type Q { a: Int }"},
                                  {"query": "{ a }", "variables": [1]},
                                  {"query": "{ a }", "variables": "x"}])
def test_bad_args_before_any_call(pin_ctx, stub, args):
    assert linear_query(args) == {"ok": False, "error": "BAD_ARGS"}
    assert stub.calls == []


def test_variables_must_be_object(pin_ctx, stub):
    assert linear_query({"query": "{ a }", "variables": [1]}) == {"ok": False,
                                                                  "error": "BAD_ARGS"}


def test_works_outside_linear_session(pin_ctx, stub):
    pin_ctx(linear=False)
    assert linear_query({"query": "{ viewer { id } }"}) == {"ok": True, "data": DATA}


def test_not_connected(pin_ctx, stub):
    pin_ctx(ad=new_adapter(None, client=stub))  # no running loop
    assert linear_query({"query": "{ a }"}) == {"ok": False, "error": "NOT_CONNECTED"}


def test_result_too_large(pin_ctx, stub):
    assert QUERY_MAX_RESULT_CHARS == 50_000
    stub.data = {"a": "x" * 49_992}
    assert len(json.dumps(stub.data, ensure_ascii=False)) == 50_001
    out = linear_query({"query": "{ a }"})
    assert out["ok"] is False and out["error"] == "RESPONSE_TOO_LARGE"
    assert out["size"] == 50_001 and out["limit"] == 50_000
    assert "Paginate" in out["message"]


def test_result_at_limit_passes(pin_ctx, stub):
    stub.data = {"a": "é" * 49_991}  # counted as chars, not escaped bytes
    assert len(json.dumps(stub.data, ensure_ascii=False)) == 50_000
    assert linear_query({"query": "{ a }"}) == {"ok": True, "data": stub.data}


def test_query_too_complex(pin_ctx, stub):
    stub.fail = LinearError("Query too complex", code="INPUT_ERROR", detail=(
        "The query is too complex. Complexity: 18937750. Maximum allowed complexity: 10000."))
    out = linear_query({"query": "{ a }"})
    assert out["ok"] is False and out["error"] == "QUERY_TOO_COMPLEX"
    assert "Maximum allowed complexity: 10000" in out["message"]
    assert "first:" in out["message"]


def test_query_too_complex_without_detail_uses_message(pin_ctx, stub):
    stub.fail = LinearError("Query too complex: 20000", code="INPUT_ERROR", detail=None)
    out = linear_query({"query": "{ a }"})
    assert out == {"ok": False, "error": "QUERY_TOO_COMPLEX",
                   "message": "Query too complex: 20000 Lower first: or the nesting depth."}


def test_graphql_error_has_message(pin_ctx, stub):
    stub.fail = LinearError('Cannot query field "x"', code="GRAPHQL_VALIDATION_FAILED")
    out = linear_query({"query": "{ x }"})
    assert out == {"ok": False, "error": "GRAPHQL_ERROR", "message": 'Cannot query field "x"'}


def test_graphql_error_message_cut_at_1000_chars(pin_ctx, stub):
    stub.fail = LinearError("y" * 5000, code="GRAPHQL_VALIDATION_FAILED")
    out = linear_query({"query": "{ x }"})
    assert out["error"] == "GRAPHQL_ERROR" and out["message"] == "y" * 1000


@pytest.mark.parametrize("code", [
    "RATELIMITED", "NOT_FOUND", "UNAUTHENTICATED", "AUTHENTICATION_ERROR", "FORBIDDEN", "NETWORK"])
def test_known_codes_keep_code(pin_ctx, stub, code):
    stub.fail = LinearError(f"{code} happened", code=code)
    assert linear_query({"query": "{ a }"}) == {"ok": False, "error": code,
                                                "message": f"{code} happened"}


def test_registered_handler_returns_str(pin_ctx, stub):
    raw = plugin.tool_handler("linear_query")({"query": "{ viewer { id } }"}, session_id="s")
    assert isinstance(raw, str)
    assert json.loads(raw) == {"ok": True, "data": DATA}
