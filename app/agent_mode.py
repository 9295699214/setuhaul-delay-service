"""Agent mode: the LLM decides which tool to call; code guards every tool.

Same interface as ConversationService.handle(), so the evaluation can run
both modes on the same scenarios.

What the LLM may do: choose tools and their arguments, and write the reply.
What code enforces, whatever the LLM asks:
  * identity comes from the phone; a truck number must match one of its shipments
  * a truck the gate logged in gets no new slot
  * ETA arithmetic (planned/ops-override ETA + delay) is done in code
  * offer_slots runs the full compatibility query and holds slots atomically
  * confirm_option only accepts an option number this conversation was offered,
    and the booking is the same atomic confirm() the workflow uses
  * at most MAX_STEPS tool calls per message, then escalate
  * the reply must contain the exact times/docks the tools returned, else a
    code template is sent
"""
import json
from datetime import datetime

from . import tools
from .agent import get_llm
from .phrasing import check

FMT = tools.FMT
MAX_STEPS = 6


def _hhmm(ts: str) -> str:
    return datetime.strptime(ts, FMT).strftime("%I:%M %p").lstrip("0")


AGENT_PROMPT = """You are SetuHaul Logistics' dock-booking assistant, chatting on WhatsApp with a truck
driver who may be running late. Drivers write in English, Hindi or Hinglish; reply in the same
language and script, briefly (max 3 short lines plus any option list), no emojis.

Use the tools to act. Rules:
- Never state an ETA, slot time or dock that a tool did not return. Copy times (e.g. "12:00 PM")
  and dock names exactly as the tool gives them.
- If several trucks share this phone, ask the driver for the truck number, then call identify_truck
  with the number they wrote. Never pick a truck yourself.
- When the driver says how late they are or when they will arrive, call report_delay
  (use delay_minutes for "X hours/min late"; new_eta only for a stated clock time), then call
  offer_slots and list the returned options numbered exactly as given.
- Pass earliest / latest_end to offer_slots ONLY if the driver stated such a limit. Never invent one.
- "1 ghanta late", "do ghante", "45 min der" are delays: call report_delay right away.
- If the driver only says they are late without how late, ask how late. Do not guess.
  Never ask for the date; the backend knows it.
- Only call confirm_option after options were offered and the driver picked one.
- If no slots are available, tell the driver operations will call (escalate_to_ops).
- Don't ask for things the tools already know (truck type, load, dock compatibility)."""

TOOLS = [
    {"type": "function", "function": {
        "name": "identify_truck",
        "description": "Pick which shipment the driver is on when several trucks share this phone.",
        "parameters": {"type": "object", "properties": {
            "truck_number": {"type": "string", "description": "Vehicle registration, e.g. GJ01AB1019"}},
            "required": ["truck_number"]}}},
    {"type": "function", "function": {
        "name": "report_delay",
        "description": "Record the driver's new arrival estimate. The system computes the ETA.",
        "parameters": {"type": "object", "properties": {
            "delay_minutes": {"type": "integer", "description": "How late, in minutes"},
            "new_eta": {"type": "string", "description": "Stated arrival clock time 'YYYY-MM-DD HH:MM'"}}}}},
    {"type": "function", "function": {
        "name": "offer_slots",
        "description": "Find compatible free dock slots after the recorded ETA and hold up to 3 for 2 minutes.",
        "parameters": {"type": "object", "properties": {
            "earliest": {"type": "string", "description": "Driver accepts slots only after 'YYYY-MM-DD HH:MM'"},
            "latest_end": {"type": "string", "description": "Driver must finish by 'YYYY-MM-DD HH:MM'"}}}}},
    {"type": "function", "function": {
        "name": "confirm_option",
        "description": "Book the option the driver picked from the list offered in this conversation.",
        "parameters": {"type": "object", "properties": {
            "option_number": {"type": "integer"}}, "required": ["option_number"]}}},
    {"type": "function", "function": {
        "name": "get_booking_status",
        "description": "The driver's current booked slot, if any.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "escalate_to_ops",
        "description": "Hand over to the operations team (no suitable slot, or a request you cannot handle).",
        "parameters": {"type": "object", "properties": {
            "reason": {"type": "string"}}, "required": ["reason"]}}},
]


class AgentService:
    mode = "agent"

    def __init__(self, conn, llm="auto"):
        self.conn = conn
        self.llm = get_llm() if llm == "auto" else llm
        if self.llm is None or not hasattr(self.llm, "chat_tools"):
            raise RuntimeError("agent mode needs OPENROUTER_API_KEY (tool calling)")

    # -- state --------------------------------------------------------------
    def _load(self, cid):
        row = self.conn.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
        return dict(row) if row else {"state": "new", "offered_json": "[]", "shipment_id": None,
                                       "pending_text": None}

    def _save(self, st, now):
        self.conn.execute(
            "INSERT INTO conversations (id,phone,shipment_id,state,offered_json,pending_text,updated_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET shipment_id=excluded.shipment_id, "
            "state=excluded.state, offered_json=excluded.offered_json, updated_at=excluded.updated_at",
            (st["cid"], st["phone"], st["sid"], st["state"], json.dumps(st["offered"]), None, now))

    def _log(self, cid, direction, body, now, action=None):
        self.conn.execute("INSERT INTO messages (conversation_id,direction,body,at,action) VALUES (?,?,?,?,?)",
                          (cid, direction, body, now, action))

    def _history(self, cid, limit=10):
        rows = self.conn.execute("SELECT direction, body FROM messages WHERE conversation_id=? "
                                 "ORDER BY id DESC LIMIT ?", (cid, limit)).fetchall()
        return [{"role": "user" if r["direction"] == "in" else "assistant", "content": r["body"]}
                for r in reversed(rows)]

    # -- tools (guards live here) -------------------------------------------
    def _need_shipment(self, st):
        if st["sid"] is None:
            return {"error": "Truck not identified yet. Ask the driver for their truck number."}
        return None

    def t_identify_truck(self, st, truck_number=""):
        typed = tools.norm_reg(truck_number)
        said = tools.norm_reg(" ".join(st["driver_texts"]))
        if not typed or typed not in said:
            return {"error": "The driver has not written this truck number. Ask them for it; do not guess.",
                    "trucks_on_this_phone": [c["reg_no"] for c in st["candidates"]]}
        match = [c for c in st["candidates"] if c["reg_no"] and tools.norm_reg(c["reg_no"]) == typed]
        if len(match) != 1:
            return {"error": "No truck with that number on this phone.",
                    "trucks_on_this_phone": [c["reg_no"] for c in st["candidates"]]}
        st["sid"] = match[0]["id"]
        return {"status": "identified", "truck": match[0]["reg_no"]}

    def _gate_guard(self, st):
        """Backend beats driver: a truck the gate logged in gets no new slot."""
        gate = tools.gate_status(self.conn, st["sid"])
        if gate and gate["event"] == "gate_in":
            st["facts"] = {"times": [_hhmm(gate["at"])], "docks": [], "options": [], "truck_regs": []}
            st["template"] = (f"Our gate shows your truck checked in at {_hhmm(gate['at'])}. "
                              "Please go to the dock supervisor; no new slot is needed.")
            st["state"], st["offered"] = "new", []
            return {"status": "already_arrived", "gate_in_at": _hhmm(gate["at"]),
                    "note": "Truck is already inside. Do not offer slots."}
        return None

    def t_report_delay(self, st, delay_minutes=None, new_eta=None):
        if err := self._need_shipment(st):
            return err
        if blocked := self._gate_guard(st):
            return blocked
        if delay_minutes is not None:
            new_eta = None          # relative delay wins: the backend adds it to the planned ETA
        if new_eta:
            try:
                datetime.strptime(new_eta, FMT)
            except ValueError:
                return {"error": "new_eta must be 'YYYY-MM-DD HH:MM'"}
            eta = new_eta
        elif delay_minutes is not None and 0 <= int(delay_minutes) <= 24 * 60:  # noqa: SIM114
            base = tools.current_eta(self.conn, st["sid"])
            eta = tools._add(max(base["eta"] if base else st["now"], st["now"]), int(delay_minutes))
        else:
            return {"error": "Give delay_minutes (0-1440) or new_eta."}
        self.conn.execute("BEGIN IMMEDIATE")
        tools.record_eta(self.conn, st["sid"], eta, "driver", st["now"])
        self.conn.execute("COMMIT")
        st["eta"] = eta
        return {"status": "eta_recorded", "eta": _hhmm(eta)}

    def t_offer_slots(self, st, earliest=None, latest_end=None):
        if err := self._need_shipment(st):
            return err
        if blocked := self._gate_guard(st):
            return blocked
        if not st.get("eta"):
            base = tools.current_eta(self.conn, st["sid"])
            if not base:
                return {"error": "ETA unknown. Ask how late the driver is, then call report_delay."}
            st["eta"] = max(base["eta"], st["now"])
        for v in (earliest, latest_end):
            if v:
                try:
                    datetime.strptime(v, FMT)
                except ValueError:
                    return {"error": "times must be 'YYYY-MM-DD HH:MM'"}
        tools.release_holds(self.conn, st["cid"])
        start = max(st["eta"], earliest or st["eta"])
        cands = tools.find_options(self.conn, st["sid"], start, latest_end, now=st["now"], limit=6)
        offered = []
        for c in cands:
            h = tools.hold_slot(self.conn, c["slot_id"], st["sid"], st["cid"], st["now"])
            if h["ok"]:
                offered.append({**c, "hold_id": h["hold_id"]})
            if len(offered) == 3:
                break
        st["offered"] = offered
        if not offered:
            st["state"] = "new"
            st["facts"] = {"times": [], "allowed_times": [_hhmm(st["eta"])], "docks": [], "options": [],
                           "truck_regs": []}
            st["template"] = (f"Noted, new ETA {_hhmm(st['eta'])}. No suitable dock slot is free in that "
                              "window. Our operations team will call you shortly.")
            return {"status": "no_slots", "eta": _hhmm(st["eta"]),
                    "note": "Tell the driver operations will call."}
        st["state"] = "offered"
        lines = [f"{i}. {_hhmm(o['start_at'])} - {o['dock_name']}" for i, o in enumerate(offered, 1)]
        st["template"] = (f"Noted, new ETA {_hhmm(st['eta'])}. These slots are held for you for "
                          f"{tools.HOLD_MINUTES} minutes:\n" + "\n".join(lines) + "\nReply with the option number.")
        st["facts"] = {"times": sorted({_hhmm(o["start_at"]) for o in offered}),
                       "allowed_times": [_hhmm(st["eta"])],
                       "docks": sorted({o["dock_name"] for o in offered}),
                       "options": [str(i) for i in range(1, len(offered) + 1)], "truck_regs": []}
        return {"status": "offered", "eta": _hhmm(st["eta"]), "held_minutes": tools.HOLD_MINUTES,
                "options": [{"option": i, "time": _hhmm(o["start_at"]), "dock": o["dock_name"]}
                            for i, o in enumerate(offered, 1)]}

    def t_confirm_option(self, st, option_number=0):
        if err := self._need_shipment(st):
            return err
        if st["state"] != "offered" or not st["offered"]:
            return {"error": "No options have been offered in this conversation. Offer slots first."}
        if not 1 <= int(option_number) <= len(st["offered"]):
            return {"error": f"Option must be 1-{len(st['offered'])}."}
        chosen = st["offered"][int(option_number) - 1]
        res = tools.confirm(self.conn, chosen["hold_id"], st["now"])
        if not res["ok"]:
            st["state"], st["offered"] = "new", []
            st["facts"] = {"times": [], "docks": [], "options": [], "truck_regs": []}
            st["template"] = ("Sorry, that slot is no longer available (the hold expired). "
                              "Tell me your ETA again and I'll find new options.")
            return {"status": "confirm_failed", "reason": res["reason"],
                    "note": "Hold expired or slot taken. Nothing was booked."}
        tools.release_holds(self.conn, st["cid"], keep_hold_id=chosen["hold_id"])
        tools.record_confirmation(self.conn, res["appointment_id"], st["sid"], chosen["slot_id"], st["now"])
        st["state"], st["offered"] = "confirmed", [chosen]
        st["booked"] = {"appointment_id": res["appointment_id"], "slot_id": chosen["slot_id"]}
        st["facts"] = {"times": [_hhmm(chosen["start_at"])], "docks": [chosen["dock_name"]],
                       "options": [], "truck_regs": []}
        st["template"] = (f"Confirmed: {_hhmm(chosen['start_at'])} at {chosen['dock_name']}. "
                          "Please report at the gate on arrival.")
        return {"status": "confirmed", "time": _hhmm(chosen["start_at"]), "dock": chosen["dock_name"]}

    def t_get_booking_status(self, st):
        if err := self._need_shipment(st):
            return err
        appt = tools.current_appointment(self.conn, st["sid"])
        if not appt:
            st["template"] = "You have no slot booked right now. When will you arrive?"
            st["facts"] = {"times": [], "docks": [], "options": [], "truck_regs": []}
            return {"status": "no_booking"}
        st["template"] = f"Your slot is {_hhmm(appt['start_at'])} at {appt['dock_name']}."
        st["facts"] = {"times": [_hhmm(appt["start_at"])], "docks": [appt["dock_name"]],
                       "options": [], "truck_regs": []}
        return {"status": "booked", "time": _hhmm(appt["start_at"]), "dock": appt["dock_name"]}

    def t_escalate_to_ops(self, st, reason=""):
        st["escalated"] = reason[:200]
        if "template" not in st:
            st["template"] = "Thanks. Our operations team will call you shortly."
            st["facts"] = {"times": [], "docks": [], "options": [], "truck_regs": []}
        return {"status": "escalated"}

    ACTION = {"already_arrived": "already_arrived", "offered": "offered", "no_slots": "escalate",
              "confirmed": "confirmed", "confirm_failed": "confirm_failed", "escalated": "escalate",
              "booked": "status", "no_booking": "status", "eta_recorded": "eta_recorded",
              "identified": "identified"}

    # -- main loop ----------------------------------------------------------
    def handle(self, conversation_id: str, phone: str, text: str, now: str) -> dict:
        cands = tools.find_shipments_by_phone(self.conn, phone)
        if not cands:
            reply = ("Sorry, I can't find an active shipment for this number. "
                     "Please call the SetuHaul control room.")
            self._log(conversation_id, "in", text, now)
            self._log(conversation_id, "out", reply, now, "no_shipment")
            return {"reply": reply, "action": "no_shipment", "mode": "agent", "tool_calls": []}

        conv = self._load(conversation_id)
        sid = conv["shipment_id"] or (cands[0]["id"] if len(cands) == 1 else None)
        st = {"cid": conversation_id, "phone": phone, "now": now, "sid": sid, "candidates": cands,
              "state": conv["state"], "offered": json.loads(conv["offered_json"]), "eta": None}
        history = self._history(conversation_id)
        st["driver_texts"] = [m["content"] for m in history if m["role"] == "user"] + [text]
        self._log(conversation_id, "in", text, now)

        def context():
            sh = tools.get_shipment(self.conn, st["sid"]) if st["sid"] else None
            appt = tools.current_appointment(self.conn, st["sid"]) if st["sid"] else None
            eta = tools.current_eta(self.conn, st["sid"]) if st["sid"] else None
            return json.dumps({
                "current_time": now,
                "trucks_on_this_phone": [c["reg_no"] for c in cands],
                "identified_truck": sh["reg_no"] if sh else None,
                "planned_eta": _hhmm(eta["eta"]) if eta else None,
                "current_booking": f"{_hhmm(appt['start_at'])} at {appt['dock_name']}" if appt else None,
                "conversation_state": st["state"],
                "options_offered": [f"{i}. {_hhmm(o['start_at'])} - {o['dock_name']}"
                                    for i, o in enumerate(st["offered"], 1)] if st["state"] == "offered" else [],
            })

        messages = [{"role": "system", "content": AGENT_PROMPT},
                    {"role": "system", "content": "Facts from the backend: " + context()},
                    *history, {"role": "user", "content": text}]
        trace, last_status, reply = [], None, None
        try:
            for _ in range(MAX_STEPS):
                msg = self.llm.chat_tools(messages, TOOLS)
                calls = msg.get("tool_calls") or []
                if not calls:
                    reply = (msg.get("content") or "").strip()
                    break
                messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
                for call in calls:
                    name = call["function"]["name"]
                    try:
                        args = json.loads(call["function"].get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    fn = getattr(self, "t_" + name, None)
                    try:
                        out = fn(st, **args) if fn else {"error": f"unknown tool {name}"}
                    except TypeError as e:
                        out = {"error": f"bad arguments: {e}"}
                    trace.append({"tool": name, "args": args, "result": out})
                    if "status" in out:
                        last_status = out["status"]
                    messages.append({"role": "tool", "tool_call_id": call.get("id", name),
                                     "content": json.dumps(out)})
            else:   # step limit hit
                last_status = "escalated"
                st["template"] = "Thanks. Our operations team will call you shortly."
                st["facts"] = {"times": [], "docks": [], "options": [], "truck_regs": []}
                reply = None
        except Exception as e:      # LLM down: never leave the driver without an answer
            trace.append({"tool": "llm_error", "args": {}, "result": {"error": type(e).__name__}})
            if "template" not in st:
                st["template"] = "Sorry, I couldn't process that. Our operations team will call you shortly."
                st["facts"] = {"times": [], "docks": [], "options": [], "truck_regs": []}
                last_status = last_status or "escalated"

        if last_status in self.ACTION:
            action = self.ACTION[last_status]
        elif st["sid"] is None and len(cands) > 1:
            action = "ask_vehicle"
        elif st["state"] == "offered":
            action = "clarify"
        else:
            action = "ask_eta"

        # Fact check: the LLM reply must carry exactly what the tools returned
        source, check_note = "llm", "ok"
        ctx = json.loads(context())
        ctx_times = [t for t in (ctx["planned_eta"], (ctx["current_booking"] or "").split(" at ")[0],
                                 _hhmm(st["eta"]) if st.get("eta") else None) if t]
        if "facts" in st:
            st["facts"].setdefault("allowed_times", [])
            st["facts"]["allowed_times"] = st["facts"]["allowed_times"] + ctx_times
        if "template" in st:
            ok, check_note = (check(reply, st["facts"]) if reply else (False, "no reply"))
            if not ok:
                reply, source = st["template"], "template"
        elif reply:
            ok, check_note = check(reply, {"times": [], "allowed_times": ctx_times, "docks": [],
                                           "options": [], "truck_regs": []})
            if not ok:   # LLM mentioned a time no tool returned
                reply, source = ("Thanks. Roughly how late are you, or what time will you reach?"
                                 if action == "ask_eta" else "Thanks. Our operations team will call you shortly."), "template"
        else:
            reply, source = "Thanks. Roughly how late are you, or what time will you reach?", "template"

        self._save(st, now)
        self._log(conversation_id, "out", reply, now, action)
        result = {"reply": reply, "action": action, "mode": "agent", "reply_source": source,
                  "phrase_check": check_note, "tool_calls": trace}
        if st.get("eta"):
            result["eta"] = st["eta"]
        if action == "offered":
            result["options"] = st["offered"]
        if action == "confirmed":
            result.update(st["booked"])
        return result
