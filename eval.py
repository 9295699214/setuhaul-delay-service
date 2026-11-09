"""Evaluation: what the system does, measured.

    python eval.py            # writes reports/eval_report.md

Part A - the ladder: scripted conversations from simple to hard, each on a
fresh copy of the snapshot, each with a pass/fail check.
Part B - the race: N drivers want the same slot, repeated R times.
Measures: scenario pass rate, false promises, double bookings, turns to
confirm, escalations.
"""
import statistics
import tempfile
import threading
import time
from pathlib import Path

from app import tools
from app.db import connect, init_db
from app.service import ConversationService

T = lambda hhmm: f"2026-08-04 {hhmm}"   # noqa: E731
PH = lambda n: f"+9198000000{n:02d}"     # noqa: E731


def _active(conn, sid):
    a = tools.current_appointment(conn, sid)
    return dict(a) if a else None


# Each check gets (conn, results) where results is the list of service replies.
SCENARIOS = [
    {"rung": 1, "name": "Simple delay, driver picks option 2", "phone": PH(1),
     "turns": [("Stuck near Kotputli, 2 hrs late", T("08:30")), ("the second one", T("08:31"))],
     "check": lambda c, r: r[-1]["action"] == "confirmed"
        and _active(c, "SH-01")["slot_id"] == r[0]["options"][1]["slot_id"]
        and r[0]["eta"] == T("12:00")},
    {"rung": 1, "name": "Vague message: ask only for the ETA", "phone": PH(5),
     "turns": [("running late bhai", T("09:00")), ("about 2 hours", T("09:01")), ("1", T("09:02"))],
     "check": lambda c, r: [x["action"] for x in r] == ["ask_eta", "offered", "confirmed"]},
    {"rung": 2, "name": "Reefer load only offered the reefer dock", "phone": PH(3),
     "turns": [("running 1.5 hours late, anything after 1pm", T("09:00"))],
     "check": lambda c, r: r[0]["action"] == "offered"
        and {o["dock_id"] for o in r[0]["options"]} == {"JAI-D3"}},
    {"rung": 2, "name": "Heavy load only offered the heavy dock", "phone": PH(4),
     "turns": [("1 hour late", T("09:00"))],
     "check": lambda c, r: {o["dock_id"] for o in r[0]["options"]} == {"JAI-D4"}},
    {"rung": 3, "name": "Window + maintenance block + ops override ETA", "phone": PH(2),
     "turns": [("around 3 hours late, need to leave by 4pm", T("09:30"))],
     "check": lambda c, r: r[0]["eta"] == T("14:30")          # ops 11:30 + 3h, not driver 12:30
        and all(o["dock_id"] != "JAI-D2" for o in r[0]["options"])
        and all(o["end_at"] <= T("16:00") for o in r[0]["options"])},
    {"rung": 3, "name": "Never offers an inactive dock or overlapping slot", "phone": PH(14),
     "turns": [("30 min late", T("08:00"))],
     "check": lambda c, r: all(o["dock_id"] != "JAI-D5" and o["slot_id"] != "JAI-D1-1030"
                               for o in r[0]["options"])},
    {"rung": 4, "name": "Hold expires before driver answers: no false promise", "phone": PH(6),
     "turns": [("45 min late", T("10:00")), ("first", T("10:05"))],
     "check": lambda c, r: r[-1]["action"] == "confirm_failed"
        and _active(c, "SH-06")["slot_id"] == "JAI-D2-19"},
    {"rung": 4, "name": "Gate shows truck already inside: backend beats driver", "phone": PH(15),
     "turns": [("I am 1 hour late", T("08:30"))],
     "check": lambda c, r: r[0]["action"] == "already_arrived"},
    {"rung": 4, "name": "Unload longer than any slot: escalate, don't invent capacity", "phone": PH(13),
     "turns": [("1 hour late", T("08:30"))],
     "check": lambda c, r: r[0]["action"] == "escalate"},
    {"rung": 5, "name": "Shared phone: ask truck number, then resume", "phone": PH(18),
     "turns": [("late by 1 hour", T("11:00")), ("my truck is GJ01 AB 1019", T("11:01")), ("1", T("11:02"))],
     "check": lambda c, r: [x["action"] for x in r] == ["ask_vehicle", "offered", "confirmed"]
        and _active(c, "SH-19") is not None and _active(c, "SH-19")["slot_id"] == r[1]["options"][0]["slot_id"]},
    {"rung": 5, "name": "Unknown number", "phone": "+910000000000",
     "turns": [("late", T("08:00"))],
     "check": lambda c, r: r[0]["action"] == "no_shipment"},
    {"rung": 5, "name": "Rebook keeps exactly one active appointment and logs history", "phone": PH(1),
     "turns": [("2 hrs late", T("08:00")), ("1", T("08:01"))],
     "check": lambda c, r: c.execute("SELECT COUNT(*) FROM appointments WHERE shipment_id='SH-01' "
                                     "AND status='active'").fetchone()[0] == 1
        and c.execute("SELECT COUNT(*) FROM appointment_history WHERE shipment_id='SH-01' "
                      "AND new_status='rescheduled'").fetchone()[0] == 1
        and c.execute("SELECT COUNT(*) FROM outbound_confirmations WHERE shipment_id='SH-01'").fetchone()[0] == 1},
]


def fresh_db() -> Path:
    p = Path(tempfile.mkdtemp()) / "eval.db"
    init_db(p)
    return p


def run_scenario(sc) -> dict:
    db = fresh_db()
    conn = connect(db)
    svc = ConversationService(conn)
    results = [svc.handle("eval", sc["phone"], text, now) for text, now in sc["turns"]]
    try:
        ok = bool(sc["check"](conn, results))
    except Exception as e:   # a broken check is a failure, not a crash
        ok = False
        results.append({"action": f"check error: {e}", "reply": ""})
    false_promises = count_false_promises(conn)
    conn.close()
    return {"name": sc["name"], "rung": sc["rung"], "ok": ok, "turns": len(sc["turns"]),
            "actions": [r["action"] for r in results], "false_promises": false_promises,
            "transcript": [(t, r["reply"]) for (t, _), r in zip(sc["turns"], results)]}


def count_false_promises(conn) -> int:
    """Confirmations the service sent whose slot is not the shipment's active booking."""
    return conn.execute("""
        SELECT COUNT(*) FROM outbound_confirmations c
        WHERE c.id = (SELECT MAX(id) FROM outbound_confirmations c2 WHERE c2.shipment_id = c.shipment_id)
          AND c.sent_at > '2026-08-04 05:00'
          AND NOT EXISTS (SELECT 1 FROM appointments a WHERE a.shipment_id = c.shipment_id
                          AND a.status='active' AND a.slot_id = c.slot_id)""").fetchone()[0]


def count_double_bookings(conn) -> int:
    """Pairs of active bookings overlapping on one dock, created by the agent."""
    return conn.execute("""
        SELECT COUNT(*) FROM appointments a1 JOIN slots s1 ON s1.id=a1.slot_id
        JOIN appointments a2 ON a2.id > a1.id AND a2.status='active'
        JOIN slots s2 ON s2.id=a2.slot_id AND s2.dock_id=s1.dock_id
        WHERE a1.status='active' AND (a1.source='agent' OR a2.source='agent')
          AND s1.start_at < s2.end_at AND s2.start_at < s1.end_at""").fetchone()[0]


RACE_SHIPMENTS = ["SH-01", "SH-02", "SH-05", "SH-06", "SH-07", "SH-08", "SH-12", "SH-14", "SH-15", "SH-16"]


def race_once(drivers: int = 10) -> dict:
    """All drivers ask at the same instant for 16:00-17:00 at Jaipur."""
    db = fresh_db()
    setup = connect(db)
    setup.execute("DELETE FROM gate_events")      # make every truck eligible to rebook
    phones = {s: setup.execute("SELECT d.phone FROM shipments sh JOIN drivers d ON d.id=sh.driver_id "
                               "WHERE sh.id=?", (s,)).fetchone()[0] for s in RACE_SHIPMENTS[:drivers]}
    setup.close()
    barrier = threading.Barrier(len(phones))
    out = {}

    def run(sid, phone):
        conn = connect(db)
        try:
            svc = ConversationService(conn)
            barrier.wait()
            r1 = svc.handle(f"race-{sid}", phone, "1 hour late, after 4pm, leave by 5pm", T("08:00"))
            r2 = svc.handle(f"race-{sid}", phone, "1", T("08:01")) if r1["action"] == "offered" else r1
            out[sid] = r2
        finally:
            conn.close()

    threads = [threading.Thread(target=run, args=(s, p)) for s, p in phones.items()]
    t0 = time.perf_counter()
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    elapsed = time.perf_counter() - t0
    conn = connect(db)
    confirmed = [r for r in out.values() if r["action"] == "confirmed"]
    slots = [r["slot_id"] for r in confirmed]
    result = {"confirmed": len(confirmed), "escalated": sum(r["action"] == "escalate" for r in out.values()),
              "other": sum(r["action"] not in ("confirmed", "escalate") for r in out.values()),
              "same_slot_promised_twice": len(slots) - len(set(slots)),
              "false_promises": count_false_promises(conn),
              "double_bookings": count_double_bookings(conn), "seconds": elapsed}
    conn.close()
    return result


def main(rounds: int = 30):
    print("Part A: ladder")
    ladder = [run_scenario(s) for s in SCENARIOS]
    for r in ladder:
        print(f"  [{'PASS' if r['ok'] else 'FAIL'}] rung {r['rung']}: {r['name']}  {r['actions']}")

    print(f"Part B: race, 10 drivers x {rounds} rounds")
    races = [race_once() for _ in range(rounds)]
    agg = {k: sum(r[k] for r in races) for k in
           ("confirmed", "escalated", "other", "same_slot_promised_twice", "false_promises", "double_bookings")}
    print(f"  {agg}")

    passed = sum(r["ok"] for r in ladder)
    confirm_turns = [r["turns"] for r in ladder if r["actions"] and r["actions"][-1] == "confirmed"]
    md = ["# Evaluation report", "",
          "Generated by `python eval.py` on the stand-in snapshot (4 Aug 2026). "
          "Message understanding: offline regex extractor (no LLM calls).", "",
          "## Measures", "",
          "| Measure | Result |", "|---|---|",
          f"| Ladder scenarios passed | {passed} / {len(ladder)} |",
          f"| False promises (ladder + race) | {sum(r['false_promises'] for r in ladder) + agg['false_promises']} |",
          f"| Same slot promised to two drivers (race) | {agg['same_slot_promised_twice']} |",
          f"| Double bookings on a dock (race) | {agg['double_bookings']} |",
          f"| Race rounds | {rounds} x 10 drivers = {rounds * 10} conversations |",
          f"| Race outcomes | {agg['confirmed']} confirmed, {agg['escalated']} escalated, {agg['other']} other |",
          f"| Avg confirmed per round | {agg['confirmed'] / rounds:.1f} (capacity: 3 free compatible docks at 16:00) |",
          f"| Turns to confirmation (ladder) | median {statistics.median(confirm_turns) if confirm_turns else '-'} |",
          f"| Race round time | median {statistics.median(r['seconds'] for r in races) * 1000:.0f} ms |",
          "", "## Ladder", "", "| Rung | Scenario | Result | Actions |", "|---|---|---|---|"]
    md += [f"| {r['rung']} | {r['name']} | {'PASS' if r['ok'] else 'FAIL'} | {' → '.join(r['actions'])} |"
           for r in ladder]
    md += ["", "## Transcripts", ""]
    for r in ladder:
        md += [f"### {r['name']}", ""]
        for text, reply in r["transcript"]:
            md += [f"> **Driver:** {text}  ", f"> **SetuHaul:** {reply.replace(chr(10), ' / ')}", ""]
    out = Path(__file__).resolve().parent / "reports" / "eval_report.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(md) + "\n")
    print(f"Wrote {out}")
    return passed == len(ladder) and agg["false_promises"] == 0 and agg["same_slot_promised_twice"] == 0


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
