"""Optional scheduling tool: one facility, every relevant truck, one proposal.

Reads structured data only, never chat text (guide section 5), and returns a
proposed slot per truck. It does not book; an operator or the confirm step applies it.

Inputs, as the guide lists them:
  trucks in a dock (fixed: busy until unload start + expected minutes, overrun included)
  trucks in the yard (early / late / dock unavailable): available from gate-in
  trucks in transit: available from their ETA as known at `now`
  priority, unload minutes, weight, dock type; current appointments (the promise);
  open/blocked slots; breakdown and maintenance events; facility rules (last start, heavy kg)

Method: a transparent greedy dispatch rule. Trucks are taken by availability in 60-minute
windows, higher priority first within a window; each goes to the feasible slot with the lowest score

  score = weight[priority] * (wait_min + 2 * late_min) + 30 * changed
          + 30 * takes_another_trucks_promised_slot + 5 * uses_a_bigger_dock_than_needed

In plain words: less waiting, fewer missed appointments, fewer changes to a schedule
drivers were already told, and keep the 25 t docks for loads that need them.
An early truck does not automatically win (SHP1003): taking an earlier slot that is
another truck's promise costs as much as a change.
"""
from . import timeutil as tu
from . import tools

PRIORITY_WEIGHT = {"LOW": 1, "NORMAL": 2, "HIGH": 3, "CRITICAL": 4}
W_CHANGE, W_INTRUDE, W_SCARCE, W_LATE = 30, 30, 5, 2


def propose_schedule(conn, facility_id: str, now: str = tu.SNAPSHOT_NOW) -> dict:
    now = tu.norm(now)
    fac = tools.facility(conn, facility_id)
    last_start = tu.on_date(now, fac["last_new_start"])
    docks = {r["dock_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM docks WHERE facility_id=? AND dock_status='ACTIVE'", (facility_id,))}
    slots = [dict(r) for r in conn.execute(
        "SELECT * FROM appointment_slots WHERE facility_id=? AND slot_status='OPEN' ORDER BY slot_start_ts", (facility_id,))]
    events = [dict(r) for r in conn.execute(f"""SELECT e.* FROM dock_status_events e JOIN docks d ON d.dock_id=e.dock_id
        WHERE d.facility_id=? AND e.event_type IN {tools.BLOCKING_EVENTS}""", (facility_id,))]
    busy = {d: [] for d in docks}           # dock_id -> [(start, end)] taken in this plan

    def overlaps(a1, a2, b1, b2):
        return a1 < b2 and b1 < a2

    fixed, jobs, skipped = [], [], []
    rows = conn.execute(tools.SHIPMENT_SQL + " WHERE s.destination_facility_id=? AND s.current_status "
                        "NOT IN ('COMPLETED','CANCELLED') ORDER BY s.shipment_id", (facility_id,)).fetchall()
    for sh in map(dict, rows):
        if sh["queue_state"] == "IN_DOCK" and sh["unload_start_ts"] and sh["actual_dock_id"] in docks:
            end = max(tu.add(sh["unload_start_ts"], sh["expected_unload_min"]), now)
            busy[sh["actual_dock_id"]].append((sh["unload_start_ts"], end))
            fixed.append({"shipment": sh["shipment_id"], "dock": docks[sh["actual_dock_id"]]["dock_code"],
                          "start": sh["unload_start_ts"], "end": end, "priority": sh["priority_code"]})
            continue
        if not sh["actual_departure_ts"] and not tools.on_site(sh) and sh["planned_departure_ts"] < tu.add(now, -60):
            skipped.append({"shipment": sh["shipment_id"], "reason": "has not departed (planned "
                                                                     f"{tu.hhmm(sh['planned_departure_ts'])})"})
            continue
        if tools.on_site(sh):
            arrival, basis = sh["gate_in_ts"], "gate-in"
        else:
            eta = tools.current_eta(conn, sh["shipment_id"], now)
            arrival, basis = eta["eta"], f"ETA ({eta['source'].lower().replace('_', ' ')}, {eta['confidence'].lower()})"
        appt = tools.current_appointment(conn, sh["shipment_id"])
        jobs.append({"sh": sh, "arrival": arrival, "release": max(arrival, now), "basis": basis, "appt": appt,
                     "weight": PRIORITY_WEIGHT.get(sh["priority_code"], 2)})

    # A promise only protects a slot its truck can still reach (within the no-show grace).
    # SHP1006's 10:00 slot does not block anyone once its ETA is 11:20.
    promised = {j["sh"]["shipment_id"]: (j["appt"]["dock_id"], j["appt"]["slot_start_ts"], j["appt"]["slot_end_ts"])
                for j in jobs if j["appt"] and j["release"] <= tu.add(j["appt"]["slot_start_ts"], fac["no_show_grace_min"])}

    def compatible(sh, d):
        req = sh["required_dock_type"]
        if fac["heavy_kg"] and sh["load_weight_kg"] > fac["heavy_kg"]:
            req = "HEAVY"
        if sh["temperature_control_required"] and not d["supports_refrigerated"]:
            return False
        if req != "ANY" and d["dock_type"] != req:
            return False
        return sh["load_weight_kg"] <= d["max_vehicle_weight_kg"]

    def free(sl):
        return (sl["dock_id"] in docks
                and not any(overlaps(sl["slot_start_ts"], sl["slot_end_ts"], e["event_start_ts"], e["event_end_ts"] or "9999")
                            for e in events if e["dock_id"] == sl["dock_id"])
                and not any(overlaps(sl["slot_start_ts"], sl["slot_end_ts"], a, b) for a, b in busy[sl["dock_id"]]))

    def option_for(j, sl):
        """(start, end_of_unload, extension_slot) if this truck can use this slot, else None.
        A truck may still use its own slot after it started if it is within the no-show grace,
        but the unload must then still finish inside the slot (plus extension)."""
        sh = j["sh"]
        d = docks.get(sl["dock_id"])
        if not d or not compatible(sh, d) or sl["slot_start_ts"] > last_start or not free(sl):
            return None
        own_in_grace = (j["appt"] and j["appt"]["slot_id"] == sl["slot_id"]
                        and j["release"] <= tu.add(sl["slot_start_ts"], fac["no_show_grace_min"]))
        if sl["slot_start_ts"] < j["release"] and not own_in_grace:
            return None
        start = max(sl["slot_start_ts"], j["release"])
        finish = tu.add(start, sh["expected_unload_min"])
        ext = None
        if finish > sl["slot_end_ts"]:
            ext = next((x for x in slots if x["dock_id"] == sl["dock_id"] and x["slot_start_ts"] == sl["slot_end_ts"]
                        and free(x)), None)
            if not ext or finish > ext["slot_end_ts"]:
                return None
        return start, finish, ext

    plan = []
    # Order: by availability in 60-minute windows, higher priority first within a window, so a
    # CRITICAL truck arriving at 10:50 picks before a LOW truck that has waited since 08:20
    # (priority conflict, SHP1009 / SHP1003), but nobody picks a slot before they can use it.
    def order(j):
        return (int(tu.minutes(now, j["release"]) // 60), -j["weight"], j["release"], j["sh"]["shipment_id"])

    for j in sorted(jobs, key=order):
        sh, best = j["sh"], None
        for sl in slots:
            opt = option_for(j, sl)
            if not opt:
                continue
            start, finish, ext = opt
            d = docks[sl["dock_id"]]
            wait = max(0.0, tu.minutes(j["arrival"], start))
            late = max(0.0, tu.minutes(j["appt"]["slot_start_ts"], start)) if j["appt"] else 0.0
            changed = not (j["appt"] and j["appt"]["slot_id"] == sl["slot_id"])
            intrudes = any(dk == sl["dock_id"] and overlaps(start, finish, a, b)
                           for sid, (dk, a, b) in promised.items() if sid != sh["shipment_id"])
            scarce = d["max_vehicle_weight_kg"] - sh["load_weight_kg"] > 5000 or (
                d["dock_type"] != "STANDARD" and sh["required_dock_type"] in ("STANDARD", "ANY"))
            score = j["weight"] * (wait + W_LATE * late) + W_CHANGE * changed + W_INTRUDE * intrudes + W_SCARCE * scarce
            if best is None or score < best["score"]:
                best = {"score": round(score, 1), "slot": sl, "start": start, "end": finish, "ext": ext,
                        "wait": wait, "late": late, "changed": changed, "dock": d}
        if best is None:
            skipped.append({"shipment": sh["shipment_id"], "reason": "no feasible compatible slot (needs operations)"})
            continue
        busy[best["slot"]["dock_id"]].append((best["start"], best["end"]))
        promised.pop(sh["shipment_id"], None)       # its old promise no longer reserves anything
        plan.append({
            "shipment": sh["shipment_id"], "priority": sh["priority_code"], "slot_id": best["slot"]["slot_id"],
            "extension_slot_id": best["ext"]["slot_id"] if best["ext"] else None,
            "dock": best["dock"]["dock_code"], "start": best["start"], "end": best["end"],
            "wait_min": round(best["wait"]), "late_min": round(best["late"]), "changed": best["changed"],
            "arrival_basis": j["basis"], "score": best["score"],
            "promised": (f"{tu.hhmm(j['appt']['slot_start_ts'])} {j['appt']['dock_code']}" if j["appt"] else None)})

    return {"facility": facility_id, "now": now, "fixed": fixed, "plan": plan, "skipped": skipped,
            "totals": {"wait_min": sum(p["wait_min"] for p in plan), "late_min": sum(p["late_min"] for p in plan),
                       "changes": sum(p["changed"] for p in plan), "unplaced": len(skipped)}}


def format_schedule(r: dict) -> str:
    out = [f"Proposed schedule for {r['facility']} at {tu.hhmm(r['now'])}"]
    for f in r["fixed"]:
        out.append(f"  FIXED  {f['shipment']} {f['dock']} {tu.hhmm(f['start'])}-{tu.hhmm(f['end'])} (unloading now)")
    for p in r["plan"]:
        flag = "CHANGE" if p["changed"] else "KEEP  "
        ext = f" +{p['extension_slot_id']}" if p["extension_slot_id"] else ""
        out.append(f"  {flag} {p['shipment']} [{p['priority']}] {p['dock']} {tu.hhmm(p['start'])}-{tu.hhmm(p['end'])}{ext}"
                   f"  wait {p['wait_min']}m late {p['late_min']}m  (was {p['promised'] or 'no booking'}; {p['arrival_basis']})")
    for s in r["skipped"]:
        out.append(f"  SKIP   {s['shipment']}: {s['reason']}")
    t = r["totals"]
    out.append(f"  Totals: wait {t['wait_min']} min, late {t['late_min']} min, changes {t['changes']}, unplaced {t['unplaced']}")
    return "\n".join(out)
