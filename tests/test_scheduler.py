"""Scheduling tool on the real snapshot: in-dock, early, late, broken dock, priorities."""
from app.scheduler import propose_schedule
from conftest import T


def plan(conn, now):
    r = propose_schedule(conn, "FAC-JAI-01", T(now))
    return r, {p["shipment"]: p for p in r["plan"]}


def no_overlaps(r):
    taken = [(p["dock"], p["start"], p["end"]) for p in r["plan"]] + [(f["dock"], f["start"], f["end"]) for f in r["fixed"]]
    for i, a in enumerate(taken):
        for b in taken[i + 1:]:
            if a[0] == b[0]:
                assert a[2] <= b[1] or b[2] <= a[1], (a, b)


def test_truck_unloading_is_fixed(conn):
    r, by = plan(conn, "09:30")
    assert r["fixed"][0]["shipment"] == "SHP1002" and r["fixed"][0]["dock"] == "D2"
    assert "SHP1002" not in by


def test_no_dock_double_use(conn):
    for now in ("09:30", "10:15", "11:00"):
        no_overlaps(propose_schedule(conn, "FAC-JAI-01", T(now)))


def test_compatibility_respected(conn):
    _, by = plan(conn, "10:15")
    assert by["SHP1010"]["dock"] == "D5"                       # reefer
    assert by["SHP1016"]["dock"] == "D6" and by["SHP1011"]["dock"] == "D6"   # heavy
    assert by["SHP1005"]["dock"] in ("D2", "D4")                # 20.5 t
    assert all(p["dock"] != "D3" or p["start"] >= T("13:00") for p in by.values())   # breakdown


def test_yard_trucks_planned_from_now(conn):
    _, by = plan(conn, "10:15")
    for sid in ("SHP1003", "SHP1004", "SHP1005"):
        assert by[sid]["start"] >= T("10:15") and by[sid]["arrival_basis"] == "gate-in"


def test_critical_truck_keeps_its_promise(conn):
    _, by = plan(conn, "10:15")
    assert by["SHP1009"]["changed"] is False and by["SHP1009"]["late_min"] == 0


def test_no_feasible_and_not_departed_are_reported(conn):
    r, _ = plan(conn, "10:15")
    reasons = {s["shipment"]: s["reason"] for s in r["skipped"]}
    assert "no feasible" in reasons["SHP1015"]                  # D5 maintenance after 18:00
    assert "not departed" in reasons["SHP1018"]                 # no-show truck


def test_long_unloads_get_extension(conn):
    _, by = plan(conn, "10:15")
    for sid in ("SHP1009", "SHP1014", "SHP1005"):               # 75-minute unloads in 60-minute slots
        assert by[sid]["extension_slot_id"]


def test_proposal_books_nothing(conn):
    before = conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
    propose_schedule(conn, "FAC-JAI-01", T("10:15"))
    assert conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == before
