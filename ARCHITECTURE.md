# ALFRED Architecture

ALFRED is a layered, ports-and-adapters system. The brain (domain) is pure
logic; everything external is an adapter behind a port; wiring happens at one
composition root. This document is the binding contract for the codebase:
module responsibilities, frozen public surfaces, and the rules that keep the
layers clean.

```
            +--------------------------------------------------+
            |                    runtime/                      |
            |  cli.py   composition.py   core.py   heartbeat.py|
            |  agent_loader.py (filesystem <-> registry)       |
            +-----------------------+--------------------------+
                                    | wires adapters into ports
        +---------------------------+---------------------------+
        |                        domain/                        |
        |  structured  registry  routing  executor  dispatch    |
        |  governance  conductor  builder  lifecycle            |
        |  user_model  feedback  reflection                     |
        |          (pure logic; depends only on ports/)         |
        +---------------------------+---------------------------+
                                    | Protocols
        +---------------------------+---------------------------+
        |   ports/: ModelPort TransportPort StorePort ToolPort  |
        |           ClockPort                                   |
        +---------------------------+---------------------------+
                                    | implemented by
        +---------------------------+---------------------------+
        |  adapters/: ollama_model  discord_transport           |
        |             sqlite_store  local_tools  mcp_tools      |
        +-------------------------------------------------------+
```

## Layer rules (enforced by convention, checked in review)

1. `alfred/domain/*` imports only: stdlib, pydantic, `alfred.ports`,
   `alfred.errors`, `alfred.domain.*`. Never adapters, runtime, config, or any
   I/O library. No file, socket, or database access. Time comes from
   `ClockPort`; never call `datetime.now()` in domain code.
2. `alfred/adapters/*` imports ports, schemas, config, errors, and its own
   external library. Adapters never import the domain services or each other.
3. `alfred/runtime/*` is the only layer that imports both domain and adapters.
   All construction and wiring lives in `runtime/composition.py`.
4. `alfred/ports/*` imports nothing from alfred except `alfred.errors`.
5. Everything async-first: all port methods and domain services are `async`
   except pure functions.
6. Structured logging via `logging.getLogger(__name__)`. No `print` outside
   `runtime/cli.py`. Never log credentials or raw tokens.
7. All structured data is pydantic v2 (`model_validate`, `model_dump`,
   `model_json_schema`). Never v1 idioms (`.dict()`, `.parse_obj()`).

## Store conventions

`StorePort` is a small document store: JSON documents in named collections,
keyed (`put`/`get`) or append-only (`append`, time-ordered keys). Returned
docs carry their key as `"_key"`. `query(where=...)` is top-level equality
only. Canonical collection names live in
`alfred.domain.schemas.Collections`; never use bare strings.

Persisting a pydantic model: `store.put(coll, key, obj.model_dump(mode="json"))`.
Loading: `Model.model_validate(doc)` (extra `_key` is ignored by models unless
configured otherwise; strip it for `extra="forbid"` models like
`AgentManifest`).

## Frozen module contracts

The signatures below are binding. Implementations may add private helpers and
extra optional keyword arguments, but must not rename, remove, or change the
meaning of anything listed. Other modules are written in parallel against
these exact surfaces.

### domain/structured.py — validated LLM calls

```python
T = TypeVar("T", bound=BaseModel)

def extract_json(text: str) -> str
    # Best-effort extraction of the first JSON object from model text:
    # strips markdown fences, leading chatter, trailing junk. Raises
    # ValueError when no candidate object is present.

async def structured_call(
    model: ModelPort,
    *,
    schema: type[T],
    system: str,
    user: str,
    history: list[ModelMessage] | None = None,
    options: ModelOptions | None = None,
    max_attempts: int = 3,
) -> T
    # The reliability core. Passes schema.model_json_schema() as json_schema
    # to the model, parses/validates the reply, and on failure retries with
    # the validation errors appended to the conversation so the model can
    # correct itself. Raises StructuredCallError after max_attempts.
```

### domain/registry.py — agents in memory (pure; loading is runtime's job)

```python
class LoadedAgent(BaseModel):
    manifest: AgentManifest
    prompt: str            # contents of agent.md
    path: str = ""         # folder path, informational only

class AgentRegistry:
    def __init__(self, agents: list[LoadedAgent] | None = None) -> None
    def add(self, agent: LoadedAgent) -> None
    def get(self, name: str) -> LoadedAgent | None
    def all(self) -> list[LoadedAgent]
    def active(self) -> list[LoadedAgent]
        # excludes PAUSED and RETIRED lifecycles
    def remove(self, name: str) -> bool

def parse_manifest(raw: dict) -> AgentManifest
    # Wraps AgentManifest.model_validate; raises ManifestError with a
    # readable message on failure.
```

### domain/routing.py — which agents handle a message

```python
def route(message: InboundMessage, registry: AgentRegistry) -> list[LoadedAgent]
    # Keyword triggers match case-insensitively on word boundaries.
    # Agents with triggers.always=True are always included. PAUSED and
    # RETIRED agents never route. Deterministic order: always-agents
    # first, then keyword matches, each alphabetical. Empty list means
    # no specific agent claimed it (core falls back to general handling).
```

### domain/governance.py — tiers, policy, pending actions, proposals

```python
class Policy:
    def __init__(self, *, auto_approve_reversible: bool = True) -> None
    def requires_confirmation(self, tier: CapabilityTier, provenance: Provenance) -> bool
        # DESTRUCTIVE: always True. REVERSIBLE_WRITE: True when
        # provenance == "external" or auto_approve_reversible is False.
        # READ_ONLY: always False.
        # Net effect: external content never auto-executes anything above
        # READ_ONLY, and destructive actions are never auto-executed at all.

class PendingActions:
    def __init__(self, store: StorePort, clock: ClockPort, ttl_hours: int = 24) -> None
    async def create(self, agent: str, call: ToolCall, tier: CapabilityTier,
                     provenance: Provenance, reason: str = "") -> PendingAction
    async def get(self, action_id: str) -> PendingAction | None
    async def list_pending(self) -> list[PendingAction]   # expires stale ones
    async def resolve(self, action_id: str, *, approved: bool) -> PendingAction
        # marks confirmed/rejected; raises AlfredError on unknown id or
        # already-resolved action

class Proposals:
    def __init__(self, store: StorePort, clock: ClockPort) -> None
    async def create(self, proposal: Proposal) -> Proposal
    async def list_pending(self) -> list[Proposal]
    async def resolve(self, proposal_id: str, *, approved: bool) -> Proposal
        # Approval only marks status; applying the change to disk is the
        # runtime's job. touches_safety proposals must never be created
        # with status other than "pending".

async def audit(store: StorePort, clock: ClockPort, event: str, **data: Any) -> None
    # Appends {"event": event, "at": iso-now, **data} to Collections.AUDIT.
```

### domain/dispatch.py — the gated tool dispatcher

```python
class DispatchOutcome(BaseModel):
    result: ToolResult | None = None      # set when the call executed
    pending: PendingAction | None = None  # set when confirmation is required

class ToolDispatcher:
    def __init__(self, tools: ToolPort, store: StorePort, clock: ClockPort,
                 policy: Policy, pending: PendingActions) -> None
    async def dispatch(self, agent: LoadedAgent, call: ToolCall,
                       provenance: Provenance) -> DispatchOutcome
        # Order: (1) allowlist check against manifest.allowed_tools, deny by
        # default, raise ToolNotAllowedError and audit the violation;
        # (2) resolve ToolSpec (unknown -> ToolNotFoundError); (3) tier
        # policy via Policy.requires_confirmation -> either invoke or create
        # PendingAction; (4) audit every dispatch, execution, and gating
        # decision with agent, tool, tier, provenance.
    async def execute_confirmed(self, action_id: str, agent: LoadedAgent | None) -> ToolResult
        # Resolves the pending action as approved, re-checks the allowlist
        # against the CURRENT agent (the caller looks it up in the registry;
        # None means the agent no longer exists and the call is refused with
        # ToolNotAllowedError), invokes, audits.
```

### domain/user_model.py — the evolving model of the owner

```python
class UserModelService:
    def __init__(self, store: StorePort, clock: ClockPort) -> None
    async def get_profile(self) -> UserProfile          # default profile if none stored
    async def save_profile(self, profile: UserProfile) -> None
        # bumps version, stamps updated_at
    async def record_observation(self, source: str, kind: str, text: str) -> Observation
        # appends to Collections.OBSERVATIONS; appends, never overwrites
    async def record_outcome(self, outcome: Outcome) -> None
        # appends to Collections.OUTCOMES and updates the profile's
        # AdherenceStats for outcome.agent (consecutive_misses increments on
        # MISSED, resets on DONE/PARTIAL)
    async def recent_observations(self, limit: int = 20) -> list[Observation]
    async def recent_outcomes(self, agent: str | None = None, limit: int = 20) -> list[Outcome]
    async def summary_for_prompt(self) -> str
        # compact plain-text rendering of profile + recent signal for
        # inclusion in agent prompts
```

### domain/feedback.py — closing the loop

```python
def parse_outcome_report(text: str) -> OutcomeStatus | None
    # cheap deterministic mapping of owner phrasing ("done", "skipped it",
    # "missed", "half of it") to a status; None when ambiguous

def adherence_signal(stats: AdherenceStats) -> str
    # one of: "strong", "ok", "wobbling", "lapsing" based on rate and
    # consecutive_misses (>=2 consecutive misses -> "lapsing",
    # exactly 1 miss after success -> still "ok": one miss is fine,
    # catch the second)

def plan_adjustment_hint(stats: AdherenceStats) -> str
    # short instruction injected into the next planning prompt, e.g.
    # "previous plan repeatedly ignored: the plan is wrong, not the owner;
    # shrink it" for lapsing agents
```

### domain/conductor.py — concurrent plans that do not collide

```python
def detect_conflicts(plans: list[Plan], weekly_capacity: int) -> list[Conflict]
    # pure: overload_week (sum of loads > capacity), overload_day (any one
    # day > ceil(capacity / 5)), time_collision (same day + overlapping
    # time/duration windows)

class Conductor:
    def __init__(self, model: ModelPort, clock: ClockPort) -> None
    async def reconcile(self, plans: list[Plan], profile: UserProfile) -> ReconciledSchedule
        # detect_conflicts first; when none, passthrough with summary and
        # total_load. When conflicts exist, one structured_call asking the
        # model to resolve them (move/shrink/drop) given the user profile;
        # validates the result still fits capacity, else applies a
        # deterministic fallback (drop lowest-priority items, lowest load
        # first) so reconcile() never returns an over-capacity schedule.
```

### domain/lifecycle.py — agent lifecycle rules

```python
def check_in_interval(state: Lifecycle) -> timedelta | None
    # FORMING: 1 day; LAPSING/RESHAPED: 1 day; ESTABLISHED: 3 days;
    # MAINTENANCE: 7 days; PROPOSED/PAUSED/RETIRED: None

def next_lifecycle(state: Lifecycle, stats: AdherenceStats) -> Lifecycle
    # deterministic transitions, e.g. FORMING + rate>=0.8 over >=14 logged
    # outcomes -> ESTABLISHED; any active state + consecutive_misses>=2 ->
    # LAPSING; LAPSING + consecutive_dones>=3 -> FORMING (rebuild gently).
    # Conservative: when unsure, stay put.

class LapseDoctor:
    def __init__(self, model: ModelPort, clock: ClockPort) -> None
    async def diagnose(self, agent: LoadedAgent, stats: AdherenceStats,
                       recent_outcomes: list[Outcome],
                       owner_comment: str = "") -> LapseDiagnosis
        # structured_call producing LapseDiagnosis; tone rules: a lapse is
        # data, never moral failure; no streak shame; retiring is a valid
        # outcome
```

### domain/builder.py — the Adaptive Agent Builder

```python
class WipVerdict(BaseModel):
    allowed: bool
    forming_count: int
    detail: str = ""

def check_wip(registry: AgentRegistry, *, limit: int = 2) -> WipVerdict
    # counts FORMING + RESHAPED habit/state agents; refuses new builds at
    # the limit, and says why in plain language

class AgentBuilder:
    def __init__(self, model: ModelPort, user_model: UserModelService,
                 store: StorePort, clock: ClockPort) -> None
    async def start(self, stated_goal: str, registry: AgentRegistry) -> tuple[BuilderSession, str]
        # creates + persists a session; returns (session, first question).
        # Enforces check_wip BEFORE eliciting; at limit, the returned text
        # explains capacity honestly and the session is ABANDONED.
    async def step(self, session_id: str, owner_message: str,
                   registry: AgentRegistry) -> tuple[BuilderSession, str]
        # advances the state machine (ELICITING -> CLASSIFYING -> DESIGNING
        # -> CAPACITY_CHECK -> PROPOSING -> AWAITING_APPROVAL -> DONE).
        # Structured calls drive elicitation questions, shape classification,
        # and blueprint design. The blueprint's manifest starts at
        # lifecycle=FORMING (PROPOSED until approved), smallest viable size,
        # anchored to an existing cue. Approval ("yes"/"approve") moves to
        # DONE and the blueprint is returned for the runtime to materialise.
    async def get_session(self, session_id: str) -> BuilderSession | None
    async def active_session(self) -> BuilderSession | None
        # most recent session not DONE/ABANDONED
```

Builder behaviour rules (binding): interrogate the stated goal before
scaffolding (the stated goal is rarely the real lever); classify the shape
(habit/skill/project/state/metric) and scaffold accordingly; start at the
smallest viable size; anchor to existing cues; respect the WIP limit; never
use streak shame, fake urgency, or engagement bait; retiring or shrinking a
goal is always an acceptable recommendation.

### domain/reflection.py — periodic strategy review

```python
class ReflectionEngine:
    def __init__(self, model: ModelPort, user_model: UserModelService,
                 store: StorePort, clock: ClockPort) -> None
    async def reflect(self, registry: AgentRegistry, window_days: int = 7) -> Reflection
        # gathers recent outcomes + observations + adherence, one
        # structured_call -> Reflection; persists to Collections.REFLECTIONS;
        # applies textual profile_updates as appended profile notes (with
        # observation records, source="reflection"); proposals are persisted
        # via Proposals and NEVER auto-applied. Lifecycle transitions from
        # next_lifecycle() are emitted as LIFECYCLE_CHANGE proposals, not
        # silent edits.
```

### domain/executor.py — running one agent

```python
class AgentExecutor:
    def __init__(self, model: ModelPort, tools: ToolPort,
                 dispatcher: ToolDispatcher, user_model: UserModelService,
                 store: StorePort, clock: ClockPort) -> None
    async def run(self, agent: LoadedAgent, *, text: str,
                  provenance: Provenance, max_rounds: int = 3) -> ExecutionResult
        # Assembles the prompt: agent.md + governance preamble + user-model
        # summary + adherence hint (feedback.plan_adjustment_hint) + the
        # specs of allowlisted tools + the AgentReply output contract. Each
        # round is one structured_call(schema=AgentReply). Tool calls go
        # through ToolDispatcher; executed results are fed back as tool
        # messages, gated ones accumulate as pending. ToolNotAllowedError /
        # ToolNotFoundError are caught, audited, and fed back as refusal
        # text, never crash the run. Plans are stamped (agent, created_at)
        # and persisted to Collections.PLANS; observations recorded via
        # UserModelService. Loop continues while reply.done is False and
        # rounds remain.
```

### adapters (frozen constructor surfaces)

```python
# adapters/ollama_model.py
class OllamaModelAdapter:                       # implements ModelPort
    def __init__(self, config: ModelConfig) -> None
    async def complete(...) -> str              # maps json_schema -> format=
    async def ensure_model(self) -> str
        # hardware-aware pick: returns config.name if pulled locally, else
        # first available fallback; raises ConfigError when nothing usable

# adapters/sqlite_store.py
class SqliteStoreAdapter:                       # implements StorePort
    def __init__(self, db_path: str | Path) -> None
    # WAL mode, single documents table (collection, key, doc JSON,
    # created_at), sync sqlite3 wrapped in asyncio.to_thread
    async def close(self) -> None

# adapters/discord_transport.py
class DiscordTransportAdapter:                  # implements TransportPort
    def __init__(self, config: DiscordConfig,
                 handler: Callable[[InboundMessage], Awaitable[None]]) -> None
    async def start(self) -> None               # connects and blocks
    async def send(self, message: OutboundMessage) -> None
    async def close(self) -> None
    # Only messages from config.owner_id are processed; everything else is
    # ignored (not refused, ignored). Replies chunk at <=2000 chars.

# adapters/local_tools.py
class LocalToolAdapter:                         # implements ToolPort
    def __init__(self, store: StorePort, clock: ClockPort) -> None
    # built-ins (all READ_ONLY unless noted): current_time, list_plans,
    # list_recent_outcomes, list_agents_state, log_note (REVERSIBLE_WRITE)

# adapters/mcp_tools.py  (phase 6 surface, shipped behind optional dep)
class McpToolAdapter:                           # implements ToolPort
    @classmethod
    async def connect(cls, servers: list[McpServerConfig]) -> "McpToolAdapter"
    async def close(self) -> None
    # tool names namespaced "<server>.<tool>"; tiers from config.tool_tiers,
    # default DESTRUCTIVE for anything unclassified

class CompositeToolAdapter:                     # implements ToolPort
    def __init__(self, sources: list[ToolPort]) -> None
    # first source claiming a name wins; list_tools concatenates
```

### runtime (wave 2)

```python
# runtime/agent_loader.py
def load_agents(agents_dir: str | Path) -> AgentRegistry
    # scans agents/*/manifest.yaml + agent.md; invalid folders are logged
    # and skipped, never fatal
def materialise_agent(agents_dir: str | Path, blueprint: AgentBlueprint) -> Path
    # writes the folder for an approved blueprint; refuses to overwrite

# runtime/core.py
class AlfredCore:
    # owns: registry, executor, conductor, builder, user model, governance,
    # transport. Entry points:
    async def handle_inbound(self, message: InboundMessage) -> None
        # owner commands first (confirm/deny <id>, proposals, approve/reject
        # <id>, agents, status, new agent <goal>), then builder-session
        # continuation, then route() -> executor, multi-plan -> conductor.
        # Replies via TransportPort.
    async def run_scheduled(self, trigger: ScheduledTrigger) -> None

# runtime/heartbeat.py
class Heartbeat:
    def __init__(self, registry: AgentRegistry, clock: ClockPort,
                 store: StorePort,
                 runner: Callable[[ScheduledTrigger], Awaitable[None]],
                 config: HeartbeatConfig) -> None
    # runner is AlfredCore.run_scheduled in production; injecting a callable
    # keeps the scheduler decoupled and directly testable
    async def tick(self) -> list[ScheduledTrigger]   # due jobs, fired via core
    async def run_forever(self) -> None
    # due-ness: manifest Schedule + lifecycle check_in_interval + periodic
    # reflection; quiet hours suppress proactive sends; last-run state in
    # Collections.HEARTBEAT so restarts do not double-fire

# runtime/composition.py
def build_system(config: AlfredConfig, *, fake: bool = False) -> ComposedSystem
    # the single composition root; ComposedSystem dataclass exposes core,
    # heartbeat, transport, store, registry for the CLI to drive
```

## Governance model (binding)

| Tier | Owner-initiated | Scheduler-initiated | External content |
|---|---|---|---|
| READ_ONLY | auto | auto | auto |
| REVERSIBLE_WRITE | auto (configurable), audited | auto (configurable), audited | confirm |
| DESTRUCTIVE | confirm | confirm | confirm |

- Allowlists are deny-by-default and load-bearing. Nothing widens an
  allowlist except the owner editing a manifest or approving a
  `touches_safety` proposal.
- Every dispatch decision is audited: who, what, tier, provenance, verdict.
- Self-modification is proposal-only, versioned, reversible.

## Agent folder format

```
agents/<name>/
  manifest.yaml   # AgentManifest fields; extra keys are an error
  agent.md        # role/behaviour prompt: identity, scope, tone, output rules
  tools/          # optional, reserved
  state/          # optional, agent-local scratch (gitignored)
```

## Testing strategy

- Domain: pytest against `alfred.testing.fakes` (real in-memory port
  implementations, not mocks). The validation/retry loop, allowlist and tier
  gating, conflict detection, lifecycle transitions, WIP limits, and the
  feedback loop all have direct unit coverage.
- Adapters: thin integration tests (sqlite against tmp_path; ollama/discord
  logic tested via fakes of their client objects where practical).
- `pytest` runs fully offline. Nothing in the suite needs Ollama or Discord.
