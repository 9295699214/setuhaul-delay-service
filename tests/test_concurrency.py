"""Ten requests, one slot: exactly one may win (design question 3).

Each thread has its own connection (like separate API requests) and a Barrier
releases them together to force the race. Repeated to catch rare timings.
"""
import threading

from app import tools
from app.db import connect
from conftest import T
from eval import race_once

SHIPMENTS = ["SHP1006", "SHP1009", "SHP1012", "SHP1013", "SHP1014", "SHP1017", "SHP1007", "SHP1003", "SHP1004", "SHP1020"]
SLOT = "SLOT-JAI-047"   # D4 12:00-13:00, free


def _race(db_path, worker):
    barrier = threading.Barrier(len(SHIPMENTS))
    results = [None] * len(SHIPMENTS)

    def run(i, sid):
        conn = connect(db_path)
        try:
            barrier.wait()
            results[i] = worker(conn, sid)
        finally:
            conn.close()

    threads = [threading.Thread(target=run, args=(i, s)) for i, s in enumerate(SHIPMENTS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def active_on(db_path, slot):
    c = connect(db_path)
    n = c.execute(f"SELECT COUNT(*) FROM appointments WHERE slot_id=? AND appointment_status IN {tools.ACTIVE_SQL}",
                  (slot,)).fetchone()[0]
    c.close()
    return n


def test_ten_holds_on_one_slot(db_path):
    opt = {"slot_id": SLOT, "extension_slot_id": None}
    res = _race(db_path, lambda c, sid: tools.hold_option(c, opt, sid, f"t-{sid}", T("09:30")))
    assert sum(r["ok"] for r in res) == 1
    assert all(r["reason"] == "slot_held" for r in res if not r["ok"])


def test_ten_direct_bookings_hit_the_unique_index(db_path):
    """No hold step at all: ux_active_appointment_per_slot lets exactly one through."""
    for slot in (SLOT, "SLOT-JAI-048", "SLOT-JAI-062"):
        res = _race(db_path, lambda c, sid, s=slot: tools.book_direct(c, s, sid, T("09:30")))
        assert sum(r["ok"] for r in res) == 1
        assert active_on(db_path, slot) == 1


def test_same_hold_confirmed_ten_times(db_path):
    c = connect(db_path)
    opt = tools.find_options(c, "SHP1012", T("12:00"), now=T("09:30"), limit=1)[0]
    h = tools.hold_option(c, opt, "SHP1012", "t-1", T("09:30"))
    c.close()
    res = _race(db_path, lambda c, sid: tools.confirm(c, h["hold_group"], "t-1", T("09:30")))
    assert sum(r["ok"] for r in res) == 1
    assert active_on(db_path, opt["slot_id"]) == 1


def test_extension_slot_cannot_be_double_used(db_path):
    """Two 75-minute trucks race for the same main+extension pair; the loser gets nothing partial."""
    c = connect(db_path)
    a = tools.find_options(c, "SHP1014", T("12:00"), now=T("09:45"), limit=1)[0]
    c.close()
    res = _race(db_path, lambda c, sid: tools.hold_option(c, a, sid, f"t-{sid}", T("09:45")))
    assert sum(r["ok"] for r in res) == 1
    c = connect(db_path)
    held = c.execute("SELECT COUNT(*) FROM slot_holds WHERE status='ACTIVE'").fetchone()[0]
    c.close()
    assert held == 2                                  # one MAIN + one EXTENSION, from the single winner


def test_full_conversations_race_repeated():
    for _ in range(8):
        r = race_once()
        assert r["same_slot_promised_twice"] == 0
        assert r["false_promises"] == 0
        assert r["double_bookings"] == 0
        assert r["confirmed"] >= 1 and r["confirmed"] + r["escalated"] + r["other"] == 6
