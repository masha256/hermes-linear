"""Process-wide Linear webhook listener (spec §7.1).

One aiohttp server serves every Hermes profile at ``POST /linear/<profile>/webhook``.
Checks run in this exact order:

1. unknown profile / channel not enabled (no webhook secret) -> 404, body unread
2. body > MAX_BODY_BYTES -> 413 (Content-Length first, then enforced while reading)
3. bad ``Linear-Signature`` -> 401
   (invalid JSON after a valid signature -> 400)
4. ``webhookTimestamp`` outside +/- REPLAY_WINDOW_S of the clock -> 401
5. already-seen delivery (``Linear-Delivery`` header, else SHA-256 of the raw
   body) -> 200, not delivered. Never payload ``webhookId`` (config id, constant).
6. payload ``oauthClientId`` present and != endpoint client_id -> 401
7. 200, then ``deliver(payload)`` via ``loop.call_soon``

Never logs secrets or bodies; DEBUG logs only ``type``/``action``/``webhookId``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import logging
import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable

from aiohttp import web

from .constants import MAX_BODY_BYTES, REPLAY_WINDOW_S

logger = logging.getLogger(__name__)

ROUTE = "/linear/{profile}/webhook"
_READ_CHUNK = 65_536


def verify_signature(secret: bytes, body: bytes, header: str) -> bool:
    """Constant-time check of a hex HMAC-SHA256 ``Linear-Signature`` header."""
    if not secret or not header:
        return False
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest().encode("ascii")
    given = header.strip().lower().encode("utf-8", "replace")
    return hmac.compare_digest(expected, given)


@dataclass
class ProfileEndpoint:
    profile: str
    webhook_secret: Callable[[], bytes | None]
    client_id: str
    deliver: Callable[[dict], None]
    seen: Callable[[str], bool]


def _json(status: int, **body: Any) -> web.Response:
    return web.json_response(body, status=status)


class WebhookListener:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        clock: Callable[[], float] = time.time,
        autostart: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self._clock = clock
        self._autostart = autostart
        self._endpoints: dict[str, ProfileEndpoint] = {}
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._lock = asyncio.Lock()
        self._lifecycle: asyncio.Task | None = None
        self._pending_tasks: set[asyncio.Task] = set()
        self.start_error: BaseException | None = None
        self.app = web.Application()
        self.app.router.add_post(ROUTE, self._handle)

    # ------------------------------------------------------------ lifecycle

    @property
    def running(self) -> bool:
        return self._runner is not None

    @property
    def bound_port(self) -> int | None:
        """Actual listening port (useful when constructed with port 0)."""
        if self._runner is None:
            return None
        for addr in self._runner.addresses:
            if isinstance(addr, tuple) and len(addr) >= 2:
                return int(addr[1])
        return None

    async def start(self) -> None:
        async with self._lock:
            await self._start_locked()

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()

    async def _start_locked(self) -> None:
        if self._runner is not None:
            return
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        try:
            site = web.TCPSite(runner, self.host, self.port)
            await site.start()
        except BaseException:
            await runner.cleanup()
            raise
        self._runner, self._site = runner, site
        self.start_error = None
        logger.info("Linear webhook listener on %s:%s", self.host, self.bound_port)

    async def _stop_locked(self) -> None:
        runner, self._runner, self._site = self._runner, None, None
        if runner is not None:
            await runner.cleanup()
            logger.info("Linear webhook listener stopped")

    def register(self, ep: ProfileEndpoint) -> None:
        if ep.profile in self._endpoints:
            logger.warning("Linear webhook endpoint for profile %r replaced", ep.profile)
        self._endpoints[ep.profile] = ep
        self._schedule_reconcile()

    def unregister(self, profile: str) -> None:
        self._endpoints.pop(profile, None)
        self._schedule_reconcile()

    def _schedule_reconcile(self) -> None:
        if not self._autostart:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("no running loop; call start()/stop() explicitly")
            return
        self._lifecycle = loop.create_task(self._reconcile())

    async def _reconcile(self) -> None:
        """Run when the server should be up iff at least one profile is registered."""
        async with self._lock:
            try:
                if self._endpoints:
                    await self._start_locked()
                else:
                    await self._stop_locked()
            except Exception as exc:  # e.g. port already in use
                self.start_error = exc
                logger.error(
                    "Linear webhook listener %s:%s failed: %s",
                    self.host, self.port, type(exc).__name__,
                )

    async def wait_idle(self) -> None:
        """Wait until scheduled start/stop work has finished (tests, shutdown)."""
        while self._lifecycle is not None and not self._lifecycle.done():
            await asyncio.shield(self._lifecycle)
        async with self._lock:
            pass

    # ------------------------------------------------------------ handler

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        profile = request.match_info.get("profile", "")

        # 1. Profile must exist and be enabled (have a secret). Body unread.
        ep = self._endpoints.get(profile)
        if ep is None:
            return _json(404, error="not found")
        try:
            secret = ep.webhook_secret()
        except Exception as exc:
            logger.error("Linear webhook secret lookup failed for %r: %s", profile, type(exc).__name__)
            secret = None
        if not secret:
            logger.debug("Linear webhook for %r: channel not enabled", profile)
            return _json(404, error="not found")

        # 2. Size: trust-but-verify Content-Length, then cap while reading.
        if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
            return _json(413, error="payload too large")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await request.content.read(_READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                return _json(413, error="payload too large")
            chunks.append(chunk)
        body = b"".join(chunks)

        # 3. Signature.
        if not verify_signature(secret, body, request.headers.get("Linear-Signature", "")):
            logger.warning("Linear webhook for %r: bad signature", profile)
            return _json(401, error="bad signature")

        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            payload = None
        if not isinstance(payload, dict):
            logger.warning("Linear webhook for %r: body is not a JSON object", profile)
            return _json(400, error="invalid json")

        wid = payload.get("webhookId")
        logger.debug(
            "Linear webhook %r: type=%s action=%s webhookId=%s",
            profile, payload.get("type"), payload.get("action"), wid,
        )

        # 4. Replay window (epoch ms; symmetric to tolerate clock skew).
        ts = payload.get("webhookTimestamp")
        if (
            isinstance(ts, bool)
            or not isinstance(ts, (int, float))
            or abs(self._clock() * 1000 - ts) > REPLAY_WINDOW_S * 1000
        ):
            logger.warning("Linear webhook for %r: timestamp outside replay window", profile)
            return _json(401, error="stale")

        # 5. Dedup on the per-delivery id. NOT payload ``webhookId``: that is
        # the webhook *config* id, identical on every delivery.
        delivery = request.headers.get("Linear-Delivery", "").strip()
        dedup_key = delivery or "sha256:" + hashlib.sha256(body).hexdigest()
        try:
            dup = ep.seen(dedup_key)
        except Exception as exc:
            logger.error("Linear webhook dedup failed for %r: %s", profile, type(exc).__name__)
            return _json(500, error="internal")
        if dup:
            logger.info("Linear webhook %r: duplicate delivery %s dropped", profile, dedup_key)
            return _json(200, ok=True)

        # 6. App identity, where the payload carries one.
        cid = payload.get("oauthClientId")
        if cid is not None and cid != ep.client_id:
            logger.warning("Linear webhook for %r: oauthClientId mismatch", profile)
            return _json(401, error="client mismatch")

        # 7. Ack first, then hand off.
        resp = _json(200, ok=True)
        await resp.prepare(request)
        await resp.write_eof()
        asyncio.get_running_loop().call_soon(self._safe_deliver, ep, payload)
        return resp

    def _safe_deliver(self, ep: ProfileEndpoint, payload: dict) -> None:
        try:
            result = ep.deliver(payload)
        except Exception as exc:
            self._log_deliver_error(ep.profile, exc)
            return
        if inspect.isawaitable(result):
            task = asyncio.ensure_future(result)
            self._pending_tasks.add(task)
            task.add_done_callback(lambda t, p=ep.profile: self._deliver_done(p, t))

    def _deliver_done(self, profile: str, task: asyncio.Future) -> None:
        self._pending_tasks.discard(task)  # type: ignore[arg-type]
        if not task.cancelled() and task.exception() is not None:
            self._log_deliver_error(profile, task.exception())

    @staticmethod
    def _log_deliver_error(profile: str, exc: BaseException | None) -> None:
        # Exception messages may echo payload text: keep them out of ERROR/INFO.
        logger.error("Linear webhook delivery failed for %r: %s", profile, type(exc).__name__)
        if exc is not None and logger.isEnabledFor(logging.DEBUG):
            # Frames only: the exception message itself may contain payload text.
            frames = "".join(traceback.format_tb(exc.__traceback__))
            logger.debug("delivery traceback (%s):\n%s", type(exc).__name__, frames)


_LISTENER: WebhookListener | None = None


def get_listener(host: str, port: int) -> WebhookListener:
    """Process-wide listener; the first call fixes host and port."""
    global _LISTENER
    if _LISTENER is None:
        _LISTENER = WebhookListener(host, port)
    elif (host, port) != (_LISTENER.host, _LISTENER.port):
        logger.warning(
            "Linear webhook listener already configured on %s:%s; ignoring %s:%s",
            _LISTENER.host, _LISTENER.port, host, port,
        )
    return _LISTENER


def _reset_listener_for_tests() -> None:
    global _LISTENER
    _LISTENER = None
