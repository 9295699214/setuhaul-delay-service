# Data issues report

23 checks run against the supplied snapshot (4 Aug 2026), **18 found problems**. Each check is a query in `analysis/data_issues.sql`; the rows below are the evidence. Checks with no rows are guards that pass on this snapshot.

| # | Check | Rows |
|---|---|---|
| 1 | Confirmed booking during a dock breakdown | 1 |
| 2 | Slot still OPEN although a dock event covers it | 1 |
| 3 | Active booking heavier than the dock allows | 2 |
| 4 | Unload takes longer than the booked slot | 4 |
| 5 | Latest ETA is after the booked slot starts (truck not yet at the gate) | 4 |
| 6 | Vehicle booked for a new trip while it is still busy on the previous one | 3 |
| 7 | One driver on two moving trucks at once | 1 |
| 8 | Driver with more than one open shipment | 4 |
| 9 | latest_eta_ts changed with no ETA update behind it | 4 |
| 10 | Booking resting on a low-confidence ETA | 2 |
| 11 | Duplicate driver message left as an open thread | 1 |
| 12 | Operational message not delivered | 2 |
| 13 | Warehouse confirmation still pending | 2 |
| 14 | No-show with nothing done afterwards | 1 |
| 15 | Facility contact without an email | 1 |
| 16 | Truck in dock running into the next booking | 1 |
| 17 | Truck waiting in the yard for a dock that is broken | 1 |
| 18 | The snapshot is not one moment in time | 10 |
| 19 | Booking at a facility other than the shipment's destination | 0 |
| 20 | Load type and dock type mismatch | 0 |
| 21 | Load over vehicle capacity, or reefer load on a non-reefer truck | 0 |
| 22 | Load over 25 t not marked heavy (RULE004) | 0 |
| 23 | Timestamps not in +05:30 | 0 |

## 1. Confirmed booking during a dock breakdown

Why it matters: SHP1005 is told to unload at D3 09:00-10:00 while the leveller is broken from 09:15.

| appointment_id | shipment_id | slot_id | slot_status | slot_start | slot_end | dock_event_id | event_type | event_start | event_end |
|---|---|---|---|---|---|---|---|---|---|
| APT1005 | SHP1005 | SLOT-JAI-030 | OPEN | 09:00 | 10:00 | DEVT001 | BREAKDOWN | 09:15 | 13:00 |

<details><summary>Query</summary>

```sql
SELECT a.appointment_id, a.shipment_id, sl.slot_id, sl.slot_status,
       substr(sl.slot_start_ts,12,5) AS slot_start, substr(sl.slot_end_ts,12,5) AS slot_end,
       e.dock_event_id, e.event_type, substr(e.event_start_ts,12,5) AS event_start, substr(e.event_end_ts,12,5) AS event_end
FROM appointments a
JOIN appointment_slots sl ON sl.slot_id = a.slot_id
JOIN dock_status_events e ON e.dock_id = sl.dock_id
WHERE a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')
  AND e.event_type IN ('BREAKDOWN','MAINTENANCE','MANUAL_BLOCK')
  AND e.event_start_ts < sl.slot_end_ts AND COALESCE(e.event_end_ts,'9999') > sl.slot_start_ts
```

</details>

## 2. Slot still OPEN although a dock event covers it

Why it matters: slot_status and dock_status_events disagree, so v_slot_availability can offer a broken dock.

| slot_id | dock_id | slot_status | slot_start | dock_event_id | event_type | reason |
|---|---|---|---|---|---|---|
| SLOT-JAI-030 | DOCK-JAI-D3 | OPEN | 09:00 | DEVT001 | BREAKDOWN | Hydraulic dock leveller failure |

<details><summary>Query</summary>

```sql
SELECT sl.slot_id, sl.dock_id, sl.slot_status, substr(sl.slot_start_ts,12,5) AS slot_start,
       e.dock_event_id, e.event_type, e.reason
FROM appointment_slots sl
JOIN dock_status_events e ON e.dock_id = sl.dock_id
WHERE sl.slot_status = 'OPEN'
  AND e.event_type IN ('BREAKDOWN','MAINTENANCE','MANUAL_BLOCK')
  AND e.event_start_ts < sl.slot_end_ts AND COALESCE(e.event_end_ts,'9999') > sl.slot_start_ts
```

</details>

## 3. Active booking heavier than the dock allows

Why it matters: the dock cannot physically take the vehicle; the booking will fail at the gate.

| appointment_id | shipment_id | appointment_status | load_weight_kg | dock_code | max_vehicle_weight_kg |
|---|---|---|---|---|---|
| APT1014A | SHP1014 | PENDING_CONFIRMATION | 21000 | D1 | 20000 |
| APT1005 | SHP1005 | CONFIRMED | 20500 | D3 | 20000 |

<details><summary>Query</summary>

```sql
SELECT a.appointment_id, a.shipment_id, a.appointment_status, s.load_weight_kg, d.dock_code, d.max_vehicle_weight_kg
FROM appointments a
JOIN shipments s ON s.shipment_id = a.shipment_id
JOIN appointment_slots sl ON sl.slot_id = a.slot_id
JOIN docks d ON d.dock_id = sl.dock_id
WHERE a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')
  AND s.load_weight_kg > d.max_vehicle_weight_kg
```

</details>

## 4. Unload takes longer than the booked slot

Why it matters: the truck overruns into the next booking on that dock (SHP1002 already has).

| appointment_id | shipment_id | expected_unload_min | slot_minutes |
|---|---|---|---|
| APT1014A | SHP1014 | 75 | 60 |
| APT1002 | SHP1002 | 70 | 60 |
| APT1005 | SHP1005 | 75 | 60 |
| APT1009 | SHP1009 | 75 | 60 |

<details><summary>Query</summary>

```sql
SELECT a.appointment_id, a.shipment_id, s.expected_unload_min,
       CAST(ROUND((julianday(sl.slot_end_ts) - julianday(sl.slot_start_ts)) * 1440) AS INTEGER) AS slot_minutes
FROM appointments a
JOIN shipments s ON s.shipment_id = a.shipment_id
JOIN appointment_slots sl ON sl.slot_id = a.slot_id
WHERE a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')
  AND s.expected_unload_min > ROUND((julianday(sl.slot_end_ts) - julianday(sl.slot_start_ts)) * 1440)
```

</details>

## 5. Latest ETA is after the booked slot starts (truck not yet at the gate)

Why it matters: the booking is already broken; these drivers need a new slot before they arrive.

| appointment_id | shipment_id | appointment_status | eta | eta_confidence | slot_start | slot_end |
|---|---|---|---|---|---|---|
| APT1006 | SHP1006 | CONFIRMED | 11:20 | HIGH | 10:00 | 11:00 |
| APT1014A | SHP1014 | PENDING_CONFIRMATION | 11:25 | HIGH | 11:00 | 12:00 |
| APT1015A | SHP1015 | CONFIRMED | 18:30 | HIGH | 17:00 | 18:00 |
| APT1017 | SHP1017 | CONFIRMED | 12:45 | LOW | 12:00 | 13:00 |

<details><summary>Query</summary>

```sql
SELECT a.appointment_id, a.shipment_id, a.appointment_status, substr(le.effective_eta_ts,12,5) AS eta,
       le.eta_confidence, substr(sl.slot_start_ts,12,5) AS slot_start, substr(sl.slot_end_ts,12,5) AS slot_end
FROM appointments a
JOIN v_latest_eta le ON le.shipment_id = a.shipment_id
JOIN appointment_slots sl ON sl.slot_id = a.slot_id
LEFT JOIN facility_checkins fc ON fc.shipment_id = a.shipment_id
WHERE a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED')
  AND fc.gate_in_ts IS NULL
  AND le.effective_eta_ts > sl.slot_start_ts
```

</details>

## 6. Vehicle booked for a new trip while it is still busy on the previous one

Why it matters: the next trip departs from another city before the truck has left Jaipur.

| vehicle_id | busy_shipment | busy_status | busy_until | next_shipment | next_status | next_departs | next_from |
|---|---|---|---|---|---|---|---|
| VEH001 | SHP1001 | COMPLETED | 08:50 | SHP1017 | IN_TRANSIT | 08:00 | Neemrana |
| VEH003 | SHP1003 | WAITING | 09:05 | SHP1018 | ASSIGNED | 08:00 | Bhiwadi |
| VEH015 | SHP1006 | IN_TRANSIT | 12:05 | SHP1021 | ASSIGNED | 10:00 | Noida |

<details><summary>Query</summary>

```sql
SELECT s1.vehicle_id, s1.shipment_id AS busy_shipment, s1.current_status AS busy_status,
       strftime('%H:%M', COALESCE(julianday(fc.gate_out_ts), julianday(le.effective_eta_ts) + s1.expected_unload_min/1440.0) + 5.5/24) AS busy_until,
       s2.shipment_id AS next_shipment, s2.current_status AS next_status,
       substr(s2.planned_departure_ts,12,5) AS next_departs, s2.origin_city AS next_from
FROM shipments s1
JOIN shipments s2 ON s2.vehicle_id = s1.vehicle_id AND s2.shipment_id > s1.shipment_id
JOIN v_latest_eta le ON le.shipment_id = s1.shipment_id
LEFT JOIN facility_checkins fc ON fc.shipment_id = s1.shipment_id
WHERE s1.current_status <> 'CANCELLED' AND s2.current_status <> 'CANCELLED'
  AND julianday(s2.planned_departure_ts) < COALESCE(julianday(fc.gate_out_ts), julianday(le.effective_eta_ts) + s1.expected_unload_min/1440.0)
```

</details>

## 7. One driver on two moving trucks at once

Why it matters: physically impossible; one of the two ETAs (and its slot) cannot be trusted.

| driver_id | moving_trips | trips |
|---|---|---|
| DRV007 | 2 | SHP1007 on VEH008 (IN_TRANSIT) | SHP1016 on VEH007 (IN_TRANSIT) |

<details><summary>Query</summary>

```sql
SELECT driver_id, COUNT(*) AS moving_trips,
       GROUP_CONCAT(shipment_id || ' on ' || vehicle_id || ' (' || current_status || ')', ' | ') AS trips
FROM shipments
WHERE current_status IN ('IN_TRANSIT','AT_GATE','WAITING','IN_DOCK')
GROUP BY driver_id HAVING COUNT(*) > 1
```

</details>

## 8. Driver with more than one open shipment

Why it matters: a message from that phone cannot be matched to one shipment without a rule or a question.

| driver_id | open_shipments | shipments |
|---|---|---|
| DRV003 | 2 | SHP1003 WAITING, SHP1018 ASSIGNED |
| DRV004 | 2 | SHP1004 WAITING, SHP1020 ASSIGNED |
| DRV006 | 2 | SHP1006 IN_TRANSIT, SHP1021 ASSIGNED |
| DRV007 | 2 | SHP1007 IN_TRANSIT, SHP1016 IN_TRANSIT |

<details><summary>Query</summary>

```sql
SELECT driver_id, COUNT(*) AS open_shipments,
       GROUP_CONCAT(shipment_id || ' ' || current_status, ', ') AS shipments
FROM shipments
WHERE current_status NOT IN ('COMPLETED','CANCELLED')
GROUP BY driver_id HAVING COUNT(*) > 1
```

</details>

## 9. latest_eta_ts changed with no ETA update behind it

Why it matters: v_latest_eta falls back to a value nobody can trace to a source or confidence.

| shipment_id | original_eta | latest_eta |
|---|---|---|
| SHP1002 | 07:55 | 07:58 |
| SHP1009 | 10:45 | 10:50 |
| SHP1010 | 10:50 | 10:55 |
| SHP1011 | 10:40 | 10:45 |

<details><summary>Query</summary>

```sql
SELECT s.shipment_id, substr(s.original_eta_ts,12,5) AS original_eta, substr(s.latest_eta_ts,12,5) AS latest_eta
FROM shipments s
WHERE s.latest_eta_ts <> s.original_eta_ts
  AND NOT EXISTS (SELECT 1 FROM eta_updates e WHERE e.shipment_id = s.shipment_id)
```

</details>

## 10. Booking resting on a low-confidence ETA

Why it matters: "late by one hour" and "12:30 to 13:00" were turned into firm times and slots.

| appointment_id | shipment_id | appointment_status | eta_source | eta_confidence | eta_note |
|---|---|---|---|---|---|
| APT1013A | SHP1013 | PENDING_CONFIRMATION | DRIVER_DECLARED | LOW | Driver said only: late by one hour. |
| APT1017 | SHP1017 | CONFIRMED | DRIVER_DECLARED | LOW | Driver expects arrival between 12:30 and 13:00. |

<details><summary>Query</summary>

```sql
SELECT a.appointment_id, a.shipment_id, a.appointment_status, le.eta_source, le.eta_confidence, le.eta_note
FROM appointments a
JOIN v_latest_eta le ON le.shipment_id = a.shipment_id
WHERE a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED') AND le.eta_confidence = 'LOW'
```

</details>

## 11. Duplicate driver message left as an open thread

Why it matters: the retry (THR009) still looks like a live request and could get a second set of slots.

| thread_id | thread_status | chat_message_id | external_message_id | message_text |
|---|---|---|---|---|
| THR009 | OPEN | MSG015 | wa-9009 | Traffic after Shahpura. Reaching around 11:20. Any slot after 12? |

<details><summary>Query</summary>

```sql
SELECT t.thread_id, t.thread_status, m.chat_message_id, m.external_message_id, m.message_text
FROM chat_messages m JOIN chat_threads t ON t.thread_id = m.thread_id
WHERE m.is_duplicate = 1 AND t.thread_status NOT IN ('CLOSED','RESOLVED')
```

</details>

## 12. Operational message not delivered

Why it matters: SHP1015's escalation FAILED, to an address that is not a facility contact; the thread says ESCALATED but nobody was told.

| operational_message_id | shipment_id | recipient_address | delivery_status | is_known_contact | threads |
|---|---|---|---|---|---|
| OM002 | SHP1013 | neha.bansal@setuhaul.example | QUEUED | 1 | THR003:WAITING_FOR_DRIVER |
| OM004 | SHP1015 | night.shift@setuhaul.example | FAILED | 0 | THR005:ESCALATED |

<details><summary>Query</summary>

```sql
SELECT o.operational_message_id, o.shipment_id, o.recipient_address, o.delivery_status,
       (SELECT COUNT(*) FROM facility_contacts fc WHERE fc.email = o.recipient_address) AS is_known_contact,
       (SELECT GROUP_CONCAT(t.thread_id || ':' || t.thread_status) FROM chat_threads t WHERE t.shipment_id = o.shipment_id) AS threads
FROM operational_messages o
WHERE o.delivery_status IN ('FAILED','QUEUED')
```

</details>

## 13. Warehouse confirmation still pending

Why it matters: the slot is locked by a pending booking that may never be confirmed; it needs an expiry.

| appointment_id | shipment_id | slot_id | booked_at | confirmation_request |
|---|---|---|---|---|
| APT1013A | SHP1013 | SLOT-JAI-018 | 09:32 | OM002:QUEUED |
| APT1014A | SHP1014 | SLOT-JAI-004 | 09:52 | OM001:SENT |

<details><summary>Query</summary>

```sql
SELECT a.appointment_id, a.shipment_id, a.slot_id, substr(a.booked_at,12,5) AS booked_at,
       (SELECT GROUP_CONCAT(o.operational_message_id || ':' || o.delivery_status) FROM operational_messages o
        WHERE o.appointment_id = a.appointment_id) AS confirmation_request
FROM appointments a WHERE a.appointment_status = 'PENDING_CONFIRMATION'
```

</details>

## 14. No-show with nothing done afterwards

Why it matters: SHP1018 missed its slot, never departed and has no new booking, yet is still ASSIGNED.

| appointment_id | shipment_id | current_status | actual_departure_ts |
|---|---|---|---|
| APT1018 | SHP1018 | ASSIGNED |  |

<details><summary>Query</summary>

```sql
SELECT a.appointment_id, a.shipment_id, s.current_status, s.actual_departure_ts
FROM appointments a JOIN shipments s ON s.shipment_id = a.shipment_id
WHERE a.appointment_status = 'NO_SHOW' AND s.current_status NOT IN ('CANCELLED','COMPLETED')
  AND NOT EXISTS (SELECT 1 FROM appointments b WHERE b.shipment_id = a.shipment_id
                  AND b.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS'))
```

</details>

## 15. Facility contact without an email

Why it matters: escalations to Gurugram's night shift have no email route.

| contact_id | facility_id | contact_role | contact_name | email | phone |
|---|---|---|---|---|---|
| CON005 | FAC-GGN-01 | NIGHT_SHIFT_COORDINATOR | Vacant Position |  | +91-124-5001209 |

<details><summary>Query</summary>

```sql
SELECT contact_id, facility_id, contact_role, contact_name, email, phone
FROM facility_contacts WHERE email IS NULL OR email = ''
```

</details>

## 16. Truck in dock running into the next booking

Why it matters: SHP1002's 70-minute unload on D2 started 08:08 and runs to 09:18, over SHP1004's 09:00 slot.

| shipment_id | dock_code | unload_started | expected_unload_min | expected_end | next_shipment | next_slot_start |
|---|---|---|---|---|---|---|
| SHP1002 | D2 | 08:08 | 70 | 09:18 | SHP1004 | 09:00 |

<details><summary>Query</summary>

```sql
SELECT fc.shipment_id, d.dock_code, substr(fc.unload_start_ts,12,5) AS unload_started, s.expected_unload_min,
       strftime('%H:%M', julianday(fc.unload_start_ts) + s.expected_unload_min/1440.0 + 5.5/24) AS expected_end,
       a2.shipment_id AS next_shipment, substr(sl2.slot_start_ts,12,5) AS next_slot_start
FROM facility_checkins fc
JOIN shipments s ON s.shipment_id = fc.shipment_id
JOIN docks d ON d.dock_id = fc.actual_dock_id
JOIN appointment_slots sl2 ON sl2.dock_id = fc.actual_dock_id
JOIN appointments a2 ON a2.slot_id = sl2.slot_id AND a2.shipment_id <> fc.shipment_id
     AND a2.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')
WHERE fc.queue_state = 'IN_DOCK' AND fc.unload_end_ts IS NULL
  AND sl2.slot_start_ts > fc.unload_start_ts
  AND julianday(fc.unload_start_ts) + s.expected_unload_min/1440.0 > julianday(sl2.slot_start_ts)
```

</details>

## 17. Truck waiting in the yard for a dock that is broken

Why it matters: SHP1005 is at the gate with a booking it can never use; it needs re-slotting now.

| shipment_id | queue_state | slot_id | dock_code | event_type | reason |
|---|---|---|---|---|---|
| SHP1005 | WAITING_DOCK_UNAVAILABLE | SLOT-JAI-030 | D3 | BREAKDOWN | Hydraulic dock leveller failure |

<details><summary>Query</summary>

```sql
SELECT fc.shipment_id, fc.queue_state, sl.slot_id, d.dock_code, e.event_type, e.reason
FROM facility_checkins fc
JOIN appointments a ON a.shipment_id = fc.shipment_id AND a.is_current = 1
JOIN appointment_slots sl ON sl.slot_id = a.slot_id
JOIN docks d ON d.dock_id = sl.dock_id
JOIN dock_status_events e ON e.dock_id = sl.dock_id AND e.event_type IN ('BREAKDOWN','MAINTENANCE')
     AND e.event_start_ts < sl.slot_end_ts AND COALESCE(e.event_end_ts,'9999') > sl.slot_start_ts
WHERE fc.gate_out_ts IS NULL
```

</details>

## 18. The snapshot is not one moment in time

Why it matters: records run to 16:30 while 10 trucks are still "in transit" with morning ETAs; "now" must be chosen per question.

| latest_record | shipment_id | current_status | eta |
|---|---|---|---|
| 2026-08-04T16:30:00+05:30 | SHP1006 | IN_TRANSIT | 11:20 |
| 2026-08-04T16:30:00+05:30 | SHP1007 | IN_TRANSIT | 09:50 |
| 2026-08-04T16:30:00+05:30 | SHP1009 | IN_TRANSIT | 10:50 |
| 2026-08-04T16:30:00+05:30 | SHP1010 | IN_TRANSIT | 10:55 |
| 2026-08-04T16:30:00+05:30 | SHP1011 | IN_TRANSIT | 10:45 |
| 2026-08-04T16:30:00+05:30 | SHP1012 | IN_TRANSIT | 11:10 |
| 2026-08-04T16:30:00+05:30 | SHP1013 | IN_TRANSIT | 11:00 |
| 2026-08-04T16:30:00+05:30 | SHP1014 | IN_TRANSIT | 11:25 |
| 2026-08-04T16:30:00+05:30 | SHP1016 | IN_TRANSIT | 12:00 |
| 2026-08-04T16:30:00+05:30 | SHP1017 | IN_TRANSIT | 12:45 |

<details><summary>Query</summary>

```sql
SELECT (SELECT MAX(updated_at) FROM shipments) AS latest_record, s.shipment_id, s.current_status,
       substr(le.effective_eta_ts,12,5) AS eta
FROM shipments s JOIN v_latest_eta le ON le.shipment_id = s.shipment_id
WHERE s.current_status = 'IN_TRANSIT'
  AND le.effective_eta_ts < (SELECT MAX(updated_at) FROM shipments)
  AND NOT EXISTS (SELECT 1 FROM facility_checkins fc WHERE fc.shipment_id = s.shipment_id)
```

</details>

## 19. Booking at a facility other than the shipment's destination

Why it matters: guards the multi-facility model; clean in this snapshot.

No rows. Clean on this snapshot.

## 20. Load type and dock type mismatch

Why it matters: reefer loads need D5, heavy loads D6; clean in this snapshot.

No rows. Clean on this snapshot.

## 21. Load over vehicle capacity, or reefer load on a non-reefer truck

Why it matters: catches impossible assignments; clean in this snapshot.

No rows. Clean on this snapshot.

## 22. Load over 25 t not marked heavy (RULE004)

Why it matters: such a load would be offered standard docks; clean in this snapshot.

No rows. Clean on this snapshot.

## 23. Timestamps not in +05:30

Why it matters: string comparison of times is only safe if every row uses the same offset; clean.

No rows. Clean on this snapshot.
