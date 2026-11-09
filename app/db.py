"""Database connection, schema and a stand-in snapshot.

The FDE Academy supplies the real SQLite file (18 tables, 2 facilities,
9 docks, 21 shipments, snapshot 4 Aug 2026, Asia/Kolkata). Until it
arrives, this module builds a stand-in with the same shape and with
deliberately messy rows, so every data-issue query has evidence to find.

All timestamps are local Asia/Kolkata text 'YYYY-MM-DD HH:MM', so plain
string comparison orders them correctly.
"""
import sqlite3
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "setuhaul.db"
DAY = "2026-08-04"

SCHEMA = """
-- ---------- Group 1: master data (6) ----------
CREATE TABLE IF NOT EXISTS facilities (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, city TEXT, tz TEXT NOT NULL DEFAULT 'Asia/Kolkata');
CREATE TABLE IF NOT EXISTS docks (
    id TEXT PRIMARY KEY, facility_id TEXT NOT NULL REFERENCES facilities(id),
    name TEXT NOT NULL, supports_reefer INTEGER NOT NULL DEFAULT 0,
    max_weight_kg INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS carriers (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL);          -- own | contracted
CREATE TABLE IF NOT EXISTS drivers (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL,
    carrier_id TEXT REFERENCES carriers(id), language TEXT DEFAULT 'hi');
CREATE TABLE IF NOT EXISTS vehicles (
    id TEXT PRIMARY KEY, reg_no TEXT NOT NULL, vehicle_type TEXT NOT NULL,
    carrier_id TEXT REFERENCES carriers(id));
CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY, name TEXT NOT NULL);

-- ---------- Group 2: plan and operations (8) ----------
CREATE TABLE IF NOT EXISTS shipments (
    id TEXT PRIMARY KEY,
    vehicle_id TEXT REFERENCES vehicles(id), driver_id TEXT REFERENCES drivers(id),
    customer_id TEXT REFERENCES customers(id),
    facility_id TEXT NOT NULL REFERENCES facilities(id),
    load_weight_kg INTEGER NOT NULL, needs_reefer INTEGER NOT NULL DEFAULT 0,
    unload_minutes INTEGER NOT NULL DEFAULT 60,
    priority INTEGER NOT NULL DEFAULT 1,                 -- 1 normal, 2 high, 3 critical
    status TEXT NOT NULL DEFAULT 'in_transit');          -- in_transit|arrived|delivered|cancelled
CREATE TABLE IF NOT EXISTS slots (
    id TEXT PRIMARY KEY, dock_id TEXT NOT NULL REFERENCES docks(id),
    start_at TEXT NOT NULL, end_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dock_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, dock_id TEXT NOT NULL REFERENCES docks(id),
    start_at TEXT NOT NULL, end_at TEXT NOT NULL, reason TEXT);
CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_id TEXT NOT NULL REFERENCES slots(id), shipment_id TEXT NOT NULL REFERENCES shipments(id),
    status TEXT NOT NULL DEFAULT 'active',               -- active|rescheduled|cancelled|completed
    created_at TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'plan');
CREATE UNIQUE INDEX IF NOT EXISTS ux_appt_slot_active ON appointments(slot_id) WHERE status='active';
CREATE UNIQUE INDEX IF NOT EXISTS ux_appt_shipment_active ON appointments(shipment_id) WHERE status='active';
CREATE TABLE IF NOT EXISTS appointment_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, appointment_id INTEGER NOT NULL,
    shipment_id TEXT NOT NULL, slot_id TEXT NOT NULL,
    old_status TEXT, new_status TEXT NOT NULL, changed_at TEXT NOT NULL, actor TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS eta_updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT, shipment_id TEXT NOT NULL REFERENCES shipments(id),
    source TEXT NOT NULL,                                -- plan|driver|warehouse|ops_override
    eta TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gate_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, shipment_id TEXT REFERENCES shipments(id),
    facility_id TEXT NOT NULL, vehicle_id TEXT, event TEXT NOT NULL, at TEXT NOT NULL);  -- gate_in|gate_out
CREATE TABLE IF NOT EXISTS dock_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, appointment_id INTEGER REFERENCES appointments(id),
    dock_id TEXT NOT NULL, event TEXT NOT NULL, at TEXT NOT NULL);                      -- unload_start|unload_end

-- ---------- Group 3: conversations and messaging (4) ----------
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY, shipment_id TEXT, phone TEXT,
    state TEXT NOT NULL DEFAULT 'new',                   -- new|offered|confirmed
    offered_json TEXT NOT NULL DEFAULT '[]', pending_text TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
    direction TEXT NOT NULL, body TEXT NOT NULL, at TEXT NOT NULL, action TEXT);         -- in|out
CREATE TABLE IF NOT EXISTS outbound_confirmations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, appointment_id INTEGER NOT NULL,
    shipment_id TEXT NOT NULL, slot_id TEXT NOT NULL, channel TEXT NOT NULL, sent_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS slot_holds (
    id INTEGER PRIMARY KEY AUTOINCREMENT, slot_id TEXT NOT NULL REFERENCES slots(id),
    shipment_id TEXT NOT NULL REFERENCES shipments(id), conversation_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',               -- active|confirmed|released|expired
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS ux_hold_slot_active ON slot_holds(slot_id) WHERE status='active';
"""


def connect(path=DEFAULT_DB) -> sqlite3.Connection:
    """One connection per thread/request. Autocommit mode; writers open
    transactions explicitly with BEGIN IMMEDIATE."""
    conn = sqlite3.connect(str(path), timeout=15, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_db(path=DEFAULT_DB, seed: bool = True) -> None:
    conn = connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    if seed and conn.execute("SELECT COUNT(*) FROM facilities").fetchone()[0] == 0:
        _seed(conn)
    conn.close()


def t(hhmm: str) -> str:
    return f"{DAY} {hhmm}"


# Shipments: id, facility, driver#, kg, reefer, unload, priority, status, booked slot, plan ETA
SHIPMENTS = [
    # --- Jaipur: clean cases (ladder rungs 1-2)
    ("SH-01", "JAI-DC", 1, 12000, 0, 60, 1, "in_transit", "JAI-D1-10", "10:00"),
    ("SH-02", "JAI-DC", 2, 15000, 0, 60, 2, "in_transit", "JAI-D2-11", "11:00"),
    ("SH-03", "JAI-DC", 3, 9000, 1, 60, 2, "in_transit", "JAI-D3-12", "12:00"),
    ("SH-04", "JAI-DC", 4, 32000, 0, 60, 1, "in_transit", "JAI-D4-13", "13:00"),
    ("SH-05", "JAI-DC", 5, 14000, 0, 60, 1, "in_transit", "JAI-D1-18", "18:00"),
    ("SH-06", "JAI-DC", 6, 11000, 0, 60, 1, "in_transit", "JAI-D2-19", "19:00"),
    # --- Jaipur: planted data problems
    ("SH-07", "JAI-DC", 7, 10000, 0, 60, 1, "in_transit", "JAI-D1-1030", "10:30"),  # overlaps SH-01
    ("SH-08", "JAI-DC", 8, 13000, 0, 60, 1, "in_transit", "JAI-D2-14", "14:00"),    # inside maintenance
    ("SH-09", "JAI-DC", 9, 8000, 1, 60, 3, "in_transit", "JAI-D1-15", "15:00"),     # reefer on dry dock
    ("SH-10", "AMD-WH", 10, 12000, 0, 60, 1, "in_transit", "JAI-D1-16", "16:00"),   # wrong facility
    ("SH-11", "JAI-DC", 11, 9000, 0, 60, 1, "cancelled", "JAI-D2-17", "17:00"),     # cancelled, still booked
    ("SH-12", "JAI-DC", 12, 10000, 0, 60, 1, "in_transit", "JAI-D1-08", "08:00"),   # ETA after slot ends
    ("SH-13", "JAI-DC", 13, 18000, 0, 90, 1, "in_transit", "JAI-D2-12", "12:00"),   # 90 min in 60 min slot
    ("SH-14", "JAI-DC", 14, 11000, 0, 60, 1, "in_transit", "JAI-D5-10", "10:00"),   # inactive dock
    ("SH-15", "JAI-DC", 15, 12000, 0, 60, 1, "in_transit", None, "09:00"),          # gate-in, no booking
    ("SH-16", "JAI-DC", 16, 12000, 0, 60, 1, "in_transit", None, None),             # no ETA at all
    # --- Ahmedabad
    ("SH-17", "AMD-WH", 17, 14000, 0, 60, 1, "delivered", "AMD-A2-10", "10:00"),
    ("SH-18", "AMD-WH", 18, 9000, 1, 60, 2, "in_transit", "AMD-A3-17", "17:00"),    # shares phone with 19
    ("SH-19", "AMD-WH", 19, 12000, 0, 60, 1, "in_transit", "AMD-A1-12", "12:00"),   # late truck
    ("SH-20", "AMD-WH", 20, 30000, 0, 60, 2, "arrived", "AMD-A4-14", "14:00"),      # already unloading
    ("SH-21", "AMD-WH", 21, 11000, 0, 60, 1, "arrived", "AMD-A2-15", "15:00"),      # early, stale confirmation
]


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN")
    x = conn.execute
    x("INSERT INTO facilities VALUES ('JAI-DC','Jaipur Distribution Centre','Jaipur','Asia/Kolkata')")
    x("INSERT INTO facilities VALUES ('AMD-WH','Ahmedabad Warehouse','Ahmedabad','Asia/Kolkata')")
    docks = [  # 9 docks
        ("JAI-D1", "JAI-DC", "Jaipur Dock 1", 0, 20000, 1),
        ("JAI-D2", "JAI-DC", "Jaipur Dock 2", 0, 20000, 1),
        ("JAI-D3", "JAI-DC", "Jaipur Dock 3 (reefer)", 1, 25000, 1),
        ("JAI-D4", "JAI-DC", "Jaipur Dock 4 (heavy)", 0, 40000, 1),
        ("JAI-D5", "JAI-DC", "Jaipur Dock 5", 0, 20000, 0),          # under repair
        ("AMD-A1", "AMD-WH", "Ahmedabad Dock 1", 0, 20000, 1),
        ("AMD-A2", "AMD-WH", "Ahmedabad Dock 2", 0, 20000, 1),
        ("AMD-A3", "AMD-WH", "Ahmedabad Dock 3 (reefer)", 1, 25000, 1),
        ("AMD-A4", "AMD-WH", "Ahmedabad Dock 4 (heavy)", 0, 40000, 1),
    ]
    conn.executemany("INSERT INTO docks VALUES (?,?,?,?,?,?)", docks)
    for d in docks:
        for h in range(6, 22):
            x("INSERT INTO slots VALUES (?,?,?,?)", (f"{d[0]}-{h:02d}", d[0], t(f"{h:02d}:00"), t(f"{h+1:02d}:00")))
    x("INSERT INTO slots VALUES ('JAI-D1-1030','JAI-D1',?,?)", (t("10:30"), t("11:30")))   # overlapping slot
    x("INSERT INTO slots VALUES ('AMD-A2-BAD','AMD-A2',?,?)", (t("12:00"), t("11:00")))    # ends before start
    x("INSERT INTO dock_blocks (dock_id,start_at,end_at,reason) VALUES ('JAI-D2',?,?,'maintenance')",
      (t("14:00"), t("16:00")))
    x("INSERT INTO dock_blocks (dock_id,start_at,end_at,reason) VALUES ('AMD-A1',?,?,'power outage')",
      (t("06:00"), t("09:00")))

    conn.executemany("INSERT INTO carriers VALUES (?,?,?)", [
        ("C-OWN", "SetuHaul Fleet", "own"), ("C-RAT", "Rathore Transport", "contracted"),
        ("C-PAT", "Patel Roadlines", "contracted")])
    conn.executemany("INSERT INTO customers VALUES (?,?)", [
        ("CU-KF", "Kisan Fresh Foods"), ("CU-BM", "BuildMax Steel"), ("CU-UM", "UrbanMart Retail")])

    names = ["Ramesh", "Suresh", "Harpal", "Imran", "Gurpreet", "Mahesh", "Vikram", "Arjun", "Salim",
             "Bhavesh", "Kailash", "Deepak", "Manoj", "Rakesh", "Sanjay", "Ajay", "Kiran", "Jignesh",
             "Hitesh", "Naresh", "Pradeep"]
    for i, (sid, fac, drv, kg, reefer, unload, prio, status, slot, eta) in enumerate(SHIPMENTS, start=1):
        carrier = ["C-OWN", "C-RAT", "C-PAT"][i % 3]
        phone = f"+9198000000{drv:02d}" if drv != 19 else "+919800000018"   # drivers 18 & 19 share a phone
        x("INSERT INTO drivers VALUES (?,?,?,?, 'hi')", (f"DR-{drv:02d}", names[i - 1], phone, carrier))
        state = "RJ14" if fac == "JAI-DC" else "GJ01"
        vtype = "reefer" if reefer else ("trailer-40t" if kg > 20000 else "32ft-container")
        x("INSERT INTO vehicles VALUES (?,?,?,?)", (f"V-{i:02d}", f"{state}AB{1000 + i}", vtype, carrier))
        cust = "CU-KF" if reefer else ("CU-BM" if kg > 20000 else "CU-UM")
        x("INSERT INTO shipments VALUES (?,?,?,?,?,?,?,?,?,?)",
          (sid, f"V-{i:02d}", f"DR-{drv:02d}", cust, fac, kg, reefer, unload, prio, status))
        if eta:
            x("INSERT INTO eta_updates (shipment_id,source,eta,created_at) VALUES (?, 'plan', ?, ?)",
              (sid, t(eta), t("05:00")))
        if slot:
            st = "completed" if status == "delivered" else "active"
            cur = x("INSERT INTO appointments (slot_id,shipment_id,status,created_at) VALUES (?,?,?,?)",
                    (slot, sid, st, t("05:00")))
            x("INSERT INTO appointment_history (appointment_id,shipment_id,slot_id,old_status,new_status,"
              "changed_at,actor) VALUES (?,?,?,NULL,'active',?, 'planner')", (cur.lastrowid, sid, slot, t("05:00")))

    # Conflicting ETAs
    x("INSERT INTO eta_updates (shipment_id,source,eta,created_at) VALUES ('SH-12','driver',?,?)",
      (t("09:40"), t("07:10")))                                   # driver says 09:40; slot ended 09:00
    x("INSERT INTO eta_updates (shipment_id,source,eta,created_at) VALUES ('SH-02','ops_override',?,?)",
      (t("11:30"), t("07:30")))
    x("INSERT INTO eta_updates (shipment_id,source,eta,created_at) VALUES ('SH-02','driver',?,?)",
      (t("12:30"), t("07:45")))                                   # newer, but ops override wins
    x("INSERT INTO eta_updates (shipment_id,source,eta,created_at) VALUES ('SH-19','driver',?,?)",
      (t("14:20"), t("11:40")))                                   # late truck

    # Gate and dock events
    x("INSERT INTO gate_events (shipment_id,facility_id,vehicle_id,event,at) VALUES "
      "('SH-15','JAI-DC','V-15','gate_in',?)", (t("07:50"),))     # arrived, no appointment
    x("INSERT INTO gate_events (shipment_id,facility_id,vehicle_id,event,at) VALUES "
      "('SH-20','AMD-WH','V-20','gate_in',?)", (t("13:35"),))
    x("INSERT INTO gate_events (shipment_id,facility_id,vehicle_id,event,at) VALUES "
      "('SH-21','AMD-WH','V-21','gate_in',?)", (t("13:30"),))     # 90 min early
    appt20 = x("SELECT id FROM appointments WHERE shipment_id='SH-20'").fetchone()[0]
    x("INSERT INTO dock_events (appointment_id,dock_id,event,at) VALUES (?, 'AMD-A4','unload_start',?)",
      (appt20, t("13:50")))

    # Stale promise: SH-21 was told 13:00, then moved to 15:00 without a new confirmation
    old = x("INSERT INTO appointments (slot_id,shipment_id,status,created_at) VALUES "
            "('AMD-A2-13','SH-21','rescheduled',?)", (t("04:00"),)).lastrowid
    x("INSERT INTO outbound_confirmations (appointment_id,shipment_id,slot_id,channel,sent_at) VALUES "
      "(?, 'SH-21','AMD-A2-13','whatsapp',?)", (old, t("04:05")))
    conn.execute("COMMIT")


if __name__ == "__main__":
    if DEFAULT_DB.exists():
        DEFAULT_DB.unlink()
    init_db()
    c = connect()
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                      "AND name NOT LIKE 'sqlite_%'")]
    print(f"Created {DEFAULT_DB}: {len(tables)} tables")
