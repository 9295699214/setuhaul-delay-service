"""Workflow mode (default): message in -> reply out, in fixed steps.

The extractor (LLM or regex) only reads the text. Each step below is plain code
calling the Desk, which applies the facility rules and records everything in the
supplied tables. Order of questions (design question 1): who is writing ->
which shipment -> is the truck already here -> how late / what window -> offer.
"""
from . import timeutil as tu
from . import tools
from .agent import get_extractor, get_phraser
from .desk import Desk
from .phrasing import phrase


class ConversationService:
    mode = "workflow"

    def __init__(self, conn, extractor=None, phraser="auto"):
        self.conn = conn
        self.extract = extractor or get_extractor()
        self.phraser = get_phraser() if phraser == "auto" else phraser

    def handle(self, conversation_id: str, phone: str, text: str, now: str = tu.SNAPSHOT_NOW,
               external_message_id: str | None = None) -> dict:
        now = tu.norm(now)
        driver = tools.driver_for_phone(self.conn, phone)
        if driver is None:      # unknown number: nothing to attach a thread to
            return {"action": "no_shipment", "reply": "Sorry, I can't find this number in our driver list. "
                                                      "Please call the SetuHaul control room.", "mode": self.mode}
        desk = Desk(self.conn, conversation_id, now)
        ex = self.extract(text, tu.simple(now), None)
        desk.open(driver, ex.intent)

        dup = desk.find_duplicate(text, external_message_id)
        if dup:
            desk.log("DRIVER", text, ex.intent.upper(), external_id=external_message_id, duplicate=True)
            res = desk.result("duplicate", f"Got it, this repeats your message from {tu.hhmm(dup['message_ts'])}. "
                                           "I'm already working on it.")
            desk.log("AGENT", res["reply"], "DUPLICATE")
            return {**res, "mode": self.mode, "reply_source": "template"}

        eta_seen = tu.norm(ex.new_eta) if ex.new_eta else None
        desk.log("DRIVER", text, ex.intent.upper(), eta=eta_seen, external_id=external_message_id,
                 review=ex.confidence == "LOW")
        res = self._step(desk, text, ex)
        desk.save()
        res = phrase(self.phraser, text, res, desk.facts)       # LLM wording, code-checked facts
        desk.log("AGENT", res["reply"], res["action"].upper(), review=desk.review)
        res.setdefault("mode", self.mode)
        return res

    # ------------------------------------------------------------------
    def _step(self, desk: Desk, text: str, ex) -> dict:
        delay_like = ex.intent in ("report_delay", "ask_slots")
        sh, ask = desk.identify(text, is_delay_report=ex.intent == "report_delay")
        if ask:
            return ask
        if desk.state["pending_text"] and desk.state["pending_text"] != text:
            text = desk.state["pending_text"]           # resume the message we parked while asking
            desk.state["pending_text"] = None
            ex = self.extract(text, tu.simple(desk.now), None)
            delay_like = ex.intent in ("report_delay", "ask_slots")
        desk.state["pending_text"] = None

        if sh["current_status"] == "CANCELLED":
            return desk.result("cancelled_shipment", "This shipment was cancelled. Please contact dispatch.")

        # Driver picks one of the options we offered
        if desk.state["state"] == "OFFERED" and ex.intent in ("choose_option", "confirm"):
            idx = ex.choice_index or (1 if len(desk.state["offered"]) == 1 else 0)
            return desk.confirm(sh, idx)

        # The gate knows better than the chat: plan from now (SHP1003 / SHP1004 / SHP1005)
        if tools.on_site(sh):
            return desk.handle_on_site(sh, ex.intent)

        if ex.intent == "ask_status":
            return desk.status(sh)

        if delay_like:
            base = tools.current_eta(self.conn, sh["shipment_id"], desk.now)
            if ex.new_eta:
                eta = tu.norm(ex.new_eta)
            elif ex.delay_minutes is not None:
                eta = tu.add(max(base["eta"], desk.now), ex.delay_minutes)
            elif ex.intent == "ask_slots" and ex.earliest:
                eta = base["eta"]
            else:
                desk.exception(sh, "NEEDS_INFORMATION", description="Driver reported a delay without a time.")
                return desk.result("ask_eta", "Sorry to hear that. Roughly how late will you be, "
                                              "or what time will you reach?")
            earliest = tu.norm(ex.earliest) if ex.earliest else None
            latest = tu.norm(ex.latest_end) if ex.latest_end else None
            if ex.intent == "report_delay":
                desk.record_delay(sh, eta=eta, confidence=ex.confidence, reason=ex.delay_reason, note=text,
                                  delay=ex.delay_minutes, earliest=earliest, latest=latest)
            start = max(eta, earliest or eta)
            lead = None
            if ex.intent == "ask_slots":
                lead = f"If you reach around {tu.hhmm(eta)}, these are possible. Your current booking is unchanged."
            elif ex.confidence == "LOW":
                lead = f"Noted, planning for {tu.hhmm(eta)} (the later end of your estimate)."
            elif ex.delay_minutes is not None and not ex.new_eta:
                lead = f"Noted, new ETA {tu.hhmm(eta)} ({tu.hhmm(base['eta'])} plus {ex.delay_minutes} min)."
            return desk.offer(sh, start, latest, eta, lead=lead)

        return desk.result("ask_eta", "Thanks. Roughly how late are you, or what time will you reach?")
