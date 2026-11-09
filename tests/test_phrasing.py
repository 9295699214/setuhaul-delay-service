"""LLM replies keep every fact or fall back to the template."""
from app.phrasing import check, facts_from, phrase
from app.service import ConversationService

OFFER = {"action": "offered", "options": [
    {"dock_name": "Jaipur Dock 1"}, {"dock_name": "Jaipur Dock 4 (heavy)"}],
    "reply": "Noted, new ETA 12:00 PM. These slots are held for you for 2 minutes:\n"
             "1. 12:00 PM - Jaipur Dock 1\n2. 1:00 PM - Jaipur Dock 4 (heavy)\nReply with the option number."}


class Fake:
    def __init__(self, text=None, err=None):
        self.text, self.err, self.calls = text, err, 0

    def chat(self, system, user, json_mode=False, max_tokens=300):
        self.calls += 1
        if self.err:
            raise self.err
        return self.text


def run(text):
    return phrase(Fake(text), "do ghante late", dict(OFFER))


def test_good_hinglish_reply_is_used():
    r = run("Theek hai, naya ETA 12:00 PM. Ye slots 2 min ke liye hold hain:\n"
            "1. 12:00 PM - Jaipur Dock 1\n2. 1:00 PM - Jaipur Dock 4 (heavy)\nOption number bhejiye.")
    assert r["reply_source"] == "llm" and r["phrase_check"] == "ok"


def test_wrong_time_rejected():
    r = run("Naya ETA 12:30 PM.\n1. 12:00 PM - Jaipur Dock 1\n2. 1:00 PM - Jaipur Dock 4 (heavy)")
    assert r["reply_source"] == "template" and "time" in r["phrase_check"]
    assert r["reply"] == OFFER["reply"]


def test_dropped_dock_or_option_rejected():
    assert run("ETA 12:00 PM.\n1. 12:00 PM - Dock 1\n2. 1:00 PM - Jaipur Dock 4 (heavy)")["reply_source"] == "template"
    assert run("ETA 12:00 PM. 12:00 PM Jaipur Dock 1 ya 1:00 PM Jaipur Dock 4 (heavy)")["reply_source"] == "template"


def test_time_without_ampm_rejected():
    r = run("ETA 12:00 PM, 13:00 bhi.\n1. 12:00 PM - Jaipur Dock 1\n2. 1:00 PM - Jaipur Dock 4 (heavy)")
    assert r["reply_source"] == "template"


def test_llm_error_falls_back():
    r = phrase(Fake(err=TimeoutError()), "late", dict(OFFER))
    assert r["reply_source"] == "template" and r["reply"] == OFFER["reply"]


def test_confirmed_reply_keeps_dock():
    res = {"action": "confirmed", "reply": "Confirmed: 1:00 PM at Jaipur Dock 2. Please report at the gate on arrival."}
    f = facts_from(res, res["reply"])
    assert f["docks"] == ["Jaipur Dock 2"] and f["times"] == ["1:00 PM"]
    assert check("Pakka: 1:00 PM, Jaipur Dock 2. Gate par report karein.", f)[0]
    assert not check("Pakka: 1:00 PM, Jaipur Dock 3. Gate par report karein.", f)[0]


def test_service_uses_phraser_but_booking_unchanged(conn):
    fake = Fake("Haan ji, ho gaya: 12:00 PM, Jaipur Dock 1. Gate par aa jaiye.")
    svc = ConversationService(conn, phraser=fake)
    r1 = svc.handle("p1", "+919800000001", "2 hrs late", "2026-08-04 08:00")
    assert r1["action"] == "offered" and r1["reply_source"] == "template"   # fake text lacks the options
    r2 = svc.handle("p1", "+919800000001", "1", "2026-08-04 08:01")
    assert r2["action"] == "confirmed" and r2["reply_source"] == "llm"
    assert r2["slot_id"] == "JAI-D1-12"
