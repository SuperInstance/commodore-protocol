"""CLI interface for the Commodore Protocol.

Subcommands:
  elect        Trigger leader election
  status       Show fleet composition
  assign       Assign work to a unit
  capabilities List/query capabilities
  heartbeat    Send/receive heartbeats
  failover     Test failover scenario
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from commodore import CommodoreProtocol
from messages import (
    CommodoreHeartbeat, WorkComplete, CapabilityAnnounce,
    deserialize_message, serialize_message,
)
from unit import Unit, Role, HealthStatus, LoadMetrics


def cmd_elect(args: argparse.Namespace) -> int:
    """Trigger a leader election."""
    protocol = _build_protocol(args)
    result = protocol.trigger_election(reason=args.reason)
    commodore = protocol.get_commodore()

    print(f"Election completed in {result.duration_seconds:.3f}s")
    print(f"Winner: {result.winner_id}")
    if result.votes:
        print(f"Votes: {json.dumps(result.votes, indent=2)}")
    if commodore:
        print(f"New commodore: {commodore.id}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Show fleet composition."""
    protocol = _build_protocol(args)
    status = protocol.get_fleet_status()

    print(f"Fleet Size: {status['fleet_size']}")
    print(f"Commodore: {status['commodore_id'] or 'None'}")
    print(f"Workers: {', '.join(status['workers']) or 'None'}")
    print(f"Election State: {status['election_state']}")
    print(f"Failover State: {status['failover_state']}")
    print(f"Capabilities: {', '.join(status['capabilities']) or 'None'}")
    print(f"Active Tasks: {status['active_tasks']}")

    if args.verbose:
        print("\n--- Unit Details ---")
        for uid, unit in protocol.units.items():
            print(f"  {unit}")

    return 0


def cmd_assign(args: argparse.Namespace) -> int:
    """Assign work to a unit."""
    protocol = _build_protocol(args)

    caps = args.capabilities.split(",") if args.capabilities else None
    assignment = protocol.assign_work(
        task_id=args.task_id,
        task_type=args.task_type,
        required_capabilities=caps,
        priority=args.priority,
    )

    if assignment is None:
        print("ERROR: No suitable unit available for assignment", file=sys.stderr)
        return 1

    print(f"Task {args.task_id} ({args.task_type}) assigned to {assignment.target_id}")
    if args.json:
        print(json.dumps(serialize_message(assignment), indent=2))
    return 0


def cmd_capabilities(args: argparse.Namespace) -> int:
    """List or query capabilities."""
    protocol = _build_protocol(args)
    all_caps = protocol.capability_registry.all_capabilities()

    if args.query:
        units = protocol.capability_registry.find_units_with(args.query)
        print(f"Units with '{args.query}': {', '.join(units) or 'None'}")
    else:
        print(f"Registered capabilities: {', '.join(sorted(all_caps)) or 'None'}")
        for uid in protocol.units:
            caps = protocol.capability_registry.get_capabilities(uid)
            if caps:
                print(f"  {uid}: {', '.join(sorted(caps))}")

    return 0


def cmd_heartbeat(args: argparse.Namespace) -> int:
    """Send/receive heartbeats."""
    protocol = _build_protocol(args)

    if args.send:
        msg = protocol.send_heartbeat()
        print(f"Heartbeat sent from {msg.source_id}")
        if args.json:
            print(json.dumps(serialize_message(msg), indent=2))
    else:
        health = protocol.check_fleet_health()
        print("Fleet Health:")
        for uid, status in health.items():
            unit = protocol.units.get(uid)
            last_hb = protocol.heartbeat_monitor.get_last_heartbeat(uid)
            age = time.time() - last_hb if last_hb else float("inf")
            print(f"  {uid}: {status.value} (last HB: {age:.1f}s ago)")

    return 0


def cmd_failover(args: argparse.Namespace) -> int:
    """Test failover scenario."""
    protocol = _build_protocol(args)

    commodore = protocol.get_commodore()
    if not commodore:
        # Run election first
        protocol.trigger_election(reason="pre-failover")
        commodore = protocol.get_commodore()

    if not commodore:
        print("ERROR: No commodore available", file=sys.stderr)
        return 1

    print(f"Current commodore: {commodore.id}")
    print(f"Simulating commodore death...")

    # Mark commodore as dead
    commodore.health = HealthStatus.DEAD

    plan = protocol.check_failover()
    if plan:
        print(f"Failover complete!")
        print(f"  Old commodore: {plan.old_commodore_id}")
        print(f"  New commodore: {plan.new_commodore_id}")
        if args.json:
            print(f"  Worker assignments: {json.dumps(plan.worker_assignments, indent=2)}")
    else:
        print("No failover initiated (no eligible successor)")

    return 0


# --- Helpers ---

def _build_protocol(args: argparse.Namespace) -> CommodoreProtocol:
    """Build a protocol instance from args, loading fleet config if provided."""
    protocol = CommodoreProtocol(local_unit_id=args.unit or "cli-local")

    if args.fleet:
        fleet = _load_fleet(args.fleet)
        for unit_data in fleet:
            unit = Unit.from_dict(unit_data)
            protocol.add_unit(unit)

    return protocol


def _load_fleet(path: str) -> list[dict[str, Any]]:
    """Load fleet configuration from a JSON file."""
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return data.get("units", [])
    except FileNotFoundError:
        print(f"WARNING: Fleet file '{path}' not found", file=sys.stderr)
        return []
    except json.JSONDecodeError as e:
        print(f"WARNING: Invalid JSON in fleet file: {e}", file=sys.stderr)
        return []


def build_parser() -> argparse.ArgumentParser:
    """Build the main CLI parser."""
    parser = argparse.ArgumentParser(
        prog="commodore",
        description="Commodore Protocol CLI — Multi-unit coordination for the Pelagic fleet",
    )

    # Global options
    parser.add_argument(
        "--unit", "-u",
        default="cli-local",
        help="Local unit ID (default: cli-local)",
    )
    parser.add_argument(
        "--fleet", "-f",
        help="Path to fleet configuration JSON file",
    )
    parser.add_argument(
        "--json", "-j",
        action="store_true",
        help="Output in JSON format",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose output",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # elect
    elect_p = subparsers.add_parser("elect", help="Trigger leader election")
    elect_p.add_argument("--reason", "-r", default="manual", help="Election reason")

    # status
    subparsers.add_parser("status", help="Show fleet composition")

    # assign
    assign_p = subparsers.add_parser("assign", help="Assign work to a unit")
    assign_p.add_argument("task_id", help="Task identifier")
    assign_p.add_argument("task_type", help="Task type")
    assign_p.add_argument("--capabilities", "-c", help="Required capabilities (comma-separated)")
    assign_p.add_argument("--priority", "-p", type=int, default=0, help="Task priority")

    # capabilities
    caps_p = subparsers.add_parser("capabilities", help="List or query capabilities")
    caps_p.add_argument("--query", "-q", help="Query units with a specific capability")

    # heartbeat
    hb_p = subparsers.add_parser("heartbeat", help="Send/receive heartbeats")
    hb_p.add_argument("--send", "-s", action="store_true", help="Send a heartbeat")

    # failover
    subparsers.add_parser("failover", help="Test failover scenario")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Main CLI entry point."""
    parser = build_parser()
    # Use parse_known_args so global flags after subcommand don't error
    args, _ = parser.parse_known_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    commands = {
        "elect": cmd_elect,
        "status": cmd_status,
        "assign": cmd_assign,
        "capabilities": cmd_capabilities,
        "heartbeat": cmd_heartbeat,
        "failover": cmd_failover,
    }

    handler = commands.get(args.command)
    if handler is None:
        parser.print_help()
        return 1

    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
