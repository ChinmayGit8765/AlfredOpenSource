"""Core domain models shared across ALFRED.

This module is the system's vocabulary. It is deliberately dependency-light
(pydantic plus ports) so every other module can import it freely. Models
used as LLM output schemas keep generous defaults: the structured-call
engine validates and the defaults absorb what the model omits.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from alfred.ports.model import ModelOptions
from alfred.ports.tools import CapabilityTier


def new_id() -> str:
    """Short unique id for domain objects. Not time-ordered; store keys are."""
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Store collections
# ---------------------------------------------------------------------------


class Collections:
    """Canonical collection names. Always reference these, never bare strings."""

    PLANS = "plans"
    SCHEDULES = "schedules"  # Conductor-reconciled weekly schedules
    MEMORIES = "memories"  # explicit recallable facts about the owner's life
    OUTCOMES = "outcomes"
    OBSERVATIONS = "observations"
    PROFILE = "profile"  # keyed; current profile lives at key "current"
    AUDIT = "audit"
    PENDING_ACTIONS = "pending_actions"
    PROPOSALS = "proposals"
    REFLECTIONS = "reflections"
    BUILDER_SESSIONS = "builder_sessions"
    HEARTBEAT = "heartbeat"  # keyed; last-run timestamps per job
    MESSAGES = "messages"  # inbound message log


# ---------------------------------------------------------------------------
# Messaging
# ---------------------------------------------------------------------------

Provenance = Literal["owner", "scheduler", "external"]
"""Where an instruction originated. External content is untrusted and can
never trigger auto-executed destructive actions."""


class InboundMessage(BaseModel):
    """A message arriving at the core, from any transport."""

    id: str = Field(default_factory=new_id)
    channel: str
    author: str = "owner"
    text: str
    at: datetime | None = None
    provenance: Provenance = "owner"


class ScheduledTrigger(BaseModel):
    """A heartbeat-initiated agent run (no inbound message behind it)."""

    agent: str
    reason: str
    at: datetime | None = None


# ---------------------------------------------------------------------------
# Agent manifests
# ---------------------------------------------------------------------------


class Lifecycle(StrEnum):
    """Where an agent sits in its life. Support scales inversely with autonomy."""

    PROPOSED = "proposed"
    FORMING = "forming"
    ESTABLISHED = "established"
    MAINTENANCE = "maintenance"
    LAPSING = "lapsing"
    RESHAPED = "reshaped"
    PAUSED = "paused"
    RETIRED = "retired"


class TargetShape(StrEnum):
    """What kind of thing an agent optimises. Each shape scaffolds differently."""

    HABIT = "habit"
    SKILL = "skill"
    PROJECT = "project"
    STATE = "state"
    METRIC = "metric"


class Triggers(BaseModel):
    """When an agent should handle an inbound message."""

    keywords: list[str] = Field(default_factory=list)
    always: bool = False


class Schedule(BaseModel):
    """When the heartbeat should run an agent proactively."""

    kind: Literal["none", "daily", "weekly", "interval"] = "none"
    time: str | None = None  # "HH:MM" local, for daily/weekly
    days: list[str] = Field(default_factory=list)  # ["mon", ...] for weekly
    every_minutes: int | None = None  # for interval


class AgentManifest(BaseModel):
    """The contract an agent folder declares in manifest.yaml.

    extra="forbid" so a typo in a hand-edited manifest fails loudly at
    load time instead of silently dropping a field like allowed_tools.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,40}$")
    description: str
    version: int = 1
    domain: str | None = None
    shape: TargetShape | None = None
    lifecycle: Lifecycle = Lifecycle.ESTABLISHED
    triggers: Triggers = Field(default_factory=Triggers)
    schedule: Schedule = Field(default_factory=Schedule)
    allowed_tools: list[str] = Field(default_factory=list)
    capacity_cost: int = Field(default=0, ge=0, le=20)
    model: ModelOptions | None = None


# ---------------------------------------------------------------------------
# Plans and outcomes
# ---------------------------------------------------------------------------


class PlanItem(BaseModel):
    """One concrete commitment inside a plan."""

    id: str = Field(default_factory=new_id)
    title: str
    day: str | None = None  # "mon".."sun" or an ISO date
    time: str | None = None  # "HH:MM" local
    duration_min: int | None = Field(default=None, ge=0)
    load: int = Field(default=1, ge=0, le=5)  # capacity points this item costs
    details: str = ""
    anchor: str | None = None  # existing cue this behaviour stacks onto


class Plan(BaseModel):
    """A validated plan produced by one agent for one horizon."""

    id: str = Field(default_factory=new_id)
    agent: str = ""
    week_of: date | None = None
    items: list[PlanItem] = Field(default_factory=list)
    rationale: str = ""
    version: int = 1
    created_at: datetime | None = None

    @property
    def total_load(self) -> int:
        return sum(item.load for item in self.items)


class OutcomeStatus(StrEnum):
    DONE = "done"
    PARTIAL = "partial"
    MISSED = "missed"
    SKIPPED = "skipped"  # deliberately not done; distinct from missed


class Outcome(BaseModel):
    """What actually happened against a plan item. The fuel of adaptation."""

    id: str = Field(default_factory=new_id)
    agent: str
    plan_id: str | None = None
    item_id: str | None = None
    status: OutcomeStatus
    report: str = ""
    at: datetime | None = None


# ---------------------------------------------------------------------------
# Agent execution
# ---------------------------------------------------------------------------


class ToolCall(BaseModel):
    """An agent's request to invoke one tool."""

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class AgentReply(BaseModel):
    """The structured output every agent LLM call must produce."""

    reply: str
    plan: Plan | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)
    done: bool = True  # False asks the executor for another round after tools run


class PendingAction(BaseModel):
    """A gated tool call awaiting explicit owner confirmation."""

    id: str = Field(default_factory=new_id)
    agent: str
    call: ToolCall
    tier: CapabilityTier
    provenance: Provenance
    reason: str = ""
    status: Literal["pending", "confirmed", "rejected", "expired"] = "pending"
    created_at: datetime | None = None


class ExecutionResult(BaseModel):
    """Everything one agent run produced."""

    agent: str
    replies: list[str] = Field(default_factory=list)
    plan: Plan | None = None
    pending: list[PendingAction] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Governance: proposals (human-in-the-loop self-improvement)
# ---------------------------------------------------------------------------


class ProposalKind(StrEnum):
    PROMPT_CHANGE = "prompt_change"
    MANIFEST_CHANGE = "manifest_change"
    LIFECYCLE_CHANGE = "lifecycle_change"
    NEW_AGENT = "new_agent"
    RETIRE_AGENT = "retire_agent"


class Proposal(BaseModel):
    """A proposed change to ALFRED itself. Never applied without approval."""

    id: str = Field(default_factory=new_id)
    kind: ProposalKind
    agent: str
    summary: str
    old: str | None = None
    new: str | None = None
    reason: str = ""
    touches_safety: bool = False  # allowlists/permissions; extra confirmation
    status: Literal["pending", "approved", "rejected"] = "pending"
    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# User model
# ---------------------------------------------------------------------------


class Memory(BaseModel):
    """One explicit, recallable fact in the owner's life.

    Distinct from Observation: observations are the adaptation stream
    (adherence, trends); memories are reference material the owner or an
    agent deliberately filed and expects ALFRED to bring back up at the
    right moment ("physio said no overhead pressing until March").
    """

    id: str = Field(default_factory=new_id)
    text: str
    source: str = "owner"  # "owner", an agent name, or "reflection"
    kind: Literal["fact", "preference", "person", "deadline", "context"] = "fact"
    tags: list[str] = Field(default_factory=list)
    at: datetime | None = None


class Observation(BaseModel):
    """One appended fact about the owner. The profile is a trend, not a snapshot."""

    id: str = Field(default_factory=new_id)
    source: str  # agent name, "owner", or "reflection"
    kind: Literal["preference", "constraint", "adherence", "insight", "event"]
    text: str
    at: datetime | None = None


class AdherenceStats(BaseModel):
    """Follow-through per agent. Signals for diagnosis, never for shame."""

    done: int = 0
    partial: int = 0
    missed: int = 0
    skipped: int = 0
    consecutive_misses: int = 0
    consecutive_dones: int = 0  # recovery signal; a lapse needs 3 to clear

    @property
    def total(self) -> int:
        return self.done + self.partial + self.missed + self.skipped

    @property
    def engaged(self) -> int:
        """Outcomes that signal automaticity; deliberate skips excluded.

        This, not total, is the right maturity count: a habit the owner
        mostly skipped has not formed, however high its rate looks.
        """
        return self.done + self.partial + self.missed

    @property
    def rate(self) -> float:
        """Completion rate in [0, 1]; partial counts half.

        Skips are excluded from the denominator: skipping is a deliberate
        choice, not a lapse, so it never dilutes the rate.
        """
        if self.engaged == 0:
            return 0.0
        return (self.done + 0.5 * self.partial) / self.engaged


class UserProfile(BaseModel):
    """The structured, versioned model of the owner."""

    version: int = 1
    goals: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    preferences: list[str] = Field(default_factory=list)
    weekly_capacity: int = Field(default=20, ge=0)  # capacity points per week
    adherence: dict[str, AdherenceStats] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# Conductor
# ---------------------------------------------------------------------------


class Conflict(BaseModel):
    """A detected collision between concurrent plans."""

    kind: Literal["overload_day", "overload_week", "time_collision", "recovery"]
    day: str | None = None
    detail: str = ""
    item_ids: list[str] = Field(default_factory=list)


class Adjustment(BaseModel):
    """One change the Conductor made while reconciling."""

    agent: str
    item_id: str | None = None
    action: Literal["move", "shrink", "drop", "keep"]
    detail: str = ""


class ReconciledSchedule(BaseModel):
    """The Conductor's output: concurrent plans that no longer collide."""

    week_of: date | None = None
    plans: list[Plan] = Field(default_factory=list)
    adjustments: list[Adjustment] = Field(default_factory=list)
    total_load: int = 0
    warnings: list[str] = Field(default_factory=list)
    summary: str = ""


# ---------------------------------------------------------------------------
# Adaptive Agent Builder
# ---------------------------------------------------------------------------


class BuilderStage(StrEnum):
    ELICITING = "eliciting"
    CLASSIFYING = "classifying"
    DESIGNING = "designing"
    CAPACITY_CHECK = "capacity_check"
    PROPOSING = "proposing"
    AWAITING_APPROVAL = "awaiting_approval"
    DONE = "done"
    ABANDONED = "abandoned"


class AgentBlueprint(BaseModel):
    """Everything needed to materialise an agent folder."""

    manifest: AgentManifest
    prompt_md: str


class BuilderSession(BaseModel):
    """State of one agent-building conversation, persisted between turns."""

    id: str = Field(default_factory=new_id)
    stage: BuilderStage = BuilderStage.ELICITING
    transcript: list[dict[str, str]] = Field(default_factory=list)
    stated_goal: str = ""
    real_lever: str | None = None
    shape: TargetShape | None = None
    blueprint: AgentBlueprint | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class LapseDiagnosis(BaseModel):
    """The builder's verdict on a lapsing habit. A lapse is data, not failure."""

    cause: Literal["too_big", "bad_cue", "life_event", "wrong_goal", "unknown"]
    action: Literal["shrink", "reanchor", "pause", "reshape", "retire", "hold"]
    detail: str = ""
    new_size: str | None = None
    new_anchor: str | None = None


# ---------------------------------------------------------------------------
# Reflection
# ---------------------------------------------------------------------------


class Reflection(BaseModel):
    """Output of a periodic Conductor review. Written to state so it compounds."""

    id: str = Field(default_factory=new_id)
    window_days: int = 7
    insights: list[str] = Field(default_factory=list)
    profile_updates: list[str] = Field(default_factory=list)
    proposals: list[Proposal] = Field(default_factory=list)
    created_at: datetime | None = None
