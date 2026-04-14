"""Unit representation for the Commodore Protocol.

Each Unit in the fleet has an identity, role, capabilities, load metrics,
uptime tracking, and health status. Units are comparable for election ordering.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Role(str, Enum):
    """Role a unit can hold in the fleet."""
    COMMODORE = "commodore"
    WORKER = "worker"
    CANDIDATE = "candidate"


class HealthStatus(str, Enum):
    """Health status of a unit."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"
    DEAD = "dead"


@dataclass
class LoadMetrics:
    """Resource utilization metrics for a unit."""
    cpu: float = 0.0
    memory: float = 0.0
    gpu: float = 0.0
    network_in: float = 0.0
    network_out: float = 0.0
    task_queue_depth: int = 0

    @property
    def composite_load(self) -> float:
        """Return a composite load score (0-100) weighted by resource importance."""
        return (
            self.cpu * 0.35
            + self.memory * 0.25
            + self.gpu * 0.25
            + min(self.task_queue_depth * 5.0, 15.0)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "cpu": self.cpu,
            "memory": self.memory,
            "gpu": self.gpu,
            "network_in": self.network_in,
            "network_out": self.network_out,
            "task_queue_depth": self.task_queue_depth,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LoadMetrics:
        return cls(
            cpu=data.get("cpu", 0.0),
            memory=data.get("memory", 0.0),
            gpu=data.get("gpu", 0.0),
            network_in=data.get("network_in", 0.0),
            network_out=data.get("network_out", 0.0),
            task_queue_depth=data.get("task_queue_depth", 0),
        )


@dataclass
class Unit:
    """A single unit in the Pelagic fleet.

    Units are ordered for election purposes by:
    1. Human-designated priority (highest wins)
    2. Capability count (most capabilities wins)
    3. Longest uptime (most senior wins)
    4. Lowest unit_id lexicographically (tiebreaker)
    """

    id: str
    role: Role = Role.WORKER
    capabilities: list[str] = field(default_factory=list)
    load: LoadMetrics = field(default_factory=LoadMetrics)
    uptime_start: float = field(default_factory=time.time)
    health: HealthStatus = HealthStatus.UNKNOWN
    human_designated: bool = False
    priority: int = 0  # Higher = more preferred as commodore
    metadata: dict[str, Any] = field(default_factory=dict)
    last_heartbeat: float = 0.0

    # --- derived properties ---

    @property
    def uptime_seconds(self) -> float:
        """Seconds since this unit came online."""
        return time.time() - self.uptime_start

    @property
    def is_commodore(self) -> bool:
        return self.role == Role.COMMODORE

    @property
    def is_worker(self) -> bool:
        return self.role == Role.WORKER

    @property
    def is_alive(self) -> bool:
        return self.health not in (HealthStatus.DEAD, HealthStatus.UNHEALTHY)

    @property
    def capability_count(self) -> int:
        return len(self.capabilities)

    # --- election key ---

    def election_key(self) -> tuple:
        """Key for election ordering. Higher tuple = preferred."""
        return (
            self.human_designated,
            self.priority,
            self.capability_count,
            self.uptime_seconds,
            self.id,
        )

    def __lt__(self, other: Unit) -> bool:
        """Compare for election ordering. The 'winner' is the greatest element."""
        if not isinstance(other, Unit):
            return NotImplemented
        self_key = (self.human_designated, self.priority, self.capability_count,
                     self.uptime_seconds, self.id)
        other_key = (other.human_designated, other.priority, other.capability_count,
                     other.uptime_seconds, other.id)
        for s, o in zip(self_key[:4], other_key[:4]):
            if s != o:
                return s < o
        # Tiebreaker: lower id wins -> lower id is "greater" in election
        return self.id > other.id

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Unit):
            return NotImplemented
        return self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)

    def __repr__(self) -> str:
        cap_str = ",".join(self.capabilities) if self.capabilities else "none"
        return (f"Unit(id={self.id!r}, role={self.role.value}, "
                f"capabilities=[{cap_str}], load={self.load.composite_load:.1f}%, "
                f"health={self.health.value}, human={self.human_designated})")

    # --- serialization ---

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role.value,
            "capabilities": list(self.capabilities),
            "load": self.load.to_dict(),
            "uptime_start": self.uptime_start,
            "health": self.health.value,
            "human_designated": self.human_designated,
            "priority": self.priority,
            "metadata": dict(self.metadata),
            "last_heartbeat": self.last_heartbeat,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Unit:
        return cls(
            id=data["id"],
            role=Role(data.get("role", "worker")),
            capabilities=list(data.get("capabilities", [])),
            load=LoadMetrics.from_dict(data.get("load", {})),
            uptime_start=data.get("uptime_start", time.time()),
            health=HealthStatus(data.get("health", "unknown")),
            human_designated=data.get("human_designated", False),
            priority=data.get("priority", 0),
            metadata=dict(data.get("metadata", {})),
            last_heartbeat=data.get("last_heartbeat", 0.0),
        )

    def promote_to_commodore(self) -> None:
        """Promote this unit to commodore role."""
        self.role = Role.COMMODORE

    def demote_to_worker(self) -> None:
        """Demote this unit to worker role."""
        self.role = Role.WORKER

    def update_heartbeat(self, timestamp: float | None = None) -> None:
        """Record a heartbeat at the given time."""
        self.last_heartbeat = timestamp if timestamp is not None else time.time()
        if self.health == HealthStatus.UNKNOWN:
            self.health = HealthStatus.HEALTHY
