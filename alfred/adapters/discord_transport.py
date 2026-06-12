"""Discord transport adapter: the owner's messaging channel.

Implements TransportPort over a discord.py Client. Inbound flow is
inverted: gateway events are filtered here and forwarded to the core's
handler as InboundMessage. Only the configured owner is ever obeyed;
every other author is ignored silently, never refused, so the bot leaks
no information about itself to strangers.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

import discord

from alfred.config import DiscordConfig
from alfred.domain.schemas import InboundMessage
from alfred.errors import TransportError
from alfred.ports.transport import OutboundMessage

logger = logging.getLogger(__name__)

DISCORD_MESSAGE_LIMIT = 2000


def chunk_text(text: str, limit: int = 2000) -> list[str]:
    """Split text into pieces of at most limit chars, preferring newlines.

    Pure helper so chunking stays testable without a gateway. A cut at a
    newline drops that newline (it becomes the message boundary); a single
    overlong line is hard-split at the limit. Empty text yields no chunks
    because Discord rejects empty messages.
    """
    if limit < 1:
        raise ValueError("limit must be positive")
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n")
        if cut <= 0:
            chunks.append(remaining[:limit])
            remaining = remaining[limit:]
        else:
            chunks.append(remaining[:cut])
            remaining = remaining[cut + 1 :]
    if remaining:
        chunks.append(remaining)
    return chunks


class DiscordTransportAdapter:
    """TransportPort over a Discord bot account."""

    def __init__(
        self,
        config: DiscordConfig,
        handler: Callable[[InboundMessage], Awaitable[None]],
    ) -> None:
        self._config = config
        self._handler = handler
        # discord.py runs each on_message in its own task; the core's
        # handler mutates shared state (profile, sessions) and is not safe
        # to interleave, so inbound messages are serialized here.
        self._handler_lock = asyncio.Lock()
        intents = discord.Intents.default()
        intents.message_content = True
        self._client = discord.Client(intents=intents)

        @self._client.event
        async def on_message(message: discord.Message) -> None:
            await self._handle_raw(
                author_id=message.author.id,
                channel_id=message.channel.id,
                text=message.content,
                created_at=message.created_at,
                is_self=self._client.user is not None
                and message.author.id == self._client.user.id,
            )

    async def _handle_raw(
        self,
        author_id: int,
        channel_id: int,
        text: str,
        created_at: datetime | None,
        is_self: bool,
    ) -> None:
        """Filter rules for one raw gateway message, split out for tests."""
        if is_self:
            return
        if author_id != self._config.owner_id:
            return
        if self._config.channel_id is not None and channel_id != self._config.channel_id:
            return
        message = InboundMessage(
            channel=str(channel_id),
            author=str(author_id),
            text=text,
            at=created_at,
            provenance="owner",
        )
        try:
            async with self._handler_lock:
                await self._handler(message)
        except Exception:
            # The gateway must survive any core failure.
            logger.exception("inbound handler failed for message %s", message.id)

    async def send(self, message: OutboundMessage) -> None:
        """Deliver one outbound message, chunked to Discord's size limit."""
        channel = await self._resolve_channel(message.channel)
        try:
            for piece in chunk_text(message.text, DISCORD_MESSAGE_LIMIT):
                await channel.send(piece)
        except discord.DiscordException as exc:
            raise TransportError(
                f"failed to send to Discord channel {message.channel}: {exc}"
            ) from exc

    async def _resolve_channel(self, raw: str) -> discord.abc.Messageable:
        try:
            channel_id = int(raw)
        except ValueError as exc:
            raise TransportError(f"invalid Discord channel id: {raw!r}") from exc
        channel = self._client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(channel_id)
            except discord.DiscordException as exc:
                raise TransportError(
                    f"cannot resolve Discord channel {channel_id}"
                ) from exc
        if not isinstance(channel, discord.abc.Messageable):
            raise TransportError(f"Discord channel {channel_id} does not accept messages")
        return channel

    async def start(self) -> None:
        """Connect to the gateway and block until closed."""
        # token() reads the environment; the value itself is never logged.
        await self._client.start(self._config.token())

    async def close(self) -> None:
        await self._client.close()
