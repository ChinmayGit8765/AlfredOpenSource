"""Telegram transport adapter: ALFRED in your pocket.

Long-polls the Telegram Bot API over plain HTTPS (httpx, already in the
dependency tree via ollama) so there is no webhook, no public endpoint,
and no extra dependency: the connection originates from the owner's
machine, which is exactly the local-first posture. Only the configured
owner id is ever obeyed; everyone else is ignored silently.

Channels are namespaced "telegram:<chat_id>" so multiple transports can
share one core.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import httpx

from alfred.adapters.textutil import chunk_text
from alfred.config import TelegramConfig
from alfred.domain.schemas import InboundMessage
from alfred.errors import TransportError
from alfred.ports.transport import OutboundMessage

logger = logging.getLogger(__name__)

TELEGRAM_MESSAGE_LIMIT = 4096
_POLL_TIMEOUT_S = 50
_ERROR_BACKOFF_S = 5.0

CHANNEL_PREFIX = "telegram:"


def to_inbound(update: dict[str, Any], owner_id: int) -> InboundMessage | None:
    """Map one Telegram update to an InboundMessage; None when not for us.

    Pure so the filter rules are testable without a network: non-message
    updates, empty text, and any sender other than the owner all map to
    None (ignored, never answered).
    """
    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, dict):
        return None
    sender = message.get("from") or {}
    if sender.get("id") != owner_id or owner_id == 0:
        return None
    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id is None:
        return None
    at = None
    if isinstance(message.get("date"), int):
        at = datetime.fromtimestamp(message["date"], tz=timezone.utc)
    return InboundMessage(
        channel=f"{CHANNEL_PREFIX}{chat_id}",
        author=str(sender.get("id")),
        text=text,
        at=at,
        provenance="owner",
    )


class TelegramTransportAdapter:
    """TransportPort over a Telegram bot, long-polling getUpdates."""

    def __init__(
        self,
        config: TelegramConfig,
        handler: Callable[[InboundMessage], Awaitable[None]],
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._handler = handler
        # token() reads the environment; the value is embedded in the URL
        # and must never be logged.
        self._base = f"https://api.telegram.org/bot{config.token()}"
        self._client = client or httpx.AsyncClient(timeout=_POLL_TIMEOUT_S + 10)
        self._handler_lock = asyncio.Lock()
        self._offset: int | None = None

    async def start(self) -> None:
        """Poll until cancelled. Network hiccups back off and retry."""
        logger.info("telegram transport polling (owner_id=%d)", self._config.owner_id)
        while True:
            try:
                updates = await self._get_updates()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("telegram poll failed (%s); backing off", exc)
                await asyncio.sleep(_ERROR_BACKOFF_S)
                continue
            for update in updates:
                if isinstance(update.get("update_id"), int):
                    self._offset = update["update_id"] + 1
                inbound = to_inbound(update, self._config.owner_id)
                if inbound is None:
                    continue
                try:
                    async with self._handler_lock:
                        await self._handler(inbound)
                except Exception:
                    logger.exception("inbound handler failed for %s", inbound.id)

    async def _get_updates(self) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeout": _POLL_TIMEOUT_S,
            "allowed_updates": '["message"]',
        }
        if self._offset is not None:
            params["offset"] = self._offset
        response = await self._client.get(f"{self._base}/getUpdates", params=params)
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise TransportError(f"telegram getUpdates not ok: {payload.get('description')}")
        result = payload.get("result")
        return result if isinstance(result, list) else []

    async def send(self, message: OutboundMessage) -> None:
        chat_id = message.channel.removeprefix(CHANNEL_PREFIX)
        if not chat_id.lstrip("-").isdigit():
            raise TransportError(f"invalid Telegram chat id: {message.channel!r}")
        for piece in chunk_text(message.text, TELEGRAM_MESSAGE_LIMIT):
            response = await self._client.post(
                f"{self._base}/sendMessage",
                json={"chat_id": int(chat_id), "text": piece},
            )
            if response.status_code != 200:
                raise TransportError(
                    f"telegram sendMessage failed with HTTP {response.status_code}"
                )

    async def close(self) -> None:
        await self._client.aclose()
