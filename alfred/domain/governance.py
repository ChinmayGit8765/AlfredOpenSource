"""Governance: capability tiers, confirmation policy, pending actions, proposals.

This module is the safety floor under ALFRED's reach. The Policy decides
which tool calls need explicit owner confirmation; PendingActions holds
gated calls until the owner rules on them; Proposals carries every
self-modification through a human-in-the-loop gate. Nothing here ever
widens access; it only refuses, defers, or records.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from alfred.domain.schemas import (
    Collections,
    PendingAction,
    Proposal,
    ProposalKind,
    Provenance,
    ToolCall,
    WorkflowTrustRecord,
    load_or_none,
)
from alfred.errors import AlfredError
from alfred.ports.clock import ClockPort
from alfred.ports.store import StorePort
from alfred.ports.tools import CapabilityTier

logger = logging.getLogger(__name__)


async def audit(store: StorePort, clock: ClockPort, event: str, **data: Any) -> None:
    """Append one audit record. Every governance decision flows through here."""
    await store.append(
        Collections.AUDIT,
        {"event": event, "at": clock.now().isoformat(), **data},
    )


class Policy:
    """Decides which (tier, provenance) pairs require owner confirmation."""

    def __init__(
        self,
        *,
        auto_approve_reversible: bool = True,
        dry_run_cross_system: bool = True,
    ) -> None:
        self.auto_approve_reversible = auto_approve_reversible
        self.dry_run_cross_system = dry_run_cross_system

    def requires_confirmation(
        self,
        tier: CapabilityTier,
        provenance: Provenance,
        *,
        cross_system: bool = False,
        trusted: bool = False,
    ) -> bool:
        # External content never auto-executes anything above READ_ONLY,
        # and destructive actions are never auto-executed at all.
        if tier == CapabilityTier.DESTRUCTIVE:
            return True
        # Dry run before cross-system action: until the owner trusts the
        # workflow, any write reaching an external system (an MCP server, not
        # a local tool) is previewed for confirmation rather than executed,
        # even when its tier would otherwise auto-approve. Read-only
        # cross-system calls are not actions, so they are never previewed.
        # trusted relaxes ONLY this clause: the autonomy dial can retire a
        # workflow's preview, never a destructive gate (caught above) and
        # never an external-content gate (caught below).
        if (
            cross_system
            and self.dry_run_cross_system
            and tier != CapabilityTier.READ_ONLY
            and not trusted
        ):
            return True
        if tier == CapabilityTier.REVERSIBLE_WRITE:
            return provenance == "external" or not self.auto_approve_reversible
        return False


def _strip_key(doc: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in doc.items() if k != "_key"}


class WorkflowTrust:
    """The autonomy dial's ledger: consecutive approvals per (agent, tool).

    A workflow is one agent calling one tool. Trust is earned by the owner
    confirming that workflow's previewed cross-system writes threshold
    times IN A ROW, and a single deny zeroes the run: distrust is always
    cheaper than trust. threshold 0 means the dial is off and nothing is
    ever trusted, whatever the ledger says; the ledger still records, so
    turning the dial on later honours confirmations already given.
    """

    def __init__(self, store: StorePort, clock: ClockPort, threshold: int = 0) -> None:
        self._store = store
        self._clock = clock
        self.threshold = threshold

    @staticmethod
    def _key(agent: str, tool: str) -> str:
        # Tool names may contain dots (MCP namespacing); agent slugs cannot
        # contain ':', so the double colon cannot collide.
        return f"{agent}::{tool}"

    async def is_trusted(self, agent: str, tool: str) -> bool:
        if self.threshold <= 0:
            return False
        record = await self._get(agent, tool)
        return record is not None and record.approvals >= self.threshold

    async def record_approval(self, agent: str, tool: str) -> tuple[int, bool]:
        """Count one owner confirmation; returns (approvals, newly_trusted)."""
        record = await self._get(agent, tool) or WorkflowTrustRecord(
            agent=agent, tool=tool
        )
        before = record.approvals
        record = record.model_copy(
            update={"approvals": before + 1, "updated_at": self._clock.now()}
        )
        await self._save(record)
        newly_trusted = (
            self.threshold > 0 and before < self.threshold <= record.approvals
        )
        if newly_trusted:
            await audit(
                self._store,
                self._clock,
                "workflow_trusted",
                agent=agent,
                tool=tool,
                approvals=record.approvals,
                threshold=self.threshold,
            )
        return record.approvals, newly_trusted

    async def reset(self, agent: str, tool: str, *, cause: str) -> None:
        """Zero one workflow's run; the audit says why (denied, distrust)."""
        record = await self._get(agent, tool)
        if record is None or record.approvals == 0:
            return
        await self._save(
            record.model_copy(update={"approvals": 0, "updated_at": self._clock.now()})
        )
        await audit(
            self._store,
            self._clock,
            "workflow_trust_reset",
            agent=agent,
            tool=tool,
            cause=cause,
            approvals_lost=record.approvals,
        )

    async def all_records(self) -> list[WorkflowTrustRecord]:
        docs = await self._store.query(Collections.WORKFLOW_TRUST)
        records = [
            record
            for doc in docs
            if (
                record := load_or_none(
                    WorkflowTrustRecord, doc, source=Collections.WORKFLOW_TRUST
                )
            )
            is not None
        ]
        return sorted(records, key=lambda r: (r.agent, r.tool))

    async def _get(self, agent: str, tool: str) -> WorkflowTrustRecord | None:
        doc = await self._store.get(
            Collections.WORKFLOW_TRUST, self._key(agent, tool)
        )
        if doc is None:
            return None
        return load_or_none(
            WorkflowTrustRecord, doc, source=Collections.WORKFLOW_TRUST
        )

    async def _save(self, record: WorkflowTrustRecord) -> None:
        await self._store.put(
            Collections.WORKFLOW_TRUST,
            self._key(record.agent, record.tool),
            record.model_dump(mode="json"),
        )


class PendingActions:
    """Gated tool calls awaiting explicit owner confirmation, persisted."""

    def __init__(self, store: StorePort, clock: ClockPort, ttl_hours: int = 24) -> None:
        self._store = store
        self._clock = clock
        self._ttl = timedelta(hours=ttl_hours)

    async def create(
        self,
        agent: str,
        call: ToolCall,
        tier: CapabilityTier,
        provenance: Provenance,
        reason: str = "",
        bundle_id: str | None = None,
        cross_system: bool = False,
    ) -> PendingAction:
        seq = 0
        if bundle_id is not None:
            # Emission order within the intent. Counting the stored members
            # (any status) is race-free here: the core serializes every
            # handler behind one lock, so no two creations interleave.
            seq = len(
                await self._store.query(
                    Collections.PENDING_ACTIONS, where={"bundle_id": bundle_id}
                )
            )
        action = PendingAction(
            agent=agent,
            call=call,
            tier=tier,
            provenance=provenance,
            reason=reason,
            created_at=self._clock.now(),
            bundle_id=bundle_id,
            bundle_seq=seq,
            cross_system=cross_system,
        )
        await self._save(action)
        return action

    async def get(self, action_id: str) -> PendingAction | None:
        doc = await self._store.get(Collections.PENDING_ACTIONS, action_id)
        if doc is None:
            return None
        return PendingAction.model_validate(_strip_key(doc))

    async def bundle_members(self, bundle_id: str) -> list[PendingAction]:
        """The still-pending members of one intent, in emission order.

        Empty when the id names no live bundle, which is also how callers
        tell a bundle id from a single action id.
        """
        members = [
            action
            for action in await self.list_pending()
            if action.bundle_id == bundle_id
        ]
        return sorted(members, key=lambda action: action.bundle_seq)

    async def list_pending(self) -> list[PendingAction]:
        docs = await self._store.query(
            Collections.PENDING_ACTIONS, where={"status": "pending"}
        )
        fresh: list[PendingAction] = []
        for doc in docs:
            # A drifted legacy row must not block the list: hiding every
            # confirmable action behind one unreadable one would wedge the
            # whole confirm flow. Skipped rows are warned about, and
            # unconfirmed actions can never execute, so skipping is safe.
            action = load_or_none(PendingAction, doc, source=Collections.PENDING_ACTIONS)
            if action is None:
                continue
            if self._is_stale(action):
                await self._expire(action)
                continue
            fresh.append(action)
        return fresh

    async def resolve(self, action_id: str, *, approved: bool) -> PendingAction:
        action = await self.get(action_id)
        if action is None:
            raise AlfredError(f"unknown pending action: {action_id}")
        if action.status != "pending":
            raise AlfredError(
                f"pending action {action_id} already resolved ({action.status})"
            )
        if self._is_stale(action):
            # A stale confirmation must not execute: the world may have
            # moved on since the action was gated. Expire instead.
            await self._expire(action)
            raise AlfredError(f"pending action {action_id} has expired")
        resolved = action.model_copy(
            update={"status": "confirmed" if approved else "rejected"}
        )
        await self._save(resolved)
        # Refusals matter as much as executions in the audit trail: the
        # record must show gates closing, not only gates opening.
        resolution: dict[str, object] = {
            "action_id": resolved.id,
            "agent": resolved.agent,
            "tool": resolved.call.tool,
            "tier": resolved.tier.value,
            "approved": approved,
        }
        if resolved.bundle_id is not None:
            # Members of one composed intent stay linkable in the record.
            resolution["bundle_id"] = resolved.bundle_id
        await audit(
            self._store, self._clock, "pending_action_resolved", **resolution
        )
        return resolved

    async def reopen(self, action_id: str) -> PendingAction:
        """Return a confirmed-but-unexecuted action to pending.

        Used when execution failed after approval (tool vanished, server
        down): the confirmation was consumed but nothing ran, so the owner
        must be able to confirm again once the fault is fixed. The TTL
        still counts from the original created_at, so a reopened action
        for a long-broken tool expires normally instead of living forever.
        """
        action = await self.get(action_id)
        if action is None:
            raise AlfredError(f"unknown pending action: {action_id}")
        reopened = action.model_copy(update={"status": "pending"})
        await self._save(reopened)
        await audit(
            self._store,
            self._clock,
            "pending_action_reopened",
            action_id=reopened.id,
            agent=reopened.agent,
            tool=reopened.call.tool,
            tier=reopened.tier.value,
        )
        return reopened

    def _is_stale(self, action: PendingAction) -> bool:
        if action.created_at is None:
            return False
        return self._clock.now() - action.created_at > self._ttl

    async def _expire(self, action: PendingAction) -> None:
        expired = action.model_copy(update={"status": "expired"})
        await self._save(expired)
        await audit(
            self._store,
            self._clock,
            "pending_action_expired",
            action_id=expired.id,
            agent=expired.agent,
            tool=expired.call.tool,
            tier=expired.tier.value,
        )
        logger.info("pending action %s expired after TTL", action.id)

    async def _save(self, action: PendingAction) -> None:
        await self._store.put(
            Collections.PENDING_ACTIONS, action.id, action.model_dump(mode="json")
        )


class Proposals:
    """Proposed changes to ALFRED itself. Approval marks status only;
    applying the change to disk is the runtime's job."""

    def __init__(self, store: StorePort, clock: ClockPort) -> None:
        self._store = store
        self._clock = clock

    async def create(self, proposal: Proposal) -> Proposal:
        # Force pending regardless of what the caller set: no proposal,
        # least of all a touches_safety one, is ever born pre-approved.
        update: dict[str, Any] = {"status": "pending", "created_at": self._clock.now()}
        # touches_safety must not depend on a model- or caller-supplied flag:
        # a manifest change whose new value alters the tool allowlist always
        # touches safety, so the double-confirmation gate cannot be bypassed
        # by a mis-flagged proposal.
        if (
            proposal.kind is ProposalKind.MANIFEST_CHANGE
            and proposal.new
            and "allowed_tools" in proposal.new
        ):
            update["touches_safety"] = True
        stamped = proposal.model_copy(update=update)
        await self._save(stamped)
        await audit(
            self._store,
            self._clock,
            "proposal_created",
            proposal_id=stamped.id,
            kind=stamped.kind.value,
            agent=stamped.agent,
            touches_safety=stamped.touches_safety,
        )
        return stamped

    async def list_pending(self) -> list[Proposal]:
        docs = await self._store.query(
            Collections.PROPOSALS, where={"status": "pending"}
        )
        loaded = [
            load_or_none(Proposal, doc, source=Collections.PROPOSALS) for doc in docs
        ]
        return [proposal for proposal in loaded if proposal is not None]

    async def resolve(self, proposal_id: str, *, approved: bool) -> Proposal:
        doc = await self._store.get(Collections.PROPOSALS, proposal_id)
        if doc is None:
            raise AlfredError(f"unknown proposal: {proposal_id}")
        proposal = Proposal.model_validate(_strip_key(doc))
        if proposal.status != "pending":
            raise AlfredError(
                f"proposal {proposal_id} already resolved ({proposal.status})"
            )
        resolved = proposal.model_copy(
            update={"status": "approved" if approved else "rejected"}
        )
        await self._save(resolved)
        await audit(
            self._store,
            self._clock,
            "proposal_resolved",
            proposal_id=resolved.id,
            kind=resolved.kind.value,
            agent=resolved.agent,
            touches_safety=resolved.touches_safety,
            approved=approved,
        )
        return resolved

    async def _save(self, proposal: Proposal) -> None:
        await self._store.put(
            Collections.PROPOSALS, proposal.id, proposal.model_dump(mode="json")
        )
