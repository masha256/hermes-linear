"""Gateway operational notices (home-channel prompt, setup notes) never reach a Linear ticket."""

from __future__ import annotations

from test_adapter_inbound import make_settings  # noqa: F401  (also registers the linear platform)

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult

from hermes_linear.adapter import LinearAdapter


def _adapter(extra=None):
    cfg = PlatformConfig(enabled=True)
    if extra is not None:
        cfg.extra = dict(extra)
    return LinearAdapter(cfg, settings=make_settings())


def test_notice_delivery_forced_private():
    a = _adapter()
    assert a.config.extra["notice_delivery"] == "private"


def test_notice_delivery_private_even_if_configured_public():
    a = _adapter({"notice_delivery": "public"})
    assert a.config.extra["notice_delivery"] == "private"


async def test_private_notice_is_swallowed_not_sent():
    a = _adapter()
    sent = []

    async def fake_send(*args, **kw):
        sent.append((args, kw))
        return SendResult(success=True)

    a.send = fake_send
    r = await a.send_private_notice("chat", "user", "No home channel is set for Linear.")
    assert r.success is True
    assert sent == []
