"""Deterministic tools over the real SetuHaul schema. No LLM runs here.

Capacity changes only through hold_option(), confirm() and book_direct(), each
inside BEGIN IMMEDIATE, with the supplied partial unique index
ux_active_appointment_per_slot as the final guard (design question 3).
"""
import re
import sqlite3
import uuid

from . import timeutil as tu

ACTIVE = ("PENDING_CONFIRMATION", "CONFIRMED", "IN_PROGRESS")
ACTIVE_SQL = "('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')"
UNDERWAY = ("IN_TRANSIT", "AT_GATE", "WAITING", "IN_DOCK")
BLOCKING_EVENTS = "('BREAKDOWN','MAINTENANCE','MANUAL_BLOCK')"
HOLD_MINUTES = 2
AGENT_ADDRESS = "agent@setuhaul.example"


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")[-10:]


def norm_ref(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


# ---------------------------------------------------------------- facility facts

def facility(conn, facility_id: str) -> dict:
    f = dict(conn.execute("SELECT * FROM facilities WHERE facility_id=?", (facility_id,)).fetchone())
    rules = {r["rule_type"]: r["rule_value"] for r in conn.execute(
        "SELECT rule_type, rule_value FROM facility_rules WHERE facility_id=? AND active_flag=1", (facility_id,))}
    f["rules"] = rules
    f["no_show_grace_min"] = int(rules.get("NO_SHOW_GRACE_MIN", f["checkin_grace_min"]))
    f["last_new_start"] = rules.get("LAST_NEW_START_TIME", f["close_time"])
    f["heavy_kg"] = int(rules["HEAVY_DOCK_REQUIRED_KG"]) if "HEAVY_DOCK_REQUIRED_KG" in rules else None
    return f


def contact_email(conn, facility_id: str, roles=("SHIFT_MANAGER", "WAREHOUSE_COORDINATOR")) -> str | None:
    """First active contact with an email, in role order (CON005 has none: data issue 15)."""
    for role in roles:
        r = conn.execute("SELECT email FROM facility_contacts WHERE facility_id=? AND contact_role=? "
                         "AND active_flag=1 AND email IS NOT NULL AND email<>''", (facility_id, role)).fetchone()
        if r:
            return r["email"]
    r = conn.execute("SELECT email FROM facility_contacts WHERE facility_id=? AND active_flag=1 "
                     "AND email IS NOT NULL AND email<>'' ORDER BY contact_id", (facility_id,)).fetchone()
    return r["email"] if r else None


# ---------------------------------------------------------------- shipments and drivers

SHIPMENT_SQL = """
SELECT s.*, v.registration_number AS reg_no, d.driver_name, d.phone AS driver_phone,
       fc.gate_in_ts, fc.gate_out_ts, fc.queue_state, fc.unload_start_ts, fc.actual_dock_id,
       f.facility_name
FROM shipments s
JOIN drivers d ON d.driver_id = s.driver_id
JOIN vehicles v ON v.vehicle_id = s.vehicle_id
JOIN facilities f ON f.facility_id = s.destination_facility_id
LEFT JOIN facility_checkins fc ON fc.shipment_id = s.shipment_id
"""


def get_shipment(conn, shipment_id: str) -> dict | None:
    r = conn.execute(SHIPMENT_SQL + " WHERE s.shipment_id=?", (shipment_id,)).fetchone()
    return dict(r) if r else None


def driver_for_phone(conn, phone: str) -> dict | None:
    for r in conn.execute("SELECT * FROM drivers"):
        if digits(r["phone"]) == digits(phone) and digits(phone):
            return dict(r)
    return None


def shipments_for_driver(conn, driver_id: str, open_only: bool = True) -> list[dict]:
    where = " WHERE s.driver_id=?" + (" AND s.current_status NOT IN ('COMPLETED','CANCELLED')" if open_only else "")
    return [dict(r) for r in conn.execute(SHIPMENT_SQL + where + " ORDER BY s.original_eta_ts", (driver_id,))]


def on_site(sh: dict) -> bool:
    return bool(sh.get("gate_in_ts")) and not sh.get("gate_out_ts")


def resolve_shipment(candidates: list[dict], text: str, is_delay_report: bool):
    """Which shipment is this message about? Returns (shipment | None, how).

    1. One open shipment -> it.
    2. The text names one (order ref or its last digits, shipment id, unique truck number) -> it.
    3. Exactly one trip is underway (in transit / at gate / waiting / in dock) and the others
       have not started -> that one, unless the message reports a delay while that truck is
       already on site, which contradicts it (DRV004 / THR010) -> ask.
    4. Otherwise ask (DRV007 drives two moving trucks: data issue 7).
    """
    if len(candidates) == 1:
        return candidates[0], "only_open_shipment"
    if not candidates:
        return None, "none"
    t = norm_ref(text)
    words = set(re.findall(r"\b\d{3}\b", text or ""))
    regs = [c["reg_no"] for c in candidates]
    named = [c for c in candidates
             if norm_ref(c["order_reference"]) in t or norm_ref(c["shipment_id"]) in t
             or c["order_reference"][-3:] in words
             or (regs.count(c["reg_no"]) == 1 and norm_ref(c["reg_no"]) in t)]
    if len(named) == 1:
        return named[0], "named_in_message"
    underway = [c for c in candidates if c["current_status"] in UNDERWAY]
    if len(underway) == 1:
        u = underway[0]
        if is_delay_report and on_site(u):
            return None, "contradiction_delay_but_on_site"
        return u, "only_trip_underway"
    return None, "ambiguous"


def current_eta(conn, shipment_id: str, now: str | None = None) -> dict:
    """ETA as known at `now`: operations override first, then the newest ETA update, then the
    original plan. (v_latest_eta takes the newest update and ignores overrides.) Updates created
    after `now` are ignored: the snapshot holds records up to 16:30 (data issue 18), and replaying
    an 09:19 message must not see the 09:20 ETA that message itself produced."""
    asof = tu.norm(now) if now else "9999"
    r = conn.execute("""SELECT declared_eta_ts AS eta, source_type AS source, confidence_code AS confidence
        FROM eta_updates WHERE shipment_id=? AND created_at <= ?
        ORDER BY (source_type='OPERATIONS_OVERRIDE') DESC, created_at DESC, eta_update_id DESC LIMIT 1""",
                     (shipment_id, asof)).fetchone()
    if r:
        return dict(r)
    s = conn.execute("SELECT original_eta_ts, latest_eta_ts, updated_at FROM shipments WHERE shipment_id=?",
                     (shipment_id,)).fetchone()
    eta = s["latest_eta_ts"] if s["latest_eta_ts"] and s["updated_at"] <= asof else s["original_eta_ts"]
    return {"eta": eta, "source": "SHIPMENT_RECORD", "confidence": "MEDIUM"}


def current_appointment(conn, shipment_id: str) -> dict | None:
    r = conn.execute(f"""SELECT a.*, sl.slot_start_ts, sl.slot_end_ts, sl.dock_id, d.dock_code, d.dock_type
        FROM appointments a JOIN appointment_slots sl ON sl.slot_id = a.slot_id JOIN docks d ON d.dock_id = sl.dock_id
        WHERE a.shipment_id=? AND a.is_current=1 AND a.appointment_status IN {ACTIVE_SQL}""",
                     (shipment_id,)).fetchone()
    return dict(r) if r else None


def dock_event_during(conn, dock_id: str, start: str, end: str) -> dict | None:
    r = conn.execute(f"""SELECT * FROM dock_status_events WHERE dock_id=? AND event_type IN {BLOCKING_EVENTS}
        AND event_start_ts < ? AND COALESCE(event_end_ts,'9999') > ?""", (dock_id, end, start)).fetchone()
    return dict(r) if r else None


# ---------------------------------------------------------------- finding slots

def _slot_free_sql():
    """Shared availability conditions for slot alias `sl` (params: :sid, :now)."""
    return f"""
        sl.slot_status = 'OPEN'
        AND NOT EXISTS (SELECT 1 FROM appointments a JOIN appointment_slots s2 ON s2.slot_id = a.slot_id
            WHERE a.appointment_status IN {ACTIVE_SQL} AND a.shipment_id <> :sid
              AND s2.dock_id = sl.dock_id AND s2.slot_start_ts < sl.slot_end_ts AND s2.slot_end_ts > sl.slot_start_ts)
        AND NOT EXISTS (SELECT 1 FROM dock_status_events e WHERE e.dock_id = sl.dock_id
            AND e.event_type IN {BLOCKING_EVENTS}
            AND e.event_start_ts < sl.slot_end_ts AND COALESCE(e.event_end_ts,'9999') > sl.slot_start_ts)
        AND NOT EXISTS (SELECT 1 FROM slot_holds h WHERE h.slot_id = sl.slot_id AND h.status = 'ACTIVE'
            AND h.expires_at > :now AND h.shipment_id <> :sid)"""


def find_options(conn, shipment_id: str, earliest: str, latest_end: str | None = None,
                 now: str = tu.SNAPSHOT_NOW, limit: int = 3) -> list[dict]:
    """Slots this truck can really use, earliest first.

    Checks what the database's unique indexes do not: destination facility, active dock,
    dock type (REEFER -> refrigerated reefer dock, HEAVY -> heavy dock, STANDARD -> standard
    dock, ANY -> any, standard first), weight limit and RULE004, open slot, no overlapping
    active booking on the dock, no breakdown/maintenance event (data issues 1-2), no live hold
    by another driver, not in the past, no start after the last allowed start time (RULE005),
    and unload time: if the unload is longer than the slot, the next slot on the same dock
    must also be free and is taken as an extension (data issue 4).
    """
    sh = get_shipment(conn, shipment_id)
    if sh is None:
        raise ValueError(f"unknown shipment {shipment_id}")
    fac = facility(conn, sh["destination_facility_id"])
    start_from = max(tu.norm(earliest), tu.norm(now))
    last_start = tu.on_date(start_from, fac["last_new_start"])
    close = tu.on_date(start_from, fac["close_time"])
    req = sh["required_dock_type"]
    if fac["heavy_kg"] and sh["load_weight_kg"] > fac["heavy_kg"]:
        req = "HEAVY"
    type_sql = {"REEFER": "d.dock_type = 'REEFER' AND d.supports_refrigerated = 1",
                "HEAVY": "d.dock_type = 'HEAVY'",
                "STANDARD": "d.dock_type = 'STANDARD'",
                "ANY": "1 = 1"}[req]
    if sh["temperature_control_required"]:
        type_sql += " AND d.supports_refrigerated = 1"
    params = {"fac": sh["destination_facility_id"], "kg": sh["load_weight_kg"], "start": start_from,
              "last": last_start, "close": close, "sid": shipment_id, "now": tu.norm(now)}
    rows = conn.execute(f"""
        SELECT sl.slot_id, sl.dock_id, d.dock_code, d.dock_type, d.max_vehicle_weight_kg,
               sl.slot_start_ts, sl.slot_end_ts
        FROM appointment_slots sl JOIN docks d ON d.dock_id = sl.dock_id
        WHERE sl.facility_id = :fac AND d.dock_status = 'ACTIVE' AND {type_sql}
          AND d.max_vehicle_weight_kg >= :kg
          AND sl.slot_start_ts >= :start AND sl.slot_start_ts <= :last AND sl.slot_end_ts <= :close
          AND {_slot_free_sql()}
        ORDER BY sl.slot_start_ts, (d.dock_type <> 'STANDARD'), d.max_vehicle_weight_kg - :kg, d.dock_code
    """, params).fetchall()

    out = []
    for r in rows:
        r = dict(r)
        length = tu.minutes(r["slot_start_ts"], r["slot_end_ts"])
        ext = None
        if sh["expected_unload_min"] > length:
            ext = conn.execute(f"""SELECT sl.slot_id, sl.slot_start_ts, sl.slot_end_ts FROM appointment_slots sl
                WHERE sl.dock_id = :dock AND sl.slot_start_ts = :end AND sl.slot_end_ts <= :close
                  AND {_slot_free_sql()}""",
                               {**params, "dock": r["dock_id"], "end": r["slot_end_ts"]}).fetchone()
            if not ext or length + tu.minutes(ext["slot_start_ts"], ext["slot_end_ts"]) < sh["expected_unload_min"]:
                continue
        finish = tu.add(r["slot_start_ts"], sh["expected_unload_min"])
        if latest_end and finish > tu.norm(latest_end):
            continue
        out.append({"slot_id": r["slot_id"], "dock_id": r["dock_id"], "dock_code": r["dock_code"],
                    "dock_type": r["dock_type"], "start": r["slot_start_ts"], "end": r["slot_end_ts"],
                    "unload_until": finish, "extension_slot_id": ext["slot_id"] if ext else None})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- writes (each in its own transaction)

def record_eta(conn, sh: dict, eta: str, confidence: str, reason: str | None, note: str, now: str) -> str:
    """Append a DRIVER_DECLARED ETA and refresh shipments.latest_eta_ts (call inside a transaction)."""
    eid = new_id("ETA")
    conn.execute("""INSERT INTO eta_updates (eta_update_id, shipment_id, source_type, reported_by_driver_id,
        declared_eta_ts, confidence_code, delay_reason_code, note, created_at)
        VALUES (?,?, 'DRIVER_DECLARED', ?,?,?,?,?,?)""",
                 (eid, sh["shipment_id"], sh["driver_id"], eta, confidence, reason, note[:200], now))
    conn.execute("UPDATE shipments SET latest_eta_ts=?, updated_at=? WHERE shipment_id=?",
                 (eta, now, sh["shipment_id"]))
    return eid


def hold_option(conn, opt: dict, shipment_id: str, thread_id: str, now: str) -> dict:
    """Reserve an option (main slot + extension) for HOLD_MINUTES. Either all of it or nothing."""
    group = new_id("HOLD")
    slots = [(opt["slot_id"], "MAIN")] + ([(opt["extension_slot_id"], "EXTENSION")] if opt.get("extension_slot_id") else [])
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE slot_holds SET status='EXPIRED' WHERE status='ACTIVE' AND expires_at <= ?", (now,))
        for slot_id, role in slots:
            st = conn.execute("SELECT slot_status FROM appointment_slots WHERE slot_id=?", (slot_id,)).fetchone()
            booked = conn.execute(f"SELECT 1 FROM appointments WHERE slot_id=? AND appointment_status IN {ACTIVE_SQL} "
                                  "AND shipment_id<>?", (slot_id, shipment_id)).fetchone()
            if st["slot_status"] != "OPEN" or booked:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "slot_taken"}
            conn.execute("INSERT INTO slot_holds (group_id, slot_id, role, shipment_id, thread_id, created_at, expires_at) "
                         "VALUES (?,?,?,?,?,?,?)", (group, slot_id, role, shipment_id, thread_id, now,
                                                   tu.add(now, HOLD_MINUTES)))
        conn.execute("COMMIT")
        return {"ok": True, "hold_group": group}
    except sqlite3.IntegrityError:          # another driver holds one of the slots
        conn.execute("ROLLBACK")
        return {"ok": False, "reason": "slot_held"}


def release_holds(conn, thread_id: str, keep_group: str | None = None) -> None:
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE slot_holds SET status='RELEASED' WHERE thread_id=? AND status='ACTIVE' AND group_id IS NOT ?",
                 (thread_id, keep_group))
    conn.execute("COMMIT")


def _rebook(conn, shipment_id: str, slot_id: str, ext_slot: str | None, now: str, source: str) -> str:
    """Inside an open transaction: retire the current booking (kept as history), book the new
    slot, and block the extension slot. Raises IntegrityError if the slot was taken."""
    old = conn.execute(f"SELECT appointment_id, appointment_status FROM appointments WHERE shipment_id=? "
                       f"AND is_current=1 AND appointment_status IN {ACTIVE_SQL}", (shipment_id,)).fetchone()
    if old and old["appointment_status"] == "IN_PROGRESS":
        raise ValueError("truck is already unloading")
    if old:
        conn.execute("""UPDATE appointments SET appointment_status='CANCELLED', is_current=0, cancelled_at=?,
            cancellation_reason=?, updated_at=? WHERE appointment_id=?""",
                     (now, f"Replaced via {source.lower().replace('_', ' ')}", now, old["appointment_id"]))
        conn.execute("UPDATE appointment_slots SET slot_status='OPEN', block_reason=NULL WHERE slot_status='BLOCKED' "
                     "AND block_reason=?", (f"Unload extension for {old['appointment_id']}",))
    appt = new_id("APT")
    conn.execute("""INSERT INTO appointments (appointment_id, shipment_id, slot_id, appointment_status, booking_source,
        is_current, booked_at, confirmed_at, replaced_appointment_id, updated_at)
        VALUES (?,?,?, 'CONFIRMED', ?, 1, ?, ?, ?, ?)""",
                 (appt, shipment_id, slot_id, source, now, now, old["appointment_id"] if old else None, now))
    if ext_slot:
        cur = conn.execute("UPDATE appointment_slots SET slot_status='BLOCKED', block_reason=? WHERE slot_id=? "
                           "AND slot_status='OPEN'", (f"Unload extension for {appt}", ext_slot))
        booked = conn.execute(f"SELECT 1 FROM appointments WHERE slot_id=? AND appointment_status IN {ACTIVE_SQL}",
                              (ext_slot,)).fetchone()
        if cur.rowcount != 1 or booked:
            raise sqlite3.IntegrityError("extension slot taken")
    conn.execute("UPDATE shipments SET updated_at=? WHERE shipment_id=?", (now, shipment_id))
    return appt


def confirm(conn, hold_group: str, thread_id: str, now: str) -> dict:
    """Turn a held option into a CONFIRMED appointment, atomically.

    The holds must still be ACTIVE, unexpired and belong to this thread (conditional UPDATE);
    then the old booking is retired, the new one inserted (ux_active_appointment_per_slot is
    the final guard) and the extension slot blocked. Any failure rolls everything back, so the
    old booking is never lost. Only after COMMIT may the driver be told "confirmed".
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("SELECT * FROM slot_holds WHERE group_id=? AND thread_id=?", (hold_group, thread_id)).fetchall()
        cur = conn.execute("UPDATE slot_holds SET status='CONFIRMED' WHERE group_id=? AND thread_id=? "
                           "AND status='ACTIVE' AND expires_at > ?", (hold_group, thread_id, now))
        if not rows or cur.rowcount != len(rows):
            conn.execute("ROLLBACK")
            return {"ok": False, "reason": "hold_expired_or_used"}
        main = next(r for r in rows if r["role"] == "MAIN")
        ext = next((r["slot_id"] for r in rows if r["role"] == "EXTENSION"), None)
        appt = _rebook(conn, main["shipment_id"], main["slot_id"], ext, now, "DRIVER_CHAT")
        conn.execute("COMMIT")
        return {"ok": True, "appointment_id": appt, "slot_id": main["slot_id"], "extension_slot_id": ext}
    except (sqlite3.IntegrityError, ValueError) as e:
        conn.execute("ROLLBACK")
        return {"ok": False, "reason": "slot_taken" if isinstance(e, sqlite3.IntegrityError) else str(e)}


def book_direct(conn, slot_id: str, shipment_id: str, now: str, source: str = "SCHEDULING_TOOL") -> dict:
    """Book with no hold (scheduling tool, race tests). Same transaction and unique-index guard."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        appt = _rebook(conn, shipment_id, slot_id, None, now, source)
        conn.execute("COMMIT")
        return {"ok": True, "appointment_id": appt}
    except (sqlite3.IntegrityError, ValueError):
        conn.execute("ROLLBACK")
        return {"ok": False, "reason": "slot_taken"}


def record_message(conn, shipment_id: str, appointment_id: str | None, channel: str, recipient: str | None,
                   subject: str | None, body: str, now: str) -> str:
    """operational_messages row. No recipient -> FAILED, so it can't pass for a confirmation (OM004)."""
    mid = new_id("OM")
    conn.execute("""INSERT INTO operational_messages (operational_message_id, shipment_id, appointment_id, channel,
        sender_address, recipient_address, subject, message_body, sent_at, delivery_status)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                 (mid, shipment_id, appointment_id, channel, AGENT_ADDRESS, recipient or "(no address on file)",
                  subject, body, now, "SENT" if recipient else "FAILED"))
    return mid
