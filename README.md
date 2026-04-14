# Commodore Protocol — Multi-DeckBoss Coordination

When multiple DeckBoss units are on the same boat:

1. **Auto-discovery** — DeckBoss units find each other on local network via mDNS
2. **Election** — The unit the human talks to (via chatbot) becomes Commodore
3. **Deference** — All other units defer compute and decisions to Commodore
4. **Work distribution** — Commodore assigns tasks based on each unit's capabilities
5. **Failover** — If Commodore dies, next unit promotes automatically

## Protocol
```json
{
  "type": "commodore_heartbeat",
  "unit_id": "deckboss-001",
  "role": "commodore",
  "subordinates": ["deckboss-002", "deckboss-003"],
  "load": {"cpu": 45, "mem": 60, "gpu": 80},
  "capabilities": ["navigation", "chat", "camera"]
}
```

## Scaling
- 1 DeckBoss = standalone Cocapn (commodore by default)
- 2 DeckBoss = Commodore + worker (distribute camera processing)
- 3+ DeckBoss = Commodore + specialists (one for nav, one for cameras, one for engine)
- The human only talks to one. The others work silently.

## When to Add Units
The Commodore monitors its own load. When consistently >80%:
- Suggests adding a headless DeckBoss
- Explains what capability the new unit would handle
- Offers hardware/cloud expansion options with pros/cons
