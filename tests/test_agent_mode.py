"""Agent mode with a scripted fake LLM: the guards hold whatever the model asks."""
import json

from app import tools
from app.agent_mode import MAX_STEPS, AgentService
from conftest import P, T


def call(name, **args):
    return {"id": f"c-{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class Script:
    """Plays back assistant turns; records what the agent sent."""
    def __init__(self, *turns):
        self.turns, self.seen = list(turns), []

    def chat_tools(self, messages, tools_, max_tokens=400):
        self.seen.append(messages)
        t = self.turns.pop(0)
        if isinstance(t, str):
            return {"role": "assistant", "content": t, "tool_calls": None}
        return {"role": "assistant", "content": None, "tool_calls": t}


def agent(conn, *turns):
    s = Script(*turns)
    return AgentService(conn, llm=s), s


def test_full_flow_offer_then_confirm(conn):
    a, _ = agent(conn, [call("report_delay", new_eta="2026-08-04 11:10", confidence="HIGH", reason="BREAKDOWN")],
                 [call("offer_slots", latest_end="2026-08-04 13:30")],
                 "Theek hai, ETA 11:10 AM. Slots:\n1. 12:00 PM - Dock D2\n2. 12:00 PM - Dock D4\nNumber bhejiye.")
    r = a.handle("a1", P(12), "Tyre repaired. I can reach 11:10. Leave before 1:30.", T("09:29"))
    assert r["action"] == "offered" and r["reply_source"] == "llm"
    assert [c["tool"] for c in r["tool_calls"]] == ["report_delay", "offer_slots"]
    b, s2 = agent(conn, [call("confirm_option", option_number=2)], "Pakka: 12:00 PM, Dock D4.")
    r2 = b.handle("a1", P(12), "2", T("09:30"))
    assert r2["action"] == "confirmed" and tools.current_appointment(conn, "SHP1012")["dock_code"] == "D4"
    assert any((m.get("content") or "").startswith("Tyre repaired") for m in s2.seen[0])   # history was given


def test_confirm_without_offer_is_refused(conn):
    a, _ = agent(conn, [call("confirm_option", option_number=1)], "Done!")
    r = a.handle("a2", P(13), "book me anything", T("09:30"))
    assert "error" in r["tool_calls"][0]["result"] and r["action"] != "confirmed"
    assert tools.current_appointment(conn, "SHP1013")["appointment_id"] == "APT1013A"


def test_cannot_guess_shipment_for_driver_with_two_moving_trucks(conn):
    a, _ = agent(conn, [call("identify_shipment", reference="ORD-260804-016")], "Which order?")
    r = a.handle("g1", P(7), "Heavy trailer late, what next?", T("09:39"))
    assert "do not guess" in r["tool_calls"][0]["result"]["error"]
    assert r["action"] == "ask_shipment"


def test_identify_with_what_the_driver_wrote(conn):
    a, _ = agent(conn, "Which order is it, ORD-260804-007 or ORD-260804-016?")
    assert a.handle("g1b", P(7), "Heavy trailer late", T("09:39"))["action"] == "ask_shipment"
    b, _ = agent(conn, [call("identify_shipment", reference="MH04HV4107")],
                 [call("report_delay", new_eta="2026-08-04 12:00")], [call("offer_slots")], "ok")
    r = b.handle("g1b", P(7), "MH04HV4107, ETA 12:00", T("09:40"))
    assert r["action"] == "offered" and {o["dock_code"] for o in r["options"]} == {"D6"}


def test_tools_refuse_until_identified(conn):
    a, _ = agent(conn, [call("report_delay", delay_minutes=60)], "Which order is this for?")
    r = a.handle("g2", P(7), "1 hour late", T("09:39"))
    assert "not identified" in r["tool_calls"][0]["result"]["error"]


def test_delay_beats_model_computed_eta(conn):
    a, _ = agent(conn, [call("report_delay", delay_minutes=60, new_eta="2026-08-04 10:19")], "noted")
    r = a.handle("g3", P(13), "I am late by one hour.", T("09:19"))
    assert r["eta"] == T("11:00")          # planned 10:00 + 1 h, not the model's 10:19


def test_on_site_truck_is_planned_from_now(conn):
    a, _ = agent(conn, [call("report_delay", delay_minutes=60)], [call("offer_slots")], "ok")
    r = a.handle("g4", P(5), "I am 1 hour late", T("09:20"))      # SHP1005 is in the yard
    assert r["tool_calls"][0]["result"]["status"] == "on_site"
    assert r["action"] == "offered" and all(o["dock_code"] in ("D2", "D4") for o in r["options"])
    assert conn.execute("SELECT COUNT(*) FROM eta_updates WHERE eta_update_id LIKE 'ETA-%'").fetchone()[0] == 0


def test_hallucinated_time_falls_back_to_template(conn):
    a, _ = agent(conn, [call("report_delay", new_eta="2026-08-04 11:10")], [call("offer_slots")],
                 "New ETA 11:10 AM.\n1. 11:00 AM - Dock D1\n2. 12:00 PM - Dock D4")
    r = a.handle("g5", P(12), "reach 11:10", T("09:29"))
    assert r["reply_source"] == "template" and "11:00 AM - Dock D1" not in r["reply"]


def test_option_out_of_range_refused(conn):
    a, _ = agent(conn, [call("report_delay", new_eta="2026-08-04 11:10")], [call("offer_slots")], "x")
    a.handle("g6", P(12), "reach 11:10", T("09:29"))
    b, _ = agent(conn, [call("confirm_option", option_number=7)], "ok")
    r = b.handle("g6", P(12), "7", T("09:30"))
    assert "option number" in r["tool_calls"][0]["result"]["error"] and r["action"] == "clarify"


def test_step_limit_escalates(conn):
    a, _ = agent(conn, *[[call("get_booking_status")] for _ in range(MAX_STEPS)])
    r = a.handle("g7", P(13), "loop", T("09:30"))
    assert r["action"] == "escalate" and len(r["tool_calls"]) == MAX_STEPS


def test_llm_failure_still_answers(conn):
    class Down:
        def chat_tools(self, *a, **k):
            raise TimeoutError
    r = AgentService(conn, llm=Down()).handle("g8", P(12), "2 hrs late", T("09:30"))
    assert r["action"] == "escalate" and r["reply"]


def test_bad_arguments_do_not_crash(conn):
    a, _ = agent(conn, [call("report_delay", delay_minutes=99999)], [call("offer_slots", earliest="tomorrow")],
                 "How late are you exactly?")
    r = a.handle("g9", P(12), "very late", T("09:30"))
    assert all("error" in c["result"] for c in r["tool_calls"]) and r["action"] == "ask_eta"


def test_cancelled_and_unknown_skip_the_llm(conn):
    a, s = agent(conn)
    assert a.handle("g10", P(8), "Is my 4 PM slot still active?", T("10:12"))["action"] == "cancelled_shipment"
    assert a.handle("g11", "+91-9999999999", "late", T("10:12"))["action"] == "no_shipment"
    assert s.seen == []
