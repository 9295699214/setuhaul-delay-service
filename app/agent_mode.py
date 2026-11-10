"""Agent mode: the LLM decides which tool to call; code guards every tool.

Same interface as ConversationService.handle(), and the same Desk underneath, so
both modes apply identical facility rules and write the same records.

The LLM may: choose tools and arguments, and write the reply.
Code enforces, whatever the LLM asks (design question 7):
  * identity comes from the phone; a shipment can only be chosen from the driver's
    own open shipments, by a reference the driver actually wrote
  * a truck the gate has logged in is planned from now, not from a declared ETA
  * ETA arithmetic (current ETA as of now + delay) is done in code; a stated delay
    beats any ETA the model computed itself
  * offer_slots runs the full compatibility query (dock type, weight, dock events,
    unload length, last start time) and holds slots atomically
  * confirm_option only accepts an option this thread was offered
  * at most MAX_STEPS tool calls per message, then escalate
  * the reply must contain exactly the times/docks the tools returned, else a template
"""
import json

from . import timeutil as tu
from . import tools
from .agent import get_llm
from .desk import EMPTY_FACTS, Desk, dock_label
from .phrasing import check

MAX_STEPS = 6

AGENT_PROMPT = """You are SetuHaul Logistics' dock-booking assistant, chatting on WhatsApp with a truck
driver. Drivers write in English, Hindi or Hinglish; reply in the same language and script,
briefly (max 3 short lines plus any option list), no emojis.

Use the tools to act. Rules:
- Never state an ETA, slot time, dock or order number that a tool did not return. Copy times
  (e.g. "12:00 PM") and dock names (e.g. "Dock D5 (reefer)") exactly as the tool gives them.
- If the backend facts say the shipment is not identified, ask which order it is about (list the
  order numbers from the facts), then call identify_shipment with what the driver wrote. Never pick one yourself.
- When the driver says how late they are or when they will arrive, call report_delay (delay_minutes
  for "X hours/min late"; new_eta only for a stated clock time), then offer_slots. List the options
  numbered exactly as returned.
- Pass earliest / latest_end ONLY if the driver stated such a limit. Never invent one.
- If the driver only asks what is possible ("what slots if I reach 12:45?"), call offer_slots with
  earliest set; do not call report_delay.
- If the driver is at the gate or in the yard, call offer_slots (the system plans from now) or
  get_booking_status. Do not ask for an ETA.
- If the driver only says they are late without how late, ask how late. Never ask for the date.
- Only call confirm_option after options were offered and the driver picked one.
- If offer_slots finds nothing, the system has already escalated: tell the driver operations will call.
- Don't ask for things the backend knows (truck type, load, weight, dock compatibility)."""

TOOLS = [
    {"type": "function", "function": {
        "name": "identify_shipment",
        "description": "Choose which of the driver's shipments the chat is about, by the order number or truck number the driver wrote.",
        "parameters": {"type": "object", "properties": {
            "reference": {"type": "string", "description": "e.g. ORD-260804-020, 020, or MH04HV4107"}},
            "required": ["reference"]}}},
    {"type": "function", "function": {
        "name": "report_delay",
        "description": "Record the driver's new arrival estimate. The system computes the ETA.",
        "parameters": {"type": "object", "properties": {
            "delay_minutes": {"type": "integer", "description": "How late, in minutes"},
            "new_eta": {"type": "string", "description": "Stated arrival clock time 'YYYY-MM-DD HH:MM'"},
            "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
            "reason": {"type": "string", "enum": ["TRAFFIC", "BREAKDOWN", "WEATHER", "LATE_DEPARTURE",
                                                   "LOADING_DELAY", "ROUTE_ISSUE", "OTHER"]}}}}},
    {"type": "function", "function": {
        "name": "offer_slots",
        "description": "Find compatible free dock slots (after the ETA, or from now if the truck is on site) and hold up to 3 for 2 minutes.",
        "parameters": {"type": "object", "properties": {
            "earliest": {"type": "string", "description": "Driver accepts slots only after 'YYYY-MM-DD HH:MM'"},
            "latest_end": {"type": "string", "description": "Driver must finish unloading by 'YYYY-MM-DD HH:MM'"}}}}},
    {"type": "function", "function": {
        "name": "confirm_option",
        "description": "Book the option the driver picked from the list offered in this conversation.",
        "parameters": {"type": "object", "properties": {"option_number": {"type": "integer"}},
                       "required": ["option_number"]}}},
    {"type": "function", "function": {
        "name": "get_booking_status",
        "description": "The driver's current slot, check-in or unloading status.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "escalate_to_ops",
        "description": "Hand over to the operations team (a request you cannot handle).",
        "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}}},
]


class AgentService:
    mode = "agent"

    def __init__(self, conn, llm="auto"):
        self.conn = conn
        self.llm = get_llm() if llm == "auto" else llm
        if self.llm is None or not hasattr(self.llm, "chat_tools"):
            raise RuntimeError("agent mode needs OPENROUTER_API_KEY (tool calling)")

    # ---------------------------------------------------------------- tools (guards live here)
    def _sh(self, ctx):
        if not ctx["desk"].state["shipment_id"]:
            return None, {"error": "Shipment not identified. Ask which order this is about.",
                          "orders": [c["order_reference"] for c in ctx["candidates"]]}
        return tools.get_shipment(self.conn, ctx["desk"].state["shipment_id"]), None

    def t_identify_shipment(self, ctx, reference=""):
        ref = tools.norm_ref(reference)
        said = tools.norm_ref(" ".join(ctx["driver_texts"]))
        if not ref or ref not in said:
            return {"error": "The driver has not written this reference. Ask them; do not guess.",
                    "orders": [c["order_reference"] for c in ctx["candidates"]]}
        regs = [c["reg_no"] for c in ctx["candidates"]]
        named = [c for c in ctx["candidates"]
                 if ref in (tools.norm_ref(c["order_reference"]), tools.norm_ref(c["shipment_id"]))
                 or (len(ref) == 3 and c["order_reference"].endswith(ref))
                 or (regs.count(c["reg_no"]) == 1 and ref == tools.norm_ref(c["reg_no"]))]
        if len(named) != 1:
            return {"error": "That reference does not match exactly one of the driver's shipments.",
                    "orders": [c["order_reference"] for c in ctx["candidates"]]}
        ctx["desk"].state["shipment_id"] = named[0]["shipment_id"]
        ctx["desk"].set_thread(shipment_id=named[0]["shipment_id"])
        return {"status": "identified", "order": named[0]["order_reference"]}

    def t_report_delay(self, ctx, delay_minutes=None, new_eta=None, confidence="MEDIUM", reason=None):
        sh, err = self._sh(ctx)
        if err:
            return err
        if tools.on_site(sh):
            return {"status": "on_site", "gate_in_at": tu.hhmm(sh["gate_in_ts"]),
                    "note": "The gate has logged this truck in. Do not record an ETA; call offer_slots."}
        desk = ctx["desk"]
        if delay_minutes is not None:
            new_eta = None                     # relative delay wins: the backend adds it to the current ETA
            if not 0 <= int(delay_minutes) <= 24 * 60:
                return {"error": "delay_minutes must be 0-1440"}
            base = tools.current_eta(self.conn, sh["shipment_id"], desk.now)
            eta = tu.add(max(base["eta"], desk.now), int(delay_minutes))
        elif new_eta:
            try:
                eta = tu.norm(new_eta)
            except ValueError:
                return {"error": "new_eta must be 'YYYY-MM-DD HH:MM'"}
        else:
            return {"error": "Give delay_minutes or new_eta."}
        confidence = confidence if confidence in ("HIGH", "MEDIUM", "LOW") else "MEDIUM"
        reason = reason if reason in ("TRAFFIC", "BREAKDOWN", "WEATHER", "LATE_DEPARTURE", "LOADING_DELAY",
                                      "ROUTE_ISSUE", "OTHER") else None
        desk.record_delay(sh, eta=eta, confidence=confidence, reason=reason, note=ctx["text"],
                          delay=delay_minutes, earliest=None, latest=None)
        ctx["eta"] = eta
        return {"status": "eta_recorded", "eta": tu.hhmm(eta)}

    def t_offer_slots(self, ctx, earliest=None, latest_end=None):
        sh, err = self._sh(ctx)
        if err:
            return err
        desk = ctx["desk"]
        try:
            earliest = tu.norm(earliest) if earliest else None
            latest_end = tu.norm(latest_end) if latest_end else None
        except ValueError:
            return {"error": "times must be 'YYYY-MM-DD HH:MM'"}
        if sh.get("queue_state") == "IN_DOCK":
            res = desk.status(sh)
        elif tools.on_site(sh):
            res = desk.handle_on_site(sh, "ask_status")
        else:
            eta = ctx.get("eta") or tools.current_eta(self.conn, sh["shipment_id"], desk.now)["eta"]
            res = desk.offer(sh, max(eta, earliest or eta), latest_end, eta)
        ctx["result"] = res
        out = {"status": res["action"], "message_for_driver": res["reply"]}
        if res["action"] == "offered":
            out["options"] = [{"option": i, "time": tu.hhmm(o["start"]), "dock": dock_label(o["dock_code"], o["dock_type"])}
                              for i, o in enumerate(res["options"], 1)]
        return out

    def t_confirm_option(self, ctx, option_number=0):
        sh, err = self._sh(ctx)
        if err:
            return err
        if ctx["desk"].state["state"] != "OFFERED":
            return {"error": "No options have been offered in this conversation. Call offer_slots first."}
        res = ctx["desk"].confirm(sh, int(option_number))
        if res["action"] == "clarify":
            return {"error": res["reply"]}
        ctx["result"] = res
        return {"status": res["action"], "message_for_driver": res["reply"]}

    def t_get_booking_status(self, ctx):
        sh, err = self._sh(ctx)
        if err:
            return err
        on_site = tools.on_site(sh) and sh.get("queue_state") != "IN_DOCK"
        res = ctx["desk"].handle_on_site(sh, "ask_status") if on_site else ctx["desk"].status(sh)
        ctx["result"] = res
        return {"status": res["action"], "message_for_driver": res["reply"]}

    def t_escalate_to_ops(self, ctx, reason=""):
        sh, err = self._sh(ctx)
        if err:
            return err
        ctx["result"] = ctx["desk"].escalate(sh, (reason or "Agent requested a human")[:200])
        return {"status": "escalated"}

    # ---------------------------------------------------------------- loop
    def handle(self, conversation_id: str, phone: str, text: str, now: str = tu.SNAPSHOT_NOW,
               external_message_id: str | None = None) -> dict:
        now = tu.norm(now)
        driver = tools.driver_for_phone(self.conn, phone)
        if driver is None:
            return {"action": "no_shipment", "reply": "Sorry, I can't find this number in our driver list. "
                                                      "Please call the SetuHaul control room.", "mode": "agent",
                    "tool_calls": []}
        desk = Desk(self.conn, conversation_id, now)
        desk.open(driver)
        dup = desk.find_duplicate(text, external_message_id)
        if dup:
            desk.log("DRIVER", text, "DUPLICATE", external_id=external_message_id, duplicate=True)
            reply = f"Got it, this repeats your message from {tu.hhmm(dup['message_ts'])}. I'm already working on it."
            desk.log("AGENT", reply, "DUPLICATE")
            return {"action": "duplicate", "reply": reply, "mode": "agent", "reply_source": "template", "tool_calls": []}

        history = desk.history()
        desk.log("DRIVER", text, None, external_id=external_message_id)
        cands = tools.shipments_for_driver(self.conn, driver["driver_id"])
        if not desk.state["shipment_id"]:
            if not cands:                       # cancelled / nothing open: same answer as the workflow
                _, res = desk.identify(text, False)
                desk.save()
                desk.log("AGENT", res["reply"], res["action"].upper())
                return {**res, "mode": "agent", "reply_source": "template", "tool_calls": []}
            # same identity rule as the workflow (one open, named, or the only trip underway)
            sh, _ = tools.resolve_shipment(cands, text, any(w in text.lower() for w in ("late", "delay", "der")))
            if sh:
                desk.state["shipment_id"] = sh["shipment_id"]
                desk.set_thread(shipment_id=sh["shipment_id"])

        ctx = {"desk": desk, "candidates": cands, "text": text,
               "driver_texts": [m["content"] for m in history if m["role"] == "user"] + [text]}

        def facts():
            sh = tools.get_shipment(self.conn, desk.state["shipment_id"]) if desk.state["shipment_id"] else None
            appt = tools.current_appointment(self.conn, sh["shipment_id"]) if sh else None
            eta = tools.current_eta(self.conn, sh["shipment_id"], now) if sh else None
            return {"current_time": tu.simple(now),
                    "driver_orders": [f"{c['order_reference']} ({c['origin_city']}, truck {c['reg_no']})" for c in cands],
                    "shipment_identified": sh["order_reference"] if sh else None,
                    "current_eta": tu.hhmm(eta["eta"]) if eta else None,
                    "current_booking": f"{tu.hhmm(appt['slot_start_ts'])} at Dock {appt['dock_code']}" if appt else None,
                    "at_facility": bool(sh and tools.on_site(sh)),
                    "conversation_state": desk.state["state"],
                    "options_offered": [f"{i}. {tu.hhmm(o['start'])} - Dock {o['dock_code']}"
                                        for i, o in enumerate(desk.state["offered"], 1)]
                    if desk.state["state"] == "OFFERED" else []}

        known = facts()
        messages = [{"role": "system", "content": AGENT_PROMPT},
                    {"role": "system", "content": "Facts from the backend: " + json.dumps(known)},
                    *history, {"role": "user", "content": text}]
        trace, reply = [], None
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
                        out = fn(ctx, **args) if fn else {"error": f"unknown tool {name}"}
                    except TypeError as e:
                        out = {"error": f"bad arguments: {e}"}
                    trace.append({"tool": name, "args": args,
                                  "result": {k: v for k, v in out.items() if k != "message_for_driver"}})
                    messages.append({"role": "tool", "tool_call_id": call.get("id", name), "content": json.dumps(out)})
            else:                                    # step limit hit
                sh, _ = self._sh(ctx)
                ctx["result"] = desk.escalate(sh, "Agent exceeded its step limit") if sh else \
                    desk.result("escalate", "Thanks. Our operations team will call you shortly.")
                reply = None
        except Exception as e:                       # LLM down: never leave the driver without an answer
            trace.append({"tool": "llm_error", "args": {}, "result": {"error": type(e).__name__}})
            if "result" not in ctx:
                sh, _ = self._sh(ctx)
                ctx["result"] = desk.escalate(sh, "Assistant unavailable") if sh else \
                    desk.result("escalate", "Sorry, I couldn't process that. Our operations team will call you shortly.")
            reply = None

        res = ctx.get("result")
        if res is None:                              # the model only talked
            if not desk.state["shipment_id"] and len(cands) > 1:
                desk.facts = {**EMPTY_FACTS, "orders": [c["order_reference"] for c in cands]}
                res = desk.result("ask_shipment", "Which shipment is this about? "
                                  + ", ".join(c["order_reference"] for c in cands) + ".")
                desk.set_thread("WAITING_FOR_DRIVER")
            elif ctx.get("eta"):
                desk.facts = {**EMPTY_FACTS, "times": [tu.hhmm(ctx["eta"])]}
                res = desk.result("eta_recorded", f"Noted, new ETA {tu.hhmm(ctx['eta'])}.")
            elif desk.state["state"] == "OFFERED":
                desk.facts = dict(EMPTY_FACTS)
                res = desk.result("clarify", "Please reply with an option number.")
            else:
                desk.facts = dict(EMPTY_FACTS)
                res = desk.result("ask_eta", "Thanks. Roughly how late are you, or what time will you reach?")
        template = res["reply"]
        known_times = [t for t in (known["current_eta"], (known["current_booking"] or "").split(" at ")[0]) if t]
        fx = {**desk.facts, "allowed_times": desk.facts.get("allowed_times", []) + known_times
              + [tu.hhmm(t) for t in (ctx.get("eta"),) if t]}
        ok, note = check(reply, fx) if reply else (False, "no reply")
        res = {**res, "reply": reply if ok else template, "reply_source": "llm" if ok else "template",
               "phrase_check": note, "template_reply": template, "mode": "agent", "tool_calls": trace}
        if ctx.get("eta"):
            res.setdefault("eta", ctx["eta"])
        desk.save()
        desk.log("AGENT", res["reply"], res["action"].upper(), review=desk.review)
        return res
