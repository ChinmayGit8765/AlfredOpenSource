"""Telegram transport tests: filter rules and sends, no network anywhere."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from alfred.adapters.telegram_transport import (
    TELEGRAM_MESSAGE_LIMIT,
    TelegramTransportAdapter,
    to_inbound,
)
from alfred.config import TelegramConfig
from alfred.domain.schemas import InboundMessage
from alfred.errors import TransportError
from alfred.ports.transport import OutboundMessage

OWNER = 777


def update(
    *,
    sender: int = OWNER,
    chat: int = 4242,
    text: str | None = "hello alfred",
    update_id: int = 1,
    key: str = "message",
) -> dict[str, Any]:
    message: dict[str, Any] = {"from": {"id": sender}, "chat": {"id": chat}, "date": 1750000000}
    if text is not None:
        message["text"] = text
    return {"update_id": update_id, key: message}


def test_owner_message_maps_to_namespaced_inbound() -> None:
    inbound = to_inbound(update(), OWNER)
    assert inbound is not None
    assert inbound.channel == "telegram:4242"
    assert inbound.text == "hello alfred"
    assert inbound.provenance == "owner"
    assert inbound.at is not None and inbound.at.tzinfo is not None


def test_strangers_bots_and_noise_are_ignored() -> None:
    assert to_inbound(update(sender=123), OWNER) is None  # not the owner
    assert to_inbound(update(text=None), OWNER) is None  # sticker/photo etc.
    assert to_inbound(update(text="   "), OWNER) is None  # empty text
    assert to_inbound({"update_id": 9}, OWNER) is None  # not a message update
    # owner_id 0 (unconfigured) obeys nobody, not everybody.
    assert to_inbound(update(sender=0), 0) is None


def test_edited_messages_also_route() -> None:
    inbound = to_inbound(update(key="edited_message"), OWNER)
    assert inbound is not None and inbound.text == "hello alfred"


class StubClient:
    """Records posts; shaped like the slice of httpx.AsyncClient we use."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.post_status = 200

    async def post(self, url: str, json: dict[str, Any]) -> Any:
        self.posts.append((url, json))
        status = self.post_status

        class _Response:
            status_code = status

        return _Response()

    async def aclose(self) -> None:
        pass


def make_adapter(monkeypatch: pytest.MonkeyPatch) -> tuple[TelegramTransportAdapter, StubClient]:
    monkeypatch.setenv("ALFRED_TELEGRAM_TOKEN", "test-token")
    stub = StubClient()

    async def handler(_msg: Any) -> None:
        pass

    adapter = TelegramTransportAdapter(
        TelegramConfig(enabled=True, owner_id=OWNER),
        handler,
        client=stub,  # type: ignore[arg-type]
    )
    return adapter, stub


async def test_send_strips_prefix_and_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, stub = make_adapter(monkeypatch)
    long_text = "line\n" * 2000  # ~10k chars, over the 4096 limit
    await adapter.send(OutboundMessage(channel="telegram:4242", text=long_text))

    assert len(stub.posts) >= 2
    for url, payload in stub.posts:
        assert url.endswith("/sendMessage")
        assert payload["chat_id"] == 4242
        assert len(payload["text"]) <= TELEGRAM_MESSAGE_LIMIT
    # The token lives in the URL and must never leak into the payload.
    assert all("test-token" not in str(p[1]) for p in stub.posts)


async def test_send_accepts_negative_group_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, stub = make_adapter(monkeypatch)
    await adapter.send(OutboundMessage(channel="telegram:-100123", text="hi"))
    assert stub.posts[0][1]["chat_id"] == -100123


async def test_send_rejects_garbage_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, _ = make_adapter(monkeypatch)
    with pytest.raises(TransportError):
        await adapter.send(OutboundMessage(channel="telegram:not-a-chat", text="hi"))


async def test_send_raises_on_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, stub = make_adapter(monkeypatch)
    stub.post_status = 500
    with pytest.raises(TransportError):
        await adapter.send(OutboundMessage(channel="telegram:4242", text="hi"))


# --- polling: getUpdates and the start() loop ------------------------------


class _Response:
    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)  # type: ignore[arg-type]

    def json(self) -> dict[str, Any]:
        return self._payload


class PollStub:
    """get() yields scripted batches then cancels the loop; post() is a no-op."""

    def __init__(self, batches: list[dict[str, Any]]) -> None:
        self.batches = list(batches)
        self.get_calls: list[dict[str, Any]] = []

    async def get(self, url: str, params: dict[str, Any]) -> _Response:
        self.get_calls.append(dict(params))
        if not self.batches:
            raise asyncio.CancelledError  # break the otherwise-infinite loop
        return _Response(self.batches.pop(0))

    async def aclose(self) -> None:
        pass


def make_poll_adapter(
    monkeypatch: pytest.MonkeyPatch, batches: list[dict[str, Any]]
) -> tuple[TelegramTransportAdapter, PollStub, list[InboundMessage]]:
    monkeypatch.setenv("ALFRED_TELEGRAM_TOKEN", "test-token")
    stub = PollStub(batches)
    received: list[InboundMessage] = []

    async def handler(msg: InboundMessage) -> None:
        received.append(msg)

    adapter = TelegramTransportAdapter(
        TelegramConfig(enabled=True, owner_id=OWNER),
        handler,
        client=stub,  # type: ignore[arg-type]
    )
    return adapter, stub, received


async def test_get_updates_raises_when_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, _, _ = make_poll_adapter(
        monkeypatch, [{"ok": False, "description": "unauthorized"}]
    )
    with pytest.raises(TransportError):
        await adapter._get_updates()


async def test_poll_dispatches_owner_update_and_advances_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, stub, received = make_poll_adapter(
        monkeypatch, [{"ok": True, "result": [update(update_id=42)]}]
    )

    with pytest.raises(asyncio.CancelledError):
        await adapter.start()  # cancels itself after draining the one batch

    assert len(received) == 1 and received[0].channel == "telegram:4242"
    assert adapter._offset == 43  # update_id + 1
    # The next poll carried the advanced offset so an update is never re-fired.
    assert stub.get_calls[1]["offset"] == 43


async def test_poll_swallows_a_failing_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALFRED_TELEGRAM_TOKEN", "test-token")
    stub = PollStub([{"ok": True, "result": [update(update_id=7)]}])

    async def handler(_msg: InboundMessage) -> None:
        raise RuntimeError("handler blew up")

    adapter = TelegramTransportAdapter(
        TelegramConfig(enabled=True, owner_id=OWNER), handler, client=stub  # type: ignore[arg-type]
    )

    # A crashing handler must not kill the poll loop; the offset still advances.
    with pytest.raises(asyncio.CancelledError):
        await adapter.start()
    assert adapter._offset == 8
