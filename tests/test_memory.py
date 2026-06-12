"""Memory layer tests: filing, recall scoring, forgetting, prompt context."""

from __future__ import annotations

from alfred.domain.memory import MemoryService, tokenize
from alfred.testing import FakeClock, MemoryStore


def make_service() -> tuple[MemoryService, FakeClock]:
    clock = FakeClock()
    return MemoryService(MemoryStore(), clock), clock


def test_tokenize_drops_stopwords_and_short_tokens() -> None:
    tokens = tokenize("What did the physio say about my Shoulder?")
    assert "physio" in tokens
    assert "shoulder" in tokens
    assert "say" in tokens
    assert "what" not in tokens
    assert "the" not in tokens
    assert "my" not in tokens


async def test_remember_and_recall_by_topic() -> None:
    service, _ = make_service()
    await service.remember("Physio said no overhead pressing until March")
    await service.remember("Rent is due on the 3rd of every month")
    await service.remember("Sam prefers morning climbing sessions")

    hits = await service.recall("can I do overhead press in training?")
    assert hits and "overhead pressing" in hits[0].text
    assert all("Rent" not in m.text for m in hits)


async def test_recall_ranks_higher_overlap_first_and_breaks_ties_by_recency() -> None:
    service, clock = make_service()
    await service.remember("exam timetable released for semester one")
    clock.advance(days=1)
    await service.remember("exam for FIT3170 is on June 20, morning slot")

    hits = await service.recall("when is the FIT3170 exam")
    assert hits[0].text.startswith("exam for FIT3170")

    # Equal overlap: the newer memory wins.
    service2, clock2 = make_service()
    await service2.remember("dentist appointment pending")
    clock2.advance(days=2)
    await service2.remember("dentist appointment confirmed")
    hits2 = await service2.recall("dentist appointment")
    assert hits2[0].text == "dentist appointment confirmed"


async def test_recall_returns_nothing_for_zero_overlap_or_stopword_queries() -> None:
    service, _ = make_service()
    await service.remember("Physio said no overhead pressing until March")
    assert await service.recall("completely unrelated topic") == []
    assert await service.recall("the and what") == []


async def test_tags_count_toward_recall() -> None:
    service, _ = make_service()
    await service.remember("ask for the student discount", tags=["gym"])
    hits = await service.recall("gym membership")
    assert hits and "student discount" in hits[0].text


async def test_forget_deletes_by_id_and_unknown_is_false() -> None:
    service, _ = make_service()
    memory = await service.remember("temporary fact")
    assert await service.forget(memory.id) is True
    assert await service.recall("temporary fact") == []
    assert await service.forget("nope") is False


async def test_recent_is_newest_first() -> None:
    service, clock = make_service()
    await service.remember("first")
    clock.advance(minutes=1)
    await service.remember("second")
    recent = await service.recent(limit=2)
    assert [m.text for m in recent] == ["second", "first"]


async def test_context_for_renders_block_or_empty() -> None:
    service, _ = make_service()
    assert await service.context_for("anything at all") == ""
    await service.remember("Physio said no overhead pressing until March")
    block = await service.context_for("planning overhead pressing this week")
    assert block.startswith("Relevant things the owner has told you before:")
    assert "overhead pressing" in block
    assert "2026" in block  # the date is shown so the agent can judge staleness
