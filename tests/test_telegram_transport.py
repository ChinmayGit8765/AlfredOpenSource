"""Telegram transport tests: filter rules and sends, no network anywhere."""

from __future__ import annotations

from typing import Any

import pytest

from alfred.adapters.telegram_transport import (
    TELEGRAM_MESSAGE_LIMIT,
    TelegramTransportAdapter,
    to_inbound,
)
from alfred.config import TelegramConfig
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

    async def post(self, url: str, json: dict[str, Any]) -> Any:
        self.posts.append((url, json))

        class _Response:
            status_code = 200

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
