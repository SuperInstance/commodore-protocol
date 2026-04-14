"""Core Protocol Engine for the Commodore Protocol.

Provides:
- Election: Priority-based leader election
- HeartbeatMonitor: Track liveness of all units
- LoadBalancer: Distribute work based on capabilities and load
- CapabilityRegistry: Register/query what each unit can do
- FailoverManager: Handle commodore death gracefully
- CommodoreProtocol: Main coordinator that ties everything together
"""

from __future__ import annotations

import time
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from unit import Unit, Role, HealthStatus, LoadMetrics
from messages import (
    ProtocolMessage, MessageType, CommodoreHeartbeat, ElectionRequest,
    ElectionVote, DeferRequest, WorkAssignment, WorkComplete,
    CapabilityAnnounce, ScaleSuggestion, FailoverNotice,
    deserialize_message, serialize_message,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Election
# ---------------------------------------------------------------------------

class ElectionState(str, Enum):
    """States of an election."""
    IDLE = "idle"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ElectionResult:
    """Result of an election."""
    winner_id: str
    votes: dict[str, str] = field(default_factory=dict)  # voter_id -> candidate_id
    duration_seconds: float = 0.0
    reason: str = ""


class Election:
    """Priority-based leader election.

    Priority ordering:
    1. Human-designated units
    2. Highest priority value
    3. Most capabilities
    4. Longest uptime
    5. Lowest unit_id lexicographically (tiebreaker)
    """

    def __init__(self, timeout_seconds: float = 5.0):
        self.timeout_seconds = timeout_seconds
        self.state = ElectionState.IDLE
        self.votes: dict[str, str] = {}  # voter_id -> candidate_id
        self._start_time: float = 0.0
        self._reason: str = ""

    def start_election(self, reason: str = "periodic") -> None:
        """Begin a new election, clearing previous votes."""
        self.state = ElectionState.IN_PROGRESS
        self.votes.clear()
        self._start_time = time.time()
        self._reason = reason

    def cast_vote(self, voter_id: str, candidate_id: str) -> bool:
        """Cast a vote. Returns True if accepted."""
        if self.state != ElectionState.IN_PROGRESS:
            return False
        if time.time() - self._start_time > self.timeout_seconds:
            self.state = ElectionState.FAILED
            return False
        self.votes[voter_id] = candidate_id
        return True

    def is_expired(self) -> bool:
        """Check if the election has timed out."""
        if self.state != ElectionState.IN_PROGRESS:
            return False
        return time.time() - self._start_time > self.timeout_seconds

    def resolve(self, units: dict[str, Unit]) -> ElectionResult:
        """Resolve the election based on votes and unit priorities.

        If no votes were cast, the highest-priority unit wins.
        Otherwise, the candidate with the most votes wins, with
        priority as tiebreaker.
        """
        self.state = ElectionState.COMPLETED
        duration = time.time() - self._start_time

        if not self.votes:
            # No votes: pick best unit by priority
            winner = self._pick_by_priority(units)
            if winner is None:
                self.state = ElectionState.FAILED
                return ElectionResult(
                    winner_id="",
                    votes={},
                    duration_seconds=duration,
                    reason=self._reason,
                )
            return ElectionResult(
                winner_id=winner.id,
                votes={},
                duration_seconds=duration,
                reason=self._reason,
            )

        # Count votes
        vote_counts: dict[str, int] = {}
        for candidate_id in self.votes.values():
            vote_counts[candidate_id] = vote_counts.get(candidate_id, 0) + 1

        # Find candidate with most votes, break ties by unit priority
        max_votes = max(vote_counts.values())
        top_candidates = [cid for cid, cnt in vote_counts.items() if cnt == max_votes]

        if len(top_candidates) == 1:
            winner_id = top_candidates[0]
        else:
            # Tiebreak by unit priority
            candidates = [units[cid] for cid in top_candidates if cid in units]
            if candidates:
                winner = max(candidates)
                winner_id = winner.id
            else:
                winner_id = top_candidates[0]

        return ElectionResult(
            winner_id=winner_id,
            votes=dict(self.votes),
            duration_seconds=duration,
            reason=self._reason,
        )

    def _pick_by_priority(self, units: dict[str, Unit]) -> Unit | None:
        """Pick the best unit by priority ordering among alive units."""
        alive = [u for u in units.values() if u.is_alive]
        if not alive:
            # Fallback: pick any unit
            all_units = list(units.values())
            return all_units[0] if all_units else None
        return max(alive)


# ---------------------------------------------------------------------------
# HeartbeatMonitor
# ---------------------------------------------------------------------------

@dataclass
class HeartbeatConfig:
    """Configuration for heartbeat monitoring."""
    interval_seconds: float = 2.0
    timeout_seconds: float = 6.0
    max_missed: int = 3


class HeartbeatMonitor:
    """Track liveness of all units via heartbeats."""

    def __init__(self, config: HeartbeatConfig | None = None):
        self.config = config or HeartbeatConfig()
        self._last_heartbeats: dict[str, float] = {}
        self._missed_counts: dict[str, int] = {}
        self._callbacks: list[Callable[[str, HealthStatus], None]] = []

    def register_callback(
        self, callback: Callable[[str, HealthStatus], None]
    ) -> None:
        """Register a callback for health status changes."""
        self._callbacks.append(callback)

    def record_heartbeat(self, unit_id: str, timestamp: float | None = None) -> None:
        """Record a heartbeat from a unit."""
        ts = timestamp if timestamp is not None else time.time()
        self._last_heartbeats[unit_id] = ts
        self._missed_counts[unit_id] = 0

    def get_last_heartbeat(self, unit_id: str) -> float | None:
        """Get the last heartbeat time for a unit."""
        return self._last_heartbeats.get(unit_id)

    def get_missed_count(self, unit_id: str) -> int:
        """Get the number of missed heartbeats for a unit."""
        return self._missed_counts.get(unit_id, 0)

    def check_health(self, units: dict[str, Unit]) -> dict[str, HealthStatus]:
        """Check health of all registered units.

        Returns a mapping of unit_id to HealthStatus.
        """
        now = time.time()
        results: dict[str, HealthStatus] = {}

        for unit_id, unit in units.items():
            last_hb = self._last_heartbeats.get(unit_id)
            if last_hb is None:
                results[unit_id] = HealthStatus.UNKNOWN
                continue

            elapsed = now - last_hb
            missed = int(elapsed / self.config.interval_seconds)

            if elapsed > self.config.timeout_seconds * self.config.max_missed:
                new_status = HealthStatus.DEAD
            elif elapsed > self.config.timeout_seconds:
                new_status = HealthStatus.UNHEALTHY
            elif elapsed > self.config.interval_seconds * 2:
                new_status = HealthStatus.DEGRADED
            else:
                new_status = HealthStatus.HEALTHY

            old_status = unit.health
            unit.health = new_status
            results[unit_id] = new_status

            # Fire callbacks on status change
            if old_status != new_status:
                for cb in self._callbacks:
                    try:
                        cb(unit_id, new_status)
                    except Exception:
                        logger.exception("Heartbeat callback error")

        return results

    def get_dead_units(self, units: dict[str, Unit]) -> list[str]:
        """Return IDs of units considered dead."""
        health = self.check_health(units)
        return [uid for uid, status in health.items()
                if status in (HealthStatus.DEAD, HealthStatus.UNHEALTHY)]

    def reset(self) -> None:
        """Clear all heartbeat state."""
        self._last_heartbeats.clear()
        self._missed_counts.clear()


# ---------------------------------------------------------------------------
# CapabilityRegistry
# ---------------------------------------------------------------------------

class CapabilityRegistry:
    """Register and query what each unit can do."""

    def __init__(self):
        self._capabilities: dict[str, set[str]] = {}

    def register(self, unit_id: str, capabilities: list[str]) -> None:
        """Register capabilities for a unit."""
        self._capabilities[unit_id] = set(capabilities)

    def unregister(self, unit_id: str) -> None:
        """Remove a unit from the registry."""
        self._capabilities.pop(unit_id, None)

    def get_capabilities(self, unit_id: str) -> set[str]:
        """Get capabilities for a unit."""
        return set(self._capabilities.get(unit_id, set()))

    def find_units_with(self, capability: str) -> list[str]:
        """Find all units that have a specific capability."""
        return [uid for uid, caps in self._capabilities.items()
                if capability in caps]

    def find_units_with_all(self, capabilities: list[str]) -> list[str]:
        """Find all units that have ALL specified capabilities."""
        cap_set = set(capabilities)
        return [uid for uid, caps in self._capabilities.items()
                if cap_set.issubset(caps)]

    def find_units_with_any(self, capabilities: list[str]) -> list[str]:
        """Find all units that have ANY of the specified capabilities."""
        cap_set = set(capabilities)
        return [uid for uid, caps in self._capabilities.items()
                if cap_set.intersection(caps)]

    def all_capabilities(self) -> set[str]:
        """Get the union of all capabilities across all units."""
        result: set[str] = set()
        for caps in self._capabilities.values():
            result.update(caps)
        return result

    def unit_count(self) -> int:
        """Get the number of registered units."""
        return len(self._capabilities)

    def clear(self) -> None:
        """Clear the registry."""
        self._capabilities.clear()


# ---------------------------------------------------------------------------
# LoadBalancer
# ---------------------------------------------------------------------------

@dataclass
class Assignment:
    """A work assignment tracked by the load balancer."""
    task_id: str
    task_type: str
    assigned_to: str
    priority: int = 0
    timestamp: float = field(default_factory=time.time)
    completed: bool = False


class LoadBalancer:
    """Distribute work based on capabilities and current load."""

    def __init__(self, max_load_threshold: float = 80.0):
        self.max_load_threshold = max_load_threshold
        self._assignments: dict[str, Assignment] = {}  # task_id -> Assignment
        self._unit_task_count: dict[str, int] = {}  # unit_id -> active task count

    def assign_task(
        self,
        task_id: str,
        task_type: str,
        units: dict[str, Unit],
        registry: CapabilityRegistry,
        required_capabilities: list[str] | None = None,
        priority: int = 0,
        preferred_unit: str | None = None,
    ) -> str | None:
        """Assign a task to the best available unit.

        Returns the unit_id assigned, or None if no unit available.
        """
        # Check if already assigned
        if task_id in self._assignments:
            return self._assignments[task_id].assigned_to

        # Get candidate units
        if required_capabilities:
            candidate_ids = set(registry.find_units_with_all(required_capabilities))
        else:
            candidate_ids = set(units.keys())

        # Filter out dead units and commodore
        candidates = [
            u for u in units.values()
            if u.id in candidate_ids and u.is_alive and not u.is_commodore
        ]

        if not candidates:
            return None

        # If preferred unit is available and capable, use it
        if preferred_unit and preferred_unit in candidate_ids:
            for u in candidates:
                if u.id == preferred_unit:
                    chosen = u
                    break
            else:
                chosen = self._pick_best(candidates)
        else:
            chosen = self._pick_best(candidates)

        assignment = Assignment(
            task_id=task_id,
            task_type=task_type,
            assigned_to=chosen.id,
            priority=priority,
        )
        self._assignments[task_id] = assignment
        self._unit_task_count[chosen.id] = self._unit_task_count.get(chosen.id, 0) + 1

        return chosen.id

    def _pick_best(self, candidates: list[Unit]) -> Unit:
        """Pick the best candidate: lowest composite load, then most capabilities."""
        # Filter out overloaded units unless all are overloaded
        available = [u for u in candidates
                     if u.load.composite_load < self.max_load_threshold]
        pool = available if available else candidates
        return min(pool, key=lambda u: (u.load.composite_load, -u.capability_count))

    def complete_task(self, task_id: str) -> bool:
        """Mark a task as completed. Returns True if task was found."""
        assignment = self._assignments.get(task_id)
        if assignment is None or assignment.completed:
            return False
        assignment.completed = True
        unit_id = assignment.assigned_to
        if unit_id in self._unit_task_count:
            self._unit_task_count[unit_id] = max(
                0, self._unit_task_count[unit_id] - 1
            )
        return True

    def fail_task(self, task_id: str) -> bool:
        """Remove a failed task. Returns True if task was found."""
        assignment = self._assignments.pop(task_id, None)
        if assignment is None:
            return False
        unit_id = assignment.assigned_to
        if unit_id in self._unit_task_count:
            self._unit_task_count[unit_id] = max(
                0, self._unit_task_count[unit_id] - 1
            )
        return True

    def get_assignment(self, task_id: str) -> Assignment | None:
        """Get an assignment by task_id."""
        return self._assignments.get(task_id)

    def get_active_assignments(self) -> list[Assignment]:
        """Get all non-completed assignments."""
        return [a for a in self._assignments.values() if not a.completed]

    def get_assignments_for(self, unit_id: str) -> list[Assignment]:
        """Get all assignments for a specific unit."""
        return [a for a in self._assignments.values()
                if a.assigned_to == unit_id and not a.completed]

    def get_unit_load(self, unit_id: str) -> int:
        """Get the number of active tasks for a unit."""
        return self._unit_task_count.get(unit_id, 0)

    def should_scale_up(self, units: dict[str, Unit]) -> bool:
        """Check if the fleet should scale up (commodore overloaded)."""
        for u in units.values():
            if u.is_commodore and u.is_alive:
                return u.load.composite_load > self.max_load_threshold
        return False

    def reset(self) -> None:
        """Clear all assignments."""
        self._assignments.clear()
        self._unit_task_count.clear()


# ---------------------------------------------------------------------------
# FailoverManager
# ---------------------------------------------------------------------------

class FailoverState(str, Enum):
    """States of failover management."""
    STABLE = "stable"
    SUSPECTING = "suspecting"
    FAILOVER_IN_PROGRESS = "failover_in_progress"
    COMPLETED = "completed"


@dataclass
class FailoverPlan:
    """A plan for failover."""
    old_commodore_id: str
    new_commodore_id: str
    worker_assignments: dict[str, str] = field(default_factory=dict)  # worker -> action
    timestamp: float = field(default_factory=time.time)


class FailoverManager:
    """Handle commodore death gracefully."""

    def __init__(self):
        self.state = FailoverState.STABLE
        self._current_commodore_id: str | None = None
        self._suspect_count: int = 0
        self._suspect_threshold: int = 2
        self._callbacks: list[Callable[[FailoverPlan], None]] = []

    def register_callback(self, cb: Callable[[FailoverPlan], None]) -> None:
        """Register a callback for failover events."""
        self._callbacks.append(cb)

    def set_commodore(self, unit_id: str) -> None:
        """Set the current commodore."""
        self._current_commodore_id = unit_id
        self.state = FailoverState.STABLE
        self._suspect_count = 0

    def get_commodore_id(self) -> str | None:
        """Get the current commodore ID."""
        return self._current_commodore_id

    def suspect_commodore(self) -> bool:
        """Report that the commodore may be down.

        Returns True if failover threshold reached.
        """
        if self.state == FailoverState.FAILOVER_IN_PROGRESS:
            return False
        self._suspect_count += 1
        self.state = FailoverState.SUSPECTING
        return self._suspect_count >= self._suspect_threshold

    def reset_suspicions(self) -> None:
        """Reset suspect count (commodore recovered)."""
        self._suspect_count = 0
        self.state = FailoverState.STABLE

    def initiate_failover(
        self,
        units: dict[str, Unit],
        election: Election,
    ) -> FailoverPlan | None:
        """Initiate failover: elect new commodore and create plan.

        Returns the failover plan, or None if no eligible successor.
        """
        old_commodore_id = self._current_commodore_id
        if old_commodore_id is None:
            return None

        self.state = FailoverState.FAILOVER_IN_PROGRESS

        # Remove old commodore from candidates
        candidates = {
            uid: u for uid, u in units.items()
            if uid != old_commodore_id and u.is_alive
        }

        if not candidates:
            self.state = FailoverState.STABLE
            return None

        # Run election among candidates
        election.start_election(reason="failover")
        result = election.resolve(candidates)

        # Update roles
        if result.winner_id in units:
            units[result.winner_id].promote_to_commodore()
        if old_commodore_id in units:
            units[old_commodore_id].demote_to_worker()

        plan = FailoverPlan(
            old_commodore_id=old_commodore_id,
            new_commodore_id=result.winner_id,
            worker_assignments={
                uid: "continue" for uid in units
                if uid != result.winner_id and uid != old_commodore_id
            },
        )

        self._current_commodore_id = result.winner_id
        self.state = FailoverState.COMPLETED

        for cb in self._callbacks:
            try:
                cb(plan)
            except Exception:
                logger.exception("Failover callback error")

        return plan

    def reset(self) -> None:
        """Reset failover state."""
        self.state = FailoverState.STABLE
        self._current_commodore_id = None
        self._suspect_count = 0


# ---------------------------------------------------------------------------
# CommodoreProtocol — Main coordinator
# ---------------------------------------------------------------------------

class CommodoreProtocol:
    """Main protocol coordinator that ties everything together.

    Manages the fleet: election, heartbeat, load balancing, failover.
    """

    def __init__(
        self,
        local_unit_id: str,
        heartbeat_interval: float = 2.0,
        heartbeat_timeout: float = 6.0,
        election_timeout: float = 5.0,
        load_threshold: float = 80.0,
    ):
        self.local_unit_id = local_unit_id
        self.units: dict[str, Unit] = {}

        # Subsystems
        self.election = Election(timeout_seconds=election_timeout)
        hb_config = HeartbeatConfig(
            interval_seconds=heartbeat_interval,
            timeout_seconds=heartbeat_timeout,
        )
        self.heartbeat_monitor = HeartbeatMonitor(hb_config)
        self.capability_registry = CapabilityRegistry()
        self.load_balancer = LoadBalancer(max_load_threshold=load_threshold)
        self.failover_manager = FailoverManager()

        # Message history
        self._message_log: list[ProtocolMessage] = []

    # --- Fleet management ---

    def add_unit(self, unit: Unit) -> None:
        """Add a unit to the fleet."""
        self.units[unit.id] = unit
        self.capability_registry.register(unit.id, unit.capabilities)

    def remove_unit(self, unit_id: str) -> None:
        """Remove a unit from the fleet."""
        self.units.pop(unit_id, None)
        self.capability_registry.unregister(unit_id)

    def get_commodore(self) -> Unit | None:
        """Get the current commodore unit."""
        for u in self.units.values():
            if u.is_commodore:
                return u
        return None

    def get_workers(self) -> list[Unit]:
        """Get all worker units."""
        return [u for u in self.units.values() if u.is_worker and u.is_alive]

    def get_fleet_status(self) -> dict[str, Any]:
        """Get overall fleet status."""
        commodore = self.get_commodore()
        return {
            "fleet_size": len(self.units),
            "commodore_id": commodore.id if commodore else None,
            "workers": [u.id for u in self.get_workers()],
            "all_units": list(self.units.keys()),
            "election_state": self.election.state.value,
            "failover_state": self.failover_manager.state.value,
            "capabilities": sorted(self.capability_registry.all_capabilities()),
            "active_tasks": len(self.load_balancer.get_active_assignments()),
        }

    # --- Election ---

    def trigger_election(self, reason: str = "manual") -> ElectionResult:
        """Trigger a new leader election."""
        self.election.start_election(reason=reason)

        # Auto-vote for the best candidate
        best = self._pick_best_candidate()
        if best:
            self.election.cast_vote(self.local_unit_id, best.id)

        result = self.election.resolve(self.units)
        self._apply_election_result(result)
        return result

    def process_vote(self, voter_id: str, candidate_id: str) -> bool:
        """Process an incoming vote."""
        accepted = self.election.cast_vote(voter_id, candidate_id)
        if accepted and len(self.election.votes) >= len(self.units):
            result = self.election.resolve(self.units)
            self._apply_election_result(result)
        return accepted

    def _pick_best_candidate(self) -> Unit | None:
        """Pick the best candidate for commodore."""
        alive = [u for u in self.units.values() if u.is_alive]
        if not alive:
            return None
        return max(alive)

    def _apply_election_result(self, result: ElectionResult) -> None:
        """Apply election result: update roles."""
        for u in self.units.values():
            if u.id == result.winner_id:
                u.promote_to_commodore()
                self.failover_manager.set_commodore(u.id)
            else:
                u.demote_to_worker()

    # --- Heartbeat ---

    def send_heartbeat(self) -> CommodoreHeartbeat:
        """Generate a heartbeat message from the local unit."""
        unit = self.units.get(self.local_unit_id)
        if unit is None:
            unit = Unit(id=self.local_unit_id)

        return CommodoreHeartbeat(
            source_id=self.local_unit_id,
            role=unit.role.value,
            subordinates=[u.id for u in self.get_workers()],
            load=unit.load.to_dict(),
            capabilities=list(unit.capabilities),
        )

    def receive_heartbeat(self, msg: CommodoreHeartbeat) -> None:
        """Process an incoming heartbeat."""
        self.heartbeat_monitor.record_heartbeat(msg.source_id, msg.timestamp)
        unit = self.units.get(msg.source_id)
        if unit is not None:
            unit.update_heartbeat(msg.timestamp)

    def check_fleet_health(self) -> dict[str, HealthStatus]:
        """Check health of all units."""
        return self.heartbeat_monitor.check_health(self.units)

    # --- Work assignment ---

    def assign_work(
        self,
        task_id: str,
        task_type: str,
        required_capabilities: list[str] | None = None,
        priority: int = 0,
    ) -> WorkAssignment | None:
        """Assign work to a unit. Returns the assignment message or None."""
        unit_id = self.load_balancer.assign_task(
            task_id=task_id,
            task_type=task_type,
            units=self.units,
            registry=self.capability_registry,
            required_capabilities=required_capabilities,
            priority=priority,
        )
        if unit_id is None:
            return None

        return WorkAssignment(
            source_id=self.local_unit_id,
            task_id=task_id,
            task_type=task_type,
            target_id=unit_id,
            priority=priority,
            required_capabilities=required_capabilities or [],
        )

    def complete_work(self, task_id: str, result: dict | None = None) -> WorkComplete | None:
        """Mark work as completed. Returns completion message or None."""
        assignment = self.load_balancer.get_assignment(task_id)
        if assignment is None:
            return None

        self.load_balancer.complete_task(task_id)
        return WorkComplete(
            source_id=assignment.assigned_to,
            task_id=task_id,
            worker_id=assignment.assigned_to,
            result=result or {},
        )

    # --- Failover ---

    def check_failover(self) -> FailoverPlan | None:
        """Check if failover is needed and initiate if so."""
        commodore = self.get_commodore()
        if commodore is None:
            return None

        health = self.check_fleet_health()
        if health.get(commodore.id) in (HealthStatus.DEAD, HealthStatus.UNHEALTHY):
            should_failover = self.failover_manager.suspect_commodore()
            if should_failover:
                return self.failover_manager.initiate_failover(
                    self.units, self.election
                )
        else:
            self.failover_manager.reset_suspicions()

        return None

    # --- Message processing ---

    def process_message(self, msg: ProtocolMessage) -> Any:
        """Process an incoming protocol message. Returns response or None."""
        self._message_log.append(msg)

        if isinstance(msg, CommodoreHeartbeat):
            self.receive_heartbeat(msg)
        elif isinstance(msg, ElectionRequest):
            self.trigger_election(reason=msg.reason)
        elif isinstance(msg, ElectionVote):
            self.process_vote(msg.voter_id, msg.candidate_id)
        elif isinstance(msg, WorkComplete):
            self.load_balancer.complete_task(msg.task_id)
        elif isinstance(msg, CapabilityAnnounce):
            unit = self.units.get(msg.source_id)
            if unit:
                unit.capabilities = list(msg.capabilities)
                self.capability_registry.register(msg.source_id, msg.capabilities)
        elif isinstance(msg, DeferRequest):
            return self._handle_defer(msg)
        elif isinstance(msg, FailoverNotice):
            self._handle_failover_notice(msg)

        return None

    def _handle_defer(self, msg: DeferRequest) -> None:
        """Handle a worker deferring tasks."""
        pass  # In a real implementation, would reassign deferred tasks

    def _handle_failover_notice(self, msg: FailoverNotice) -> None:
        """Handle receiving a failover notice."""
        old_id = msg.old_commodore_id
        new_id = msg.new_commodore_id
        if old_id in self.units:
            self.units[old_id].demote_to_worker()
        if new_id in self.units:
            self.units[new_id].promote_to_commodore()
            self.failover_manager.set_commodore(new_id)

    # --- Scaling ---

    def check_scaling(self) -> ScaleSuggestion | None:
        """Check if scaling is recommended."""
        if self.load_balancer.should_scale_up(self.units):
            commodore = self.get_commodore()
            load = commodore.load.composite_load if commodore else 0.0
            # Suggest capability that's most loaded
            return ScaleSuggestion(
                source_id=self.local_unit_id,
                action="add",
                reason="Commodore load exceeds threshold",
                suggested_capability=self._most_needed_capability(),
                current_load=load,
            )
        return None

    def _most_needed_capability(self) -> str:
        """Determine which capability is most needed for scaling."""
        caps = self.capability_registry.all_capabilities()
        if not caps:
            return "general"
        # Return the capability with the fewest units
        cap_counts: dict[str, int] = {}
        for cap in caps:
            cap_counts[cap] = len(self.capability_registry.find_units_with(cap))
        return min(cap_counts, key=cap_counts.get)  # type: ignore[arg-type]

    # --- Serialization ---

    def to_dict(self) -> dict[str, Any]:
        """Serialize the protocol state."""
        return {
            "local_unit_id": self.local_unit_id,
            "units": {uid: u.to_dict() for uid, u in self.units.items()},
            "fleet_status": self.get_fleet_status(),
            "election_state": self.election.state.value,
            "failover_state": self.failover_manager.state.value,
            "active_assignments": len(self.load_balancer.get_active_assignments()),
        }
