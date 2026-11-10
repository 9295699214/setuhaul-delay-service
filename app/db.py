"""Database access for the FDE Academy snapshot (data/setuhaul_freight_operations.db).

The supplied file is never modified. The service works on a runtime copy
(SETUHAUL_DB, default ./setuhaul_runtime.db), and reset() restores that copy
from the original.

Only two tables are added, both for things the snapshot has no place for:
  slot_holds          short reservations while a driver chooses (design question 2)
  agent_thread_state  which options a chat thread was offered, and its state
Everything else the service records goes into the existing tables: chat_threads,
chat_messages, eta_updates, driver_exceptions, appointments,
appointment_slots (extension blocks) and operational_messages.
"""
import os
import shutil
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRISTINE_DB = ROOT / "data" / "setuhaul_freight_operations.db"
DEFAULT_DB = Path(os.getenv("SETUHAUL_DB", str(ROOT / "setuhaul_runtime.db")))

ADDITIONS = """
CREATE TABLE IF NOT EXISTS slot_holds (
    hold_id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,                  -- one offered option = main slot (+ extension slot)
    slot_id TEXT NOT NULL REFERENCES appointment_slots(slot_id),
    role TEXT NOT NULL CHECK (role IN ('MAIN','EXTENSION')),
    shipment_id TEXT NOT NULL REFERENCES shipments(shipment_id),
    thread_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','CONFIRMED','RELEASED','EXPIRED')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_hold_per_slot ON slot_holds(slot_id) WHERE status = 'ACTIVE';

CREATE TABLE IF NOT EXISTS agent_thread_state (
    thread_id TEXT PRIMARY KEY REFERENCES chat_threads(thread_id),
    shipment_id TEXT,
    state TEXT NOT NULL DEFAULT 'NEW',       -- NEW | OFFERED | CONFIRMED | ESCALATED
    offered_json TEXT NOT NULL DEFAULT '[]',
    pending_text TEXT,
    updated_at TEXT NOT NULL
);
"""


def connect(path=DEFAULT_DB) -> sqlite3.Connection:
    """One connection per thread/request. Autocommit mode; writers open
    transactions explicitly with BEGIN IMMEDIATE."""
    conn = sqlite3.connect(str(path), timeout=15, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_db(path=DEFAULT_DB, reset: bool = False) -> None:
    path = Path(path)
    if reset:
        for suffix in ("", "-wal", "-shm"):
            p = path.with_name(path.name + suffix)
            if p.exists():
                p.unlink()
    if not path.exists():
        shutil.copyfile(PRISTINE_DB, path)
    conn = connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(ADDITIONS)
    conn.close()


def reset(path=DEFAULT_DB) -> None:
    init_db(path, reset=True)


if __name__ == "__main__":
    reset()
    c = connect()
    n = c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone()[0]
    print(f"Runtime copy at {DEFAULT_DB}: {n} tables (18 supplied + 2 added)")
