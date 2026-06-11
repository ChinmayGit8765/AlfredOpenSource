"""Tests for alfred.domain.routing: keyword and always-on message routing."""

from __future__ import annotations

from alfred.domain.registry import AgentRegistry, LoadedAgent
from alfred.domain.routing import route
from alfred.domain.schemas import AgentManifest, InboundMessage, Lifecycle, Triggers


def make_agent(
    name: str,
    *,
    lifecycle: Lifecycle = Lifecycle.ESTABLISHED,
    keywords: list[str] | None = None,
    always: bool = False,
) -> LoadedAgent:
    manifest = AgentManifest(
        name=name,
        description=f"{name} test agent",
        lifecycle=lifecycle,
        triggers=Triggers(keywords=keywords or [], always=always),
    )
    return LoadedAgent(manifest=manifest, prompt=f"You are {name}.")


def msg(text: str) -> InboundMessage:
    return InboundMessage(channel="test", text=text)


def names(agents: list[LoadedAgent]) -> list[str]:
    return [a.manifest.name for a in agents]


def test_keyword_match_routes_agent():
    registry = AgentRegistry([make_agent("training", keywords=["gym"])])

    assert names(route(msg("heading to the gym now"), registry)) == ["training"]


def test_any_one_of_multiple_keywords_matches():
    registry = AgentRegistry([make_agent("study", keywords=["exam", "lecture"])])

    assert names(route(msg("lecture notes are due"), registry)) == ["study"]


def test_keyword_match_is_case_insensitive():
    registry = AgentRegistry([make_agent("training", keywords=["Gym"])])

    assert names(route(msg("GYM session done"), registry)) == ["training"]
    assert names(route(msg("gym session done"), registry)) == ["training"]


def test_keyword_respects_word_boundaries():
    registry = AgentRegistry([make_agent("training", keywords=["run"])])

    # "run" inside "grundy" must not trigger.
    assert route(msg("reading about grundy numbers"), registry) == []
    # "run" as its own word must trigger.
    assert names(route(msg("finished my morning run"), registry)) == ["training"]


def test_always_agent_included_regardless_of_keywords():
    registry = AgentRegistry(
        [make_agent("overseer", keywords=["nevermatch"], always=True)]
    )

    assert names(route(msg("totally unrelated text"), registry)) == ["overseer"]


def test_paused_agent_never_routes_even_with_matching_keyword():
    registry = AgentRegistry(
        [make_agent("training", lifecycle=Lifecycle.PAUSED, keywords=["gym"])]
    )

    assert route(msg("gym time"), registry) == []


def test_retired_agent_never_routes_even_when_always():
    registry = AgentRegistry(
        [make_agent("oldtimer", lifecycle=Lifecycle.RETIRED, always=True)]
    )

    assert route(msg("anything at all"), registry) == []


def test_order_always_alphabetical_then_keyword_alphabetical():
    registry = AgentRegistry(
        [
            make_agent("zeta", always=True),
            make_agent("alpha", always=True),
            make_agent("mike", keywords=["plan"]),
            make_agent("bravo", keywords=["plan"]),
        ]
    )

    assert names(route(msg("what is the plan"), registry)) == [
        "alpha",
        "zeta",
        "bravo",
        "mike",
    ]


def test_no_duplicates_when_always_and_keyword_both_hit():
    registry = AgentRegistry(
        [
            make_agent("watcher", keywords=["plan"], always=True),
            make_agent("aaa-planner", keywords=["plan"]),
        ]
    )

    routed = route(msg("plan the week"), registry)

    # The always-on agent appears once, in the always block, ahead of
    # alphabetically-earlier keyword matches.
    assert names(routed) == ["watcher", "aaa-planner"]


def test_unmatched_text_returns_empty_list():
    registry = AgentRegistry(
        [
            make_agent("training", keywords=["gym"]),
            make_agent("study", keywords=["exam"]),
        ]
    )

    assert route(msg("nothing relevant here"), registry) == []
