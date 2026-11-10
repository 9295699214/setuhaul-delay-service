"""Evaluation on the FDE Academy snapshot: what the system does, measured.

    python eval.py            # writes reports/eval_report.md

Part A - the ladder: the guide's seeded cases (section 6), simple to hard, each on a
fresh copy of the supplied database, each with a pass/fail check on what the
service replied AND what it wrote to the tables.
Part B - the race: the guide's "simultaneous slot competition" (SHP1006, SHP1012,
SHP1013, SHP1014) plus SHP1009 and SHP1017 all ask at once for the same few
12:00 slots, repeated R times.
Measures: scenario pass rate, false promises, double bookings, escalations,
turns to confirmation, time per message.
"""
import statistics
import tempfile
import threading
import time
from pathlib import Path

from app import tools
from app.db import connect, init_db
from app.service import ConversationService

T = lambda hhmm: f"2026-08-04T{hhmm}:00+05:30"   # noqa: E731
P = lambda n: f"+91-90000100{n:02d}"              # noqa: E731  driver phone, e.g. P(12) = DRV012


def make_service(conn, mode: str = "workflow"):
    """workflow: fixed steps in code. agent: the LLM picks tools (needs OPENROUTER_API_KEY)."""
    if mode == "agent":
        from app.agent_mode import AgentService
        return AgentService(conn)
    return ConversationService(conn)


def appt(c, sid):
    return tools.current_appointment(c, sid)


def one(c, sql, *args):
    return c.execute(sql, args).fetchone()[0]


def opts(r):
    return r[0].get("options", [])


SCENARIOS = [
    {"rung": 1, "case": "Late ETA reported before arrival", "ref": "SHP1012 / THR002",
     "name": "Tyre delay with a leave-by limit, then book", "phone": P(12),
     "turns": [("Tyre repaired. I can reach 11:10. I need to leave the warehouse before 1:30 for another pickup.", "09:29"),
               ("1", "09:30")],
     "check": lambda c, r: r[0]["action"] == "offered" and r[1]["action"] == "confirmed"
        and all(o["start"] >= T("11:10") and o["unload_until"] <= T("13:30") and o["dock_type"] == "STANDARD" for o in opts(r))
        and appt(c, "SHP1012")["booking_source"] == "DRIVER_CHAT"
        and one(c, "SELECT COUNT(*) FROM eta_updates WHERE shipment_id='SHP1012' AND declared_eta_ts=? AND created_at=?",
                T("11:10"), T("09:29")) == 1
        and one(c, "SELECT COUNT(*) FROM operational_messages WHERE appointment_id=? AND delivery_status='SENT'",
                r[1]["appointment_id"]) == 2},
    {"rung": 1, "case": "Uncertain ETA", "ref": "SHP1013 / THR003",
     "name": "'Late by one hour' is added to the plan and stated back", "phone": P(13),
     "turns": [("I am late by one hour.", "09:19")],
     "check": lambda c, r: r[0]["action"] == "offered" and r[0]["eta"] == T("11:00")
        and "10:00 AM plus 60 min" in r[0].get("template_reply", r[0]["reply"])},
    {"rung": 2, "case": "Reefer compatibility", "ref": "SHP1010",
     "name": "Reefer load offered only the reefer dock D5", "phone": P(10),
     "turns": [("Running 30 min late", "10:00")],
     "check": lambda c, r: r[0]["action"] == "offered" and {o["dock_code"] for o in opts(r)} == {"D5"}},
    {"rung": 2, "case": "Heavy vehicle compatibility + ambiguous driver", "ref": "SHP1016 / THR006",
     "name": "Driver on two moving trucks: ask, then heavy dock D6 only", "phone": P(7),
     "turns": [("Heavy trailer ETA 12:00. Original heavy-bay slot will be missed. What is next?", "09:39"),
               ("MH04HV4107", "09:40"), ("1", "09:41")],
     "check": lambda c, r: [x["action"] for x in r] == ["ask_shipment", "offered", "confirmed"]
        and {o["dock_code"] for o in r[1]["options"]} == {"D6"}
        and appt(c, "SHP1016")["dock_code"] == "D6"},
    {"rung": 2, "case": "Different unloading durations", "ref": "SHP1014 / THR004",
     "name": "CRITICAL 21 t, 75-min unload: 25 t dock + extension slot", "phone": P(14),
     "turns": [("Origin released me late. ETA 11:25. This load is urgent. Can I get the first possible dock?", "09:44"),
               ("1", "09:45")],
     "check": lambda c, r: r[1]["action"] == "confirmed" and r[1]["extension_slot_id"]
        and all(o["dock_code"] in ("D2", "D4") and o["extension_slot_id"] for o in opts(r))
        and one(c, "SELECT block_reason FROM appointment_slots WHERE slot_id=?", r[1]["extension_slot_id"])
            == f"Unload extension for {r[1]['appointment_id']}"
        and one(c, "SELECT appointment_status || is_current FROM appointments WHERE appointment_id='APT1014A'") == "CANCELLED0"
        and appt(c, "SHP1014")["replaced_appointment_id"] == "APT1014A"},
    {"rung": 3, "case": "Reefer dock unavailable / no feasible slot", "ref": "SHP1015 / THR005",
     "name": "Only reefer dock under maintenance: escalate and tell a real contact", "phone": P(15),
     "turns": [("Evening traffic. ETA 6:30. Can the reefer unload tonight?", "16:24")],
     "check": lambda c, r: r[0]["action"] == "escalate"
        and one(c, "SELECT COUNT(*) FROM operational_messages WHERE shipment_id='SHP1015' AND channel='INTERNAL' "
                   "AND recipient_address='anita.joshi@setuhaul.example' AND delivery_status='SENT'") == 1
        and one(c, "SELECT exception_status FROM driver_exceptions WHERE thread_id='CHAT-eval'") == "ESCALATED"},
    {"rung": 3, "case": "Early arrival / early truck does not automatically win", "ref": "SHP1003 / THR007",
     "name": "Early truck at the gate keeps its 09:00 slot", "phone": P(3),
     "turns": [("I am already at the gate. Can I get a dock now instead of waiting for 9?", "08:22")],
     "check": lambda c, r: r[0]["action"] == "status" and appt(c, "SHP1003")["slot_id"] == "SLOT-JAI-002"
        and "doesn't move you ahead" in r[0].get("template_reply", r[0]["reply"])},
    {"rung": 3, "case": "Late arrival already at yard + cancellation frees capacity", "ref": "SHP1004 / THR008 / APT1008",
     "name": "Late truck past grace re-slotted from now; freed slot offered first", "phone": P(4),
     "turns": [("I reached late and am in the yard. Has a new dock been assigned?", "09:40")],
     "check": lambda c, r: r[0]["action"] == "offered" and opts(r)[0]["slot_id"] == "SLOT-JAI-017"
        and all(o["start"] >= T("09:40") for o in opts(r))},
    {"rung": 3, "case": "Dock breakdown", "ref": "SHP1005 / DEVT001",
     "name": "Truck waiting for a broken dock re-slotted off D3", "phone": P(5),
     "turns": [("Dock D3 is not working. What should I do?", "09:20")],
     "check": lambda c, r: r[0]["action"] == "offered" and all(o["dock_code"] in ("D2", "D4") for o in opts(r))},
    {"rung": 4, "case": "Ambiguous shipment", "ref": "DRV004 / THR010",
     "name": "Two assignments: ask, then resume the parked delay", "phone": P(4),
     "turns": [("I will be late by 45 minutes.", "09:31"), ("020", "09:32"), ("1", "09:33")],
     "check": lambda c, r: [x["action"] for x in r] == ["ask_shipment", "offered", "confirmed"]
        and r[1]["eta"] == T("18:45") and appt(c, "SHP1020")["booking_source"] == "DRIVER_CHAT"
        and appt(c, "SHP1004")["appointment_id"] == "APT1004"},
    {"rung": 4, "case": "Duplicate driver message", "ref": "THR001 / THR009",
     "name": "Messaging retry is answered but not processed twice", "phone": P(6),
     "turns": [("Traffic after Shahpura. Reaching around 11:20. Any slot after 12?", "09:34"),
               ("Traffic after Shahpura. Reaching around 11:20. Any slot after 12?", "09:35")],
     "check": lambda c, r: [x["action"] for x in r] == ["offered", "duplicate"]
        and one(c, "SELECT COUNT(*) FROM slot_holds WHERE thread_id='CHAT-eval' AND role='MAIN'") == len(opts(r))
        and one(c, "SELECT COUNT(*) FROM chat_messages WHERE thread_id='CHAT-eval' AND is_duplicate=1") == 1
        and one(c, "SELECT COUNT(*) FROM eta_updates WHERE shipment_id='SHP1006' AND eta_update_id LIKE 'ETA-%'") == 1},
    {"rung": 4, "case": "Ask-only conversation", "ref": "SHP1017 / THR011",
     "name": "'What slots if I reach 12:45?' shows options, books nothing", "phone": P(1),
     "turns": [("What slots are possible after 1 PM if I reach around 12:45?", "10:02")],
     "check": lambda c, r: r[0]["action"] == "offered" and all(o["start"] >= T("13:00") for o in opts(r))
        and appt(c, "SHP1017")["appointment_id"] == "APT1017"
        and one(c, "SELECT COUNT(*) FROM eta_updates WHERE shipment_id='SHP1017' AND eta_update_id LIKE 'ETA-%'") == 0},
    {"rung": 4, "case": "Race protection (single driver)", "ref": "holds",
     "name": "Hold expires before the driver answers: no false promise", "phone": P(12),
     "turns": [("Tyre repaired. I can reach 11:10.", "09:29"), ("1", "09:34")],
     "check": lambda c, r: r[1]["action"] == "confirm_failed" and appt(c, "SHP1012") is None},
    {"rung": 5, "case": "Cancelled shipment message", "ref": "SHP1019 / THR012",
     "name": "Cancelled shipment: tell the driver, don't offer slots", "phone": P(8),
     "turns": [("Is my 4 PM slot still active?", "10:12")],
     "check": lambda c, r: r[0]["action"] == "cancelled_shipment" and "ORD-260804-019" in r[0]["reply"]},
    {"rung": 5, "case": "Unload in progress", "ref": "SHP1002",
     "name": "Truck in the dock gets its status, nothing is rebooked", "phone": P(2),
     "turns": [("How long more for unloading?", "09:00")],
     "check": lambda c, r: r[0]["action"] == "status" and appt(c, "SHP1002")["appointment_status"] == "IN_PROGRESS"},
    {"rung": 5, "case": "Unknown sender", "ref": "-",
     "name": "Number not in the driver list", "phone": "+91-9999999999",
     "turns": [("late", "09:00")],
     "check": lambda c, r: r[0]["action"] == "no_shipment"},
]


def fresh_db() -> Path:
    p = Path(tempfile.mkdtemp()) / "eval.db"
    init_db(p)
    return p


def run_scenario(sc, mode: str = "workflow") -> dict:
    conn = connect(fresh_db())
    svc = make_service(conn, mode)
    results, latencies = [], []
    for text, hhmm in sc["turns"]:
        t0 = time.perf_counter()
        results.append(svc.handle("eval", sc["phone"], text, T(hhmm)))
        latencies.append(time.perf_counter() - t0)
    try:
        ok = bool(sc["check"](conn, results))
    except Exception as e:      # a broken check is a failure, not a crash
        ok = False
        results.append({"action": f"check error: {type(e).__name__}: {e}", "reply": ""})
    out = {"name": sc["name"], "case": sc["case"], "ref": sc["ref"], "rung": sc["rung"], "ok": ok,
           "turns": len(sc["turns"]), "latency_s": latencies,
           "tool_calls": sum(len(r.get("tool_calls", [])) for r in results),
           "llm_replies": sum(r.get("reply_source") == "llm" for r in results),
           "actions": [r["action"] for r in results], "false_promises": count_false_promises(conn),
           "double_bookings": count_double_bookings(conn),
           "transcript": [(t, r["reply"]) for (t, _), r in zip(sc["turns"], results)]}
    conn.close()
    return out


def count_false_promises(conn) -> int:
    """Driver confirmations we sent whose appointment is not the shipment's current active booking."""
    return conn.execute(f"""
        SELECT COUNT(*) FROM operational_messages o
        WHERE o.channel='WHATSAPP' AND o.sender_address='{tools.AGENT_ADDRESS}'
          AND o.operational_message_id = (SELECT o2.operational_message_id FROM operational_messages o2
              WHERE o2.shipment_id=o.shipment_id AND o2.channel='WHATSAPP' AND o2.sender_address='{tools.AGENT_ADDRESS}'
              ORDER BY o2.sent_at DESC, o2.rowid DESC LIMIT 1)
          AND NOT EXISTS (SELECT 1 FROM appointments a WHERE a.appointment_id=o.appointment_id AND a.is_current=1
              AND a.appointment_status IN {tools.ACTIVE_SQL})""").fetchone()[0]


def count_double_bookings(conn) -> int:
    """Overlaps on one dock involving a booking made by the service: two active bookings,
    or an active booking on a slot that is blocked as another booking's extension."""
    pairs = conn.execute(f"""
        SELECT COUNT(*) FROM appointments a1 JOIN appointment_slots s1 ON s1.slot_id=a1.slot_id
        JOIN appointments a2 ON a2.appointment_id > a1.appointment_id AND a2.appointment_status IN {tools.ACTIVE_SQL}
        JOIN appointment_slots s2 ON s2.slot_id=a2.slot_id AND s2.dock_id=s1.dock_id
        WHERE a1.appointment_status IN {tools.ACTIVE_SQL} AND 'DRIVER_CHAT' IN (a1.booking_source, a2.booking_source)
          AND s1.slot_start_ts < s2.slot_end_ts AND s2.slot_start_ts < s1.slot_end_ts""").fetchone()[0]
    ext = conn.execute(f"""
        SELECT COUNT(*) FROM appointment_slots x JOIN appointments a ON a.slot_id = x.slot_id
        WHERE x.slot_status='BLOCKED' AND x.block_reason LIKE 'Unload extension for %'
          AND a.appointment_status IN {tools.ACTIVE_SQL}""").fetchone()[0]
    return pairs + ext


RACE = [(6, "SHP1006"), (12, "SHP1012"), (13, "SHP1013"), (14, "SHP1014"), (9, "SHP1009"), (1, "SHP1017")]
RACE_TEXT = "Running late, reaching 11:50. Must finish unloading by 1:15 pm."


def race_once(mode: str = "workflow") -> dict:
    """Six drivers ask at the same instant for slots 11:50-13:15 at Jaipur (few fit)."""
    db = fresh_db()
    barrier = threading.Barrier(len(RACE))
    out = {}

    def run(n, sid):
        conn = connect(db)
        try:
            svc = make_service(conn, mode)
            barrier.wait()
            r1 = svc.handle(f"race-{sid}", P(n), RACE_TEXT, T("09:45"))
            out[sid] = svc.handle(f"race-{sid}", P(n), "1", T("09:46")) if r1["action"] == "offered" else r1
        finally:
            conn.close()

    threads = [threading.Thread(target=run, args=x) for x in RACE]
    t0 = time.perf_counter()
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    elapsed = time.perf_counter() - t0
    conn = connect(db)
    confirmed = [r for r in out.values() if r["action"] == "confirmed"]
    slots = [r["slot_id"] for r in confirmed] + [r["extension_slot_id"] for r in confirmed if r.get("extension_slot_id")]
    res = {"confirmed": len(confirmed), "escalated": sum(r["action"] == "escalate" for r in out.values()),
           "other": sum(r["action"] not in ("confirmed", "escalate") for r in out.values()),
           "same_slot_promised_twice": len(slots) - len(set(slots)),
           "false_promises": count_false_promises(conn), "double_bookings": count_double_bookings(conn),
           "seconds": elapsed}
    conn.close()
    return res


def run_all(mode: str = "workflow", rounds: int = 30) -> dict:
    """Ladder + race for one mode, as a JSON-able summary."""
    ladder = [run_scenario(s, mode) for s in SCENARIOS]
    races = [race_once(mode=mode) for _ in range(rounds)]
    agg = {k: sum(r[k] for r in races) for k in
           ("confirmed", "escalated", "other", "same_slot_promised_twice", "false_promises", "double_bookings")}
    lat = [x for r in ladder for x in r["latency_s"]]
    msgs = sum(r["turns"] for r in ladder)
    return {"mode": mode, "rounds": rounds, "drivers_per_round": len(RACE),
            "ladder_passed": sum(r["ok"] for r in ladder), "ladder_total": len(ladder),
            "ladder": [{k: r[k] for k in ("rung", "case", "ref", "name", "ok", "actions", "tool_calls", "llm_replies")}
                       for r in ladder],
            "false_promises": sum(r["false_promises"] for r in ladder) + agg["false_promises"],
            "double_bookings": sum(r["double_bookings"] for r in ladder) + agg["double_bookings"],
            "race": agg, "median_latency_s": round(statistics.median(lat), 3) if lat else None,
            "race_median_s": round(statistics.median(r["seconds"] for r in races), 3),
            "tool_calls_per_message": round(sum(r["tool_calls"] for r in ladder) / msgs, 2),
            "llm_reply_share": round(sum(r["llm_replies"] for r in ladder) / msgs, 2),
            "_ladder_full": ladder}


def comparison_section() -> list[str]:
    """Workflow vs agent, from JSON results of live runs saved in reports/ (agent mode needs a
    live LLM, so it is run on the server via POST /admin/eval and saved here)."""
    import json
    rep = Path(__file__).resolve().parent / "reports"
    runs = {m: json.loads((rep / f"eval_{m}_llm.json").read_text())
            for m in ("workflow", "agent") if (rep / f"eval_{m}_llm.json").exists()}
    if len(runs) < 2:
        return []
    w, a = runs["workflow"], runs["agent"]
    row = lambda label, f: f"| {label} | {f(w)} | {f(a)} |"   # noqa: E731
    md = ["", "## Workflow vs agent (live, LLM on)", "",
          f"Both runs on the deployed service with OpenRouter `{a.get('model', 'anthropic/claude-haiku-4.5')}`. "
          "Workflow: the LLM reads and words messages, code picks every step. "
          "Agent: the LLM also chooses which tool to call; code guards each tool.", "",
          "| Measure | Workflow | Agent (tool calling) |", "|---|---|---|",
          row("Ladder scenarios passed", lambda r: f"{r['ladder_passed']} / {r['ladder_total']}"),
          row("False promises", lambda r: r["false_promises"]),
          row("Double bookings", lambda r: r.get("double_bookings", r["race"]["double_bookings"])),
          row("Same slot promised twice (race)", lambda r: r["race"]["same_slot_promised_twice"]),
          row("Race outcomes", lambda r: f"{r['race']['confirmed']} confirmed, {r['race']['escalated']} escalated, "
                                         f"{r['race']['other']} other"),
          row("Median time per message", lambda r: f"{r['median_latency_s']} s"),
          row("Tool calls chosen by the LLM per message", lambda r: r["tool_calls_per_message"]),
          row("Replies in LLM wording (passed fact check)", lambda r: f"{round(r['llm_reply_share'] * 100)}%"),
          "", "| Rung | Seeded case | Workflow | Agent |", "|---|---|---|---|"]
    for lw, la in zip(w["ladder"], a["ladder"]):
        md.append(f"| {lw['rung']} | {lw['name']} | {'PASS' if lw['ok'] else 'FAIL'} | "
                  f"{'PASS' if la['ok'] else 'FAIL'} ({' → '.join(la['actions'])}) |")
    return md


def main(rounds: int = 30):
    res = run_all("workflow", rounds)
    ladder = res.pop("_ladder_full")
    print("Part A: ladder (guide's seeded cases)")
    for r in ladder:
        print(f"  [{'PASS' if r['ok'] else 'FAIL'}] rung {r['rung']}: {r['name']}  {r['actions']}")
    print(f"Part B: race, {len(RACE)} drivers x {rounds} rounds\n  {res['race']}")
    confirm_turns = [r["turns"] for r in ladder if r["actions"] and r["actions"][-1] == "confirmed"]
    md = ["# Evaluation report", "",
          "Generated by `python eval.py` on the FDE Academy snapshot (4 Aug 2026), each scenario on a fresh copy. "
          "Message understanding: offline regex reader (no LLM calls).", "",
          "## Measures", "", "| Measure | Result |", "|---|---|",
          f"| Seeded cases passed | {res['ladder_passed']} / {res['ladder_total']} |",
          f"| False promises (ladder + race) | {res['false_promises']} |",
          f"| Double bookings incl. extension slots (ladder + race) | {res['double_bookings']} |",
          f"| Same slot promised to two drivers (race) | {res['race']['same_slot_promised_twice']} |",
          f"| Race | {rounds} rounds x {len(RACE)} drivers (SHP1006, 1012, 1013, 1014, 1009, 1017) |",
          f"| Race outcomes | {res['race']['confirmed']} confirmed, {res['race']['escalated']} escalated, {res['race']['other']} other |",
          f"| Avg confirmed per round | {res['race']['confirmed'] / rounds:.1f} |",
          f"| Turns to confirmation | median {statistics.median(confirm_turns) if confirm_turns else '-'} |",
          f"| Time per message (offline) | median {res['median_latency_s'] * 1000:.0f} ms |",
          "", "## Ladder", "", "| Rung | Guide case | Seed ref | Scenario | Result | Actions |", "|---|---|---|---|---|---|"]
    md += [f"| {r['rung']} | {r['case']} | {r['ref']} | {r['name']} | {'PASS' if r['ok'] else 'FAIL'} | "
           f"{' → '.join(r['actions'])} |" for r in ladder]
    md += comparison_section()
    md += ["", "## Transcripts", ""]
    for r in ladder:
        md += [f"### {r['name']} ({r['ref']})", ""]
        for text, reply in r["transcript"]:
            md += [f"> **Driver:** {text}  ", f"> **SetuHaul:** {reply.replace(chr(10), ' / ')}", ""]
    out = Path(__file__).resolve().parent / "reports" / "eval_report.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(md) + "\n")
    print(f"Wrote {out}")
    return res["ladder_passed"] == res["ladder_total"] and res["false_promises"] == 0 and res["double_bookings"] == 0


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
