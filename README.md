# Commodore Protocol

> Decentralized coordination for Multi-DeckBoss instances.

[![Build Status](https://img.shields.io/github/actions/workflow/status/SuperInstance/commodore-protocol/build.yml?branch=main)](https://github.com/SuperInstance/commodore-protocol/actions)
[![License](https://img.shields.io/github/license/SuperInstance/commodore-protocol)](https://github.com/SuperInstance/commodore-protocol/blob/main/LICENSE)
[![Fleet Status](https://img.shields.io/badge/fleet-status-online-green)](https://github.com/SuperInstance/commodore-protocol)
[![Cocapn Fleet](https://img.shields.io/badge/cocapn-fleet-member-blue)](https://github.com/cocapn)

The Commodore Protocol is the brain that keeps multiple DeckBoss units working together on a single vessel. It handles **auto-discovery**, **leader election**, **deference**, **work distribution**, and **automatic failover** — so whether you're running one unit or a dozen, the fleet always has exactly one commodore calling the shots.

---

## Overview

When multiple DeckBoss instances operate on the same vessel, they need a way to agree on who is in charge, distribute computational work, and recover if the leader goes down. The Commodore Protocol solves this as a fully decentralized, peer-to-peer coordination layer:

- **Auto-discovery** — DeckBoss units find each other on the local network via mDNS
- **Election** — A priority-based algorithm selects the most qualified unit as Commodore
- **Deference** — All other units defer compute and decisions to the Commodore
- **Work distribution** — The Commodore assigns tasks based on each unit's capabilities and current load
- **Failover** — If the Commodore dies, the next-best unit promotes automatically

The human only ever talks to one unit. The rest work silently in the background.

---

## Problem Statement

Running multiple autonomous DeckBoss units on the same vessel without a coordination layer creates a set of hard, cascading failures:

| Problem | Without Coordination | With Commodore Protocol |
|---|---|---|
| **Split brain** | Multiple units try to make conflicting decisions | Exactly one Commodore at all times |
| **Wasted resources** | All units duplicate the same compute work | Tasks distributed by capability and load |
| **No failover** | Leader crash = total system failure | Automatic promotion within heartbeat timeout |
| **Discovery** | Manual configuration of every unit's peers | mDNS auto-discovery on the local network |
| **Scaling** | No visibility into when to add hardware | Load monitoring triggers scale-up suggestions |
| **Capability gaps** | No way to know which unit handles what | Central capability registry with routing |

Without coordination, adding a second DeckBoss unit can actually make things *worse* — not better. The Commodore Protocol turns additional units into genuine force multipliers.

---

## Architecture

```
                          ┌──────────────────────────────────┐
                          │          Cocapn Fleet            │
                          │   (inter-vessel coordination)    │
                          └─────────────┬────────────────────┘
                                        │
                          ┌─────────────▼────────────────────┐
                          │       Commodore Protocol        │
                          │                                 │
   ┌─────────────────┐    │  ┌───────────┐ ┌────────────┐  │
   │  DeckBoss-001   │◄───┼──┤ Election  │ │ Heartbeat  │  │
   │  ★ COMMODORE    │    │  │  Engine   │ │  Monitor   │  │
   │  chat,nav,cam   │    │  └─────┬─────┘ └─────┬──────┘  │
   └───────┬─────────┘    │        │             │         │
           │               │  ┌─────▼─────┐ ┌─────▼──────┐  │
   ┌───────▼─────────┐    │  │  Load     │ │ Failover   │  │
   │  DeckBoss-002   │◄───┼──┤  Balancer │ │  Manager   │  │
   │  WORKER         │    │  └───────────┘ └────────────┘  │
   │  camera         │    │                                 │
   └─────────────────┘    │  ┌───────────────────────────┐  │
                          │  │   Capability Registry     │  │
   ┌─────────────────┐    │  └───────────────────────────┘  │
   │  DeckBoss-003   │◄───┼───  mDNS Discovery Layer        │
   │  WORKER         │    │                                 │
   │  navigation     │    └─────────────────────────────────┘
   └─────────────────┘
```

**Communication flow:**

```
  Election Phase:                     Steady State:
  ┌─────────┐                         ┌─────────┐
  │  Unit A │──ElectionRequest──────► │  Unit B │
  │         │◄──ElectionVote───────── │         │
  │         │                         │         │
  │         │──ElectionVote──────────►│         │
  │  (New   │                         │         │
  │  Comm.) │◄──CommodoreHeartbeat─── │ (Worker)│
  └─────────┘                         └─────────┘

  Failover:                           Work Distribution:
  ┌─────────┐                         ┌─────────┐
  │  Comm.  │  ✗ (heartbeat timeout)  │  Comm.  │
  │  (dies) │                         │         │──WorkAssignment──► Worker
  └─────────┘                         │         │◄──WorkComplete──── Worker
        ↓                             └─────────┘
  ┌─────────┐
  │  Worker │──FailoverNotice───────► Other Workers
  │ promotes│
  │ to Comm.│
  └─────────┘
```

---

## Core Concepts

### Discovery

DeckBoss units announce themselves on the local network via mDNS. When a new unit appears, it broadcasts a `capability_announce` message so the fleet knows what it can do. Existing units respond with their own heartbeats, allowing the newcomer to immediately join the fleet.

### Election

Leader selection uses a **priority-based algorithm** with a deterministic tiebreaker chain. The unit with the highest election key becomes Commodore:

| Priority Level | Criterion | Description |
|---|---|---|
| 1 | **Human-designated** | A unit the human is actively talking to (via chatbot) always wins |
| 2 | **Priority value** | Explicit numeric priority set per unit |
| 3 | **Capability count** | More capabilities = more qualified |
| 4 | **Uptime** | Longer-running units are preferred (seniority) |
| 5 | **Unit ID** | Lexicographically lowest ID breaks any remaining tie |

Elections are triggered on: fleet startup, commodore heartbeat timeout, manual request, or periodic re-evaluation.

### Deference

Once a Commodore is elected, all other units adopt the **worker** role. Workers:

- Do not make autonomous decisions — they execute tasks assigned by the Commodore
- Report results back via `work_complete` messages
- Can send `defer_request` messages if they cannot handle assigned work
- Continue sending heartbeats so the Commodore tracks their health

### Failover

The `FailoverManager` monitors the Commodore's health through the heartbeat system:

1. **Suspecting** — If the Commodore misses heartbeats, workers enter suspect state
2. **Threshold** — After 2 consecutive suspect reports, failover is triggered
3. **Election** — A new election runs among remaining alive workers (old Commodore excluded)
4. **Promotion** — The winner is promoted; old Commodore is demoted
5. **Broadcast** — A `failover_notice` is sent to all workers so they acknowledge the new leader

```
  STABLE ──► SUSPECTING ──► SUSPECTING ──► FAILOVER_IN_PROGRESS ──► COMPLETED
    ▲                                                              │
    └──────────────────────────────────────────────────────────────┘
                              (commodore recovers / new commodore set)
```

### Quorum & Scaling

The protocol adapts to fleet size:

| Fleet Size | Configuration | Behavior |
|---|---|---|
| **1 unit** | Standalone | That unit is Commodore by default |
| **2 units** | Commodore + worker | Distribute heavy compute (e.g., camera processing) |
| **3+ units** | Commodore + specialists | Each worker specializes (nav, cameras, engine, etc.) |

The Commodore monitors its own load (default threshold: **80%**). When consistently overloaded, it emits a `scale_suggestion` recommending which capability the fleet needs most and suggests adding a headless DeckBoss unit.

---

## Quick Start

### Installation

```bash
# Requires Python >= 3.10
pip install pyyaml

# Clone the repository
git clone https://github.com/SuperInstance/commodore-protocol.git
cd commodore-protocol
```

### Running Tests

```bash
pytest tests/
```

### CLI Usage

The `commodore` CLI provides direct access to all protocol operations:

```bash
# Trigger a leader election
python cli.py elect --reason "startup"

# View fleet composition
python cli.py status --verbose

# Send a heartbeat
python cli.py heartbeat --send

# Check fleet health
python cli.py heartbeat

# Assign work to a capable unit
python cli.py assign task-001 camera --capabilities camera --priority 5

# Query which units have navigation capability
python cli.py capabilities --query navigation

# List all capabilities across the fleet
python cli.py capabilities

# Simulate commodore death and test failover
python cli.py failover

# Use a fleet configuration file
python cli.py --fleet fleet.json status
```

### Programmatic Usage

```python
from commodore import CommodoreProtocol
from unit import Unit, Role, HealthStatus, LoadMetrics

# Create the protocol coordinator
protocol = CommodoreProtocol(local_unit_id="deckboss-001")

# Add fleet units
protocol.add_unit(Unit(
    id="deckboss-001",
    role=Role.COMMODORE,
    capabilities=["navigation", "chat", "camera"],
    human_designated=True,
))

protocol.add_unit(Unit(
    id="deckboss-002",
    capabilities=["camera"],
))

# Trigger election
result = protocol.trigger_election(reason="startup")
print(f"Commodore: {result.winner_id}")

# Assign work
assignment = protocol.assign_work(
    task_id="cam-001",
    task_type="object_detection",
    required_capabilities=["camera"],
    priority=5,
)
if assignment:
    print(f"Assigned to: {assignment.target_id}")

# Check fleet health
health = protocol.check_fleet_health()
for unit_id, status in health.items():
    print(f"  {unit_id}: {status.value}")
```

### Fleet Configuration File

Define your fleet in JSON for the CLI:

```json
{
  "units": [
    {
      "id": "deckboss-001",
      "role": "commodore",
      "capabilities": ["navigation", "chat", "camera"],
      "human_designated": true,
      "priority": 10
    },
    {
      "id": "deckboss-002",
      "capabilities": ["camera"],
      "load": {"cpu": 30, "memory": 40, "gpu": 20}
    },
    {
      "id": "deckboss-003",
      "capabilities": ["navigation", "engine"],
      "load": {"cpu": 15, "memory": 25, "gpu": 0}
    }
  ]
}
```

```bash
python cli.py --fleet fleet.json elect
python cli.py --fleet fleet.json status -v
python cli.py --fleet fleet.json assign detect-001 camera -c camera
```

---

## Protocol Details

### Message Format

All protocol messages share a common envelope and are serialized as JSON:

```json
{
  "type": "<message_type>",
  "source_id": "deckboss-001",
  "timestamp": 1713136800.123,
  "msg_id": "a1b2c3d4e5f6",
  "payload": { ... }
}
```

Each message auto-generates a `msg_id` (12-char hex) for deduplication and tracing.

### Message Types

| Type | String | Direction | Description |
|---|---|---|---|
| **Heartbeat** | `commodore_heartbeat` | Commodore → All | Periodic health + load + subordinate list |
| **ElectionRequest** | `election_request` | Any → All | Trigger a new leader election |
| **ElectionVote** | `election_vote` | Any → All | Cast vote for a commodore candidate |
| **DeferRequest** | `defer_request` | Worker → Commodore | Worker cannot handle assigned task |
| **WorkAssignment** | `work_assignment` | Commodore → Worker | Assign a task to a specific unit |
| **WorkComplete** | `work_complete` | Worker → Commodore | Report task completion or failure |
| **CapabilityAnnounce** | `capability_announce` | Any → All | Broadcast available capabilities |
| **ScaleSuggestion** | `scale_suggestion` | Commodore → Human | Recommend adding/removing units |
| **FailoverNotice** | `failover_notice` | New Commodore → All | Announce new leader after failover |

### Heartbeat Message Example

```json
{
  "type": "commodore_heartbeat",
  "source_id": "deckboss-001",
  "timestamp": 1713136800.123,
  "msg_id": "f7e8d9c0b1a2",
  "payload": {
    "role": "commodore",
    "subordinates": ["deckboss-002", "deckboss-003"],
    "load": {
      "cpu": 45,
      "memory": 60,
      "gpu": 80,
      "network_in": 12.5,
      "network_out": 8.3,
      "task_queue_depth": 2
    },
    "capabilities": ["navigation", "chat", "camera"]
  }
}
```

### Election Algorithm

```
1. TRIGGER: Fleet startup, heartbeat timeout, manual, or periodic
2. BROADCAST: election_request to all units
3. VOTE: Each unit votes for the best candidate (by priority chain)
4. COLLECT: Votes gathered until all units have responded or timeout (default: 5s)
5. RESOLVE:
   a. If votes exist → candidate with most votes wins
   b. If tie → break by unit priority chain
   c. If no votes → highest-priority alive unit wins
6. APPLY: Winner promoted to Commodore, all others demoted to Worker
```

### Heartbeat Mechanism

| Parameter | Default | Description |
|---|---|---|
| `interval_seconds` | 2.0 | How often the Commodore sends heartbeats |
| `timeout_seconds` | 6.0 | Time before a unit is considered unhealthy |
| `max_missed` | 3 | Consecutive timeouts before marking a unit dead |

Health transitions based on elapsed time since last heartbeat:

```
  Within 2× interval     → HEALTHY
  Within timeout          → DEGRADED
  Beyond timeout          → UNHEALTHY
  Beyond timeout × missed → DEAD
```

### Composite Load Score

Resource utilization is combined into a single 0-100 score:

```
composite_load = (cpu × 0.35) + (memory × 0.25) + (gpu × 0.25) + min(task_queue_depth × 5, 15)
```

The load balancer assigns tasks to the worker with the **lowest composite load** that has the required capabilities. Units above the load threshold (default 80%) are avoided unless all workers are overloaded.

---

## Fleet Integration

### Connection to Cocapn

The Commodore Protocol operates as a **vessel-level** protocol within the Cocapn fleet architecture. While Cocapn handles inter-vessel coordination (ship-to-ship), the Commodore Protocol handles **intra-vessel** coordination (unit-to-unit on the same boat).

```
  ┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
  │    Vessel A     │     │    Vessel B     │     │    Vessel C     │
  │ ┌─────────────┐ │     │ ┌─────────────┐ │     │ ┌─────────────┐ │
  │ │ Commodore   │ │     │ │ Commodore   │ │     │ │ Commodore   │ │
  │ │ Protocol    │◄├─────┤► Protocol    │◄├─────┤► Protocol    │ │
  │ └─────────────┘ │     │ └─────────────┘ │     │ └─────────────┘ │
  │  DB-001 DB-002  │     │  DB-003 DB-004  │     │  DB-005        │
  └─────────────────┘     └─────────────────┘     └─────────────────┘
          ▲                       ▲                       ▲
          └───────────────────────┴───────────────────────┘
                         Cocapn Fleet Protocol
                    (inter-vessel messaging)
```

### DeckBoss Integration

Each DeckBoss instance runs the Commodore Protocol as part of its core stack:

- **Single instance**: The DeckBoss runs as a standalone Cocapn node and is Commodore by default
- **Multi-instance**: The first unit the human interacts with becomes the human-designated Commodore; other units self-assign as workers
- **Work handoff**: The Commodore routes specialized tasks (camera processing, navigation calculations, engine monitoring) to workers with matching capabilities
- **Human interface**: The human only ever talks to the Commodore. Workers operate headlessly

### I2I Protocol Compatibility

The protocol's JSON message format is designed for I2I (Instance-to-Instance) compatibility within the Cocapn fleet. Messages can be:

- Serialized via `serialize_message(msg)` → dict → JSON
- Deserialized via `deserialize_message(data)` → typed message object
- Unknown message types gracefully fall back to base `ProtocolMessage`

---

## Project Structure

```
commodore-protocol/
├── commodore.py          # Core engine: Election, HeartbeatMonitor,
│                         # LoadBalancer, CapabilityRegistry, FailoverManager,
│                         # CommodoreProtocol
├── messages.py           # Protocol message types (9 message types)
│                         # Serialization/deserialization registry
├── unit.py               # Unit model: Role, HealthStatus, LoadMetrics
│                         # Election ordering via __lt__ / __gt__
├── cli.py                # CLI interface: elect, status, assign,
│                         # capabilities, heartbeat, failover
├── tests/
│   ├── test_commodore.py # Comprehensive test suite
│   └── __init__.py
├── pyproject.toml        # Python 3.10+, pytest, pyyaml
├── CHARTER.md            # Fleet charter (mission, captain, maintainer)
├── STATE.md              # Current operational status
├── DOCKSIDE-EXAM.md      # Certification checklist
├── COMMODORE.md          # Protocol design document
└── LICENSE
```

---

## License

See [LICENSE](LICENSE) for details.

---

<img src="callsign1.jpg" width="128" alt="callsign">
