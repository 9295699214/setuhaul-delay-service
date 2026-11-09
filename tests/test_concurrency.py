"""Ten drivers, one slot. Exactly one may win.

Each thread gets its own connection (like separate API requests); a Barrier
releases them all at once to force the race. Repeated to catch rare timings.
"""
import threading

from app import tools
from app.db import connect
from eval import race_once

NOW = "2026-08-04 08:00"
SLOT = "JAI-D2-16"
DRIVERS = ["SH-01", "SH-02", "SH-05", "SH-06", "SH-07", "SH-08", "SH-12", "SH-14", "SH-15", "SH-16"]


def _race(db_path, worker):
    barrier = threading.Barrier(len(DRIVERS))
    results = [None] * len(DRIVERS)

    def run(i, sid):
        conn = connect(db_path)
        try:
            barrier.wait()
            results[i] = worker(conn, sid)
        finally:
            conn.close()

    threads = [threading.Thread(target=run, args=(i, s)) for i, s in enumerate(DRIVERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def _active_on(db_path, slot):
    c = connect(db_path)
    n = c.execute("SELECT COUNT(*) FROM appointments WHERE slot_id=? AND status='active'", (slot,)).fetchone()[0]
    c.close()
    return n


def test_ten_drivers_hold_same_slot(db_path):
    res = _race(db_path, lambda c, sid: tools.hold_slot(c, SLOT, sid, f"conv-{sid}", NOW))
    assert sum(r["ok"] for r in res) == 1
    assert all(r["reason"] == "slot_held" for r in res if not r["ok"])


def test_ten_drivers_book_directly(db_path):
    """No hold step at all: the partial unique index still lets exactly one through."""
    for slot in ("JAI-D2-16", "JAI-D3-20", "JAI-D4-21"):
        res = _race(db_path, lambda c, sid, s=slot: tools.book_direct(c, s, sid, NOW))
        assert sum(r["ok"] for r in res) == 1
        assert _active_on(db_path, slot) == 1


def test_same_hold_confirmed_ten_times(db_path):
    """Double-taps and retried requests must not create two bookings."""
    c = connect(db_path)
    hold = tools.hold_slot(c, SLOT, "SH-05", "conv-5", NOW)
    c.close()
    res = _race(db_path, lambda c, sid: tools.confirm(c, hold["hold_id"], NOW))
    assert sum(r["ok"] for r in res) == 1
    assert _active_on(db_path, SLOT) == 1


def test_full_conversations_race_repeated():
    for _ in range(10):
        r = race_once()
        assert r["same_slot_promised_twice"] == 0
        assert r["false_promises"] == 0
        assert r["double_bookings"] == 0
        assert 1 <= r["confirmed"] <= 3          # 3 compatible docks free at 16:00 in Jaipur
        assert r["confirmed"] + r["escalated"] == 10
