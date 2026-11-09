"""Unit tests for the deterministic tools and the extractor."""
from app import tools
from app.agent import rule_extract

NOW = "2026-08-04 08:00"
T = lambda hhmm: f"2026-08-04 {hhmm}"  # noqa: E731


def ids(opts):
    return [o["slot_id"] for o in opts]


def test_options_are_after_eta(conn):
    opts = tools.find_options(conn, "SH-01", T("12:00"), now=NOW)
    assert opts and all(o["start_at"] >= T("12:00") for o in opts)


def test_never_offers_slot_booked_by_someone_else(conn):
    opts = tools.find_options(conn, "SH-01", T("11:00"), T("12:00"), now=NOW, limit=20)
    assert "JAI-D2-11" not in ids(opts)          # SH-02's booking


def test_only_own_facility(conn):
    opts = tools.find_options(conn, "SH-19", T("14:00"), now=NOW, limit=50)
    assert opts and all(o["dock_id"].startswith("AMD-") for o in opts)


def test_reefer_only_reefer_dock(conn):
    opts = tools.find_options(conn, "SH-03", T("09:00"), now=NOW, limit=20)
    assert opts and {o["dock_id"] for o in opts} == {"JAI-D3"}


def test_heavy_only_heavy_dock(conn):
    opts = tools.find_options(conn, "SH-04", T("09:00"), now=NOW, limit=20)
    assert opts and {o["dock_id"] for o in opts} == {"JAI-D4"}


def test_dry_load_prefers_general_docks(conn):
    opts = tools.find_options(conn, "SH-05", T("16:00"), T("17:00"), now=NOW, limit=1)
    assert opts[0]["dock_id"] == "JAI-D2"          # D1-16 is taken; D3/D4 kept for reefer/heavy


def test_blocked_inactive_overlapping_and_bad_slots_skipped(conn):
    opts = tools.find_options(conn, "SH-05", T("06:00"), now=NOW, limit=200)
    got = ids(opts)
    assert not any(s.startswith("JAI-D2-14") or s.startswith("JAI-D2-15") for s in got)   # maintenance
    assert not any(s.startswith("JAI-D5") for s in got)                                    # inactive
    assert "JAI-D1-1030" not in got and "JAI-D1-11" not in got                             # overlap with SH-07


def test_slot_shorter_than_unload_skipped(conn):
    assert tools.find_options(conn, "SH-13", T("09:00"), now=NOW, limit=50) == []


def test_past_slots_never_offered(conn):
    opts = tools.find_options(conn, "SH-05", T("06:00"), now=T("13:10"), limit=5)
    assert all(o["start_at"] >= T("13:10") for o in opts)


def test_eta_trust_order(conn):
    assert tools.current_eta(conn, "SH-02") == {"eta": T("11:30"), "source": "ops_override"}
    assert tools.current_eta(conn, "SH-12")["source"] == "driver"
    assert tools.current_eta(conn, "SH-16") is None


def test_shared_phone_returns_two_shipments(conn):
    assert [r["id"] for r in tools.find_shipments_by_phone(conn, "+919800000018")] == ["SH-18", "SH-19"]


def test_held_slot_hidden_from_other_drivers(conn):
    assert tools.hold_slot(conn, "JAI-D2-16", "SH-05", "c5", NOW)["ok"]
    assert "JAI-D2-16" not in ids(tools.find_options(conn, "SH-06", T("16:00"), T("17:00"), now=NOW))


def test_expired_hold_cannot_confirm(conn):
    h = tools.hold_slot(conn, "JAI-D2-16", "SH-05", "c5", NOW)
    assert tools.confirm(conn, h["hold_id"], T("08:05"))["ok"] is False


def test_failed_confirm_keeps_old_appointment(conn):
    h = tools.hold_slot(conn, "JAI-D2-16", "SH-05", "c5", NOW)
    tools.confirm(conn, h["hold_id"], T("09:00"))
    assert tools.current_appointment(conn, "SH-05")["slot_id"] == "JAI-D1-18"


def test_confirm_moves_booking_and_logs_history(conn):
    h = tools.hold_slot(conn, "JAI-D2-16", "SH-05", "c5", NOW)
    assert tools.confirm(conn, h["hold_id"], NOW)["ok"]
    assert tools.current_appointment(conn, "SH-05")["slot_id"] == "JAI-D2-16"
    hist = conn.execute("SELECT slot_id, new_status FROM appointment_history WHERE shipment_id='SH-05' "
                        "AND actor='agent' ORDER BY id").fetchall()
    assert [tuple(r) for r in hist] == [("JAI-D1-18", "rescheduled"), ("JAI-D2-16", "active")]


def test_extract_delay_and_window():
    ex = rule_extract("around 2 hrs late, anything after 7, must leave by 9", T("14:00"))
    assert (ex.intent, ex.delay_minutes, ex.earliest, ex.latest_end) == \
        ("report_delay", 120, T("19:00"), T("21:00"))


def test_extract_variants():
    assert rule_extract("1.5 hours late", NOW).delay_minutes == 90
    assert rule_extract("two hours late", NOW).delay_minutes == 120
    assert rule_extract("45 min late", NOW).delay_minutes == 45
    assert rule_extract("will reach by 3pm", NOW).new_eta == T("15:00")
    assert rule_extract("the second one works", NOW).choice_index == 2
    assert rule_extract("2", NOW).choice_index == 2
    assert rule_extract("my truck is GJ01 AB 1019", NOW).delay_minutes is None
