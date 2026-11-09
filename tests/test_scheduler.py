"""Early truck, late truck and a truck already unloading share one facility."""
from app.scheduler import propose_schedule


def by_sid(result):
    return {p["shipment"]: p for p in result["plan"]}


def test_truck_in_progress_is_fixed(conn):
    r = propose_schedule(conn, "AMD-WH", "2026-08-04 13:55")
    assert r["fixed"][0]["shipment"] == "SH-20" and r["fixed"][0]["dock"] == "AMD-A4"
    assert "SH-20" not in by_sid(r)
    # nothing else uses A4 while SH-20 is unloading
    assert all(not (p["dock"] == "AMD-A4" and p["start"] < "2026-08-04 14:50") for p in r["plan"])


def test_early_truck_served_now(conn):
    p = by_sid(propose_schedule(conn, "AMD-WH", "2026-08-04 13:55"))["SH-21"]
    assert p["start"] == "2026-08-04 13:55" and p["arrival_basis"] == "gate_in"


def test_late_truck_not_scheduled_before_arrival(conn):
    p = by_sid(propose_schedule(conn, "AMD-WH", "2026-08-04 13:55"))["SH-19"]
    assert p["start"] >= "2026-08-04 14:20" and p["wait_min"] == 0


def test_no_dock_overlaps_and_compatibility(conn):
    r = propose_schedule(conn, "JAI-DC", "2026-08-04 08:00")
    plan = r["plan"]
    for i, a in enumerate(plan):
        for b in plan[i + 1:]:
            if a["dock"] == b["dock"]:
                assert a["end"] <= b["start"] or b["end"] <= a["start"], (a, b)
    reefer = by_sid(r)["SH-09"]
    assert reefer["dock"] == "JAI-D3"                       # fixes the planted reefer-on-dry booking
    assert all(p["dock"] != "JAI-D5" for p in plan)         # inactive dock


def test_keeps_promises_when_possible(conn):
    r = by_sid(propose_schedule(conn, "JAI-DC", "2026-08-04 08:00"))
    for sid in ("SH-01", "SH-03", "SH-04", "SH-05", "SH-06"):
        assert r[sid]["changed"] is False, sid


def test_shipment_without_eta_is_reported(conn):
    r = propose_schedule(conn, "JAI-DC", "2026-08-04 08:00")
    assert {"shipment": "SH-16", "reason": "no ETA and no gate-in"} in r["skipped"]
