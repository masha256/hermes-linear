"""Tests for hermes_linear.listener (spec §7.1 order of operations)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import logging

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from conftest import load_fixture
from hermes_linear import listener as listener_mod
from hermes_linear.constants import MAX_BODY_BYTES, REPLAY_WINDOW_S
from hermes_linear.listener import (
    ProfileEndpoint,
    WebhookListener,
    get_listener,
    verify_signature,
)

SECRET = b"test-webhook-secret-neo"
OTHER_SECRET = b"test-webhook-secret-raymond"
CLIENT_ID = "00000000000000000000000000000001"
NOW = 1_795_294_565.0  # seconds; fixture webhookTimestamp is ~ NOW * 1000


class Clock:
    def __init__(self, t: float = NOW) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Recorder:
    """Endpoint callbacks that record what the listener does."""

    def __init__(self) -> None:
        self.delivered: list[dict] = []
        self.seen_ids: set[str] = set()
        self.seen_calls: list[str] = []
        self.secret_calls = 0

    def deliver(self, payload: dict) -> None:
        self.delivered.append(payload)

    def seen(self, webhook_id: str) -> bool:
        self.seen_calls.append(webhook_id)
        if webhook_id in self.seen_ids:
            return True
        self.seen_ids.add(webhook_id)
        return False


def make_ep(profile="neo", secret=SECRET, client_id=CLIENT_ID, rec=None, deliver=None):
    rec = rec or Recorder()

    def secret_fn():
        rec.secret_calls += 1
        return secret

    ep = ProfileEndpoint(
        profile=profile,
        webhook_secret=secret_fn,
        client_id=client_id,
        deliver=deliver or rec.deliver,
        seen=rec.seen,
    )
    return ep, rec


def sign(secret: bytes, body: bytes) -> str:
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def payload(name="agent_created_mention", *, ts_ms=None, **overrides) -> dict:
    p = load_fixture(name)
    p["webhookTimestamp"] = int(NOW * 1000) if ts_ms is None else ts_ms
    p.update(overrides)
    return p


def encode(p: dict) -> bytes:
    return json.dumps(p).encode("utf-8")


async def settle() -> None:
    """Let call_soon callbacks scheduled by the handler run."""
    for _ in range(3):
        await asyncio.sleep(0)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
async def make_client(clock):
    clients: list[TestClient] = []

    async def factory(*endpoints: ProfileEndpoint):
        lst = WebhookListener("127.0.0.1", 0, clock=clock, autostart=False)
        for ep in endpoints:
            lst.register(ep)
        client = TestClient(TestServer(lst.app))
        await client.start_server()
        clients.append(client)
        return client, lst

    yield factory
    for c in clients:
        await c.close()


async def post(client, profile, body: bytes, secret: bytes | None = SECRET, headers=None):
    h = {"Content-Type": "application/json"}
    if secret is not None:
        h["Linear-Signature"] = sign(secret, body)
    h.update(headers or {})
    data = io.BytesIO(body) if len(body) > 65_536 else body  # avoid aiohttp ResourceWarning
    return await client.post(f"/linear/{profile}/webhook", data=data, headers=h)


# ---------------------------------------------------------------- signature


def test_verify_signature():
    body = b'{"a":1}'
    good = sign(SECRET, body)
    assert verify_signature(SECRET, body, good)
    assert verify_signature(SECRET, body, good.upper())
    assert not verify_signature(SECRET, body, sign(OTHER_SECRET, body))
    assert not verify_signature(SECRET, body + b" ", good)
    assert not verify_signature(SECRET, body, "")
    assert not verify_signature(SECRET, body, "zz-not-hex-é")
    assert not verify_signature(b"", body, sign(b"", body))


# ---------------------------------------------------------------- 1. profile


class ExplodingPayload:
    """A request payload that fails the test if the handler reads it."""

    def __init__(self) -> None:
        self.read_called = False

    async def read(self, n=-1):
        self.read_called = True
        raise AssertionError("body must not be read")

    readany = readchunk = readexactly = read


async def test_unknown_profile_404_body_unread(clock, make_client):
    lst = WebhookListener("127.0.0.1", 0, clock=clock, autostart=False)
    ep, rec = make_ep()
    lst.register(ep)
    stream = ExplodingPayload()
    req = make_mocked_request(
        "POST",
        "/linear/ghost/webhook",
        headers={"Content-Length": "10", "Linear-Signature": "00"},
        match_info={"profile": "ghost"},
        payload=stream,
        app=lst.app,
    )
    resp = await lst._handle(req)
    assert resp.status == 404
    assert not stream.read_called
    assert rec.secret_calls == 0

    # Over real HTTP too: even a huge body gets 404, not 413.
    client, _ = await make_client(ep)
    r = await post(client, "ghost", b"x" * (MAX_BODY_BYTES + 10))
    assert r.status == 404
    assert rec.delivered == []


async def test_profile_without_secret_404(clock, make_client):
    for secret in (None, b""):
        lst = WebhookListener("127.0.0.1", 0, clock=clock, autostart=False)
        ep, rec = make_ep(secret=secret)
        lst.register(ep)
        stream = ExplodingPayload()
        req = make_mocked_request(
            "POST",
            "/linear/neo/webhook",
            headers={"Content-Length": "10"},
            match_info={"profile": "neo"},
            payload=stream,
            app=lst.app,
        )
        resp = await lst._handle(req)
        assert resp.status == 404
        assert not stream.read_called

    # Over HTTP: 404, and the shared listener keeps serving other profiles.
    ep_none, rec_none = make_ep(profile="neo", secret=None)
    ep_ok, rec_ok = make_ep(profile="raymond", secret=OTHER_SECRET)
    client, _ = await make_client(ep_none, ep_ok)
    body = encode(payload())
    r = await post(client, "neo", body, secret=b"")
    assert r.status == 404
    r = await post(client, "raymond", body, secret=OTHER_SECRET)
    assert r.status == 200
    await settle()
    assert rec_none.delivered == [] and len(rec_ok.delivered) == 1


async def test_secret_callable_raising_404_and_listener_survives(make_client):
    def boom():
        raise RuntimeError("secret store down")

    rec = Recorder()
    ep = ProfileEndpoint("neo", boom, CLIENT_ID, rec.deliver, rec.seen)
    ep_ok, rec_ok = make_ep(profile="raymond", secret=OTHER_SECRET)
    client, _ = await make_client(ep, ep_ok)
    body = encode(payload())
    assert (await post(client, "neo", body)).status == 404
    assert (await post(client, "raymond", body, secret=OTHER_SECRET)).status == 200


async def test_unregistered_profile_404(make_client):
    ep, rec = make_ep()
    client, lst = await make_client(ep)
    lst.unregister("neo")
    r = await post(client, "neo", encode(payload()))
    assert r.status == 404


async def test_wrong_method_and_path(make_client):
    ep, _ = make_ep()
    client, _ = await make_client(ep)
    assert (await client.get("/linear/neo/webhook")).status == 405
    assert (await client.post("/linear/neo/other", data=b"{}")).status == 404


# ---------------------------------------------------------------- 2. size


async def test_body_over_1mb_413(make_client):
    ep, rec = make_ep()
    client, _ = await make_client(ep)
    big = b"x" * (MAX_BODY_BYTES + 1)
    r = await post(client, "neo", big)
    assert r.status == 413
    assert rec.delivered == [] and rec.seen_calls == []


async def test_body_over_1mb_without_content_length_413(make_client):
    ep, rec = make_ep()
    client, _ = await make_client(ep)

    async def gen():
        chunk = b"x" * 65536
        for _ in range(MAX_BODY_BYTES // len(chunk) + 2):
            yield chunk

    r = await client.post(
        "/linear/neo/webhook", data=gen(), headers={"Linear-Signature": "00"}
    )
    assert r.status == 413
    assert rec.delivered == []


async def test_body_exactly_1mb_not_413(make_client):
    ep, _ = make_ep()
    client, _ = await make_client(ep)
    r = await post(client, "neo", b"x" * MAX_BODY_BYTES)
    assert r.status == 400  # signed but not JSON: passes size + signature


# ---------------------------------------------------------------- 3. signature


async def test_bad_signature_401(make_client):
    ep, rec = make_ep()
    client, _ = await make_client(ep)
    body = encode(payload())
    r = await post(client, "neo", body, secret=b"wrong")
    assert r.status == 401
    r = await post(client, "neo", body, secret=None)  # header missing
    assert r.status == 401
    await settle()
    assert rec.delivered == [] and rec.seen_calls == []


async def test_invalid_json_after_valid_signature_400(make_client):
    ep, rec = make_ep()
    client, _ = await make_client(ep)
    for body in (b"not json", b"[1, 2]", b"\xff\xfe"):
        r = await post(client, "neo", body)
        assert r.status == 400
    assert rec.seen_calls == [] and rec.delivered == []


# ---------------------------------------------------------------- 4. replay


async def test_stale_timestamp_401(clock, make_client):
    ep, rec = make_ep()
    client, _ = await make_client(ep)
    stale = int((clock() - REPLAY_WINDOW_S - 1) * 1000)
    r = await post(client, "neo", encode(payload(ts_ms=stale)))
    assert r.status == 401
    await settle()
    assert rec.delivered == [] and rec.seen_calls == []

    # Inside the window is fine.
    fresh = int((clock() - REPLAY_WINDOW_S + 1) * 1000)
    r = await post(client, "neo", encode(payload(ts_ms=fresh)))
    assert r.status == 200


async def test_future_timestamp_401(clock, make_client):
    ep, rec = make_ep()
    client, _ = await make_client(ep)
    future = int((clock() + REPLAY_WINDOW_S + 1) * 1000)
    r = await post(client, "neo", encode(payload(ts_ms=future)))
    assert r.status == 401
    assert rec.seen_calls == []


async def test_missing_or_bad_timestamp_401(make_client):
    ep, rec = make_ep()
    client, _ = await make_client(ep)
    p = payload()
    del p["webhookTimestamp"]
    assert (await post(client, "neo", encode(p))).status == 401
    for bad in ("1795294565211", None, True):
        r = await post(client, "neo", encode(payload(webhookTimestamp=bad)))
        assert r.status == 401
    assert rec.seen_calls == []


async def test_replay_uses_injected_clock(clock, make_client):
    ep, rec = make_ep()
    client, _ = await make_client(ep)
    body = encode(payload())  # timestamp == NOW
    clock.t = NOW + REPLAY_WINDOW_S + 5
    assert (await post(client, "neo", body)).status == 401


# ---------------------------------------------------------------- 5. dedup


async def test_duplicate_webhook_200_not_delivered(make_client):
    ep, rec = make_ep()
    client, _ = await make_client(ep)
    body = encode(payload())
    hdr = {"Linear-Delivery": "d-1"}
    r1 = await post(client, "neo", body, headers=hdr)
    r2 = await post(client, "neo", body, headers=hdr)
    assert r1.status == 200 and r2.status == 200
    await settle()
    assert len(rec.delivered) == 1
    assert rec.seen_calls == ["d-1"] * 2


async def test_same_webhook_id_distinct_deliveries_all_delivered(make_client):
    # Regression: payload webhookId is the webhook *config* id, identical on
    # every delivery. Deduping on it dropped every event after the first.
    ep, rec = make_ep()
    client, _ = await make_client(ep)
    p1 = payload(webhookId="cfg-1", action="created")
    p2 = payload(webhookId="cfg-1", action="prompted")
    r1 = await post(client, "neo", encode(p1), headers={"Linear-Delivery": "d-1"})
    r2 = await post(client, "neo", encode(p2), headers={"Linear-Delivery": "d-2"})
    assert r1.status == 200 and r2.status == 200
    await settle()
    assert rec.delivered == [p1, p2]


async def test_same_delivery_id_dropped_even_if_body_differs(make_client):
    ep, rec = make_ep()
    client, _ = await make_client(ep)
    p1 = payload(action="created")
    p2 = payload(action="prompted")
    hdr = {"Linear-Delivery": "d-same"}
    assert (await post(client, "neo", encode(p1), headers=hdr)).status == 200
    assert (await post(client, "neo", encode(p2), headers=hdr)).status == 200
    await settle()
    assert rec.delivered == [p1]


async def test_without_delivery_header_dedups_on_body(make_client):
    ep, rec = make_ep()
    client, _ = await make_client(ep)
    p1 = payload(webhookId="cfg-1", action="created")
    p2 = payload(webhookId="cfg-1", action="prompted")
    for p in (p1, p2, p1):
        assert (await post(client, "neo", encode(p))).status == 200
    await settle()
    assert rec.delivered == [p1, p2]
    assert rec.seen_calls[0] == "sha256:" + hashlib.sha256(encode(p1)).hexdigest()


async def test_duplicate_drop_logged_at_info(make_client, caplog):
    caplog.set_level(logging.INFO, logger="hermes_linear.listener")
    ep, _ = make_ep()
    client, _ = await make_client(ep)
    body = encode(payload())
    hdr = {"Linear-Delivery": "d-dup"}
    await post(client, "neo", body, headers=hdr)
    await post(client, "neo", body, headers=hdr)
    assert any(
        r.levelno == logging.INFO and "duplicate" in r.getMessage() and "d-dup" in r.getMessage()
        for r in caplog.records
    )


async def test_seen_raising_500_and_not_delivered(make_client):
    rec = Recorder()

    def bad_seen(_wid):
        raise RuntimeError("db locked")

    ep = ProfileEndpoint("neo", lambda: SECRET, CLIENT_ID, rec.deliver, bad_seen)
    client, _ = await make_client(ep)
    r = await post(client, "neo", encode(payload()))
    assert r.status == 500
    await settle()
    assert rec.delivered == []


# ---------------------------------------------------------------- 6. client id


async def test_client_id_mismatch_401(make_client):
    ep, rec = make_ep(client_id="some-other-client")
    client, _ = await make_client(ep)
    r = await post(client, "neo", encode(payload()))
    assert r.status == 401
    await settle()
    assert rec.delivered == []


async def test_payload_without_client_id_accepted(make_client):
    # Data-change events (Issue, Comment, ...) carry no oauthClientId.
    ep, rec = make_ep(client_id="whatever")
    client, _ = await make_client(ep)
    p = payload("comment_create_reply")
    assert "oauthClientId" not in p
    r = await post(client, "neo", encode(p))
    assert r.status == 200
    await settle()
    assert rec.delivered == [p]


# ---------------------------------------------------------------- 7. deliver


async def test_valid_event_200_then_delivered(make_client):
    ep, rec = make_ep()
    client, _ = await make_client(ep)
    p = payload()
    r = await post(client, "neo", encode(p))
    assert r.status == 200
    assert (await r.json()) == {"ok": True}
    await settle()
    assert rec.delivered == [p]
    assert rec.delivered[0]["agentSession"] == p["agentSession"]


async def test_deliver_not_called_inside_handler(clock):
    """deliver is scheduled with call_soon, i.e. after the handler finished."""
    lst = WebhookListener("127.0.0.1", 0, clock=clock, autostart=False)
    ep, rec = make_ep()
    lst.register(ep)
    client = TestClient(TestServer(lst.app))
    await client.start_server()
    try:
        calls: list = []
        loop = asyncio.get_running_loop()
        real = loop.call_soon

        def spy(cb, *args, **kw):
            calls.append(cb)
            return real(cb, *args, **kw)

        loop.call_soon = spy  # type: ignore[method-assign]
        try:
            r = await post(client, "neo", encode(payload()))
        finally:
            loop.call_soon = real  # type: ignore[method-assign]
        assert r.status == 200
        await settle()
        assert len(rec.delivered) == 1
        assert any(getattr(c, "__name__", "") == "_safe_deliver" for c in calls)
    finally:
        await client.close()


async def test_deliver_exception_does_not_break_listener(make_client, caplog):
    calls: list[dict] = []

    def deliver(p):
        calls.append(p)
        raise RuntimeError(f"adapter exploded on {p['promptContext']}")

    ep, rec = make_ep(deliver=deliver)
    client, _ = await make_client(ep)
    caplog.set_level(logging.DEBUG, logger="hermes_linear.listener")
    r1 = await post(client, "neo", encode(payload()))
    assert r1.status == 200
    await settle()
    r2 = await post(client, "neo", encode(payload(webhookId="another-id")))
    assert r2.status == 200
    await settle()
    assert len(calls) == 2
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "this is a top-level comment" not in text
    assert "Test ticket for neo" not in text  # exception message echoes payload
    assert any("RuntimeError" in r.getMessage() for r in caplog.records)


async def test_async_deliver_exception_logged(make_client):
    calls: list[dict] = []

    async def deliver(p):
        calls.append(p)
        raise RuntimeError("async boom")

    ep, _ = make_ep(deliver=deliver)
    client, _ = await make_client(ep)
    assert (await post(client, "neo", encode(payload()))).status == 200
    await settle()
    assert len(calls) == 1
    assert (await post(client, "neo", encode(payload(webhookId="x2")))).status == 200


async def test_two_profiles_isolated(make_client):
    ep_neo, rec_neo = make_ep(profile="neo", secret=SECRET)
    ep_ray, rec_ray = make_ep(profile="raymond", secret=OTHER_SECRET)
    client, _ = await make_client(ep_neo, ep_ray)
    p1 = payload(webhookId="w-neo")
    p2 = payload(webhookId="w-ray")
    b1, b2 = encode(p1), encode(p2)
    d1, d2 = {"Linear-Delivery": "d-neo"}, {"Linear-Delivery": "d-ray"}

    # Cross-signed requests are rejected.
    assert (await post(client, "neo", b1, secret=OTHER_SECRET, headers=d1)).status == 401
    assert (await post(client, "raymond", b2, secret=SECRET, headers=d2)).status == 401
    # Own signatures accepted.
    assert (await post(client, "neo", b1, secret=SECRET, headers=d1)).status == 200
    assert (await post(client, "raymond", b2, secret=OTHER_SECRET, headers=d2)).status == 200
    await settle()
    assert rec_neo.delivered == [p1]
    assert rec_ray.delivered == [p2]
    assert rec_neo.seen_calls == ["d-neo"] and rec_ray.seen_calls == ["d-ray"]


async def test_no_secret_or_body_logged(make_client, caplog):
    caplog.set_level(logging.DEBUG, logger="hermes_linear.listener")
    ep, _ = make_ep()
    client, _ = await make_client(ep)
    await post(client, "neo", encode(payload()), secret=b"wrong")
    await post(client, "neo", encode(payload()))
    await settle()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert SECRET.decode() not in text
    assert "top-level comment" not in text


# ---------------------------------------------------------------- lifecycle


@pytest.fixture
def reset_singleton():
    listener_mod._reset_listener_for_tests()
    yield
    listener_mod._reset_listener_for_tests()


async def test_get_listener_one_per_host_port(reset_singleton):
    a = get_listener("127.0.0.1", 8650)
    b = get_listener("0.0.0.0", 9999)  # another profile's listener_port: its own listener
    assert b is not a and (b.host, b.port) == ("0.0.0.0", 9999)
    assert (a.host, a.port) == ("127.0.0.1", 8650)


async def test_get_listener_replaces_only_a_disposable_listener(reset_singleton, clock):
    ep, _ = make_ep()
    a = get_listener("127.0.0.1", 0, ep)  # lookup + register in one step
    assert not a.disposable
    assert get_listener("127.0.0.1", 0) is a  # in use: shared, never replaced
    a.unregister("neo")
    await a.wait_idle()
    assert a.disposable and not a.running
    assert get_listener("127.0.0.1", 0) is not a  # idle: a fresh one (new loop, tests)


async def test_unregister_with_endpoint_removes_only_that_endpoint(clock):
    lst = WebhookListener("127.0.0.1", 0, clock=clock, autostart=False)
    old, _ = make_ep()
    new, _ = make_ep()
    lst.register(old)
    lst.register(new)  # a reconnect's fresh adapter replaced the route
    lst.unregister("neo", old)  # the old adapter's late disconnect
    assert lst._endpoints["neo"] is new
    lst.unregister("neo", new)
    assert "neo" not in lst._endpoints
    lst.unregister("neo")  # unknown profile: no-op


def test_error_text_names_errno_never_the_message():
    assert listener_mod.error_text(OSError(98, "Address already in use")) == \
        "OSError errno 98 (Address already in use)"
    assert listener_mod.error_text(ValueError("payload text")) == "ValueError"


async def test_start_stop_on_register_unregister(clock):
    lst = WebhookListener("127.0.0.1", 0, clock=clock)
    ep, rec = make_ep()
    lst.register(ep)
    await lst.wait_idle()
    assert lst.running
    port = lst.bound_port
    assert port

    body = encode(payload())
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"http://127.0.0.1:{port}/linear/neo/webhook",
            data=body,
            headers={"Linear-Signature": sign(SECRET, body)},
        ) as r:
            assert r.status == 200
    await settle()
    assert len(rec.delivered) == 1

    ep2, _ = make_ep(profile="raymond")
    lst.register(ep2)
    lst.unregister("neo")
    await lst.wait_idle()
    assert lst.running  # raymond still registered
    lst.unregister("raymond")
    await lst.wait_idle()
    assert not lst.running


async def test_register_unregister_quickly_ends_stopped(clock):
    lst = WebhookListener("127.0.0.1", 0, clock=clock)
    ep, _ = make_ep()
    lst.register(ep)
    lst.unregister("neo")
    await lst.wait_idle()
    assert not lst.running
    await lst.stop()  # idempotent


async def test_explicit_start_stop(clock):
    lst = WebhookListener("127.0.0.1", 0, clock=clock, autostart=False)
    await lst.start()
    await lst.start()  # idempotent
    assert lst.running and lst.bound_port
    await lst.stop()
    assert not lst.running
