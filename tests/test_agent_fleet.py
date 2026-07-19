"""Fleet cohesion: the shipped agent folders stay consistent with the
real tool surface and the routing contract, and the meta agents (qa,
scout) stay out of the owner's week."""

from __future__ import annotations

from pathlib import Path

from alfred.adapters.local_tools import LocalToolAdapter
from alfred.domain.memory import MemoryService
from alfred.domain.routing import route
from alfred.domain.schemas import InboundMessage
from alfred.runtime.agent_loader import load_agents
from alfred.testing import FakeClock, MemoryStore

REPO_AGENTS_DIR = Path(__file__).parent.parent / "agents"

META_AGENTS = {"qa", "scout"}


def repo_registry():
    return load_agents(REPO_AGENTS_DIR)


async def test_every_allowlisted_local_tool_exists() -> None:
    # A typo in a manifest allowlist would pass loading and only surface
    # as ToolNotFoundError mid-run; catch it here instead.
    store = MemoryStore()
    clock = FakeClock()
    adapter = LocalToolAdapter(store, clock, memory=MemoryService(store, clock))
    local = {spec.name for spec in await adapter.list_tools()}

    for agent in repo_registry().all():
        unknown = [
            tool
            for tool in agent.manifest.allowed_tools
            # MCP tools are namespaced "<server>.<tool>" and only resolvable
            # with a live server; local names must resolve right now.
            if "." not in tool and tool not in local
        ]
        assert not unknown, (
            f"agent '{agent.manifest.name}' allowlists unknown local tools: {unknown}"
        )


def test_meta_agents_claim_no_owner_capacity() -> None:
    # qa and scout work on ALFRED's output, not the owner's week: they must
    # never eat into weekly_capacity or the builder's capacity check.
    for agent in repo_registry().all():
        if agent.manifest.name in META_AGENTS:
            assert agent.manifest.capacity_cost == 0
            assert not agent.manifest.triggers.always
            # The executor drops meta-agent plans on this flag; without it
            # a scheduled planning prompt could pull a reviewer into the
            # very week it reviews.
            assert not agent.manifest.emits_plans


def test_double_check_routes_to_qa() -> None:
    registry = repo_registry()
    message = InboundMessage(channel="cli", text="double check this week's plans")
    assert "qa" in {a.manifest.name for a in route(message, registry)}


def test_suggestions_route_to_scout() -> None:
    registry = repo_registry()
    message = InboundMessage(
        channel="cli", text="any suggestions for expanding what you can do?"
    )
    assert "scout" in {a.manifest.name for a in route(message, registry)}


def test_domain_routing_is_not_hijacked_by_meta_agents() -> None:
    # A plain domain message keeps routing to its domain agent alone; the
    # meta agents only join when explicitly summoned.
    registry = repo_registry()
    message = InboundMessage(channel="cli", text="plan my gym week around soreness")
    names = {a.manifest.name for a in route(message, registry)}
    assert "training" in names
    assert not names & META_AGENTS


def test_fleet_schedules_do_not_collide() -> None:
    # Two agents firing at the same weekly slot would race the same
    # planning morning; keep every (day, time) pair unique across the fleet.
    slots: dict[tuple[str, str | None], str] = {}
    for agent in repo_registry().all():
        schedule = agent.manifest.schedule
        if schedule.kind != "weekly":
            continue
        for day in schedule.days:
            slot = (day, schedule.time)
            assert slot not in slots, (
                f"'{agent.manifest.name}' and '{slots[slot]}' share slot {slot}"
            )
            slots[slot] = agent.manifest.name
