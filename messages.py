"""Protocol messages for the Commodore Protocol.

All messages are serializable to/from dicts (and thus JSON).
Each message has a type string, a source unit_id, a timestamp,
and type-specific payload fields.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MessageType(str, Enum):
    """All protocol message types."""
    HEARTBEAT = "commodore_heartbeat"
    ELECTION_REQUEST = "election_request"
    ELECTION_VOTE = "election_vote"
    DEFER_REQUEST = "defer_request"
    WORK_ASSIGNMENT = "work_assignment"
    WORK_COMPLETE = "work_complete"
    CAPABILITY_ANNOUNCE = "capability_announce"
    SCALE_SUGGESTION = "scale_suggestion"
    FAILOVER_NOTICE = "failover_notice"


@dataclass
class ProtocolMessage:
    """Base class for all protocol messages."""
    source_id: str
    msg_type: MessageType = field(default=None)
    timestamp: float = field(default_factory=time.time)
    msg_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        pass

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.msg_type.value,
            "source_id": self.source_id,
            "timestamp": self.timestamp,
            "msg_id": self.msg_id,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProtocolMessage:
        return cls(
            source_id=data["source_id"],
            msg_type=MessageType(data["type"]),
            timestamp=data.get("timestamp", time.time()),
            msg_id=data.get("msg_id", uuid.uuid4().hex[:12]),
            payload=dict(data.get("payload", {})),
        )

    def __repr__(self) -> str:
        return (f"ProtocolMessage(type={self.msg_type.value if self.msg_type else 'unknown'}, "
                f"src={self.source_id!r}, id={self.msg_id!r})")


# --- Concrete message types ---


@dataclass
class CommodoreHeartbeat(ProtocolMessage):
    """Periodic health + load report from the commodore."""
    role: str = "commodore"
    subordinates: list[str] = field(default_factory=list)
    load: dict[str, Any] = field(default_factory=dict)
    capabilities: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.msg_type = MessageType.HEARTBEAT

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["payload"].update({
            "role": self.role,
            "subordinates": list(self.subordinates),
            "load": dict(self.load),
            "capabilities": list(self.capabilities),
        })
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CommodoreHeartbeat:
        payload = data.get("payload", {})
        return cls(
            source_id=data["source_id"],
            msg_type=MessageType(data["type"]),
            timestamp=data.get("timestamp", time.time()),
            msg_id=data.get("msg_id", uuid.uuid4().hex[:12]),
            payload=payload,
            role=payload.get("role", "commodore"),
            subordinates=list(payload.get("subordinates", [])),
            load=dict(payload.get("load", {})),
            capabilities=list(payload.get("capabilities", [])),
        )


@dataclass
class ElectionRequest(ProtocolMessage):
    """Trigger a new leader election."""
    reason: str = "periodic"
    candidate_id: str = ""

    def __post_init__(self):
        self.msg_type = MessageType.ELECTION_REQUEST

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["payload"].update({
            "reason": self.reason,
            "candidate_id": self.candidate_id,
        })
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ElectionRequest:
        payload = data.get("payload", {})
        return cls(
            source_id=data["source_id"],
            msg_type=MessageType(data["type"]),
            timestamp=data.get("timestamp", time.time()),
            msg_id=data.get("msg_id", uuid.uuid4().hex[:12]),
            payload=payload,
            reason=payload.get("reason", "periodic"),
            candidate_id=payload.get("candidate_id", ""),
        )


@dataclass
class ElectionVote(ProtocolMessage):
    """Cast a vote for a commodore candidate."""
    candidate_id: str = ""
    voter_id: str = ""
    voter_priority: int = 0

    def __post_init__(self):
        self.msg_type = MessageType.ELECTION_VOTE

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["payload"].update({
            "candidate_id": self.candidate_id,
            "voter_id": self.voter_id,
            "voter_priority": self.voter_priority,
        })
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ElectionVote:
        payload = data.get("payload", {})
        return cls(
            source_id=data["source_id"],
            msg_type=MessageType(data["type"]),
            timestamp=data.get("timestamp", time.time()),
            msg_id=data.get("msg_id", uuid.uuid4().hex[:12]),
            payload=payload,
            candidate_id=payload.get("candidate_id", ""),
            voter_id=payload.get("voter_id", ""),
            voter_priority=payload.get("voter_priority", 0),
        )


@dataclass
class DeferRequest(ProtocolMessage):
    """Worker defers to the commodore."""
    worker_id: str = ""
    deferred_tasks: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.msg_type = MessageType.DEFER_REQUEST

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["payload"].update({
            "worker_id": self.worker_id,
            "deferred_tasks": list(self.deferred_tasks),
        })
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeferRequest:
        payload = data.get("payload", {})
        return cls(
            source_id=data["source_id"],
            msg_type=MessageType(data["type"]),
            timestamp=data.get("timestamp", time.time()),
            msg_id=data.get("msg_id", uuid.uuid4().hex[:12]),
            payload=payload,
            worker_id=payload.get("worker_id", ""),
            deferred_tasks=list(payload.get("deferred_tasks", [])),
        )


@dataclass
class WorkAssignment(ProtocolMessage):
    """Commodore assigns a task to a worker."""
    task_id: str = ""
    task_type: str = ""
    target_id: str = ""
    task_payload: dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    required_capabilities: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.msg_type = MessageType.WORK_ASSIGNMENT

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["payload"].update({
            "task_id": self.task_id,
            "task_type": self.task_type,
            "target_id": self.target_id,
            "task_payload": dict(self.task_payload),
            "priority": self.priority,
            "required_capabilities": list(self.required_capabilities),
        })
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkAssignment:
        payload = data.get("payload", {})
        return cls(
            source_id=data["source_id"],
            msg_type=MessageType(data["type"]),
            timestamp=data.get("timestamp", time.time()),
            msg_id=data.get("msg_id", uuid.uuid4().hex[:12]),
            payload=payload,
            task_id=payload.get("task_id", ""),
            task_type=payload.get("task_type", ""),
            target_id=payload.get("target_id", ""),
            task_payload=dict(payload.get("task_payload", {})),
            priority=payload.get("priority", 0),
            required_capabilities=list(payload.get("required_capabilities", [])),
        )


@dataclass
class WorkComplete(ProtocolMessage):
    """Worker reports a task is done."""
    task_id: str = ""
    worker_id: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    success: bool = True
    error: str = ""

    def __post_init__(self):
        self.msg_type = MessageType.WORK_COMPLETE

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["payload"].update({
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "result": dict(self.result),
            "success": self.success,
            "error": self.error,
        })
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkComplete:
        payload = data.get("payload", {})
        return cls(
            source_id=data["source_id"],
            msg_type=MessageType(data["type"]),
            timestamp=data.get("timestamp", time.time()),
            msg_id=data.get("msg_id", uuid.uuid4().hex[:12]),
            payload=payload,
            task_id=payload.get("task_id", ""),
            worker_id=payload.get("worker_id", ""),
            result=dict(payload.get("result", {})),
            success=payload.get("success", True),
            error=payload.get("error", ""),
        )


@dataclass
class CapabilityAnnounce(ProtocolMessage):
    """Unit announces its capabilities."""
    capabilities: list[str] = field(default_factory=list)
    version: str = "1.0"

    def __post_init__(self):
        self.msg_type = MessageType.CAPABILITY_ANNOUNCE

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["payload"].update({
            "capabilities": list(self.capabilities),
            "version": self.version,
        })
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CapabilityAnnounce:
        payload = data.get("payload", {})
        return cls(
            source_id=data["source_id"],
            msg_type=MessageType(data["type"]),
            timestamp=data.get("timestamp", time.time()),
            msg_id=data.get("msg_id", uuid.uuid4().hex[:12]),
            payload=payload,
            capabilities=list(payload.get("capabilities", [])),
            version=payload.get("version", "1.0"),
        )


@dataclass
class ScaleSuggestion(ProtocolMessage):
    """Commodore suggests adding/removing units."""
    action: str = "add"  # "add" or "remove"
    reason: str = ""
    suggested_capability: str = ""
    current_load: float = 0.0

    def __post_init__(self):
        self.msg_type = MessageType.SCALE_SUGGESTION

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["payload"].update({
            "action": self.action,
            "reason": self.reason,
            "suggested_capability": self.suggested_capability,
            "current_load": self.current_load,
        })
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScaleSuggestion:
        payload = data.get("payload", {})
        return cls(
            source_id=data["source_id"],
            msg_type=MessageType(data["type"]),
            timestamp=data.get("timestamp", time.time()),
            msg_id=data.get("msg_id", uuid.uuid4().hex[:12]),
            payload=payload,
            action=payload.get("action", "add"),
            reason=payload.get("reason", ""),
            suggested_capability=payload.get("suggested_capability", ""),
            current_load=payload.get("current_load", 0.0),
        )


@dataclass
class FailoverNotice(ProtocolMessage):
    """Announcement of a new commodore after failover."""
    new_commodore_id: str = ""
    old_commodore_id: str = ""
    reason: str = ""
    worker_ids: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.msg_type = MessageType.FAILOVER_NOTICE

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["payload"].update({
            "new_commodore_id": self.new_commodore_id,
            "old_commodore_id": self.old_commodore_id,
            "reason": self.reason,
            "worker_ids": list(self.worker_ids),
        })
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FailoverNotice:
        payload = data.get("payload", {})
        return cls(
            source_id=data["source_id"],
            msg_type=MessageType(data["type"]),
            timestamp=data.get("timestamp", time.time()),
            msg_id=data.get("msg_id", uuid.uuid4().hex[:12]),
            payload=payload,
            new_commodore_id=payload.get("new_commodore_id", ""),
            old_commodore_id=payload.get("old_commodore_id", ""),
            reason=payload.get("reason", ""),
            worker_ids=list(payload.get("worker_ids", [])),
        )


# --- Registry for message deserialization ---

_MESSAGE_REGISTRY: dict[str, type] = {
    MessageType.HEARTBEAT.value: CommodoreHeartbeat,
    MessageType.ELECTION_REQUEST.value: ElectionRequest,
    MessageType.ELECTION_VOTE.value: ElectionVote,
    MessageType.DEFER_REQUEST.value: DeferRequest,
    MessageType.WORK_ASSIGNMENT.value: WorkAssignment,
    MessageType.WORK_COMPLETE.value: WorkComplete,
    MessageType.CAPABILITY_ANNOUNCE.value: CapabilityAnnounce,
    MessageType.SCALE_SUGGESTION.value: ScaleSuggestion,
    MessageType.FAILOVER_NOTICE.value: FailoverNotice,
}


def deserialize_message(data: dict[str, Any]) -> ProtocolMessage:
    """Deserialize a message dict into the appropriate message type."""
    msg_type_str = data.get("type", "")
    msg_cls = _MESSAGE_REGISTRY.get(msg_type_str, ProtocolMessage)
    try:
        return msg_cls.from_dict(data)
    except (ValueError, KeyError):
        # Fallback: return base ProtocolMessage with raw data
        return ProtocolMessage(
            source_id=data.get("source_id", "unknown"),
            timestamp=data.get("timestamp", time.time()),
            msg_id=data.get("msg_id", uuid.uuid4().hex[:12]),
            payload=dict(data.get("payload", {})),
            # For unknown types, bypass __post_init__ by setting directly
        )


def serialize_message(msg: ProtocolMessage) -> dict[str, Any]:
    """Serialize a message to a dict."""
    return msg.to_dict()
