"""Deterministic tools. No LLM ever runs in this file.

The agent may only change capacity through these functions. Every write
runs inside BEGIN IMMEDIATE, so two conversations can never both win
the same slot.
"""
import re
import sqlite3
from datetime import datetime, timedelta

FMT = "%Y-%m-%d %H:%M"
HOLD_MINUTES = 2


def _add(ts: str, minutes: int) -> str:
    return (datetime.strptime(ts, FMT) + timedelta(minutes=minutes)).strftime(FMT)


def norm_reg(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


# ---------------------------------------------------------------- reads

def get_shipment(conn, shipment_id: str):
    return conn.execute("""
        SELECT sh.*, v.reg_no, d.phone AS driver_phone, d.name AS driver_name
        FROM shipments sh LEFT JOIN vehicles v ON v.id = sh.vehicle_id
        LEFT JOIN drivers d ON d.id = sh.driver_id WHERE sh.id=?""", (shipment_id,)).fetchone()


def find_shipments_by_phone(conn, phone: str) -> list:
    """All open shipments for this phone. More than one means the backend
    cannot tell who is writing, so the service must ask (see data issue 13)."""
    return conn.execute("""
        SELECT sh.*, v.reg_no FROM shipments sh
        JOIN drivers d ON d.id = sh.driver_id LEFT JOIN vehicles v ON v.id = sh.vehicle_id
        WHERE d.phone = ? AND sh.status IN ('in_transit','arrived')
        ORDER BY sh.id""", (phone,)).fetchall()


def current_eta(conn, shipment_id: str):
    """Trust order: ops override > newest of driver/warehouse/plan."""
    row = conn.execute("""
        SELECT eta, source FROM eta_updates WHERE shipment_id=?
        ORDER BY (source='ops_override') DESC, created_at DESC, id DESC LIMIT 1
    """, (shipment_id,)).fetchone()
    return dict(row) if row else None


def gate_status(conn, shipment_id: str):
    """Latest gate event. 'gate_in' means the truck is physically on site."""
    row = conn.execute("SELECT event, at FROM gate_events WHERE shipment_id=? "
                       "ORDER BY at DESC, id DESC LIMIT 1", (shipment_id,)).fetchone()
    return dict(row) if row else None


def current_appointment(conn, shipment_id: str):
    return conn.execute("""
        SELECT a.id, a.slot_id, s.dock_id, d.name AS dock_name, s.start_at, s.end_at
        FROM appointments a JOIN slots s ON s.id=a.slot_id JOIN docks d ON d.id = s.dock_id
        WHERE a.shipment_id=? AND a.status='active'
    """, (shipment_id,)).fetchone()


def find_options(conn, shipment_id: str, earliest: str, latest_end: str | None = None,
                 now: str | None = None, limit: int = 3) -> list[dict]:
    """Free slots the truck can really use.

    Checks what the unique indexes do NOT cover: right facility, active dock,
    reefer/weight compatibility, valid slot length >= unload time, blocked
    windows, overlapping bookings on the same dock, live holds, past slots.
    """
    sh = get_shipment(conn, shipment_id)
    if sh is None:
        raise ValueError(f"unknown shipment {shipment_id}")
    now = now or datetime.now().strftime(FMT)
    earliest = max(earliest, now)
    sql = """
        SELECT s.id AS slot_id, s.dock_id, d.name AS dock_name, s.start_at, s.end_at
        FROM slots s JOIN docks d ON d.id = s.dock_id
        WHERE d.facility_id = :facility AND d.active = 1
          AND (:reefer = 0 OR d.supports_reefer = 1)
          AND d.max_weight_kg >= :kg
          AND s.end_at > s.start_at
          AND ROUND((julianday(s.end_at) - julianday(s.start_at)) * 1440) >= :unload
          AND s.start_at >= :earliest
          AND (:latest IS NULL OR s.end_at <= :latest)
          AND NOT EXISTS (
              SELECT 1 FROM appointments a JOIN slots s2 ON s2.id = a.slot_id
              WHERE a.status='active' AND s2.dock_id = s.dock_id
                AND s2.start_at < s.end_at AND s2.end_at > s.start_at
                AND a.shipment_id != :sid)
          AND NOT EXISTS (
              SELECT 1 FROM dock_blocks b
              WHERE b.dock_id = s.dock_id AND b.start_at < s.end_at AND b.end_at > s.start_at)
          AND NOT EXISTS (
              SELECT 1 FROM slot_holds h
              WHERE h.slot_id = s.id AND h.status='active' AND h.expires_at > :now
                AND h.shipment_id != :sid)
        -- earliest first; at equal times keep scarce docks (reefer/heavy) for loads that need them
        ORDER BY s.start_at, d.supports_reefer > :reefer, d.max_weight_kg - :kg, s.dock_id
        LIMIT :limit
    """
    rows = conn.execute(sql, {"facility": sh["facility_id"], "reefer": sh["needs_reefer"],
                              "kg": sh["load_weight_kg"], "unload": sh["unload_minutes"],
                              "earliest": earliest, "latest": latest_end, "sid": shipment_id,
                              "now": now, "limit": limit}).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- writes

def record_eta(conn, shipment_id: str, eta: str, source: str, now: str) -> None:
    conn.execute("INSERT INTO eta_updates (shipment_id,source,eta,created_at) VALUES (?,?,?,?)",
                 (shipment_id, source, eta, now))


def hold_slot(conn, slot_id: str, shipment_id: str, conversation_id: str, now: str) -> dict:
    """Reserve a slot for HOLD_MINUTES. Returns {'ok': bool, ...}."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE slot_holds SET status='expired' "
                     "WHERE status='active' AND expires_at <= ?", (now,))
        booked = conn.execute(
            "SELECT 1 FROM appointments WHERE slot_id=? AND status='active' AND shipment_id != ?",
            (slot_id, shipment_id)).fetchone()
        if booked:
            conn.execute("ROLLBACK")
            return {"ok": False, "reason": "slot_booked"}
        cur = conn.execute(
            "INSERT INTO slot_holds (slot_id,shipment_id,conversation_id,created_at,expires_at) "
            "VALUES (?,?,?,?,?)",
            (slot_id, shipment_id, conversation_id, now, _add(now, HOLD_MINUTES)))
        conn.execute("COMMIT")
        return {"ok": True, "hold_id": cur.lastrowid, "slot_id": slot_id}
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
        return {"ok": False, "reason": "slot_held"}


def release_holds(conn, conversation_id: str, keep_hold_id: int | None = None) -> None:
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE slot_holds SET status='released' WHERE conversation_id=? "
                 "AND status='active' AND id IS NOT ?", (conversation_id, keep_hold_id))
    conn.execute("COMMIT")


def _move_booking(conn, shipment_id: str, slot_id: str, now: str, actor: str) -> int:
    """Inside an open transaction: retire the old booking, create the new one,
    and write both changes to appointment_history."""
    old = conn.execute("SELECT id, slot_id FROM appointments WHERE shipment_id=? AND status='active'",
                       (shipment_id,)).fetchone()
    if old:
        conn.execute("UPDATE appointments SET status='rescheduled' WHERE id=?", (old["id"],))
        conn.execute("INSERT INTO appointment_history (appointment_id,shipment_id,slot_id,old_status,"
                     "new_status,changed_at,actor) VALUES (?,?,?,'active','rescheduled',?,?)",
                     (old["id"], shipment_id, old["slot_id"], now, actor))
    new_id = conn.execute("INSERT INTO appointments (slot_id,shipment_id,created_at,source) "
                          "VALUES (?,?,?,?)", (slot_id, shipment_id, now, actor)).lastrowid
    conn.execute("INSERT INTO appointment_history (appointment_id,shipment_id,slot_id,old_status,"
                 "new_status,changed_at,actor) VALUES (?,?,?,NULL,'active',?,?)",
                 (new_id, shipment_id, slot_id, now, actor))
    return new_id


def confirm(conn, hold_id: int, now: str) -> dict:
    """Turn a hold into an appointment, atomically.

    1. The hold must still be active and unexpired (conditional UPDATE).
    2. Old appointment -> 'rescheduled', new appointment inserted.
    3. The partial unique index is the final guard. Any failure rolls back
       everything, so the old appointment is never lost.
    Only after COMMIT may the caller tell the driver "confirmed".
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        hold = conn.execute("SELECT * FROM slot_holds WHERE id=?", (hold_id,)).fetchone()
        cur = conn.execute("UPDATE slot_holds SET status='confirmed' "
                           "WHERE id=? AND status='active' AND expires_at > ?", (hold_id, now))
        if hold is None or cur.rowcount == 0:
            conn.execute("ROLLBACK")
            return {"ok": False, "reason": "hold_expired_or_used"}
        appt_id = _move_booking(conn, hold["shipment_id"], hold["slot_id"], now, "agent")
        conn.execute("COMMIT")
        return {"ok": True, "appointment_id": appt_id, "slot_id": hold["slot_id"]}
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
        return {"ok": False, "reason": "slot_taken"}


def book_direct(conn, slot_id: str, shipment_id: str, now: str, actor: str = "ops") -> dict:
    """Book without a hold (ops tools and the race test). Same safety:
    one transaction, unique index as the final guard."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        appt_id = _move_booking(conn, shipment_id, slot_id, now, actor)
        conn.execute("COMMIT")
        return {"ok": True, "appointment_id": appt_id}
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
        return {"ok": False, "reason": "slot_taken"}


def record_confirmation(conn, appointment_id: int, shipment_id: str, slot_id: str, now: str) -> None:
    """Called only after a successful COMMIT, so every row here is a true promise."""
    conn.execute("INSERT INTO outbound_confirmations (appointment_id,shipment_id,slot_id,channel,sent_at) "
                 "VALUES (?,?,?, 'whatsapp', ?)", (appointment_id, shipment_id, slot_id, now))
