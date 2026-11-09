# Architecture

```mermaid
flowchart TD
    D[Driver chat message] --> API[Conversation service<br/>FastAPI · app/api.py + app/service.py]
    API --> AG[LLM agent<br/>app/agent.py<br/>text → Extraction]
    AG --> API
    API --> T1[find_options]
    API --> T2[hold_slot]
    API --> T3[confirm]
    OPS[Ops / agent] --> T4[propose_schedule<br/>app/scheduler.py]
    T1 & T2 & T3 & T4 --> DB[(SQLite<br/>18 tables · partial unique indexes)]
    T3 -->|after COMMIT only| MSG[Confirmation sent + logged]
```

The agent only reads text. The tools only read structured data. Only the
tools write capacity, and only inside transactions.

## One message, end to end

1. Log the inbound message (`messages`).
2. Work out the shipment from the phone. If several match, ask for the truck number.
3. Extract fields from the text (LLM or regex) → validated `Extraction`.
4. If the gate shows the truck inside → "already arrived", stop.
5. Compute the new ETA (ops override > newest source) and record it (`eta_updates`).
6. `find_options` → `hold_slot` up to 3 → reply with options (state `offered`).
7. Driver picks one → `confirm` (one transaction) → after commit, send and log
   the confirmation, release the other holds.
8. Any failure → honest reply + fresh search or escalate. Never a false promise.

## Conversation states

```mermaid
stateDiagram-v2
    [*] --> new
    new --> new: ask_eta / ask_vehicle / already_arrived / escalate
    new --> offered: options held
    offered --> confirmed: confirm OK
    offered --> new: hold expired / slot taken
    offered --> offered: new ETA (old holds released)
```

## Tables (18, three groups)

| Group | Tables |
|---|---|
| Master data | facilities, docks, carriers, drivers, vehicles, customers |
| Plan and operations | shipments, slots, dock_blocks, appointments, appointment_history, eta_updates, gate_events, dock_events |
| Conversations and messaging | conversations, messages, outbound_confirmations, slot_holds |

Integrity rules in the database:
- `ux_appt_slot_active`: one active appointment per slot (supplied by the brief)
- `ux_appt_shipment_active`: one active appointment per shipment (supplied by the brief)
- `ux_hold_slot_active`: one active hold per slot (added)

## Concurrency, in one picture

```
Driver A chat ─┐                    ┌─ hold_slot(S) → INSERT hold … OK
               ├─ both see S free ──┤
Driver B chat ─┘                    └─ hold_slot(S) → unique index → slot_held → B gets other options
```
Even with holds bypassed (`book_direct`), the appointment unique index lets
exactly one insert through. Tested with 10 threads released by a barrier.
