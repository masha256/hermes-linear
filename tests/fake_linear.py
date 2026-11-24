"""Fake Linear API (``/oauth/token`` + ``/graphql``) for client tests.

Usage::

    fake = FakeLinear()
    await fake.start()            # binds an unused 127.0.0.1 port
    fake.on("Viewer", {"data": {"viewer": {"id": "app-user"}}})
    client = LinearClient("cid", lambda: "secret", base_url=fake.url)
    ...
    await fake.close()

Responses are programmable per GraphQL operation name (taken from the
``operation Name(`` header of the query document). ``on(op, *responses)``
queues responses consumed in order; the last one is sticky. A response is
either a JSON-able dict (HTTP 200) or a ``Resp(status, body, headers)``, or
a callable ``(call) -> dict | Resp`` evaluated per request.

Tokens: each ``/oauth/token`` request issues ``tok-1``, ``tok-2``, ... unless
``token_responses`` is programmed. ``reject_tokens`` makes ``/graphql``
return HTTP 401 for requests bearing any token in that set.

Uploads: ``PUT /upload/{name}`` stands in for the signed storage URL that
``fileUpload`` returns. It records the raw bytes in ``Call.body`` and answers
``upload_status`` (default 200); ``upload_calls()`` lists those requests.

Every request is appended to ``calls`` as a ``Call``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from aiohttp import web
from aiohttp.test_utils import TestServer

_OP_RE = re.compile(r"^\s*(?:query|mutation)\s+(\w+)")


@dataclass
class Resp:
    status: int = 200
    body: Any = None
    headers: dict = field(default_factory=dict)


@dataclass
class Call:
    method: str
    path: str
    headers: dict
    form: dict | None = None
    json: dict | None = None
    body: bytes | None = None

    @property
    def op(self) -> str | None:
        if not self.json:
            return None
        m = _OP_RE.match(self.json.get("query") or "")
        return m.group(1) if m else None

    @property
    def variables(self) -> dict:
        return (self.json or {}).get("variables") or {}

    @property
    def bearer(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        return auth[len("Bearer "):] if auth.startswith("Bearer ") else None


class FakeLinear:
    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.responses: dict[str, list] = {}
        self.token_responses: list = []
        self.reject_tokens: set[str] = set()
        self.expires_in = 2_591_999
        self.upload_status = 200
        self._issued = 0
        app = web.Application()
        app.router.add_post("/oauth/token", self._token)
        app.router.add_post("/graphql", self._graphql)
        app.router.add_put("/upload/{name}", self._upload)
        self.server = TestServer(app, host="127.0.0.1")

    # -- control -------------------------------------------------------
    async def start(self) -> None:
        await self.server.start_server()

    async def close(self) -> None:
        await self.server.close()

    @property
    def url(self) -> str:
        return str(self.server.make_url("")).rstrip("/")

    def on(self, op: str, *responses: Any) -> None:
        self.responses[op] = list(responses)

    def token_calls(self) -> list[Call]:
        return [c for c in self.calls if c.path == "/oauth/token"]

    def gql_calls(self, op: str | None = None) -> list[Call]:
        return [c for c in self.calls if c.path == "/graphql" and (op is None or c.op == op)]

    def upload_calls(self) -> list[Call]:
        return [c for c in self.calls if c.path.startswith("/upload/")]

    # -- handlers ------------------------------------------------------
    @staticmethod
    def _render(resp: Any) -> web.Response:
        if not isinstance(resp, Resp):
            resp = Resp(200, resp)
        if resp.body is None:
            return web.Response(status=resp.status, headers=resp.headers)
        return web.json_response(resp.body, status=resp.status, headers=resp.headers)

    async def _token(self, request: web.Request) -> web.Response:
        form = dict(await request.post())
        call = Call(request.method, request.path, dict(request.headers), form=form)
        self.calls.append(call)
        if self.token_responses:
            resp = self.token_responses.pop(0) if len(self.token_responses) > 1 else self.token_responses[0]
            return self._render(resp(call) if callable(resp) else resp)
        self._issued += 1
        return web.json_response({
            "access_token": f"tok-{self._issued}",
            "token_type": "Bearer",
            "expires_in": self.expires_in,
            "scope": form.get("scope", ""),
        })

    async def _upload(self, request: web.Request) -> web.Response:
        body = await request.read()
        self.calls.append(Call(request.method, request.path, dict(request.headers), body=body))
        return web.Response(status=self.upload_status)

    async def _graphql(self, request: web.Request) -> web.Response:
        body = await request.json()
        call = Call(request.method, request.path, dict(request.headers), json=body)
        self.calls.append(call)
        if call.bearer is None or call.bearer in self.reject_tokens:
            return web.json_response(
                {"errors": [{"message": "Authentication required",
                             "extensions": {"code": "AUTHENTICATION_ERROR"}}]},
                status=401,
            )
        queue = self.responses.get(call.op or "")
        if not queue:
            return web.json_response(
                {"errors": [{"message": f"fake: no response for {call.op}"}]}, status=400
            )
        resp = queue.pop(0) if len(queue) > 1 else queue[0]
        if callable(resp):
            resp = resp(call)
        return self._render(resp)
