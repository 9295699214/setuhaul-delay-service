"""Optional scheduling tool: one facility, all its trucks, one proposal.

Reads structured data only (never chat text). Returns a proposed sequence;
it does not write bookings. A human or the agent's confirm step applies it.

Model (from the brief):
  truck/shipment -> job            dock door -> machine
  latest ETA     -> release time   gate-in   -> available now
  unload minutes -> processing     appointment -> due time
  truck unloading now -> fixed task   priority -> weight

Method: a transparent greedy dispatch rule. Jobs are taken in order of
release time (higher priority first on ties). Each job goes to the
compatible dock and start time with the lowest score:

  score = priority * (W_WAIT * wait_min + W_LATE * late_min) + W_CHANGE * changed
          + W_INTRUDE * (overlaps another truck's promised slot)
          + W_SCARCE  * (reefer/heavy dock used by a load that doesn't need it)

Plain-words objective: less waiting, fewer missed appointments, and fewer
changes to a schedule a driver has already been told.
"""
from datetime import datetime, timedelta

from . import tools

FMT = tools.FMT
W_WAIT, W_LATE, W_CHANGE = 1.0, 2.0, 30.0
W_INTRUDE = 30.0   # taking a time another truck was already promised
W_SCARCE = 5.0     # using a reefer/heavy dock for a load that doesn't need it


def _dt(s):
    return datetime.strptime(s, FMT)


def _s(d):
    return d.strftime(FMT)


def propose_schedule(conn, facility_id: str, now: str, horizon_end: str | None = None) -> dict:
    now_d = _dt(now)
    docks = [dict(r) for r in conn.execute(
        "SELECT * FROM docks WHERE facility_id=? AND active=1 ORDER BY id", (facility_id,))]
    blocks = {}
    for b in conn.execute("SELECT b.* FROM dock_blocks b JOIN docks d ON d.id=b.dock_id "
                          "WHERE d.facility_id=?", (facility_id,)):
        blocks.setdefault(b["dock_id"], []).append((_dt(b["start_at"]), _dt(b["end_at"])))
    free_at = {d["id"]: now_d for d in docks}

    fixed = []
    # Trucks already unloading: fixed tasks the schedule may not move
    for r in conn.execute("""
        SELECT a.shipment_id, de.dock_id, de.at, sh.unload_minutes
        FROM dock_events de JOIN appointments a ON a.id = de.appointment_id
        JOIN shipments sh ON sh.id = a.shipment_id JOIN docks d ON d.id = de.dock_id
        WHERE d.facility_id = ? AND de.event='unload_start'
          AND NOT EXISTS (SELECT 1 FROM dock_events e2 WHERE e2.appointment_id = de.appointment_id
                          AND e2.event='unload_end')""", (facility_id,)):
        end = _dt(r["at"]) + timedelta(minutes=r["unload_minutes"])
        free_at[r["dock_id"]] = max(free_at[r["dock_id"]], end)
        fixed.append({"shipment": r["shipment_id"], "dock": r["dock_id"], "start": r["at"],
                      "end": _s(end), "fixed": True})
    fixed_ids = {f["shipment"] for f in fixed}

    jobs, skipped = [], []
    for sh in conn.execute("SELECT * FROM shipments WHERE facility_id=? AND status IN "
                           "('in_transit','arrived') ORDER BY id", (facility_id,)):
        sid = sh["id"]
        if sid in fixed_ids:
            continue
        gate = tools.gate_status(conn, sid)
        eta = tools.current_eta(conn, sid)
        if gate and gate["event"] == "gate_in":
            arrival, basis = _dt(gate["at"]), "gate_in"
        elif eta:
            arrival, basis = _dt(eta["eta"]), eta["source"]
        else:
            skipped.append({"shipment": sid, "reason": "no ETA and no gate-in"})
            continue
        appt = tools.current_appointment(conn, sid)
        jobs.append({"sid": sid, "arrival": arrival, "release": max(arrival, now_d), "basis": basis,
                     "priority": sh["priority"], "unload": sh["unload_minutes"],
                     "reefer": sh["needs_reefer"], "kg": sh["load_weight_kg"],
                     "appt": dict(appt) if appt else None})

    def earliest_start(dock_id, t0, minutes):
        t = max(t0, free_at[dock_id])
        moved = True
        while moved:
            moved = False
            for bs, be in blocks.get(dock_id, []):
                if bs < t + timedelta(minutes=minutes) and be > t:
                    t, moved = be, True
        return t

    promised = {j["sid"]: (j["appt"]["dock_id"], _dt(j["appt"]["start_at"]), _dt(j["appt"]["end_at"]))
                for j in jobs if j["appt"]}

    def intrudes(sid, dock_id, st, en):
        return any(d == dock_id and ps < en and pe > st
                   for other, (d, ps, pe) in promised.items() if other != sid)

    plan = []
    for j in sorted(jobs, key=lambda j: (j["release"], -j["priority"], j["sid"])):
        best = None
        for d in docks:
            if (j["reefer"] and not d["supports_reefer"]) or j["kg"] > d["max_weight_kg"]:
                continue
            start = earliest_start(d["id"], j["release"], j["unload"])
            # keeping the promised slot is an option if it is still feasible
            options = [start]
            if j["appt"] and j["appt"]["dock_id"] == d["id"]:
                kept = _dt(j["appt"]["start_at"])
                if kept >= start and earliest_start(d["id"], kept, j["unload"]) == kept:
                    options.append(kept)
            for st in options:
                wait = (st - j["arrival"]).total_seconds() / 60
                due = _dt(j["appt"]["start_at"]) if j["appt"] else None
                late = max(0.0, (st - due).total_seconds() / 60) if due else 0.0
                changed = int(not (j["appt"] and j["appt"]["dock_id"] == d["id"] and due == st))
                en = st + timedelta(minutes=j["unload"])
                scarce = (d["supports_reefer"] and not j["reefer"]) or (d["max_weight_kg"] > 20000 >= j["kg"])
                score = (j["priority"] * (W_WAIT * max(wait, 0) + W_LATE * late) + W_CHANGE * changed
                         + W_INTRUDE * intrudes(j["sid"], d["id"], st, en) + W_SCARCE * scarce)
                if best is None or score < best["score"]:
                    best = {"shipment": j["sid"], "dock": d["id"], "start": _s(st),
                            "end": _s(st + timedelta(minutes=j["unload"])), "score": round(score, 1),
                            "wait_min": round(max(wait, 0)), "late_min": round(late),
                            "changed": bool(changed), "arrival_basis": j["basis"],
                            "promised": j["appt"]["start_at"] + " " + j["appt"]["dock_id"]
                            if j["appt"] else None}
        if best is None:
            skipped.append({"shipment": j["sid"], "reason": "no compatible active dock"})
            continue
        if horizon_end and best["start"] > horizon_end:
            skipped.append({"shipment": j["sid"], "reason": "beyond horizon"})
            continue
        free_at[best["dock"]] = _dt(best["end"])
        promised.pop(j["sid"], None)          # its old promise no longer reserves anything
        plan.append(best)

    return {"facility": facility_id, "now": now, "fixed": fixed, "plan": plan, "skipped": skipped,
            "totals": {"wait_min": sum(p["wait_min"] for p in plan),
                       "late_min": sum(p["late_min"] for p in plan),
                       "changes": sum(p["changed"] for p in plan)}}


def format_schedule(result: dict) -> str:
    out = [f"Proposed schedule for {result['facility']} at {result['now']}"]
    for f in result["fixed"]:
        out.append(f"  FIXED  {f['shipment']} on {f['dock']} {f['start'][-5:]}-{f['end'][-5:]} (unloading now)")
    for p in result["plan"]:
        flag = "CHANGE" if p["changed"] else "KEEP  "
        out.append(f"  {flag} {p['shipment']} on {p['dock']} {p['start'][-5:]}-{p['end'][-5:]}  "
                   f"wait {p['wait_min']}m late {p['late_min']}m  (was {p['promised'] or 'no booking'}, "
                   f"arrival from {p['arrival_basis']})")
    for s in result["skipped"]:
        out.append(f"  SKIP   {s['shipment']}: {s['reason']}")
    tt = result["totals"]
    out.append(f"  Totals: wait {tt['wait_min']} min, late {tt['late_min']} min, changes {tt['changes']}")
    return "\n".join(out)
