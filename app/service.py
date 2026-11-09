"""Conversation workflow: message in -> reply out.

A fixed workflow, not a free agent. The extractor reads the text; the
steps and every capacity decision are plain code. Each step follows one
rule: ask the driver only for what the backend cannot know, and never
trust the driver over a fact the backend holds.
"""
import json
from datetime import datetime

from . import tools
from .agent import get_extractor, get_phraser
from .phrasing import phrase

FMT = tools.FMT


def _hhmm(ts: str) -> str:
    return datetime.strptime(ts, FMT).strftime("%I:%M %p").lstrip("0")


class ConversationService:
    def __init__(self, conn, extractor=None, phraser="auto"):
        self.conn = conn
        self.extract = extractor or get_extractor()
        self.phraser = get_phraser() if phraser == "auto" else phraser

    # -- persistence ---------------------------------------------------------
    def _load(self, cid):
        row = self.conn.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
        return dict(row) if row else None

    def _save(self, cid, phone, shipment_id, state, offered, now, pending=None):
        self.conn.execute(
            "INSERT INTO conversations (id,phone,shipment_id,state,offered_json,pending_text,updated_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET shipment_id=excluded.shipment_id, "
            "state=excluded.state, offered_json=excluded.offered_json, "
            "pending_text=excluded.pending_text, updated_at=excluded.updated_at",
            (cid, phone, shipment_id, state, json.dumps(offered), pending, now))

    def _log(self, cid, direction, body, now, action=None):
        self.conn.execute("INSERT INTO messages (conversation_id,direction,body,at,action) "
                          "VALUES (?,?,?,?,?)", (cid, direction, body, now, action))

    # -- entry point ---------------------------------------------------------
    def handle(self, conversation_id: str, phone: str, text: str, now: str) -> dict:
        self._log(conversation_id, "in", text, now)
        result = self._handle(conversation_id, phone, text, now)
        result = phrase(self.phraser, text, result)    # LLM wording, code-checked facts
        self._log(conversation_id, "out", result["reply"], now, result["action"])
        return result

    def _handle(self, cid, phone, text, now):
        conv = self._load(cid) or {"state": "new", "offered_json": "[]", "shipment_id": None,
                                   "pending_text": None}

        # 1. Who is writing? Resolved from the backend, asked only if ambiguous.
        sid = conv["shipment_id"]
        if sid is None:
            candidates = tools.find_shipments_by_phone(self.conn, phone)
            if not candidates:
                return {"reply": "Sorry, I can't find an active shipment for this number. "
                                 "Please call the SetuHaul control room.", "action": "no_shipment"}
            if len(candidates) > 1:
                typed = tools.norm_reg(text)
                match = [c for c in candidates if c["reg_no"] and tools.norm_reg(c["reg_no"]) in typed]
                if len(match) != 1:
                    self._save(cid, phone, None, "new", [], now, pending=conv["pending_text"] or text)
                    regs = ", ".join(c["reg_no"] for c in candidates)
                    return {"reply": f"This number is linked to more than one truck ({regs}). "
                                     "Please reply with your truck number.", "action": "ask_vehicle"}
                sid = match[0]["id"]
                self._save(cid, phone, sid, "new", [], now)
                if conv["pending_text"]:          # resume the message we parked
                    text = conv["pending_text"]
            else:
                sid = candidates[0]["id"]
                self._save(cid, phone, sid, conv["state"], json.loads(conv["offered_json"]), now)

        offered = json.loads(conv["offered_json"])
        ex = self.extract(text, now, offered)

        # 2. Driver picks one of the options we offered
        if conv["state"] == "offered" and ex.intent in ("choose_option", "confirm"):
            idx = ex.choice_index or (1 if len(offered) == 1 else None)
            if idx is None or not 1 <= idx <= len(offered):
                return {"reply": f"Please reply with an option number (1-{len(offered)}).",
                        "action": "clarify"}
            return self._confirm(cid, phone, sid, offered, idx - 1, now)

        if ex.intent == "report_delay":
            return self._offer(cid, phone, sid, ex, now)

        if ex.intent == "ask_status":
            appt = tools.current_appointment(self.conn, sid)
            if appt:
                return {"reply": f"Your slot is {_hhmm(appt['start_at'])} at {appt['dock_name']}.",
                        "action": "status"}
            return {"reply": "You have no slot booked right now. When will you arrive?", "action": "status"}

        return {"reply": "Thanks. Roughly how late are you, or what time will you reach?",
                "action": "ask_eta"}

    # -- steps ---------------------------------------------------------------
    def _offer(self, cid, phone, sid, ex, now):
        # Backend beats driver: if the gate already logged the truck in, don't re-plan.
        gate = tools.gate_status(self.conn, sid)
        if gate and gate["event"] == "gate_in":
            return {"reply": f"Our gate shows your truck checked in at {_hhmm(gate['at'])}. "
                             "Please go to the dock supervisor; no new slot is needed.",
                    "action": "already_arrived"}

        if ex.new_eta:
            eta = ex.new_eta
        elif ex.delay_minutes is not None:
            base = tools.current_eta(self.conn, sid)
            eta = tools._add(max(base["eta"] if base else now, now), ex.delay_minutes)
        else:
            return {"reply": "Sorry to hear that. How late do you expect to be?", "action": "ask_eta"}

        self.conn.execute("BEGIN IMMEDIATE")
        tools.record_eta(self.conn, sid, eta, "driver", now)
        self.conn.execute("COMMIT")

        tools.release_holds(self.conn, cid)
        earliest = max(eta, ex.earliest or eta)
        candidates = tools.find_options(self.conn, sid, earliest, ex.latest_end, now=now, limit=6)

        offered = []
        for c in candidates:            # hold up to 3; skip any another chat grabbed meanwhile
            h = tools.hold_slot(self.conn, c["slot_id"], sid, cid, now)
            if h["ok"]:
                offered.append({**c, "hold_id": h["hold_id"]})
            if len(offered) == 3:
                break

        if not offered:
            self._save(cid, phone, sid, "new", [], now)
            return {"reply": f"Noted, new ETA {_hhmm(eta)}. No suitable dock slot is free in that "
                             "window. Our operations team will call you shortly.",
                    "action": "escalate", "eta": eta}

        self._save(cid, phone, sid, "offered", offered, now)
        lines = [f"{i}. {_hhmm(o['start_at'])} - {o['dock_name']}" for i, o in enumerate(offered, 1)]
        return {"reply": f"Noted, new ETA {_hhmm(eta)}. These slots are held for you for "
                         f"{tools.HOLD_MINUTES} minutes:\n" + "\n".join(lines) +
                         "\nReply with the option number.",
                "action": "offered", "eta": eta, "options": offered}

    def _confirm(self, cid, phone, sid, offered, i, now):
        chosen = offered[i]
        res = tools.confirm(self.conn, chosen["hold_id"], now)
        if not res["ok"]:
            self._save(cid, phone, sid, "new", [], now)
            return {"reply": "Sorry, that slot is no longer available (the hold expired). "
                             "Tell me your ETA again and I'll find new options.",
                    "action": "confirm_failed", "reason": res["reason"]}
        tools.release_holds(self.conn, cid, keep_hold_id=chosen["hold_id"])
        tools.record_confirmation(self.conn, res["appointment_id"], sid, chosen["slot_id"], now)
        self._save(cid, phone, sid, "confirmed", [chosen], now)
        return {"reply": f"Confirmed: {_hhmm(chosen['start_at'])} at {chosen['dock_name']}. "
                         "Please report at the gate on arrival.",
                "action": "confirmed", "appointment_id": res["appointment_id"],
                "slot_id": chosen["slot_id"]}
