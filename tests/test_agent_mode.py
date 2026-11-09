"""Agent mode with a scripted fake LLM: the guards hold whatever the model asks."""
import json

from app import tools
from app.agent_mode import MAX_STEPS, AgentService

T = lambda hhmm: f"2026-08-04 {hhmm}"  # noqa: E731


def call(name, **args):
    return {"id": f"c-{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class Script:
    """Plays back assistant messages; records what the agent sent."""
    def __init__(self, *turns):
        self.turns, self.seen = list(turns), []

    def chat_tools(self, messages, tools_, max_tokens=400):
        self.seen.append(messages)
        t = self.turns.pop(0)
        if isinstance(t, str):
            return {"role": "assistant", "content": t, "tool_calls": None}
        return {"role": "assistant", "content": None, "tool_calls": t}

    def last_tool_result(self):
        return json.loads(self.seen[-1][-1]["content"])


def test_full_flow_offer_then_confirm(conn):
    s = Script([call("report_delay", delay_minutes=120)], [call("offer_slots")],
               "Theek hai, naya ETA 12:00 PM. Slots:\n1. 12:00 PM - Jaipur Dock 1\n"
               "2. 12:00 PM - Jaipur Dock 4 (heavy)\n3. 1:00 PM - Jaipur Dock 1\nNumber bhejiye.")
    r = AgentService(conn, llm=s).handle("a1", "+919800000001", "do ghante late", T("08:30"))
    assert r["action"] == "offered" and r["eta"] == T("12:00") and r["reply_source"] == "llm"
    assert [c["tool"] for c in r["tool_calls"]] == ["report_delay", "offer_slots"]

    s2 = Script([call("confirm_option", option_number=2)], "Pakka: 12:00 PM, Jaipur Dock 4 (heavy).")
    r2 = AgentService(conn, llm=s2).handle("a1", "+919800000001", "doosra wala", T("08:31"))
    assert r2["action"] == "confirmed" and r2["slot_id"] == "JAI-D4-12"
    assert tools.current_appointment(conn, "SH-01")["slot_id"] == "JAI-D4-12"
    # history from the first message was given to the model
    assert any(m.get("content") == "do ghante late" for m in s2.seen[0])


def test_confirm_without_offer_is_refused(conn):
    s = Script([call("confirm_option", option_number=1)], "Done!")
    r = AgentService(conn, llm=s).handle("a2", "+919800000001", "book me anything", T("08:30"))
    assert "error" in r["tool_calls"][0]["result"]
    assert r["action"] != "confirmed"
    assert tools.current_appointment(conn, "SH-01")["slot_id"] == "JAI-D1-10"   # unchanged


def test_option_out_of_range_refused(conn):
    AgentService(conn, llm=Script([call("report_delay", delay_minutes=120)], [call("offer_slots")], "x")) \
        .handle("a3", "+919800000001", "2 hrs late", T("08:30"))
    s = Script([call("confirm_option", option_number=7)], "ok")
    r = AgentService(conn, llm=s).handle("a3", "+919800000001", "7", T("08:31"))
    assert r["tool_calls"][0]["result"]["error"].startswith("Option must be")
    assert r["action"] == "clarify"


def test_gate_in_blocks_new_slot(conn):
    s = Script([call("report_delay", delay_minutes=60)], [call("offer_slots")], "Here you go")
    r = AgentService(conn, llm=s).handle("a4", "+919800000015", "1 hour late", T("08:30"))
    assert r["tool_calls"][0]["result"]["status"] == "already_arrived"
    # the model ignored the note and called offer_slots anyway: still refused
    assert r["tool_calls"][1]["result"]["status"] == "already_arrived"
    assert r["action"] == "already_arrived" and "7:50 AM" in r["reply"]
    assert conn.execute("SELECT COUNT(*) FROM slot_holds WHERE conversation_id='a4'").fetchone()[0] == 0


def test_shared_phone_requires_identification(conn):
    s = Script([call("report_delay", delay_minutes=60)], "Which truck are you on? GJ01AB1018 or GJ01AB1019?")
    r = AgentService(conn, llm=s).handle("a5", "+919800000018", "late by 1 hour", T("11:00"))
    assert "not identified" in r["tool_calls"][0]["result"]["error"]
    assert r["action"] == "ask_vehicle"
    s2 = Script([call("identify_truck", truck_number="GJ01 AB 1019")],
                [call("report_delay", delay_minutes=60)], [call("offer_slots")], "1. … (bad list)")
    r2 = AgentService(conn, llm=s2).handle("a5", "+919800000018", "GJ01 AB 1019", T("11:01"))
    assert r2["action"] == "offered" and r2["reply_source"] == "template"   # bad list -> template


def test_hallucinated_time_falls_back_to_template(conn):
    s = Script([call("report_delay", delay_minutes=120)], [call("offer_slots")],
               "New ETA 12:00 PM.\n1. 11:00 AM - Jaipur Dock 1\n2. 12:00 PM - Jaipur Dock 4 (heavy)\n3. 1:00 PM - Jaipur Dock 1")
    r = AgentService(conn, llm=s).handle("a6", "+919800000001", "2 hrs late", T("08:30"))
    assert r["reply_source"] == "template" and "11:00 AM" not in r["reply"]


def test_step_limit_escalates(conn):
    s = Script(*[[call("get_booking_status")] for _ in range(MAX_STEPS)])
    r = AgentService(conn, llm=s).handle("a7", "+919800000001", "loop", T("08:30"))
    assert r["action"] == "escalate" and len(r["tool_calls"]) == MAX_STEPS


def test_llm_failure_still_answers(conn):
    class Down:
        def chat_tools(self, *a, **k):
            raise TimeoutError
    r = AgentService(conn, llm=Down()).handle("a8", "+919800000001", "2 hrs late", T("08:30"))
    assert r["action"] == "escalate" and r["reply"]


def test_bad_arguments_do_not_crash(conn):
    s = Script([call("report_delay", delay_minutes=99999)], [call("offer_slots", earliest="tomorrow")],
               "How late are you exactly?")
    r = AgentService(conn, llm=s).handle("a9", "+919800000001", "very late", T("08:30"))
    assert all("error" in c["result"] for c in r["tool_calls"])
    assert r["action"] == "ask_eta"
