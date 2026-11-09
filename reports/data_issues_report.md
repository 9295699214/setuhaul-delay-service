# Data issues report

15 checks run, **15 found problems**. Each check is a query in `analysis/data_issues.sql`; the rows below are the evidence.

| # | Check | Rows |
|---|---|---|
| 1 | Overlapping slots on the same dock | 2 |
| 2 | Two active bookings at the same time on one dock | 1 |
| 3 | Active booking inside a blocked window | 1 |
| 4 | Shipment booked on an incompatible dock | 1 |
| 5 | Booking at a different facility from the shipment's destination | 1 |
| 6 | Active booking for a cancelled or delivered shipment | 1 |
| 7 | Current ETA is after the booked slot has ended | 2 |
| 8 | ETA sources disagree by more than 60 minutes | 3 |
| 9 | Slot that ends before it starts | 1 |
| 10 | Unload time longer than the booked slot | 1 |
| 11 | Booking on an inactive dock | 1 |
| 12 | Truck at the gate with no active appointment | 1 |
| 13 | One phone number linked to more than one driver | 1 |
| 14 | Open shipment with no ETA from any source | 1 |
| 15 | Driver's last confirmation points to a slot that is no longer booked | 1 |

## 1. Overlapping slots on the same dock

Why it matters: the "one active booking per slot" index cannot see two different slots covering the same time.

| slot_a | slot_b | dock_id | a_start | a_end | b_start | b_end |
|---|---|---|---|---|---|---|
| JAI-D1-10 | JAI-D1-1030 | JAI-D1 | 2026-08-04 10:00 | 2026-08-04 11:00 | 2026-08-04 10:30 | 2026-08-04 11:30 |
| JAI-D1-1030 | JAI-D1-11 | JAI-D1 | 2026-08-04 10:30 | 2026-08-04 11:30 | 2026-08-04 11:00 | 2026-08-04 12:00 |

<details><summary>Query</summary>

```sql
SELECT a.id AS slot_a, b.id AS slot_b, a.dock_id, a.start_at AS a_start, a.end_at AS a_end,
       b.start_at AS b_start, b.end_at AS b_end
FROM slots a JOIN slots b
  ON a.dock_id = b.dock_id AND a.id < b.id
 AND a.start_at < b.end_at AND b.start_at < a.end_at
 AND a.end_at > a.start_at AND b.end_at > b.start_at
```

</details>

## 2. Two active bookings at the same time on one dock

Why it matters: a physical double booking that passes both unique indexes.

| dock_id | shipment_a | a_start | shipment_b | b_start |
|---|---|---|---|---|
| JAI-D1 | SH-01 | 2026-08-04 10:00 | SH-07 | 2026-08-04 10:30 |

<details><summary>Query</summary>

```sql
SELECT s1.dock_id, a1.shipment_id AS shipment_a, s1.start_at AS a_start,
       a2.shipment_id AS shipment_b, s2.start_at AS b_start
FROM appointments a1 JOIN slots s1 ON s1.id = a1.slot_id
JOIN appointments a2 ON a2.id > a1.id AND a2.status = 'active'
JOIN slots s2 ON s2.id = a2.slot_id AND s2.dock_id = s1.dock_id
WHERE a1.status = 'active' AND s1.start_at < s2.end_at AND s2.start_at < s1.end_at
```

</details>

## 3. Active booking inside a blocked window

Why it matters: the truck is told to unload at a dock that is under maintenance.

| shipment_id | dock_id | start_at | end_at | reason | block_start | block_end |
|---|---|---|---|---|---|---|
| SH-08 | JAI-D2 | 2026-08-04 14:00 | 2026-08-04 15:00 | maintenance | 2026-08-04 14:00 | 2026-08-04 16:00 |

<details><summary>Query</summary>

```sql
SELECT a.shipment_id, s.dock_id, s.start_at, s.end_at, b.reason, b.start_at AS block_start, b.end_at AS block_end
FROM appointments a JOIN slots s ON s.id = a.slot_id
JOIN dock_blocks b ON b.dock_id = s.dock_id AND b.start_at < s.end_at AND s.start_at < b.end_at
WHERE a.status = 'active'
```

</details>

## 4. Shipment booked on an incompatible dock

Why it matters: a reefer load at a dry dock spoils; an overweight load cannot be handled.

| shipment_id | dock | needs_reefer | supports_reefer | load_weight_kg | max_weight_kg |
|---|---|---|---|---|---|
| SH-09 | JAI-D1 | 1 | 0 | 8000 | 20000 |

<details><summary>Query</summary>

```sql
SELECT a.shipment_id, d.id AS dock, sh.needs_reefer, d.supports_reefer, sh.load_weight_kg, d.max_weight_kg
FROM appointments a JOIN shipments sh ON sh.id = a.shipment_id
JOIN slots s ON s.id = a.slot_id JOIN docks d ON d.id = s.dock_id
WHERE a.status = 'active'
  AND ((sh.needs_reefer = 1 AND d.supports_reefer = 0) OR sh.load_weight_kg > d.max_weight_kg)
```

</details>

## 5. Booking at a different facility from the shipment's destination

Why it matters: the truck would drive to the wrong city.

| shipment_id | shipment_facility | dock_facility | slot |
|---|---|---|---|
| SH-10 | AMD-WH | JAI-DC | JAI-D1-16 |

<details><summary>Query</summary>

```sql
SELECT a.shipment_id, sh.facility_id AS shipment_facility, d.facility_id AS dock_facility, s.id AS slot
FROM appointments a JOIN shipments sh ON sh.id = a.shipment_id
JOIN slots s ON s.id = a.slot_id JOIN docks d ON d.id = s.dock_id
WHERE a.status = 'active' AND sh.facility_id != d.facility_id
```

</details>

## 6. Active booking for a cancelled or delivered shipment

Why it matters: dead bookings hide capacity that late drivers need.

| shipment_id | status | slot_id |
|---|---|---|
| SH-11 | cancelled | JAI-D2-17 |

<details><summary>Query</summary>

```sql
SELECT a.shipment_id, sh.status, a.slot_id
FROM appointments a JOIN shipments sh ON sh.id = a.shipment_id
WHERE a.status = 'active' AND sh.status IN ('cancelled', 'delivered')
```

</details>

## 7. Current ETA is after the booked slot has ended

Why it matters: the booking is already broken; the driver needs a new slot. Uses the same trust order as the service (ops override first).

| shipment_id | current_eta | source | start_at | end_at |
|---|---|---|---|---|
| SH-12 | 2026-08-04 09:40 | driver | 2026-08-04 08:00 | 2026-08-04 09:00 |
| SH-19 | 2026-08-04 14:20 | driver | 2026-08-04 12:00 | 2026-08-04 13:00 |

<details><summary>Query</summary>

```sql
WITH ranked AS (
  SELECT shipment_id, eta, source,
         ROW_NUMBER() OVER (PARTITION BY shipment_id
                            ORDER BY (source = 'ops_override') DESC, created_at DESC, id DESC) rn
  FROM eta_updates)
SELECT a.shipment_id, r.eta AS current_eta, r.source, s.start_at, s.end_at
FROM appointments a JOIN slots s ON s.id = a.slot_id
JOIN ranked r ON r.shipment_id = a.shipment_id AND r.rn = 1
WHERE a.status = 'active' AND r.eta >= s.end_at
```

</details>

## 8. ETA sources disagree by more than 60 minutes

Why it matters: the system must pick one source to trust, and should say which.

| shipment_id | earliest_eta | latest_eta | sources |
|---|---|---|---|
| SH-02 | 2026-08-04 11:00 | 2026-08-04 12:30 | plan=11:00 | ops_override=11:30 | driver=12:30 |
| SH-12 | 2026-08-04 08:00 | 2026-08-04 09:40 | plan=08:00 | driver=09:40 |
| SH-19 | 2026-08-04 12:00 | 2026-08-04 14:20 | plan=12:00 | driver=14:20 |

<details><summary>Query</summary>

```sql
SELECT shipment_id, MIN(eta) AS earliest_eta, MAX(eta) AS latest_eta,
       GROUP_CONCAT(source || '=' || substr(eta, 12), ' | ') AS sources
FROM eta_updates GROUP BY shipment_id
HAVING ROUND((julianday(MAX(eta)) - julianday(MIN(eta))) * 1440) > 60
```

</details>

## 9. Slot that ends before it starts

Why it matters: impossible window; must never be offered.

| id | dock_id | start_at | end_at |
|---|---|---|---|
| AMD-A2-BAD | AMD-A2 | 2026-08-04 12:00 | 2026-08-04 11:00 |

<details><summary>Query</summary>

```sql
SELECT id, dock_id, start_at, end_at FROM slots WHERE end_at <= start_at
```

</details>

## 10. Unload time longer than the booked slot

Why it matters: the truck overruns into the next booking on that dock.

| shipment_id | unload_minutes | slot_minutes | slot |
|---|---|---|---|
| SH-13 | 90 | 60.0 | JAI-D2-12 |

<details><summary>Query</summary>

```sql
SELECT a.shipment_id, sh.unload_minutes,
       ROUND((julianday(s.end_at) - julianday(s.start_at)) * 1440) AS slot_minutes, s.id AS slot
FROM appointments a JOIN shipments sh ON sh.id = a.shipment_id JOIN slots s ON s.id = a.slot_id
WHERE a.status = 'active'
  AND sh.unload_minutes > ROUND((julianday(s.end_at) - julianday(s.start_at)) * 1440)
```

</details>

## 11. Booking on an inactive dock

Why it matters: the dock is out of service.

| shipment_id | dock | name |
|---|---|---|
| SH-14 | JAI-D5 | Jaipur Dock 5 |

<details><summary>Query</summary>

```sql
SELECT a.shipment_id, d.id AS dock, d.name
FROM appointments a JOIN slots s ON s.id = a.slot_id JOIN docks d ON d.id = s.dock_id
WHERE a.status = 'active' AND d.active = 0
```

</details>

## 12. Truck at the gate with no active appointment

Why it matters: it is waiting on site (detention) with nowhere to go.

| shipment_id | facility_id | gate_in_at |
|---|---|---|
| SH-15 | JAI-DC | 2026-08-04 07:50 |

<details><summary>Query</summary>

```sql
SELECT g.shipment_id, g.facility_id, g.at AS gate_in_at
FROM gate_events g
WHERE g.event = 'gate_in'
  AND NOT EXISTS (SELECT 1 FROM gate_events o WHERE o.shipment_id = g.shipment_id
                  AND o.event = 'gate_out' AND o.at > g.at)
  AND NOT EXISTS (SELECT 1 FROM appointments a WHERE a.shipment_id = g.shipment_id AND a.status = 'active')
```

</details>

## 13. One phone number linked to more than one driver

Why it matters: an incoming message cannot be matched to one shipment, so the agent must ask.

| phone | drivers | who |
|---|---|---|
| +919800000018 | 2 | DR-18 Jignesh, DR-19 Hitesh |

<details><summary>Query</summary>

```sql
SELECT phone, COUNT(*) AS drivers, GROUP_CONCAT(id || ' ' || name, ', ') AS who
FROM drivers GROUP BY phone HAVING COUNT(*) > 1
```

</details>

## 14. Open shipment with no ETA from any source

Why it matters: neither the agent nor the scheduler can plan it.

| id | facility_id | status |
|---|---|---|
| SH-16 | JAI-DC | in_transit |

<details><summary>Query</summary>

```sql
SELECT sh.id, sh.facility_id, sh.status
FROM shipments sh
WHERE sh.status IN ('in_transit', 'arrived')
  AND NOT EXISTS (SELECT 1 FROM eta_updates e WHERE e.shipment_id = sh.id)
```

</details>

## 15. Driver's last confirmation points to a slot that is no longer booked

Why it matters: a promise the driver believes but the database does not hold.

| shipment_id | confirmed_slot | sent_at | actual_active_slot |
|---|---|---|---|
| SH-21 | AMD-A2-13 | 2026-08-04 04:05 | AMD-A2-15 |

<details><summary>Query</summary>

```sql
WITH last_conf AS (
  SELECT shipment_id, slot_id, sent_at,
         ROW_NUMBER() OVER (PARTITION BY shipment_id ORDER BY sent_at DESC, id DESC) rn
  FROM outbound_confirmations)
SELECT c.shipment_id, c.slot_id AS confirmed_slot, c.sent_at, a.slot_id AS actual_active_slot
FROM last_conf c
LEFT JOIN appointments a ON a.shipment_id = c.shipment_id AND a.status = 'active'
WHERE c.rn = 1 AND (a.slot_id IS NULL OR a.slot_id != c.slot_id)
```

</details>
