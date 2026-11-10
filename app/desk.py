"""The dispatcher's desk: every action on a driver conversation, shared by the
workflow (service.py) and the agent (agent_mode.py), so both obey the same rules
and leave the same trail in the supplied tables:

  chat_threads / chat_messages   the conversation, with parsed intent, extracted ETA,
                                 duplicate flag and requires_human_review
  eta_updates                    DRIVER_DECLARED ETAs with confidence and reason
  driver_exceptions              one structured exception per thread, with dedupe_key
  appointments                   CONFIRMED / CANCELLED history with replaced_appointment_id
  appointment_slots              extension slots BLOCKED for long unloads
  operational_messages           driver confirmation + warehouse notice; escalations
"""
import json

from . import timeutil as tu
from . import tools

SEVERITY = {"CRITICAL": "CRITICAL", "HIGH": "HIGH", "NORMAL": "MEDIUM", "LOW": "LOW"}
THREAD_INTENT = {"report_delay": "REPORT_DELAY", "ask_slots": "ASK_SLOT_OPTIONS", "ask_status": "CHECK_STATUS",
                 "early_arrival": "EARLY_ARRIVAL", "choose_option": "REPORT_DELAY", "confirm": "REPORT_DELAY"}
EMPTY_FACTS = {"times": [], "docks": [], "options": [], "truck_regs": []}


def dock_label(code: str, dock_type: str) -> str:
    return f"Dock {code}" + ({"REEFER": " (reefer)", "HEAVY": " (heavy)"}.get(dock_type, ""))


class Desk:
    def __init__(self, conn, conversation_id: str, now: str):
        self.conn = conn
        self.now = tu.norm(now)
        self.thread_id = f"CHAT-{conversation_id}"[:60]
        self.driver = None
        self.state = {"shipment_id": None, "state": "NEW", "offered": [], "pending_text": None}
        self.facts = dict(EMPTY_FACTS)       # what any reply must contain (phrasing check)
        self.review = False                  # requires_human_review on the agent message

    # ------------------------------------------------------------ thread + messages
    def open(self, driver: dict, intent: str = "unknown") -> None:
        self.driver = driver
        row = self.conn.execute("SELECT 1 FROM chat_threads WHERE thread_id=?", (self.thread_id,)).fetchone()
        if not row:
            self.conn.execute("INSERT INTO chat_threads (thread_id, driver_id, shipment_id, opened_at, thread_status, "
                              "thread_intent) VALUES (?,?,NULL,?, 'OPEN', ?)",
                              (self.thread_id, driver["driver_id"], self.now, THREAD_INTENT.get(intent, "UNKNOWN")))
        st = self.conn.execute("SELECT * FROM agent_thread_state WHERE thread_id=?", (self.thread_id,)).fetchone()
        if st:
            self.state = {"shipment_id": st["shipment_id"], "state": st["state"],
                          "offered": json.loads(st["offered_json"]), "pending_text": st["pending_text"]}

    def save(self) -> None:
        self.conn.execute("""INSERT INTO agent_thread_state (thread_id, shipment_id, state, offered_json, pending_text, updated_at)
            VALUES (?,?,?,?,?,?) ON CONFLICT(thread_id) DO UPDATE SET shipment_id=excluded.shipment_id,
            state=excluded.state, offered_json=excluded.offered_json, pending_text=excluded.pending_text,
            updated_at=excluded.updated_at""",
                          (self.thread_id, self.state["shipment_id"], self.state["state"],
                           json.dumps(self.state["offered"]), self.state["pending_text"], self.now))

    def set_thread(self, status: str | None = None, shipment_id: str | None = None, intent: str | None = None,
                   closed: bool = False) -> None:
        if status:
            self.conn.execute("UPDATE chat_threads SET thread_status=? WHERE thread_id=?", (status, self.thread_id))
        if shipment_id:
            self.conn.execute("UPDATE chat_threads SET shipment_id=? WHERE thread_id=?", (shipment_id, self.thread_id))
        if intent:
            self.conn.execute("UPDATE chat_threads SET thread_intent=? WHERE thread_id=? AND thread_intent='UNKNOWN'",
                              (THREAD_INTENT.get(intent, "UNKNOWN"), self.thread_id))
        if closed:
            self.conn.execute("UPDATE chat_threads SET closed_at=? WHERE thread_id=?", (self.now, self.thread_id))

    def log(self, sender: str, text: str, intent: str | None = None, eta: str | None = None,
            external_id: str | None = None, duplicate: bool = False, review: bool = False) -> None:
        self.conn.execute("""INSERT INTO chat_messages (chat_message_id, thread_id, sender_type, sender_reference,
            message_text, message_ts, external_message_id, is_duplicate, parsed_intent, extracted_eta_ts, requires_human_review)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                          (tools.new_id("MSG"), self.thread_id, sender,
                           self.driver["driver_id"] if sender == "DRIVER" else "agent", text, self.now, external_id,
                           int(duplicate), intent, eta, int(review)))

    def find_duplicate(self, text: str, external_id: str | None) -> dict | None:
        """Messaging retries (THR001 / THR009): same external id, or the same text from the same
        driver within 3 minutes."""
        if external_id:
            r = self.conn.execute("SELECT message_ts FROM chat_messages WHERE external_message_id=?", (external_id,)).fetchone()
            if r:
                return dict(r)
        r = self.conn.execute("""SELECT m.message_ts FROM chat_messages m JOIN chat_threads t ON t.thread_id = m.thread_id
            WHERE t.driver_id=? AND t.thread_id LIKE 'CHAT-%' AND m.sender_type='DRIVER' AND m.is_duplicate=0
              AND lower(trim(m.message_text))=lower(trim(?))
            ORDER BY m.message_ts DESC LIMIT 1""", (self.driver["driver_id"], text)).fetchone()
        if r and 0 <= tu.minutes(r["message_ts"], self.now) <= 3:
            return dict(r)
        return None

    def history(self, limit: int = 10) -> list[dict]:
        rows = self.conn.execute("SELECT sender_type, message_text FROM chat_messages WHERE thread_id=? AND is_duplicate=0 "
                                 "ORDER BY message_ts DESC, rowid DESC LIMIT ?", (self.thread_id, limit)).fetchall()
        return [{"role": "user" if r["sender_type"] == "DRIVER" else "assistant", "content": r["message_text"]}
                for r in reversed(rows)]

    # ------------------------------------------------------------ exceptions + messages
    def exception(self, sh: dict | None, status: str, *, kind: str = "DELAY", delay: int | None = None,
                  eta: str | None = None, earliest: str | None = None, latest: str | None = None,
                  description: str = "") -> None:
        """One driver_exceptions row per thread, updated as it progresses."""
        sid = sh["shipment_id"] if sh else None
        row = self.conn.execute("SELECT exception_id FROM driver_exceptions WHERE thread_id=?", (self.thread_id,)).fetchone()
        if row:
            self.conn.execute("""UPDATE driver_exceptions SET shipment_id=COALESCE(?, shipment_id), exception_status=?,
                reported_delay_min=COALESCE(?, reported_delay_min), declared_eta_ts=COALESCE(?, declared_eta_ts),
                earliest_acceptable_ts=COALESCE(?, earliest_acceptable_ts), latest_acceptable_ts=COALESCE(?, latest_acceptable_ts),
                description=CASE WHEN ?<>'' THEN ? ELSE description END WHERE exception_id=?""",
                              (sid, status, delay, eta, earliest, latest, description, description, row["exception_id"]))
            return
        sev = SEVERITY.get(sh["priority_code"], "MEDIUM") if sh else "MEDIUM"
        key = f"{self.driver['driver_id']}-{sid or 'UNKNOWN'}-{self.now[:10].replace('-', '')}-{self.now[11:16].replace(':', '')}"
        self.conn.execute("""INSERT INTO driver_exceptions (exception_id, shipment_id, driver_id, thread_id, exception_type,
            reported_at, reported_delay_min, declared_eta_ts, earliest_acceptable_ts, latest_acceptable_ts, severity_code,
            exception_status, description, dedupe_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (tools.new_id("EXC"), sid, self.driver["driver_id"], self.thread_id, kind, self.now, delay,
                           eta, earliest, latest, sev, status, description or "Reported via driver chat.", key))

    # ------------------------------------------------------------ actions (return a result dict)
    def identify(self, text: str, is_delay_report: bool) -> tuple[dict | None, dict | None]:
        """(shipment, None) or (None, result to send back)."""
        if self.state["shipment_id"]:
            return tools.get_shipment(self.conn, self.state["shipment_id"]), None
        cands = tools.shipments_for_driver(self.conn, self.driver["driver_id"])
        if not cands:
            everything = tools.shipments_for_driver(self.conn, self.driver["driver_id"], open_only=False)
            cancelled = [s for s in everything if s["current_status"] == "CANCELLED"]
            if cancelled:
                c = cancelled[-1]
                self.set_thread("CLOSED", c["shipment_id"], "ask_status", closed=True)
                return None, self.result("cancelled_shipment",
                                         f"Your shipment {c['order_reference']} and its appointment were cancelled. "
                                         "Please contact dispatch before travelling.")
            return None, self.result("no_open_shipment", "I can't find an open shipment for you today. "
                                                         "Please call the SetuHaul control room.")
        sh, how = tools.resolve_shipment(cands, text, is_delay_report)
        if sh:
            self.state["shipment_id"] = sh["shipment_id"]
            self.set_thread(shipment_id=sh["shipment_id"])
            return sh, None
        self.state["pending_text"] = self.state["pending_text"] or text
        self.set_thread("WAITING_FOR_DRIVER")
        self.review = True
        lines = [f"{c['order_reference']} ({c['origin_city']} → {c['facility_name'].replace('SetuHaul ', '')}, "
                 f"truck {c['reg_no']})" for c in cands]
        self.facts = {**EMPTY_FACTS, "orders": [c["order_reference"] for c in cands],
                      "truck_regs": sorted({c["reg_no"] for c in cands})}
        return None, self.result("ask_shipment", "You have more than one shipment today:\n" + "\n".join(
            f"- {l}" for l in lines) + "\nWhich one is this about? Reply with the order number.", how=how)

    def record_delay(self, sh: dict, *, eta: str, confidence: str, reason: str | None, note: str,
                     delay: int | None, earliest: str | None, latest: str | None) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        tools.record_eta(self.conn, sh, eta, confidence, reason, note, self.now)
        kind = reason if reason in ("TRAFFIC", "BREAKDOWN", "WEATHER") else "DELAY"
        appt = tools.current_appointment(self.conn, sh["shipment_id"])
        desc = (f"New ETA {tu.hhmm(eta)}" + (f"; current slot {tu.hhmm(appt['slot_start_ts'])} at Dock {appt['dock_code']}"
                                           if appt else "; no current slot") + ".")
        self.exception(sh, "OPEN", kind=kind, delay=delay, eta=eta, earliest=earliest, latest=latest, description=desc)
        self.conn.execute("COMMIT")
        if confidence == "LOW":
            self.review = True

    def offer(self, sh: dict, start_from: str, latest_end: str | None, eta_for_reply: str | None,
              lead: str | None = None) -> dict:
        """Find options, hold up to 3, and say so; or escalate if nothing fits."""
        tools.release_holds(self.conn, self.thread_id)
        cands = tools.find_options(self.conn, sh["shipment_id"], start_from, latest_end, now=self.now, limit=6)
        offered = []
        for c in cands:
            h = tools.hold_option(self.conn, c, sh["shipment_id"], self.thread_id, self.now)
            if h["ok"]:
                offered.append({**c, "hold_group": h["hold_group"]})
            if len(offered) == 3:
                break
        head = lead or (f"Noted, new ETA {tu.hhmm(eta_for_reply)}." if eta_for_reply else "Noted.")
        if not offered:
            return self.escalate(sh, f"No compatible slot after {tu.hhmm(start_from)}"
                                     + (f" finishing by {tu.hhmm(latest_end)}" if latest_end else ""), head=head)
        self.state.update(state="OFFERED", offered=offered)
        self.set_thread("WAITING_FOR_DRIVER")
        self.exception(sh, "SLOT_OPTIONS_SHARED")
        lines = [f"{i}. {tu.hhmm(o['start'])} - {dock_label(o['dock_code'], o['dock_type'])}"
                 + (f" (until {tu.hhmm(o['unload_until'])})" if o["extension_slot_id"] else "")
                 for i, o in enumerate(offered, 1)]
        self.facts = {"times": sorted({tu.hhmm(o["start"]) for o in offered}),
                      "allowed_times": [tu.hhmm(eta_for_reply)] if eta_for_reply else [],
                      "docks": sorted({dock_label(o["dock_code"], o["dock_type"]) for o in offered}),
                      "options": [str(i) for i in range(1, len(offered) + 1)], "truck_regs": []}
        import re
        self.facts["allowed_times"] += [tu.hhmm(o["unload_until"]) for o in offered] + \
            re.findall(r"\b\d{1,2}:\d{2} [AP]M\b", head)
        return self.result("offered", f"{head} These slots are held for you for {tools.HOLD_MINUTES} minutes:\n"
                           + "\n".join(lines) + "\nReply with the option number.", options=offered,
                           eta=eta_for_reply)

    def confirm(self, sh: dict, index: int) -> dict:
        offered = self.state["offered"]
        if self.state["state"] != "OFFERED" or not 1 <= index <= len(offered):
            return self.result("clarify", f"Please reply with an option number (1-{len(offered)})." if offered
                               else "I haven't offered any slots yet. When will you reach?")
        chosen = offered[index - 1]
        res = tools.confirm(self.conn, chosen["hold_group"], self.thread_id, self.now)
        if not res["ok"]:
            self.state.update(state="NEW", offered=[])
            self.facts = dict(EMPTY_FACTS)
            return self.result("confirm_failed", "Sorry, that slot is no longer available (the hold expired). "
                                                 "Tell me your ETA again and I'll find new options.", reason=res["reason"])
        tools.release_holds(self.conn, self.thread_id, keep_group=chosen["hold_group"])
        label = dock_label(chosen["dock_code"], chosen["dock_type"])
        when = tu.hhmm(chosen["start"])
        self.conn.execute("BEGIN IMMEDIATE")
        tools.record_message(self.conn, sh["shipment_id"], res["appointment_id"], "WHATSAPP", sh["driver_phone"], None,
                             f"Confirmed: {when} at {label}, {sh['facility_name']}.", self.now)
        tools.record_message(self.conn, sh["shipment_id"], res["appointment_id"], "INTERNAL",
                             tools.contact_email(self.conn, sh["destination_facility_id"], ("WAREHOUSE_COORDINATOR",)),
                             f"Rebooked {sh['shipment_id']} via driver chat",
                             f"{sh['shipment_id']} ({sh['order_reference']}) now {when} at Dock {chosen['dock_code']}"
                             + (f", extension slot {res['extension_slot_id']} blocked" if res["extension_slot_id"] else "")
                             + f". Appointment {res['appointment_id']}.", self.now)
        self.exception(sh, "RESOLVED")
        self.conn.execute("COMMIT")
        self.set_thread("RESOLVED")
        self.state.update(state="CONFIRMED", offered=[chosen])
        self.facts = {**EMPTY_FACTS, "times": [when], "docks": [label]}
        return self.result("confirmed", f"Confirmed: {when} at {label}. Please report at the gate on arrival.",
                           appointment_id=res["appointment_id"], slot_id=chosen["slot_id"],
                           extension_slot_id=res["extension_slot_id"])

    def status(self, sh: dict) -> dict:
        appt = tools.current_appointment(self.conn, sh["shipment_id"])
        if sh.get("queue_state") == "IN_DOCK":
            dock = self.conn.execute("SELECT dock_code, dock_type FROM docks WHERE dock_id=?", (sh["actual_dock_id"],)).fetchone()
            label = dock_label(dock["dock_code"], dock["dock_type"])
            self.facts = {**EMPTY_FACTS, "times": [tu.hhmm(sh["unload_start_ts"])], "docks": [label]}
            return self.result("status", f"You're unloading at {label} since {tu.hhmm(sh['unload_start_ts'])}.")
        if not appt:
            self.facts = dict(EMPTY_FACTS)
            return self.result("status", "You have no slot booked right now. When will you reach?")
        label = dock_label(appt["dock_code"], appt["dock_type"])
        self.facts = {**EMPTY_FACTS, "times": [tu.hhmm(appt["slot_start_ts"])], "docks": [label]}
        return self.result("status", f"Your slot is {tu.hhmm(appt['slot_start_ts'])} at {label}.")

    def handle_on_site(self, sh: dict, intent: str) -> dict:
        """The gate has the truck: plan from now, never from a driver-declared ETA."""
        if sh.get("queue_state") == "IN_DOCK":
            return self.status(sh)
        fac = tools.facility(self.conn, sh["destination_facility_id"])
        appt = tools.current_appointment(self.conn, sh["shipment_id"])
        gate = tu.hhmm(sh["gate_in_ts"])
        if appt:
            broken = tools.dock_event_during(self.conn, appt["dock_id"], appt["slot_start_ts"], appt["slot_end_ts"])
            missed = self.now > tu.add(appt["slot_start_ts"], fac["no_show_grace_min"])
            label = dock_label(appt["dock_code"], appt["dock_type"])
            if not broken and not missed:
                early = self.now < appt["slot_start_ts"]
                if early:   # an earlier free slot may be used, but nobody is displaced (SHP1003)
                    sooner = tools.find_options(self.conn, sh["shipment_id"], self.now, None, now=self.now, limit=3)
                    sooner = [o for o in sooner if o["start"] < appt["slot_start_ts"]]
                    if sooner:
                        return self.offer(sh, self.now, tu.add(appt["slot_start_ts"], sh["expected_unload_min"]), None,
                                          lead=f"You're checked in since {gate}. An earlier slot is free;"
                                               f" your {tu.hhmm(appt['slot_start_ts'])} slot stays unless you pick one.")
                self.facts = {**EMPTY_FACTS, "times": [gate, tu.hhmm(appt["slot_start_ts"])], "docks": [label]}
                note = (" Arriving early doesn't move you ahead of trucks already scheduled; no earlier dock is free."
                        if early else "")
                return self.result("status", f"You're checked in since {gate}. Your slot is "
                                             f"{tu.hhmm(appt['slot_start_ts'])} at {label}.{note} Wait for the call to dock.")
            why = (f"Dock {appt['dock_code']} is unavailable ({broken['reason'].lower()})" if broken
                   else f"Your {tu.hhmm(appt['slot_start_ts'])} slot has passed")
        else:
            why = "You have no slot booked"
        self.exception(sh, "OPEN", kind="DOCK_UNAVAILABLE" if appt and broken else "DELAY",
                       eta=sh["gate_in_ts"], description=f"{why}; truck in yard since {gate}.")
        return self.offer(sh, self.now, None, None, lead=f"You're in the yard since {gate}. {why}.")

    def escalate(self, sh: dict, reason: str, head: str | None = None) -> dict:
        self.review = True
        self.state.update(state="ESCALATED", offered=[])
        self.conn.execute("BEGIN IMMEDIATE")
        to = tools.contact_email(self.conn, sh["destination_facility_id"])
        tools.record_message(self.conn, sh["shipment_id"], None, "INTERNAL", to,
                             f"Driver exception needs a decision: {sh['shipment_id']}",
                             f"{sh['shipment_id']} ({sh['order_reference']}, {sh['priority_code']}): {reason}.", self.now)
        self.exception(sh, "ESCALATED", description=reason + ".")
        self.conn.execute("COMMIT")
        self.set_thread("ESCALATED")
        import re
        allowed = re.findall(r"\b\d{1,2}:\d{2} [AP]M\b", head or "")
        self.facts = {**EMPTY_FACTS, "allowed_times": allowed}
        return self.result("escalate", f"{head + ' ' if head else ''}No suitable dock slot is free for that. "
                                       "Our operations team will call you shortly.", reason=reason)

    # ------------------------------------------------------------ result helper
    def result(self, action: str, reply: str, **extra) -> dict:
        r = {"action": action, "reply": reply, **extra}
        if "options" in r:
            r["options"] = [{k: o[k] for k in ("slot_id", "dock_id", "dock_code", "dock_type", "start", "end",
                                                "unload_until", "extension_slot_id")} for o in r["options"]]
        return r
