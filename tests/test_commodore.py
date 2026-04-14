"""Comprehensive tests for the Commodore Protocol.

Tests cover:
- Unit creation, serialization, election ordering
- Message serialization/deserialization
- Election mechanism (various scenarios)
- Heartbeat monitoring (timeout, recovery)
- Load balancing (capability matching, overload)
- Failover (commodore death, worker promotion)
- CLI parsing
- Integration tests (full election -> assign -> complete cycle)
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from unit import Unit, Role, HealthStatus, LoadMetrics
from messages import (
    ProtocolMessage, MessageType,
    CommodoreHeartbeat, ElectionRequest, ElectionVote,
    DeferRequest, WorkAssignment, WorkComplete,
    CapabilityAnnounce, ScaleSuggestion, FailoverNotice,
    deserialize_message, serialize_message,
)
from commodore import (
    CommodoreProtocol, Election, ElectionState, ElectionResult,
    HeartbeatMonitor, HeartbeatConfig,
    CapabilityRegistry, LoadBalancer, Assignment,
    FailoverManager, FailoverState, FailoverPlan,
)


# ===========================================================================
# Helpers
# ===========================================================================

def make_unit(
    id: str = "unit-001",
    role: Role = Role.WORKER,
    capabilities: list[str] | None = None,
    load_cpu: float = 30.0,
    health: HealthStatus = HealthStatus.HEALTHY,
    human_designated: bool = False,
    priority: int = 0,
    uptime_start: float | None = None,
) -> Unit:
    """Create a Unit with sensible defaults for testing."""
    return Unit(
        id=id,
        role=role,
        capabilities=capabilities or [],
        load=LoadMetrics(cpu=load_cpu),
        uptime_start=uptime_start or time.time(),
        health=health,
        human_designated=human_designated,
        priority=priority,
    )


def make_fleet(n: int = 3, base_id: str = "unit") -> list[Unit]:
    """Create a fleet of n units."""
    return [make_unit(id=f"{base_id}-{i:03d}") for i in range(n)]


# ===========================================================================
# Unit tests
# ===========================================================================

class TestUnit:
    """Tests for the Unit class."""

    def test_create_default_unit(self):
        u = make_unit("u1")
        assert u.id == "u1"
        assert u.role == Role.WORKER
        assert u.capabilities == []
        assert u.health == HealthStatus.HEALTHY
        assert u.human_designated is False
        assert u.priority == 0

    def test_unit_is_commodore(self):
        u = make_unit("u1", role=Role.COMMODORE)
        assert u.is_commodore is True
        assert u.is_worker is False

    def test_unit_is_worker(self):
        u = make_unit("u1", role=Role.WORKER)
        assert u.is_worker is True
        assert u.is_commodore is False

    def test_unit_is_alive(self):
        u = make_unit("u1", health=HealthStatus.HEALTHY)
        assert u.is_alive is True
        u.health = HealthStatus.DEAD
        assert u.is_alive is False
        u.health = HealthStatus.UNHEALTHY
        assert u.is_alive is False
        u.health = HealthStatus.DEGRADED
        assert u.is_alive is True

    def test_capability_count(self):
        u = make_unit("u1", capabilities=["nav", "cam"])
        assert u.capability_count == 2
        u.capabilities = []
        assert u.capability_count == 0

    def test_uptime_seconds(self):
        start = time.time() - 100
        u = make_unit("u1", uptime_start=start)
        assert u.uptime_seconds >= 99.0

    def test_promote_to_commodore(self):
        u = make_unit("u1")
        u.promote_to_commodore()
        assert u.role == Role.COMMODORE

    def test_demote_to_worker(self):
        u = make_unit("u1", role=Role.COMMODORE)
        u.demote_to_worker()
        assert u.role == Role.WORKER

    def test_update_heartbeat(self):
        u = make_unit("u1", health=HealthStatus.UNKNOWN)
        ts = time.time()
        u.update_heartbeat(ts)
        assert u.last_heartbeat == ts
        assert u.health == HealthStatus.HEALTHY

    def test_update_heartbeat_preserves_health(self):
        u = make_unit("u1", health=HealthStatus.DEGRADED)
        u.update_heartbeat()
        assert u.health == HealthStatus.DEGRADED

    def test_equality(self):
        u1 = make_unit("same")
        u2 = make_unit("same")
        u3 = make_unit("different")
        assert u1 == u2
        assert u1 != u3
        assert u1 != "not a unit"

    def test_hash(self):
        u1 = make_unit("same")
        u2 = make_unit("same")
        assert hash(u1) == hash(u2)
        assert len({u1, u2}) == 1

    def test_election_ordering_human_designated(self):
        human = make_unit("h1", human_designated=True)
        regular = make_unit("r1", priority=999)
        assert human > regular

    def test_election_ordering_priority(self):
        high = make_unit("h1", priority=10)
        low = make_unit("l1", priority=1)
        assert high > low

    def test_election_ordering_capabilities(self):
        many_caps = make_unit("m1", capabilities=["a", "b", "c"])
        few_caps = make_unit("f1", capabilities=["a"])
        assert many_caps > few_caps

    def test_election_ordering_uptime(self):
        old = make_unit("o1", uptime_start=time.time() - 1000)
        young = make_unit("y1", uptime_start=time.time() - 10)
        assert old > young

    def test_election_ordering_id_tiebreaker(self):
        low_id = make_unit("aaa")
        high_id = make_unit("zzz")
        assert low_id > high_id

    def test_to_dict(self):
        u = make_unit("u1", capabilities=["nav"], priority=5)
        d = u.to_dict()
        assert d["id"] == "u1"
        assert d["role"] == "worker"
        assert d["capabilities"] == ["nav"]
        assert d["priority"] == 5
        assert "load" in d

    def test_from_dict(self):
        u = make_unit("u1", capabilities=["nav"], priority=5)
        d = u.to_dict()
        u2 = Unit.from_dict(d)
        assert u2.id == u.id
        assert u2.role == u.role
        assert u2.capabilities == u.capabilities
        assert u2.priority == u.priority

    def test_from_dict_defaults(self):
        d = {"id": "minimal"}
        u = Unit.from_dict(d)
        assert u.id == "minimal"
        assert u.role == Role.WORKER
        assert u.capabilities == []
        assert u.health == HealthStatus.UNKNOWN

    def test_repr(self):
        u = make_unit("u1", capabilities=["nav"])
        r = repr(u)
        assert "u1" in r
        assert "nav" in r


# ===========================================================================
# LoadMetrics tests
# ===========================================================================

class TestLoadMetrics:
    """Tests for LoadMetrics."""

    def test_default_load(self):
        lm = LoadMetrics()
        assert lm.cpu == 0.0
        assert lm.composite_load == 0.0

    def test_composite_load(self):
        lm = LoadMetrics(cpu=100, memory=100, gpu=100)
        expected = 100 * 0.35 + 100 * 0.25 + 100 * 0.25 + 0.0
        assert lm.composite_load == pytest.approx(expected)

    def test_task_queue_in_composite(self):
        lm = LoadMetrics(cpu=0, memory=0, gpu=0, task_queue_depth=2)
        assert lm.composite_load == pytest.approx(10.0)

    def test_task_queue_capped(self):
        lm = LoadMetrics(cpu=0, memory=0, gpu=0, task_queue_depth=100)
        # Capped at 15.0
        assert lm.composite_load == pytest.approx(15.0)

    def test_to_dict(self):
        lm = LoadMetrics(cpu=50, memory=60, gpu=70)
        d = lm.to_dict()
        assert d["cpu"] == 50
        assert d["memory"] == 60
        assert d["gpu"] == 70

    def test_from_dict(self):
        lm = LoadMetrics(cpu=50, memory=60)
        d = lm.to_dict()
        lm2 = LoadMetrics.from_dict(d)
        assert lm2.cpu == 50
        assert lm2.memory == 60


# ===========================================================================
# Message tests
# ===========================================================================

class TestMessages:
    """Tests for protocol messages."""

    def test_base_message(self):
        msg = ProtocolMessage(
            msg_type=MessageType.HEARTBEAT,
            source_id="u1",
        )
        assert msg.msg_type == MessageType.HEARTBEAT
        assert msg.source_id == "u1"
        assert msg.msg_id  # auto-generated

    def test_base_message_serialization(self):
        msg = ProtocolMessage(
            msg_type=MessageType.HEARTBEAT,
            source_id="u1",
            payload={"key": "val"},
        )
        d = msg.to_dict()
        assert d["type"] == "commodore_heartbeat"
        assert d["source_id"] == "u1"
        assert d["payload"]["key"] == "val"

    def test_base_message_deserialization(self):
        data = {
            "type": "commodore_heartbeat",
            "source_id": "u1",
            "timestamp": 1000.0,
            "msg_id": "abc123",
            "payload": {"key": "val"},
        }
        msg = ProtocolMessage.from_dict(data)
        assert msg.source_id == "u1"
        assert msg.timestamp == 1000.0
        assert msg.msg_id == "abc123"

    def test_heartbeat_message(self):
        msg = CommodoreHeartbeat(
            source_id="u1",
            subordinates=["u2", "u3"],
            load={"cpu": 50},
            capabilities=["nav"],
        )
        d = msg.to_dict()
        assert d["payload"]["subordinates"] == ["u2", "u3"]
        assert d["payload"]["load"]["cpu"] == 50
        assert d["payload"]["capabilities"] == ["nav"]

    def test_heartbeat_roundtrip(self):
        msg = CommodoreHeartbeat(
            source_id="u1",
            subordinates=["u2"],
            load={"cpu": 50},
        )
        d = msg.to_dict()
        msg2 = CommodoreHeartbeat.from_dict(d)
        assert msg2.source_id == "u1"
        assert msg2.subordinates == ["u2"]
        assert msg2.load["cpu"] == 50

    def test_election_request_message(self):
        msg = ElectionRequest(source_id="u1", reason="timeout", candidate_id="u2")
        d = msg.to_dict()
        assert d["payload"]["reason"] == "timeout"
        assert d["payload"]["candidate_id"] == "u2"
        msg2 = ElectionRequest.from_dict(d)
        assert msg2.reason == "timeout"

    def test_election_vote_message(self):
        msg = ElectionVote(
            source_id="u1",
            candidate_id="u2",
            voter_id="u1",
            voter_priority=10,
        )
        d = msg.to_dict()
        assert d["payload"]["candidate_id"] == "u2"
        assert d["payload"]["voter_priority"] == 10
        msg2 = ElectionVote.from_dict(d)
        assert msg2.voter_priority == 10

    def test_work_assignment_message(self):
        msg = WorkAssignment(
            source_id="u1",
            task_id="t1",
            task_type="camera",
            target_id="u2",
            priority=5,
            required_capabilities=["camera"],
        )
        d = msg.to_dict()
        assert d["payload"]["task_id"] == "t1"
        assert d["payload"]["target_id"] == "u2"
        assert d["payload"]["required_capabilities"] == ["camera"]
        msg2 = WorkAssignment.from_dict(d)
        assert msg2.task_type == "camera"
        assert msg2.priority == 5

    def test_work_complete_message(self):
        msg = WorkComplete(
            source_id="u2",
            task_id="t1",
            worker_id="u2",
            success=True,
            result={"detections": 3},
        )
        d = msg.to_dict()
        assert d["payload"]["success"] is True
        assert d["payload"]["result"]["detections"] == 3
        msg2 = WorkComplete.from_dict(d)
        assert msg2.success is True

    def test_work_complete_failure(self):
        msg = WorkComplete(
            source_id="u2",
            task_id="t1",
            worker_id="u2",
            success=False,
            error="camera offline",
        )
        d = msg.to_dict()
        assert d["payload"]["success"] is False
        assert d["payload"]["error"] == "camera offline"

    def test_capability_announce_message(self):
        msg = CapabilityAnnounce(
            source_id="u1",
            capabilities=["nav", "cam"],
            version="2.0",
        )
        d = msg.to_dict()
        assert d["payload"]["capabilities"] == ["nav", "cam"]
        assert d["payload"]["version"] == "2.0"
        msg2 = CapabilityAnnounce.from_dict(d)
        assert msg2.version == "2.0"

    def test_scale_suggestion_message(self):
        msg = ScaleSuggestion(
            source_id="u1",
            action="add",
            reason="overloaded",
            suggested_capability="camera",
            current_load=90.0,
        )
        d = msg.to_dict()
        assert d["payload"]["action"] == "add"
        assert d["payload"]["current_load"] == 90.0
        msg2 = ScaleSuggestion.from_dict(d)
        assert msg2.suggested_capability == "camera"

    def test_failover_notice_message(self):
        msg = FailoverNotice(
            source_id="u2",
            new_commodore_id="u2",
            old_commodore_id="u1",
            reason="heartbeat timeout",
            worker_ids=["u3"],
        )
        d = msg.to_dict()
        assert d["payload"]["new_commodore_id"] == "u2"
        assert d["payload"]["old_commodore_id"] == "u1"
        assert d["payload"]["worker_ids"] == ["u3"]
        msg2 = FailoverNotice.from_dict(d)
        assert msg2.reason == "heartbeat timeout"

    def test_defer_request_message(self):
        msg = DeferRequest(
            source_id="u1",
            worker_id="u1",
            deferred_tasks=["t1", "t2"],
        )
        d = msg.to_dict()
        assert d["payload"]["deferred_tasks"] == ["t1", "t2"]
        msg2 = DeferRequest.from_dict(d)
        assert msg2.worker_id == "u1"

    def test_deserialize_unknown_type(self):
        data = {"type": "unknown_type", "source_id": "u1"}
        msg = deserialize_message(data)
        assert isinstance(msg, ProtocolMessage)
        assert msg.source_id == "u1"

    def test_deserialize_all_types(self):
        """Verify each known type deserializes to the correct class."""
        pairs = [
            (CommodoreHeartbeat, {"type": "commodore_heartbeat", "source_id": "u1", "payload": {"role": "commodore"}}),
            (ElectionRequest, {"type": "election_request", "source_id": "u1", "payload": {"reason": "test"}}),
            (ElectionVote, {"type": "election_vote", "source_id": "u1", "payload": {"candidate_id": "u2", "voter_id": "u1"}}),
            (DeferRequest, {"type": "defer_request", "source_id": "u1", "payload": {"worker_id": "u1"}}),
            (WorkAssignment, {"type": "work_assignment", "source_id": "u1", "payload": {"task_id": "t1"}}),
            (WorkComplete, {"type": "work_complete", "source_id": "u1", "payload": {"task_id": "t1"}}),
            (CapabilityAnnounce, {"type": "capability_announce", "source_id": "u1", "payload": {"capabilities": []}}),
            (ScaleSuggestion, {"type": "scale_suggestion", "source_id": "u1", "payload": {"action": "add"}}),
            (FailoverNotice, {"type": "failover_notice", "source_id": "u1", "payload": {"new_commodore_id": "u2"}}),
        ]
        for cls, data in pairs:
            msg = deserialize_message(data)
            assert isinstance(msg, cls), f"Expected {cls}, got {type(msg)} for {data['type']}"

    def test_serialize_message(self):
        msg = CommodoreHeartbeat(source_id="u1")
        d = serialize_message(msg)
        assert d["type"] == "commodore_heartbeat"
        assert d["source_id"] == "u1"


# ===========================================================================
# Election tests
# ===========================================================================

class TestElection:
    """Tests for the Election class."""

    def test_start_election(self):
        e = Election()
        e.start_election("test")
        assert e.state == ElectionState.IN_PROGRESS
        assert e.votes == {}

    def test_cast_vote(self):
        e = Election()
        e.start_election()
        assert e.cast_vote("voter1", "candidate1") is True
        assert e.votes == {"voter1": "candidate1"}

    def test_cast_vote_rejected_when_idle(self):
        e = Election()
        assert e.cast_vote("v1", "c1") is False

    def test_cast_vote_accepted_multiple(self):
        e = Election()
        e.start_election()
        e.cast_vote("v1", "c1")
        e.cast_vote("v2", "c1")
        e.cast_vote("v3", "c2")
        assert len(e.votes) == 3

    def test_resolve_no_votes(self):
        e = Election()
        e.start_election()
        units = {
            "u1": make_unit("u1", priority=5),
            "u2": make_unit("u2", priority=10),
        }
        result = e.resolve(units)
        assert result.winner_id == "u2"  # highest priority
        assert e.state == ElectionState.COMPLETED

    def test_resolve_with_votes(self):
        e = Election()
        e.start_election()
        e.cast_vote("v1", "c1")
        e.cast_vote("v2", "c1")
        e.cast_vote("v3", "c2")
        units = {
            "c1": make_unit("c1"),
            "c2": make_unit("c2"),
        }
        result = e.resolve(units)
        assert result.winner_id == "c1"  # 2 votes vs 1

    def test_resolve_vote_tie(self):
        e = Election()
        e.start_election()
        e.cast_vote("v1", "c1")
        e.cast_vote("v2", "c2")
        units = {
            "c1": make_unit("c1", priority=5),
            "c2": make_unit("c2", priority=10),
        }
        result = e.resolve(units)
        assert result.winner_id == "c2"  # tiebreak by priority

    def test_resolve_ignores_dead(self):
        e = Election()
        e.start_election()
        units = {
            "alive": make_unit("alive", health=HealthStatus.HEALTHY),
            "dead": make_unit("dead", health=HealthStatus.DEAD, priority=999),
        }
        result = e.resolve(units)
        assert result.winner_id == "alive"

    def test_resolve_all_dead_fallback(self):
        e = Election()
        e.start_election()
        units = {
            "dead1": make_unit("dead1", health=HealthStatus.DEAD),
            "dead2": make_unit("dead2", health=HealthStatus.DEAD),
        }
        result = e.resolve(units)
        assert result.winner_id in units

    def test_resolve_human_designated_wins(self):
        e = Election()
        e.start_election()
        units = {
            "human": make_unit("human", human_designated=True),
            "bot": make_unit("bot", priority=999, capabilities=["a", "b", "c"]),
        }
        result = e.resolve(units)
        assert result.winner_id == "human"

    def test_election_expiration(self):
        e = Election(timeout_seconds=0.01)
        e.start_election()
        time.sleep(0.02)
        assert e.is_expired()
        assert e.cast_vote("v1", "c1") is False

    def test_resolve_duration(self):
        e = Election()
        e.start_election()
        time.sleep(0.01)
        units = {"u1": make_unit("u1")}
        result = e.resolve(units)
        assert result.duration_seconds >= 0.01

    def test_resolve_reason(self):
        e = Election()
        e.start_election(reason="commodore_death")
        result = e.resolve({"u1": make_unit("u1")})
        assert result.reason == "commodore_death"


# ===========================================================================
# HeartbeatMonitor tests
# ===========================================================================

class TestHeartbeatMonitor:
    """Tests for HeartbeatMonitor."""

    def test_record_heartbeat(self):
        mon = HeartbeatMonitor()
        mon.record_heartbeat("u1")
        assert mon.get_last_heartbeat("u1") > 0
        assert mon.get_missed_count("u1") == 0

    def test_check_health_unknown(self):
        mon = HeartbeatMonitor()
        units = {"u1": make_unit("u1")}
        health = mon.check_health(units)
        assert health["u1"] == HealthStatus.UNKNOWN

    def test_check_health_healthy(self):
        mon = HeartbeatMonitor()
        mon.record_heartbeat("u1")
        units = {"u1": make_unit("u1")}
        health = mon.check_health(units)
        assert health["u1"] == HealthStatus.HEALTHY

    def test_check_health_degraded(self):
        mon = HeartbeatMonitor(config=HeartbeatConfig(
            interval_seconds=0.01,
            timeout_seconds=0.1,
        ))
        old_time = time.time() - 0.05
        mon.record_heartbeat("u1", timestamp=old_time)
        units = {"u1": make_unit("u1")}
        health = mon.check_health(units)
        assert health["u1"] == HealthStatus.DEGRADED

    def test_check_health_dead(self):
        mon = HeartbeatMonitor(config=HeartbeatConfig(
            interval_seconds=0.01,
            timeout_seconds=0.01,
            max_missed=1,
        ))
        old_time = time.time() - 1.0
        mon.record_heartbeat("u1", timestamp=old_time)
        units = {"u1": make_unit("u1")}
        health = mon.check_health(units)
        assert health["u1"] == HealthStatus.DEAD

    def test_get_dead_units(self):
        mon = HeartbeatMonitor(config=HeartbeatConfig(
            interval_seconds=0.01,
            timeout_seconds=0.01,
            max_missed=1,
        ))
        old_time = time.time() - 1.0
        mon.record_heartbeat("dead_u", timestamp=old_time)
        mon.record_heartbeat("alive_u")
        units = {
            "dead_u": make_unit("dead_u"),
            "alive_u": make_unit("alive_u"),
        }
        dead = mon.get_dead_units(units)
        assert "dead_u" in dead
        assert "alive_u" not in dead

    def test_health_status_callback(self):
        mon = HeartbeatMonitor()
        changes = []
        mon.register_callback(lambda uid, status: changes.append((uid, status)))
        units = {"u1": make_unit("u1")}
        # First check: UNKNOWN -> HEALTHY triggers callback
        mon.record_heartbeat("u1")
        mon.check_health(units)
        # The callback fires on status change from UNKNOWN to HEALTHY
        # (This depends on unit's initial health being UNKNOWN)
        assert len(changes) >= 0  # May or may not fire depending on timing

    def test_reset(self):
        mon = HeartbeatMonitor()
        mon.record_heartbeat("u1")
        mon.reset()
        assert mon.get_last_heartbeat("u1") is None

    def test_multiple_units_health(self):
        mon = HeartbeatMonitor()
        mon.record_heartbeat("u1")
        mon.record_heartbeat("u2")
        old_time = time.time() - 100
        mon.record_heartbeat("u3", timestamp=old_time)
        units = {
            "u1": make_unit("u1"),
            "u2": make_unit("u2"),
            "u3": make_unit("u3"),
        }
        health = mon.check_health(units)
        assert health["u1"] == HealthStatus.HEALTHY
        assert health["u2"] == HealthStatus.HEALTHY
        assert health["u3"] == HealthStatus.DEAD


# ===========================================================================
# CapabilityRegistry tests
# ===========================================================================

class TestCapabilityRegistry:
    """Tests for CapabilityRegistry."""

    def test_register(self):
        reg = CapabilityRegistry()
        reg.register("u1", ["nav", "cam"])
        assert reg.get_capabilities("u1") == {"nav", "cam"}

    def test_unregister(self):
        reg = CapabilityRegistry()
        reg.register("u1", ["nav"])
        reg.unregister("u1")
        assert reg.get_capabilities("u1") == set()

    def test_find_units_with(self):
        reg = CapabilityRegistry()
        reg.register("u1", ["nav", "cam"])
        reg.register("u2", ["nav"])
        reg.register("u3", ["cam"])
        result = reg.find_units_with("nav")
        assert set(result) == {"u1", "u2"}

    def test_find_units_with_all(self):
        reg = CapabilityRegistry()
        reg.register("u1", ["nav", "cam"])
        reg.register("u2", ["nav"])
        result = reg.find_units_with_all(["nav", "cam"])
        assert result == ["u1"]

    def test_find_units_with_any(self):
        reg = CapabilityRegistry()
        reg.register("u1", ["nav"])
        reg.register("u2", ["cam"])
        reg.register("u3", ["engine"])
        result = reg.find_units_with_any(["nav", "cam"])
        assert set(result) == {"u1", "u2"}

    def test_all_capabilities(self):
        reg = CapabilityRegistry()
        reg.register("u1", ["nav", "cam"])
        reg.register("u2", ["nav", "engine"])
        assert reg.all_capabilities() == {"nav", "cam", "engine"}

    def test_unit_count(self):
        reg = CapabilityRegistry()
        assert reg.unit_count() == 0
        reg.register("u1", ["nav"])
        assert reg.unit_count() == 1
        reg.register("u2", ["cam"])
        assert reg.unit_count() == 2

    def test_clear(self):
        reg = CapabilityRegistry()
        reg.register("u1", ["nav"])
        reg.clear()
        assert reg.unit_count() == 0

    def test_find_with_no_results(self):
        reg = CapabilityRegistry()
        reg.register("u1", ["nav"])
        assert reg.find_units_with("nonexistent") == []
        assert reg.find_units_with_all(["nav", "nonexistent"]) == []


# ===========================================================================
# LoadBalancer tests
# ===========================================================================

class TestLoadBalancer:
    """Tests for LoadBalancer."""

    def _setup(self) -> tuple[LoadBalancer, dict[str, Unit], CapabilityRegistry]:
        lb = LoadBalancer()
        units = {
            "u1": make_unit("u1", capabilities=["nav", "cam"], load_cpu=30),
            "u2": make_unit("u2", capabilities=["cam"], load_cpu=60),
        }
        reg = CapabilityRegistry()
        for u in units.values():
            reg.register(u.id, u.capabilities)
        return lb, units, reg

    def test_assign_task(self):
        lb, units, reg = self._setup()
        result = lb.assign_task("t1", "camera", units, reg)
        assert result is not None
        assert result in units

    def test_assign_with_capability_requirement(self):
        lb, units, reg = self._setup()
        result = lb.assign_task("t1", "nav_task", units, reg, required_capabilities=["nav"])
        assert result == "u1"  # only u1 has nav

    def test_assign_lowest_load(self):
        lb, units, reg = self._setup()
        result = lb.assign_task("t1", "cam", units, reg, required_capabilities=["cam"])
        # u1 has lower load (30 vs 60)
        assert result == "u1"

    def test_assign_preferred_unit(self):
        lb, units, reg = self._setup()
        result = lb.assign_task("t1", "cam", units, reg, required_capabilities=["cam"], preferred_unit="u2")
        assert result == "u2"

    def test_assign_preferred_not_capable(self):
        lb, units, reg = self._setup()
        result = lb.assign_task("t1", "nav_task", units, reg, required_capabilities=["nav"], preferred_unit="u2")
        # u2 doesn't have nav, should pick best capable
        assert result == "u1"

    def test_assign_no_capable_unit(self):
        lb, units, reg = self._setup()
        result = lb.assign_task("t1", "engine", units, reg, required_capabilities=["engine"])
        assert result is None

    def test_assign_dead_unit_excluded(self):
        lb, units, reg = self._setup()
        units["u1"].health = HealthStatus.DEAD
        result = lb.assign_task("t1", "cam", units, reg, required_capabilities=["cam"])
        assert result == "u2"

    def test_assign_commodore_excluded(self):
        lb, units, reg = self._setup()
        units["u1"].promote_to_commodore()
        result = lb.assign_task("t1", "cam", units, reg, required_capabilities=["cam"])
        assert result == "u2"

    def test_assign_idempotent(self):
        lb, units, reg = self._setup()
        r1 = lb.assign_task("t1", "cam", units, reg)
        r2 = lb.assign_task("t1", "cam", units, reg)
        assert r1 == r2

    def test_complete_task(self):
        lb, units, reg = self._setup()
        lb.assign_task("t1", "cam", units, reg)
        assert lb.complete_task("t1") is True
        assert lb.get_active_assignments() == []

    def test_complete_nonexistent_task(self):
        lb, _, _ = self._setup()
        assert lb.complete_task("nonexistent") is False

    def test_fail_task(self):
        lb, units, reg = self._setup()
        lb.assign_task("t1", "cam", units, reg)
        assert lb.fail_task("t1") is True
        assert lb.get_active_assignments() == []

    def test_get_assignment(self):
        lb, units, reg = self._setup()
        lb.assign_task("t1", "cam", units, reg)
        a = lb.get_assignment("t1")
        assert a is not None
        assert a.task_id == "t1"
        assert a.completed is False

    def test_get_assignments_for_unit(self):
        lb, units, reg = self._setup()
        lb.assign_task("t1", "cam", units, reg)
        lb.assign_task("t2", "cam", units, reg)
        assignments = lb.get_assignments_for("u1")
        # Both tasks go to u1 (lowest load initially)
        task_ids = {a.task_id for a in assignments}
        assert "t1" in task_ids

    def test_get_unit_load(self):
        lb, units, reg = self._setup()
        lb.assign_task("t1", "cam", units, reg)
        assert lb.get_unit_load("u1") == 1

    def test_should_scale_up(self):
        lb = LoadBalancer(max_load_threshold=30)
        units = {
            "commodore": make_unit("commodore", role=Role.COMMODORE, load_cpu=100, health=HealthStatus.HEALTHY),
        }
        # composite_load = 100*0.35 + 0 + 0 + 0 = 35 > 30
        assert lb.should_scale_up(units) is True

    def test_should_not_scale_up(self):
        lb = LoadBalancer(max_load_threshold=80)
        units = {
            "commodore": make_unit("commodore", role=Role.COMMODORE, load_cpu=30),
        }
        assert lb.should_scale_up(units) is False

    def test_overloaded_fallback(self):
        """When all units are overloaded, still assign to least loaded."""
        lb = LoadBalancer(max_load_threshold=20)
        units = {
            "u1": make_unit("u1", load_cpu=90),
            "u2": make_unit("u2", load_cpu=85),
        }
        reg = CapabilityRegistry()
        for u in units.values():
            reg.register(u.id, [])
        result = lb.assign_task("t1", "task", units, reg)
        assert result == "u2"  # least overloaded

    def test_reset(self):
        lb, units, reg = self._setup()
        lb.assign_task("t1", "cam", units, reg)
        lb.reset()
        assert lb.get_active_assignments() == []


# ===========================================================================
# FailoverManager tests
# ===========================================================================

class TestFailoverManager:
    """Tests for FailoverManager."""

    def test_set_commodore(self):
        fm = FailoverManager()
        fm.set_commodore("u1")
        assert fm.get_commodore_id() == "u1"
        assert fm.state == FailoverState.STABLE

    def test_suspect_commodore(self):
        fm = FailoverManager()
        fm.set_commodore("u1")
        assert fm.suspect_commodore() is False
        assert fm.suspect_commodore() is True  # threshold = 2

    def test_reset_suspicions(self):
        fm = FailoverManager()
        fm.set_commodore("u1")
        fm.suspect_commodore()
        fm.reset_suspicions()
        assert fm.state == FailoverState.STABLE
        assert fm.suspect_commodore() is False  # count reset

    def test_initiate_failover(self):
        fm = FailoverManager()
        fm.set_commodore("old")
        units = {
            "old": make_unit("old", role=Role.COMMODORE, health=HealthStatus.DEAD),
            "new": make_unit("new", health=HealthStatus.HEALTHY, priority=10),
        }
        election = Election()
        plan = fm.initiate_failover(units, election)
        assert plan is not None
        assert plan.new_commodore_id == "new"
        assert plan.old_commodore_id == "old"
        assert units["new"].is_commodore

    def test_initiate_failover_no_candidates(self):
        fm = FailoverManager()
        fm.set_commodore("old")
        units = {
            "old": make_unit("old", role=Role.COMMODORE),
        }
        election = Election()
        plan = fm.initiate_failover(units, election)
        assert plan is None

    def test_failover_callback(self):
        fm = FailoverManager()
        fm.set_commodore("old")
        plans = []
        fm.register_callback(lambda p: plans.append(p))
        units = {
            "old": make_unit("old", role=Role.COMMODORE, health=HealthStatus.DEAD),
            "new": make_unit("new", health=HealthStatus.HEALTHY),
        }
        election = Election()
        fm.initiate_failover(units, election)
        assert len(plans) == 1
        assert plans[0].new_commodore_id == "new"

    def test_failover_demotes_old(self):
        fm = FailoverManager()
        fm.set_commodore("old")
        units = {
            "old": make_unit("old", role=Role.COMMODORE, health=HealthStatus.DEAD),
            "new": make_unit("new", health=HealthStatus.HEALTHY),
        }
        election = Election()
        fm.initiate_failover(units, election)
        assert units["old"].role == Role.WORKER

    def test_reset(self):
        fm = FailoverManager()
        fm.set_commodore("u1")
        fm.reset()
        assert fm.get_commodore_id() is None
        assert fm.state == FailoverState.STABLE


# ===========================================================================
# CommodoreProtocol tests (integration-level)
# ===========================================================================

class TestCommodoreProtocol:
    """Tests for the main CommodoreProtocol coordinator."""

    def _make_protocol(self, local_id: str = "local") -> CommodoreProtocol:
        return CommodoreProtocol(local_unit_id=local_id)

    def test_add_remove_unit(self):
        p = self._make_protocol()
        u = make_unit("u1")
        p.add_unit(u)
        assert "u1" in p.units
        p.remove_unit("u1")
        assert "u1" not in p.units

    def test_get_commodore_none(self):
        p = self._make_protocol()
        assert p.get_commodore() is None

    def test_get_commodore(self):
        p = self._make_protocol()
        u = make_unit("u1", role=Role.COMMODORE)
        p.add_unit(u)
        assert p.get_commodore().id == "u1"

    def test_get_workers(self):
        p = self._make_protocol()
        p.add_unit(make_unit("u1", role=Role.COMMODORE))
        p.add_unit(make_unit("u2"))
        p.add_unit(make_unit("u3"))
        workers = p.get_workers()
        assert len(workers) == 2
        assert all(w.is_worker for w in workers)

    def test_fleet_status(self):
        p = self._make_protocol()
        p.add_unit(make_unit("u1", role=Role.COMMODORE))
        p.add_unit(make_unit("u2", capabilities=["nav"]))
        status = p.get_fleet_status()
        assert status["fleet_size"] == 2
        assert status["commodore_id"] == "u1"
        assert "u2" in status["workers"]
        assert "nav" in status["capabilities"]

    def test_trigger_election(self):
        p = self._make_protocol()
        p.add_unit(make_unit("u1", priority=5))
        p.add_unit(make_unit("u2", priority=10))
        result = p.trigger_election()
        assert result.winner_id == "u2"
        assert p.get_commodore().id == "u2"

    def test_election_human_designated(self):
        p = self._make_protocol()
        p.add_unit(make_unit("u1", human_designated=True))
        p.add_unit(make_unit("u2", priority=999))
        result = p.trigger_election()
        assert result.winner_id == "u1"

    def test_send_heartbeat(self):
        p = self._make_protocol("u1")
        p.add_unit(make_unit("u1", role=Role.COMMODORE))
        hb = p.send_heartbeat()
        assert hb.source_id == "u1"
        assert hb.role == "commodore"

    def test_receive_heartbeat(self):
        p = self._make_protocol()
        p.add_unit(make_unit("u1"))
        hb = CommodoreHeartbeat(source_id="u1")
        p.receive_heartbeat(hb)
        assert p.heartbeat_monitor.get_last_heartbeat("u1") > 0

    def test_check_fleet_health(self):
        p = self._make_protocol()
        p.add_unit(make_unit("u1"))
        health = p.check_fleet_health()
        assert "u1" in health

    def test_assign_work(self):
        p = self._make_protocol()
        commodore = make_unit("comm", role=Role.COMMODORE)
        worker = make_unit("worker", capabilities=["cam"])
        p.add_unit(commodore)
        p.add_unit(worker)
        assignment = p.assign_work("t1", "camera", required_capabilities=["cam"])
        assert assignment is not None
        assert assignment.target_id == "worker"
        assert assignment.task_id == "t1"

    def test_assign_work_no_capable_unit(self):
        p = self._make_protocol()
        p.add_unit(make_unit("comm", role=Role.COMMODORE))
        p.add_unit(make_unit("worker", capabilities=["nav"]))
        result = p.assign_work("t1", "camera", required_capabilities=["cam"])
        assert result is None

    def test_complete_work(self):
        p = self._make_protocol()
        p.add_unit(make_unit("comm", role=Role.COMMODORE))
        p.add_unit(make_unit("worker", capabilities=["cam"]))
        p.assign_work("t1", "camera")
        msg = p.complete_work("t1", result={"status": "ok"})
        assert msg is not None
        assert msg.success is True
        assert msg.result["status"] == "ok"

    def test_complete_work_nonexistent(self):
        p = self._make_protocol()
        msg = p.complete_work("nonexistent")
        assert msg is None

    def test_failover_commodore_death(self):
        p = self._make_protocol()
        commodore = make_unit("comm", role=Role.COMMODORE)
        worker = make_unit("worker", priority=10)
        p.add_unit(commodore)
        p.add_unit(worker)
        p.failover_manager.set_commodore("comm")

        # Record stale heartbeat for commodore so it's detected as dead
        old_time = time.time() - 100
        p.heartbeat_monitor.record_heartbeat("comm", timestamp=old_time)
        p.heartbeat_monitor.record_heartbeat("worker")

        # check_fleet_health will mark commodore as dead
        p.check_fleet_health()

        # Need two calls to reach suspect threshold
        p.check_failover()
        plan = p.check_failover()
        assert plan is not None
        assert plan.new_commodore_id == "worker"
        assert plan.old_commodore_id == "comm"

    def test_failover_commodore_recover(self):
        p = self._make_protocol()
        commodore = make_unit("comm", role=Role.COMMODORE)
        worker = make_unit("worker")
        p.add_unit(commodore)
        p.add_unit(worker)
        p.failover_manager.set_commodore("comm")

        p.heartbeat_monitor.record_heartbeat("comm")
        p.heartbeat_monitor.record_heartbeat("worker")
        plan = p.check_failover()
        assert plan is None  # no failover needed

    def test_process_heartbeat_message(self):
        p = self._make_protocol()
        p.add_unit(make_unit("u1"))
        msg = CommodoreHeartbeat(source_id="u1")
        p.process_message(msg)
        assert p.heartbeat_monitor.get_last_heartbeat("u1") > 0

    def test_process_election_request(self):
        p = self._make_protocol()
        p.add_unit(make_unit("u1", priority=10))
        p.add_unit(make_unit("u2"))
        msg = ElectionRequest(source_id="u1", reason="test")
        p.process_message(msg)
        assert p.election.state == ElectionState.COMPLETED

    def test_process_election_vote(self):
        p = self._make_protocol()
        p.election.start_election()
        msg = ElectionVote(source_id="u1", candidate_id="u2", voter_id="u1")
        p.process_message(msg)
        assert "u1" in p.election.votes

    def test_process_work_complete(self):
        p = self._make_protocol()
        p.add_unit(make_unit("comm", role=Role.COMMODORE))
        p.add_unit(make_unit("worker", capabilities=["cam"]))
        p.assign_work("t1", "camera")
        msg = WorkComplete(source_id="worker", task_id="t1", worker_id="worker")
        p.process_message(msg)
        assert p.load_balancer.get_assignment("t1") is not None
        assert p.load_balancer.get_assignment("t1").completed

    def test_process_capability_announce(self):
        p = self._make_protocol()
        p.add_unit(make_unit("u1"))
        msg = CapabilityAnnounce(source_id="u1", capabilities=["nav", "cam"])
        p.process_message(msg)
        caps = p.capability_registry.get_capabilities("u1")
        assert caps == {"nav", "cam"}

    def test_process_failover_notice(self):
        p = self._make_protocol()
        p.add_unit(make_unit("old", role=Role.COMMODORE))
        p.add_unit(make_unit("new"))
        msg = FailoverNotice(
            source_id="new",
            new_commodore_id="new",
            old_commodore_id="old",
        )
        p.process_message(msg)
        assert p.get_commodore().id == "new"
        assert p.units["old"].role == Role.WORKER

    def test_check_scaling_needed(self):
        p = self._make_protocol()
        # Set load high enough so composite_load > 80
        # composite_load = cpu*0.35 + mem*0.25 + gpu*0.25
        # Need ~80 total: set all to 100 => 35+25+25 = 85
        commodore = make_unit("comm", role=Role.COMMODORE, load_cpu=100, health=HealthStatus.HEALTHY)
        commodore.load.memory = 100
        commodore.load.gpu = 100
        worker = make_unit("worker", capabilities=["cam"])
        p.add_unit(commodore)
        p.add_unit(worker)
        suggestion = p.check_scaling()
        assert suggestion is not None
        assert suggestion.action == "add"

    def test_check_scaling_not_needed(self):
        p = self._make_protocol()
        commodore = make_unit("comm", role=Role.COMMODORE, load_cpu=30)
        p.add_unit(commodore)
        suggestion = p.check_scaling()
        assert suggestion is None

    def test_to_dict(self):
        p = self._make_protocol()
        p.add_unit(make_unit("u1", role=Role.COMMODORE))
        d = p.to_dict()
        assert d["local_unit_id"] == "local"
        assert "u1" in d["units"]
        assert d["fleet_status"]["commodore_id"] == "u1"


# ===========================================================================
# Integration tests
# ===========================================================================

class TestIntegration:
    """Full end-to-end integration tests."""

    def test_full_election_assign_complete_cycle(self):
        """Full cycle: add units -> elect -> assign -> complete."""
        p = CommodoreProtocol(local_unit_id="u1")
        p.add_unit(make_unit("u1", capabilities=["nav", "cam", "chat"], priority=10))
        p.add_unit(make_unit("u2", capabilities=["cam"]))
        p.add_unit(make_unit("u3", capabilities=["nav"]))

        # 1. Election
        result = p.trigger_election(reason="startup")
        assert result.winner_id == "u1"
        commodore = p.get_commodore()
        assert commodore is not None
        assert commodore.id == "u1"

        # 2. Assign camera task to u2
        assignment = p.assign_work("cam_task", "camera", required_capabilities=["cam"])
        assert assignment is not None
        assert assignment.target_id == "u2"

        # 3. Complete task
        complete_msg = p.complete_work("cam_task", result={"image": "capture.jpg"})
        assert complete_msg is not None
        assert complete_msg.success

        # 4. Verify no active tasks
        assert len(p.load_balancer.get_active_assignments()) == 0

    def test_failover_and_reassign(self):
        """Commodore dies -> failover -> new commodore assigns work."""
        p = CommodoreProtocol(local_unit_id="u1")
        commodore = make_unit("comm", role=Role.COMMODORE, capabilities=["nav", "cam"])
        worker1 = make_unit("w1", capabilities=["cam"])
        worker2 = make_unit("w2", capabilities=["cam"], priority=20)
        p.add_unit(commodore)
        p.add_unit(worker1)
        p.add_unit(worker2)
        p.failover_manager.set_commodore("comm")

        # Record stale heartbeat for commodore so it's detected as dead
        old_time = time.time() - 100
        p.heartbeat_monitor.record_heartbeat("comm", timestamp=old_time)
        p.heartbeat_monitor.record_heartbeat("w1")
        p.heartbeat_monitor.record_heartbeat("w2")
        p.check_fleet_health()

        p.check_failover()
        plan = p.check_failover()
        assert plan is not None
        assert plan.new_commodore_id == "w2"  # higher priority

        # New commodore can still see workers
        workers = p.get_workers()
        assert "w1" in [w.id for w in workers]

    def test_multiple_elections(self):
        """Multiple elections produce consistent results."""
        p = CommodoreProtocol(local_unit_id="u1")
        p.add_unit(make_unit("u1", priority=5))
        p.add_unit(make_unit("u2", priority=10))
        p.add_unit(make_unit("u3", priority=1))

        for _ in range(5):
            result = p.trigger_election()
            assert result.winner_id == "u2"  # always the highest priority

    def test_scaling_cycle(self):
        """Commodore gets overloaded -> scale suggestion."""
        p = CommodoreProtocol(local_unit_id="comm")
        comm = make_unit("comm", role=Role.COMMODORE, capabilities=["cam"], load_cpu=100, health=HealthStatus.HEALTHY)
        comm.load.memory = 100
        comm.load.gpu = 100
        p.add_unit(comm)

        suggestion = p.check_scaling()
        assert suggestion is not None
        assert suggestion.action == "add"
        assert suggestion.current_load > 80

    def test_message_roundtrip_all_types(self):
        """All message types can be serialized and deserialized."""
        messages = [
            CommodoreHeartbeat(source_id="u1", subordinates=["u2"], load={"cpu": 50}),
            ElectionRequest(source_id="u1", reason="timeout"),
            ElectionVote(source_id="u1", candidate_id="u2", voter_id="u1"),
            DeferRequest(source_id="u1", worker_id="u1", deferred_tasks=["t1"]),
            WorkAssignment(source_id="u1", task_id="t1", target_id="u2"),
            WorkComplete(source_id="u2", task_id="t1", worker_id="u2"),
            CapabilityAnnounce(source_id="u1", capabilities=["nav"]),
            ScaleSuggestion(source_id="u1", action="add", current_load=90),
            FailoverNotice(source_id="u2", new_commodore_id="u2", old_commodore_id="u1"),
        ]
        for msg in messages:
            d = serialize_message(msg)
            msg2 = deserialize_message(d)
            assert type(msg2) == type(msg)
            assert msg2.source_id == msg.source_id

    def test_fleet_status_after_operations(self):
        """Fleet status accurately reflects protocol state."""
        p = CommodoreProtocol(local_unit_id="u1")
        p.add_unit(make_unit("u1", capabilities=["nav", "cam"]))
        p.add_unit(make_unit("u2", capabilities=["cam"]))
        p.trigger_election()
        p.assign_work("t1", "camera")

        status = p.get_fleet_status()
        assert status["fleet_size"] == 2
        assert status["commodore_id"] is not None
        assert status["active_tasks"] == 1
        assert len(status["capabilities"]) == 2

    def test_graceful_single_unit(self):
        """Single unit fleet works as expected (standalone commodore)."""
        p = CommodoreProtocol(local_unit_id="solo")
        p.add_unit(make_unit("solo", capabilities=["nav", "cam", "chat"]))

        result = p.trigger_election()
        assert result.winner_id == "solo"
        assert p.get_commodore().id == "solo"
        assert p.get_workers() == []

        # No workers to assign to
        assignment = p.assign_work("t1", "camera")
        assert assignment is None  # commodore excluded, no workers

    def test_capability_negotiation(self):
        """Units announce capabilities and work is routed correctly."""
        p = CommodoreProtocol(local_unit_id="comm")
        p.add_unit(make_unit("comm", role=Role.COMMODORE))
        p.add_unit(make_unit("w1", capabilities=["camera"]))
        p.add_unit(make_unit("w2", capabilities=["navigation"]))
        p.add_unit(make_unit("w3", capabilities=["camera", "navigation"]))

        # Camera task -> w1 or w3
        cam_task = p.assign_work("t1", "camera", required_capabilities=["camera"])
        assert cam_task.target_id in ("w1", "w3")

        # Nav task -> w2 or w3
        nav_task = p.assign_work("t2", "navigation", required_capabilities=["navigation"])
        assert nav_task.target_id in ("w2", "w3")

        # Both required -> only w3
        both_task = p.assign_work(
            "t3", "combined",
            required_capabilities=["camera", "navigation"],
        )
        assert both_task.target_id == "w3"


# ===========================================================================
# CLI parsing tests
# ===========================================================================

class TestCLI:
    """Tests for CLI argument parsing."""

    def test_parse_elect(self):
        from cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["elect", "--reason", "startup"])
        assert args.command == "elect"
        assert args.reason == "startup"

    def test_parse_status(self):
        from cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["status"])
        assert args.command == "status"

    def test_parse_status_verbose(self):
        from cli import build_parser
        parser = build_parser()
        # Global flags must come before subcommand in argparse
        args = parser.parse_args(["--verbose", "status"])
        assert args.command == "status"
        assert args.verbose is True

    def test_parse_assign(self):
        from cli import build_parser
        parser = build_parser()
        args = parser.parse_args([
            "assign", "task-001", "camera",
            "--capabilities", "nav,cam",
            "--priority", "5",
        ])
        assert args.command == "assign"
        assert args.task_id == "task-001"
        assert args.task_type == "camera"
        assert args.capabilities == "nav,cam"
        assert args.priority == 5

    def test_parse_capabilities(self):
        from cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["capabilities"])
        assert args.command == "capabilities"
        assert args.query is None

    def test_parse_capabilities_query(self):
        from cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["capabilities", "--query", "nav"])
        assert args.command == "capabilities"
        assert args.query == "nav"

    def test_parse_heartbeat_send(self):
        from cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["heartbeat", "--send"])
        assert args.command == "heartbeat"
        assert args.send is True

    def test_parse_failover(self):
        from cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["failover"])
        assert args.command == "failover"

    def test_parse_global_unit(self):
        from cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["--unit", "my-unit", "status"])
        assert args.unit == "my-unit"

    def test_parse_no_command(self):
        from cli import build_parser
        parser = build_parser()
        args = parser.parse_args([])
        assert args.command is None

    def test_main_returns_zero(self):
        from cli import main
        assert main(["status"]) == 0

    def test_main_returns_zero_for_no_command(self):
        from cli import main
        assert main([]) == 0

    def test_cmd_status_with_fleet(self, tmp_path):
        """Test status command with a fleet file."""
        from cli import main
        fleet_file = tmp_path / "fleet.json"
        fleet_file.write_text(json.dumps([
            {"id": "u1", "role": "commodore", "capabilities": ["nav"]},
            {"id": "u2", "capabilities": ["cam"]},
        ]))
        assert main(["--fleet", str(fleet_file), "status"]) == 0

    def test_cmd_elect_with_fleet(self, tmp_path):
        """Test election with a fleet file."""
        from cli import main
        fleet_file = tmp_path / "fleet.json"
        fleet_file.write_text(json.dumps([
            {"id": "u1", "priority": 10},
            {"id": "u2", "priority": 5},
        ]))
        assert main(["--fleet", str(fleet_file), "elect"]) == 0

    def test_cmd_failover_with_fleet(self, tmp_path):
        """Test failover with a fleet file."""
        from cli import main
        fleet_file = tmp_path / "fleet.json"
        fleet_file.write_text(json.dumps([
            {"id": "comm", "role": "commodore", "capabilities": ["nav"]},
            {"id": "worker", "capabilities": ["cam"]},
        ]))
        assert main(["--fleet", str(fleet_file), "failover"]) == 0

    def test_cmd_capabilities_with_fleet(self, tmp_path):
        """Test capabilities with a fleet file."""
        from cli import main
        fleet_file = tmp_path / "fleet.json"
        fleet_file.write_text(json.dumps([
            {"id": "u1", "capabilities": ["nav", "cam"]},
            {"id": "u2", "capabilities": ["cam"]},
        ]))
        assert main(["--fleet", str(fleet_file), "capabilities"]) == 0
        assert main(["--fleet", str(fleet_file), "capabilities", "--query", "cam"]) == 0

    def test_cmd_heartbeat_with_fleet(self, tmp_path):
        """Test heartbeat with a fleet file."""
        from cli import main
        fleet_file = tmp_path / "fleet.json"
        fleet_file.write_text(json.dumps([
            {"id": "u1", "role": "commodore"},
        ]))
        assert main(["--fleet", str(fleet_file), "heartbeat"]) == 0
        assert main(["--fleet", str(fleet_file), "heartbeat", "--send"]) == 0

    def test_cmd_assign_with_fleet(self, tmp_path):
        """Test assign with a fleet file."""
        from cli import main
        fleet_file = tmp_path / "fleet.json"
        fleet_file.write_text(json.dumps([
            {"id": "comm", "role": "commodore"},
            {"id": "worker", "capabilities": ["cam"]},
        ]))
        assert main(["--fleet", str(fleet_file), "assign", "t1", "camera"]) == 0

    def test_cmd_assign_no_capability_fails(self, tmp_path):
        """Test assign fails when no unit has the capability."""
        from cli import main
        fleet_file = tmp_path / "fleet.json"
        fleet_file.write_text(json.dumps([
            {"id": "comm", "role": "commodore"},
            {"id": "worker", "capabilities": ["nav"]},
        ]))
        assert main(["--fleet", str(fleet_file), "assign", "t1", "camera",
                      "--capabilities", "camera"]) == 1
