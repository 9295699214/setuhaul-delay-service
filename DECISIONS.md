# Design decisions

Each decision gives the choice, the reason and the evidence: a test, a query or a
report line you can run. Everything runs against the supplied FDE Academy snapshot
(`data/setuhaul_freight_operations.db`, 4 Aug 2026, Asia/Kolkata).

## The guide's seven design questions

### 1. What should the agent clarify before calling any scheduling tool?

Only what the backend cannot know, in this order:

1. **Which shipment?** The phone identifies the driver. With several open shipments,
   the rule is: the one the message names (order number, its last digits, or a unique
   truck number); else the only trip that is underway, unless the message reports a delay
   while that truck is already on site, which is a contradiction. Otherwise ask.
   - DRV006: SHP1006 is in transit and SHP1021 hasn't started, so SHP1006, no question.
   - DRV004 / THR010: "late by 45 minutes" while SHP1004 is in the yard, so ask (matches MSG017).
   - DRV007 drives two moving trucks (data issue 7), so ask.
2. **Is the truck already here?** If `facility_checkins` has a gate-in, plan from now and
   never from a declared ETA.
3. **How late, and any window?** ETA, "after 12", "must leave before 1:30".

Truck type, load, weight, dock compatibility and the current booking are never asked;
they come from the database. *Evidence:* `tests/test_tools.py::test_resolution_rules`,
ladder cases "Two assignments" and "Driver on two moving trucks".

### 2. Should a displayed option reserve capacity, or only explicit confirmation?

**Displayed options are held for 2 minutes** (`slot_holds`, one of only two added
tables, with its own partial unique index). Without a hold, a driver often picks a slot
that is gone by the time they reply. With long holds, a delay wave freezes scarce docks.
A 75-minute unload holds its extension slot too, all or nothing. An expired hold gives an
honest "no longer available" and a fresh search. *Evidence:* ladder case "Hold expires",
`test_extension_slot_cannot_be_double_used`.

### 3. Two drivers choosing the same slot at nearly the same time?

Three layers, plus one rule:
1. Holds: the second driver can't even be offered the slot (`ux_active_hold_per_slot`).
2. `confirm` runs a conditional `UPDATE ... WHERE status='ACTIVE' AND expires_at > now`
   inside `BEGIN IMMEDIATE`, so a hold is used once and only while valid.
3. The supplied `ux_active_appointment_per_slot` rejects any second active booking.
4. The WhatsApp confirmation is written to `operational_messages` only after `COMMIT`.

*Evidence:* `eval.py` race, 30 rounds × 6 drivers (the guide's SHP1006/1012/1013/1014 plus
SHP1009/1017) for the same few 12:00 slots: zero slots promised twice, zero false promises,
zero double bookings including extension slots. `tests/test_concurrency.py` races 10
holds, 10 direct bookings with no holds at all, and 10 confirms of one hold. Exactly one
wins each time.

### 4. How to balance priority, physical arrival, waiting time and commitments?

In the **scheduling tool** (`app/scheduler.py`), with a transparent score per option:
`weight[priority] × (wait + 2 × late) + 30 × changed + 30 × takes another truck's promise
+ 5 × uses a bigger dock than needed`, where weights are LOW 1, NORMAL 2, HIGH 3, CRITICAL 4.
Trucks are taken by availability in 60-minute windows, higher priority first within a
window. So a CRITICAL truck arriving at 10:50 picks before a LOW truck waiting since 08:20,
but nobody takes a slot before they can use it. **An early truck does not automatically
win:** SHP1003's request to go now costs the same as moving another truck's promise, so at
08:22 it keeps its 09:00 slot (ladder case). A promise only protects a slot its truck can
still reach, so SHP1006's 10:00 slot stops blocking others once its ETA is 11:20.

The **driver chat** offers the earliest feasible slots and doesn't reshuffle other trucks;
that's the scheduler's job, as a proposal an operator applies.

### 5. When should a pending warehouse confirmation expire?

The snapshot has two (`APT1013A`, `APT1014A`; data issue 13), one with its request still
`QUEUED`. Our proposal: a `PENDING_CONFIRMATION` booking expires at the earlier of 30
minutes after booking or 30 minutes before slot start, and then frees the slot. The chat
itself books `CONFIRMED`, because it runs every facility rule the warehouse would check
(dock type, weight, events, unload length, last start time) inside one transaction. It
also writes an `INTERNAL` notice to the warehouse coordinator, so the warehouse is told,
not asked.

### 6. What happens when no feasible compatible slot exists?

Escalate; never invent capacity. The driver is told operations will call. The exception is
set to `ESCALATED`, the thread to `ESCALATED`, and an `INTERNAL` message goes to the first
facility contact that has an email (shift manager, then coordinator). If no contact has an
email, the message is stored as `FAILED`, never as sent (compare OM004, which went to an
address that isn't a contact). *Evidence:* ladder case SHP1015 (D5 under maintenance after
18:00): the message goes to anita.joshi@setuhaul.example and is `SENT`.

### 7. Which actions need deterministic validation even if an LLM suggested them?

All of them that touch capacity or promises:
- choosing the shipment (only from the driver's own, by a reference the driver wrote)
- ETA arithmetic (current ETA as of now, plus the delay)
- dock compatibility, weight, dock events, unload length, last start time
- holding, confirming and cancelling
- any time, dock or order number in the reply (`phrasing.check`; otherwise the template is sent)

In agent mode these are guards inside each tool. *Evidence:* `tests/test_agent_mode.py`. A
scripted model tries to guess a shipment, confirm without an offer, record an ETA for a
truck in the yard, invent a time, and loop; every attempt is refused.

---

## Other decisions

### Workflow by default, agent as an option: built both, measured both

The LLM reads messages and words replies; code picks every step (`service.py`). Agent mode
(`agent_mode.py`) lets the LLM choose tools through OpenRouter tool calling, over the same
`Desk`, so both modes apply identical rules and leave the same audit trail.

**Measured live on this snapshot** (Claude Haiku 4.5 via OpenRouter, 16 seeded cases +
3 race rounds × 6 drivers; table in `reports/eval_report.md`):

| | Workflow | Agent |
|---|---|---|
| Seeded cases passed | 16 / 16 | 15 / 16 (run 1: 13 / 16) |
| False promises / double bookings | 0 / 0 | 0 / 0 |
| Median time per message | 2.4 s | 2.4 s |
| Tool calls chosen by the LLM per message | 0 | 1.1 |
| Replies in LLM wording (passed the fact check) | 92% | 83% |

**What the agent did, and what stopped it:**
1. Asked "How late exactly? 30 minutes, or more?" for "Running 30 min late" (run 1). Fixed in the prompt.
2. In run 1, "I reached late and am in the yard" was treated as a delay report by *our* agent
   pre-step, which matched the word "late". It now uses the message reader's intent, like
   the workflow. The evaluation found this bug in our code, not the model's.
3. Told SHP1013 "you're all set" on its `PENDING_CONFIRMATION` booking (run 2). Nothing was
   booked wrongly, but it overstated a pending booking. The backend facts and status replies
   now say "pending warehouse confirmation", and the prompt forbids "all set" on it.
4. Tried to record an ETA before the shipment was identified (live demo, DRV007). The tool
   guard refused it, and the driver was asked which order.
5. After the driver named the truck, it called `offer_slots` without re-recording the 12:00
   ETA from the parked message. The workflow resumes the parked message and records it.

Across all of this, no false promise and no double booking, because capacity and identity
are guarded in code, not by the prompt. **Decision:** the workflow stays the default for the
driver path (16/16, behaviour independent of prompt wording). Agent mode stays available
(`"mode": "agent"`, `AGENT_MODE=agent`, UI toggle) as the base for open-ended operator
requests where the steps can't be planned in advance. (The earlier comparison on the
stand-in data showed the same pattern: workflow 12/12, agent 11/12.)

**Capacity under a staggered wave.** With the LLM on, requests arrive spread over seconds,
so the first driver can hold 2–3 of the few free slots while choosing. Live, 5 (workflow)
and 3 (agent) of 18 race conversations got a slot, against about 3 per round offline. It
stays correct, but slots sit held for 2 minutes. Next step: hold one option when 3 or fewer
compatible slots are free.

### Write into the supplied tables, add only two

Conversations go to `chat_threads` / `chat_messages` (intent, extracted ETA, duplicate
flag, `requires_human_review`). Delays go to `eta_updates` (`DRIVER_DECLARED`, confidence,
reason) and `driver_exceptions` (one per thread, with `dedupe_key`). Rebookings go to
`appointments` (old row `CANCELLED`, `is_current = 0`; new row `replaced_appointment_id`).
Extension slots are marked `BLOCKED` with "Unload extension for APT-…". Notices go to
`operational_messages`. Only `slot_holds` and `agent_thread_state` are new, because the
schema has no place for them. The supplied file is never modified; the service works on a
copy, and `POST /reset` restores it.

### Unloads longer than a slot use the next slot as an extension

Standard slots are 60 minutes; SHP1005, SHP1009 and SHP1014 need 75 (data issue 4). We
book the main slot and block the next slot on the same dock, if it's free, as one unit.
Rebooking releases the old extension. *Evidence:* ladder case SHP1014,
`test_rebooking_releases_previous_extension`.

### Dock type, weight and scarce docks

Reefer loads go only to D5 (RULE003). Loads over 25 t, or marked HEAVY, go only to D6
(RULE004). Standard loads go only to standard docks, so D5 and D6 stay free for the loads
that can't go anywhere else. Weight is checked against the dock (data issue 3). Among equal
times, the dock with the least spare weight comes first, keeping the 25 t docks for 21 t loads.

### "Now" is chosen per request

The snapshot is not one moment (data issue 18): records run to 16:30 while ten trucks are
"in transit" with morning ETAs. Every request carries its own clock, and ETA history is read
as of that clock. Replaying SHP1013's 09:19 message must not see ETA008, created at 09:20 by
that same message, or the delay is counted twice.

### Timestamps

All timestamps share `+05:30` (guard 23 is clean), so comparing them as strings is safe.
SQLite's `datetime()` converts them to UTC, though: SHP1002's 70-minute unload from 08:08
came out as "03:48". All time arithmetic is done in Python (`timeutil.py`) or with
`julianday()`. The first version of data check 6 missed two vehicle conflicts because of this.

### Trucks on site

Gate-in beats the chat. A truck in the dock gets its status. A truck in the yard keeps its
slot if the slot is still valid (within the 30-minute no-show grace, RULE002, and the dock
isn't broken). Otherwise it is re-slotted from now: SHP1004 after 09:30 gets D2 10:00, freed
by the APT1008 cancellation, and SHP1005 is moved off the broken D3.

### Duplicates

The same `external_message_id`, or the same text from the same driver within 3 minutes, is
answered ("already working on it") and logged with `is_duplicate = 1`, but not processed
again: no second ETA row, no second set of holds (compare THR009, left open in the data).

### Uncertain ETAs

A range ("between 12:30 and 13:00") is planned at its later end with `confidence = LOW`, and
`requires_human_review` is set. A relative delay is added to the current ETA, and the reply
states the arithmetic ("11:00 AM (10:00 AM plus 60 min)") so the driver can correct it.

### Known limits

- The extractor's offline regex covers the dataset's English phrasing. Hindi and Hinglish
  need the LLM (OpenRouter), which is on in the deployment.
- The scheduler is greedy, not optimal; it shows its score so every choice can be
  explained. CP-SAT would be the next step if greedy leaves measurable waiting at real scale.
- A truck can't yet be booked across two non-adjacent slots, and dock overruns (SHP1002) are
  read from `facility_checkins`, not predicted.
- Pending-confirmation expiry (question 5) is proposed, not automated.
