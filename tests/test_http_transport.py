"""HTTP API transport tests: auth discipline and the request/reply loop."""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from alfred.adapters.http_transport import HttpTransportAdapter, authorised
from alfred.config import HttpConfig
from alfred.domain.schemas import InboundMessage
from alfred.errors import ConfigError
from alfred.ports.transport import OutboundMessage

TOKEN = "secret-token"


def test_authorised_truth_table() -> None:
    assert authorised(f"Bearer {TOKEN}", TOKEN)
    assert not authorised(None, TOKEN)
    assert not authorised("", TOKEN)
    assert not authorised(TOKEN, TOKEN)  # missing Bearer scheme
    assert not authorised("Bearer wrong", TOKEN)
    assert not authorised("Basic abc", TOKEN)


def test_adapter_refuses_to_exist_without_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALFRED_HTTP_TOKEN", raising=False)

    async def handler(_msg: Any) -> None:
        pass

    with pytest.raises(ConfigError):
        HttpTransportAdapter(HttpConfig(enabled=True), handler)


def make_adapter(monkeypatch: pytest.MonkeyPatch) -> HttpTransportAdapter:
    monkeypatch.setenv("ALFRED_HTTP_TOKEN", TOKEN)

    adapter_holder: list[HttpTransportAdapter] = []

    async def handler(message: InboundMessage) -> None:
        # Echo core: replies through the transport like AlfredCore does.
        await adapter_holder[0].send(
            OutboundMessage(channel=message.channel, text=f"echo: {message.text}")
        )

    adapter = HttpTransportAdapter(HttpConfig(enabled=True), handler)
    adapter_holder.append(adapter)
    return adapter


async def test_post_message_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = make_adapter(monkeypatch)
    client = TestClient(TestServer(adapter.make_app()))
    await client.start_server()
    try:
        response = await client.post(
            "/message",
            json={"text": "status please"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert response.status == 200
        body = await response.json()
        assert body == {"replies": ["echo: status please"]}
    finally:
        await client.close()


async def test_unauthenticated_caller_gets_a_blank_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = make_adapter(monkeypatch)
    client = TestClient(TestServer(adapter.make_app()))
    await client.start_server()
    try:
        response = await client.post("/message", json={"text": "hi"})
        assert response.status == 404  # learns nothing, not even 401
        bad_body = await client.post(
            "/message",
            data=b"not json",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert bad_body.status == 400
        health = await client.get("/health")
        assert health.status == 200
    finally:
        await client.close()


async def test_send_to_finished_channel_is_dropped_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = make_adapter(monkeypatch)
    await adapter.send(OutboundMessage(channel="http:gone", text="late reply"))
