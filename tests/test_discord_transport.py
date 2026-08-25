"""Discord adapter unit tests: pure chunking and filter rules only.

No gateway connection anywhere; the adapter is constructed but never
started, and _handle_raw is exercised directly with plain values.
"""

from __future__ import annotations

from datetime import UTC, datetime

from alfred.adapters.discord_transport import DiscordTransportAdapter, chunk_text
from alfred.config import DiscordConfig
from alfred.domain.schemas import InboundMessage

NOW = datetime(2026, 6, 11, 12, 0, tzinfo=UTC)
OWNER = 1111
CHANNEL = 2222


class RecordingHandler:
    def __init__(self) -> None:
        self.messages: list[InboundMessage] = []

    async def __call__(self, message: InboundMessage) -> None:
        self.messages.append(message)


def make_adapter(
    handler: RecordingHandler,
    *,
    owner_id: int = OWNER,
    channel_id: int | None = None,
) -> DiscordTransportAdapter:
    config = DiscordConfig(owner_id=owner_id, channel_id=channel_id)
    return DiscordTransportAdapter(config, handler)


# ---------------------------------------------------------------------------
# chunk_text
# ---------------------------------------------------------------------------


def test_chunk_text_short_text_is_single_chunk() -> None:
    assert chunk_text("hello") == ["hello"]


def test_chunk_text_prefers_newline_boundaries() -> None:
    text = "a" * 1500 + "\n" + "b" * 1000
    chunks = chunk_text(text)
    assert chunks == ["a" * 1500, "b" * 1000]
    assert all(len(chunk) <= 2000 for chunk in chunks)


def test_chunk_text_hard_splits_a_single_long_line() -> None:
    chunks = chunk_text("x" * 5000)
    assert chunks == ["x" * 2000, "x" * 2000, "x" * 1000]


def test_chunk_text_newline_splits_reassemble() -> None:
    text = "line one\nline two\nline three"
    chunks = chunk_text(text, limit=10)
    assert all(len(chunk) <= 10 for chunk in chunks)
    assert "\n".join(chunks) == text


def test_chunk_text_empty_text_yields_no_chunks() -> None:
    assert chunk_text("") == []


# ---------------------------------------------------------------------------
# _handle_raw filter rules
# ---------------------------------------------------------------------------


async def test_owner_message_reaches_handler_with_correct_fields() -> None:
    handler = RecordingHandler()
    adapter = make_adapter(handler)

    await adapter._handle_raw(
        author_id=OWNER,
        channel_id=CHANNEL,
        text="plan my week",
        created_at=NOW,
        is_self=False,
    )

    assert len(handler.messages) == 1
    message = handler.messages[0]
    # Channels are namespaced so multiple transports can share one core.
    assert message.channel == f"discord:{CHANNEL}"
    assert message.author == str(OWNER)
    assert message.text == "plan my week"
    assert message.at == NOW
    assert message.provenance == "owner"


async def test_own_messages_are_ignored() -> None:
    handler = RecordingHandler()
    adapter = make_adapter(handler)

    await adapter._handle_raw(
        author_id=OWNER, channel_id=CHANNEL, text="echo", created_at=NOW, is_self=True
    )

    assert handler.messages == []


async def test_non_owner_is_ignored_silently() -> None:
    handler = RecordingHandler()
    adapter = make_adapter(handler)

    await adapter._handle_raw(
        author_id=9999, channel_id=CHANNEL, text="hi alfred", created_at=NOW, is_self=False
    )

    assert handler.messages == []


async def test_channel_restriction_filters_other_channels() -> None:
    handler = RecordingHandler()
    adapter = make_adapter(handler, channel_id=CHANNEL)

    await adapter._handle_raw(
        author_id=OWNER, channel_id=3333, text="wrong room", created_at=NOW, is_self=False
    )
    assert handler.messages == []

    await adapter._handle_raw(
        author_id=OWNER, channel_id=CHANNEL, text="right room", created_at=NOW, is_self=False
    )
    assert len(handler.messages) == 1


async def test_unrestricted_adapter_accepts_any_channel() -> None:
    handler = RecordingHandler()
    adapter = make_adapter(handler, channel_id=None)

    await adapter._handle_raw(
        author_id=OWNER, channel_id=4444, text="anywhere", created_at=NOW, is_self=False
    )

    assert len(handler.messages) == 1


async def test_handler_exception_is_swallowed() -> None:
    async def exploding(message: InboundMessage) -> None:
        raise RuntimeError("core blew up")

    adapter = DiscordTransportAdapter(DiscordConfig(owner_id=OWNER), exploding)

    # Must not raise; the gateway stays up whatever the core does.
    await adapter._handle_raw(
        author_id=OWNER, channel_id=CHANNEL, text="hi", created_at=NOW, is_self=False
    )
