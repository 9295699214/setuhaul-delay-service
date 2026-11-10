"""Deterministic tools on the real snapshot."""
from app import timeutil as tu
from app import tools
from app.agent import rule_extract
from conftest import T


def ids(opts):
    return [o["slot_id"] for o in opts]


# ---- time handling ---------------------------------------------------------
def test_sqlite_datetime_trap_is_avoided():
    assert tu.add(T("08:08"), 70) == T("09:18")          # SQLite datetime() would say 03:48 UTC
    assert tu.hhmm(T("13:30")) == "1:30 PM"


def test_eta_as_of_now_ignores_future_rows(conn):
    assert tools.current_eta(conn, "SHP1013", T("09:19"))["eta"] == T("10:00")   # ETA008 is created 09:20
    assert tools.current_eta(conn, "SHP1013", T("09:30"))["eta"] == T("11:00")
    assert tools.current_eta(conn, "SHP1006", T("10:00"))["eta"] == T("11:20")   # newest of two updates


# ---- identity ----------------------------------------------------------------
def test_phone_formats_match(conn):
    assert tools.driver_for_phone(conn, "9000010012")["driver_id"] == "DRV012"
    assert tools.driver_for_phone(conn, "+91 90000 10012")["driver_id"] == "DRV012"


def test_resolution_rules(conn):
    def res(driver, text, delay):
        sh, how = tools.resolve_shipment(tools.shipments_for_driver(conn, driver), text, delay)
        return (sh["shipment_id"] if sh else None), how
    assert res("DRV012", "late", True) == ("SHP1012", "only_open_shipment")
    assert res("DRV006", "late", True) == ("SHP1006", "only_trip_underway")          # SHP1021 not started
    assert res("DRV004", "late by 45", True) == (None, "contradiction_delay_but_on_site")  # THR010
    assert res("DRV004", "in the yard", False) == ("SHP1004", "only_trip_underway")
    assert res("DRV004", "it's for 020", True) == ("SHP1020", "named_in_message")
    assert res("DRV007", "late", True) == (None, "ambiguous")                         # two moving trucks
    assert res("DRV007", "MH04HV4107", True) == ("SHP1016", "named_in_message")


# ---- finding slots ------------------------------------------------------------
def test_standard_load_only_standard_docks(conn):
    opts = tools.find_options(conn, "SHP1012", T("11:10"), now=T("09:30"), limit=20)
    assert opts and {o["dock_type"] for o in opts} == {"STANDARD"}


def test_reefer_only_d5_and_not_during_maintenance(conn):
    opts = tools.find_options(conn, "SHP1010", T("10:55"), now=T("10:00"), limit=50)
    assert {o["dock_code"] for o in opts} == {"D5"}
    assert all(o["start"] < T("18:00") for o in opts)                                # DEVT002 from 18:00
    assert tools.find_options(conn, "SHP1015", T("18:30"), now=T("16:24")) == []      # no feasible slot


def test_heavy_only_d6(conn):
    opts = tools.find_options(conn, "SHP1016", T("12:00"), now=T("09:40"), limit=20)
    assert opts and {o["dock_code"] for o in opts} == {"D6"} and opts[0]["start"] == T("12:30")


def test_weight_limit_excludes_light_docks(conn):
    opts = tools.find_options(conn, "SHP1014", T("11:25"), now=T("09:44"), limit=20)   # 21 t
    assert opts and {o["dock_code"] for o in opts} <= {"D2", "D4"}                  # 25 t docks only


def test_long_unload_needs_free_extension(conn):
    opts = tools.find_options(conn, "SHP1014", T("11:25"), now=T("09:44"), limit=20)  # 75 min
    assert all(o["extension_slot_id"] for o in opts)
    assert all(o["unload_until"] == tu.add(o["start"], 75) for o in opts)


def test_breakdown_and_blocked_slots_never_offered(conn):
    opts = tools.find_options(conn, "SHP1012", T("09:00"), now=T("09:00"), limit=100)
    d3 = [o for o in opts if o["dock_code"] == "D3"]
    assert all(o["start"] >= T("13:00") for o in d3)                                 # DEVT001 09:15-13:00
    assert "SLOT-JAI-030" not in ids(opts)                                           # OPEN but broken (issue 2)


def test_last_start_rule_and_past(conn):
    opts = tools.find_options(conn, "SHP1012", T("09:00"), now=T("10:10"), limit=100)
    assert all(T("10:10") <= o["start"] <= T("21:00") for o in opts)


def test_own_booking_counts_as_available(conn):
    opts = tools.find_options(conn, "SHP1013", T("11:00"), now=T("09:19"), limit=3)
    assert opts[0]["slot_id"] == "SLOT-JAI-018"                                       # its own pending APT1013A


def test_cancelled_booking_frees_capacity(conn):
    opts = tools.find_options(conn, "SHP1004", T("09:40"), now=T("09:40"), limit=3)
    assert opts[0]["slot_id"] == "SLOT-JAI-017"                                       # APT1008 cancelled


def test_finish_by_limit(conn):
    opts = tools.find_options(conn, "SHP1012", T("11:10"), T("13:30"), now=T("09:29"), limit=20)
    assert opts and all(o["unload_until"] <= T("13:30") for o in opts)


# ---- holds and booking ---------------------------------------------------------
def opt(conn, sid, start, now):
    return tools.find_options(conn, sid, start, now=now, limit=1)[0]


def test_held_slot_hidden_from_others(conn):
    o = opt(conn, "SHP1012", T("12:00"), T("09:30"))
    assert tools.hold_option(conn, o, "SHP1012", "t1", T("09:30"))["ok"]
    assert o["slot_id"] not in ids(tools.find_options(conn, "SHP1013", T("12:00"), now=T("09:30"), limit=20))


def test_confirm_writes_history_and_extension(conn):
    o = opt(conn, "SHP1014", T("11:25"), T("09:44"))
    h = tools.hold_option(conn, o, "SHP1014", "t1", T("09:44"))
    res = tools.confirm(conn, h["hold_group"], "t1", T("09:45"))
    assert res["ok"] and res["extension_slot_id"] == o["extension_slot_id"]
    new = tools.current_appointment(conn, "SHP1014")
    assert (new["appointment_status"], new["booking_source"], new["replaced_appointment_id"]) == \
        ("CONFIRMED", "DRIVER_CHAT", "APT1014A")
    old = conn.execute("SELECT appointment_status, is_current, cancelled_at FROM appointments "
                       "WHERE appointment_id='APT1014A'").fetchone()
    assert tuple(old) == ("CANCELLED", 0, T("09:45"))
    ext = conn.execute("SELECT slot_status, block_reason FROM appointment_slots WHERE slot_id=?",
                       (o["extension_slot_id"],)).fetchone()
    assert tuple(ext) == ("BLOCKED", f"Unload extension for {res['appointment_id']}")


def test_rebooking_releases_previous_extension(conn):
    o = opt(conn, "SHP1014", T("11:25"), T("09:44"))
    h = tools.hold_option(conn, o, "SHP1014", "t1", T("09:44"))
    first = tools.confirm(conn, h["hold_group"], "t1", T("09:45"))
    o2 = tools.find_options(conn, "SHP1014", T("14:00"), now=T("09:50"), limit=1)[0]
    h2 = tools.hold_option(conn, o2, "SHP1014", "t1", T("09:50"))
    assert tools.confirm(conn, h2["hold_group"], "t1", T("09:51"))["ok"]
    st = conn.execute("SELECT slot_status FROM appointment_slots WHERE slot_id=?", (first["extension_slot_id"],)).fetchone()[0]
    assert st == "OPEN"


def test_expired_hold_keeps_old_booking(conn):
    o = opt(conn, "SHP1013", T("12:00"), T("09:30"))
    h = tools.hold_option(conn, o, "SHP1013", "t1", T("09:30"))
    assert tools.confirm(conn, h["hold_group"], "t1", T("09:35"))["ok"] is False
    assert tools.current_appointment(conn, "SHP1013")["appointment_id"] == "APT1013A"


def test_cannot_rebook_truck_already_unloading(conn):
    assert tools.book_direct(conn, "SLOT-JAI-047", "SHP1002", T("09:00"))["ok"] is False


# ---- message reading (offline reader) -----------------------------------------
def test_dataset_messages_are_read():
    m = rule_extract("Tyre repaired. I can reach 11:10. I need to leave the warehouse before 1:30 for another pickup.",
                     "2026-08-04 09:29")
    assert (m.intent, m.new_eta, m.latest_end, m.delay_reason) == \
        ("report_delay", "2026-08-04 11:10", "2026-08-04 13:30", "BREAKDOWN")
    assert rule_extract("Evening traffic. ETA 6:30. Can the reefer unload tonight?", "2026-08-04 16:24").new_eta \
        == "2026-08-04 18:30"
    assert rule_extract("I am late by one hour.", "2026-08-04 09:19").delay_minutes == 60
    r = rule_extract("Driver expects arrival between 12:30 and 13:00", "2026-08-04 09:55")
    assert (r.new_eta, r.confidence) == ("2026-08-04 13:00", "LOW")
    assert rule_extract("What slots are possible after 1 PM if I reach around 12:45?", "2026-08-04 10:02").intent \
        == "ask_slots"
    assert rule_extract("Origin released me late. ETA 11:25. Can I get the first possible dock?",
                        "2026-08-04 09:44").choice_index is None
    assert rule_extract("the second one", "2026-08-04 09:44").choice_index == 2
