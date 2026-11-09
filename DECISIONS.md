# Design decisions

Each decision has the choice, the reason, and the evidence (a test, a query
or a report line you can run). Numbers come from the stand-in snapshot until
the FDE Academy database is plugged in.

---

## 1. Where the agent is used, and where it is not

**Choice.** The LLM is used for one job: turning a driver's free text into
structured fields (`delay_minutes`, `earliest`, `latest_end`, `choice_index`).
Conversation steps are a fixed workflow (`service.py`). Every capacity
decision is plain SQL and Python (`tools.py`). The LLM has no database access
and cannot book anything.

| Part | Workflow or agent | Why |
|---|---|---|
| Understand "around 2 hrs late, anything after 7" | Agent (LLM, regex fallback) | Free text with endless phrasings |
| Decide which step comes next | Workflow | Only 4 states; a fixed path is easier to test |
| Find compatible free slots | Code (SQL) | Structured, must be exact and repeatable |
| Book a slot | Code (transaction) | Must be safe under concurrency |
| Plan a whole facility | Code (dispatch rule) | Structured inputs, needs to be explainable |

**Reason.** Anthropic's *Building effective agents* advises the simplest
solution that works. Capacity is a structured decision, so an LLM adds risk
and cost there and no accuracy.

**Evidence.** `eval.py` passes all 12 ladder scenarios in offline mode, with
zero LLM calls. Swapping in the LLM changes only `Extraction`, which is
validated by Pydantic. If the LLM returns bad JSON, the service falls back to
the regex extractor (`agent.get_extractor`).

**Provider.** OpenRouter (`OPENROUTER_API_KEY`) or Anthropic direct
(`ANTHROPIC_API_KEY`), chosen by environment variable, with no code change.
OpenRouter keeps the model swappable (`OPENROUTER_MODEL`) and is called with
the standard library only. The model gets `temperature 0` and a JSON-only
response format, because extraction should be repeatable.

## 2. Ask the driver vs look it up

**Choice.** The driver is asked only for things the backend cannot know:
their new ETA, time limits ("leave by 9") and their option choice. Shipment,
truck, load type, weight, facility, compatible docks and current booking all
come from the database.

**Exception.** If one phone number maps to more than one open shipment, the
backend can't tell who is writing, so the service asks for the truck number.
It then resumes the parked message so the driver doesn't repeat themselves.

**Evidence.** Data issue 13 (shared phone `+919800000018`). Scenario "Shared
phone: ask truck number, then resume" gives `ask_vehicle → offered → confirmed`.

## 3. Driver vs backend: who wins

**Choice.**
- Gate check-in beats the driver. If the gate logged the truck in, "I'm 1 hour
  late" gets "you're already inside, see the dock supervisor", not a new slot.
- ETA trust order: `ops_override` first, then the newest of driver, warehouse
  and plan.

**Reason.** Gate-in is physical evidence; a chat message is a claim. Ops
overrides exist because operations know something the driver doesn't (road
closure, convoy).

**Evidence.** Scenario "Gate shows truck already inside". Scenario "Window +
maintenance block + ops override ETA" gives ETA 14:30 (ops 11:30 + 3h), not
15:30 (driver 12:30 + 3h). Data issue 8 shows SH-02's sources disagree.

## 4. Preventing two drivers being promised the same slot

**Choice.** Three layers, plus one rule.
1. **Holds.** An offered slot is reserved for 2 minutes in `slot_holds`.
   A partial unique index allows only one active hold per slot, so two
   conversations can't offer the same slot.
2. **Conditional update.** `confirm` runs
   `UPDATE slot_holds SET status='confirmed' WHERE id=? AND status='active' AND expires_at > now`
   inside `BEGIN IMMEDIATE`. A hold can be used once and only while valid.
3. **Database guard.** The supplied partial unique index on active
   appointments rejects any second booking for a slot.
4. **Rule.** The confirmation message is sent and logged only after `COMMIT`.

**Alternatives rejected.**
- *Check then insert in app code*: has the race the brief describes.
- *Global lock / single worker queue*: correct, but serialises every driver
  during a delay wave, which is exactly when speed matters.
- *Optimistic retry only (no holds)*: safe, but drivers would often pick a
  slot that's gone by the time they reply.

**Evidence.** `eval.py` part B: 30 rounds × 10 simultaneous conversations
= 300. Zero slots promised twice, zero false promises, zero double bookings.
`tests/test_concurrency.py` races `hold_slot`, `book_direct` (no holds at
all) and 10 confirms of the same hold. Exactly one wins each time.

## 5. Hold duration: 2 minutes

**Choice.** 2 minutes, configurable (`tools.HOLD_MINUTES`).

**Reason.** Long enough to read 3 options on a phone and reply. Short enough
that unanswered offers don't freeze scarce slots during a delay wave. If a
hold expires, the driver gets an honest "no longer available" and a fresh
search, never a false confirmation.

**Evidence.** Scenario "Hold expires before driver answers" gives
`confirm_failed`, and SH-06 keeps its original booking.

## 6. What counts as a usable slot

`find_options` checks everything the two unique indexes do **not** cover:

| Check | Why | Planted case |
|---|---|---|
| Same facility as the shipment | Wrong-city booking | SH-10 (issue 5) |
| Dock active | Dock under repair | JAI-D5 (issue 11) |
| Reefer / weight compatible | Spoiled or unhandled load | SH-09 (issue 4) |
| Slot ends after it starts | Corrupt slot | AMD-A2-BAD (issue 9) |
| Slot length ≥ unload time | Overrun into next truck | SH-13 (issue 10) |
| No overlapping active booking on the dock | Different slot rows, same time | JAI-D1-1030 (issues 1–2) |
| No blocked window | Maintenance, outage | JAI-D2 14–16 (issue 3) |
| No live hold by another driver | Race safety | — |
| Not in the past | Can't arrive yesterday | — |

**Ranking.** Earliest first. At equal times, general docks come before
reefer and heavy docks, so scarce docks stay free for loads that need them.

## 7. When nothing fits: escalate, never invent

**Choice.** If no compatible slot exists in the window, the driver is told
operations will call, and the reply is logged with action `escalate`.

**Evidence.** Scenario "Unload longer than any slot" (SH-13, 90 minutes).
In the race, drivers beyond the 3 free compatible docks are escalated
(210 of 300), and none are given a false slot.

## 8. Offer 3 options, not 1 and not 10

**Choice.** Up to 3 held options.

**Reason.** One option forces another round trip when it doesn't suit.
More than 3 is hard to read on a phone, and every option holds scarce
capacity for 2 minutes.

## 9. Scheduling tool objective

**Choice.** A transparent greedy dispatch rule (`scheduler.py`), not CP-SAT.

`score = priority × (1·wait_min + 2·late_min) + 30·changed + 30·intrudes + 5·scarce_dock`

In plain words: less waiting, fewer missed appointments, fewer changes to a
schedule already told to a driver, don't take another truck's promised time,
and keep reefer and heavy docks for loads that need them. Trucks already
unloading are fixed tasks; gate-in counts as arrived now.

**Why not CP-SAT yet.** With 9 docks and ~20 trucks a day per facility, the
greedy rule gives schedules an operator can check by hand, and every choice
can be explained from the score. CP-SAT becomes worth it when greedy leaves
measurable waiting on the table at real scale. The scheduler's interface
(structured inputs → proposed sequence) lets a solver drop in later.

**Evidence.** `tests/test_scheduler.py` and `python demo.py` part 2 at
Ahmedabad 13:55: SH-20 unloading is fixed; SH-21 (90 min early, at gate) is
served now on a free dock; SH-19 (late) starts at its new ETA with 0 wait;
SH-10's wrong-facility booking is moved to an Ahmedabad dock; SH-18 keeps its
promise. At Jaipur, 5 of 5 clean bookings are kept unchanged.

## 10. Audit trail

Every booking change writes `appointment_history`. Every confirmation sent
writes `outbound_confirmations`. Every message in and out writes `messages`.

**Reason.** Data issue 15 shows why: SH-21 was told 13:00, then moved to
15:00 with no new message. With the trail, a query finds every driver whose
last confirmation no longer matches the database.

## 11. Scale beyond the snapshot

The snapshot has 2 facilities and 21 shipments; the design targets more.

- **Database.** Move to PostgreSQL. The same partial unique indexes exist
  there, and `BEGIN IMMEDIATE` becomes a normal transaction with the unique
  index as the guard (or `SELECT ... FOR UPDATE SKIP LOCKED` for holds).
- **App.** The service is stateless (state lives in `conversations`), so
  more API instances can run behind a load balancer.
- **Hold sweeper.** Expired holds are cleaned lazily on each write; at scale,
  add a periodic job.
- **LLM cost.** One short extraction call per inbound message, no calls for
  capacity. A delay wave of 500 messages is 500 small calls.

## 12. Known limits

- Built against a stand-in schema; table names change when the real database
  arrives (`db.py`, the SQL in `tools.py` and `analysis/data_issues.sql`).
- The regex extractor handles English and simple Hinglish ("bhai", "haan");
  free Hindi text needs the LLM path.
- Slots are fixed rows. Unloads longer than one slot (combining two
  consecutive slots) are escalated, not auto-booked.
- No live GPS, as stated in the brief; ETAs are only as good as their source.
