"""HTTP layer. Run with:  uvicorn app.api:app --reload"""
import os
import threading
import time as _time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import timeutil as tu
from . import tools
from .db import connect, init_db, reset as reset_db
from .service import ConversationService

app = FastAPI(title="SetuHaul driver delay service")
init_db()
UI_FILE = Path(__file__).resolve().parent / "static" / "index.html"


class MessageIn(BaseModel):
    conversation_id: str
    phone: str
    text: str
    now: str | None = None                 # simulated clock; defaults to 4 Aug 2026 09:30 IST
    mode: str | None = None                # "workflow" | "agent"; default from AGENT_MODE env
    external_message_id: str | None = None  # messaging-provider id, used to drop retries


def _service(conn, mode: str | None):
    mode = mode or ("agent" if os.getenv("AGENT_MODE", "").lower() in ("agent", "tools") else "workflow")
    if mode == "agent":
        try:
            from .agent_mode import AgentService
            return AgentService(conn)
        except RuntimeError:
            pass               # no tool-calling LLM configured: fall back to the workflow
    return ConversationService(conn)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def ui():
    return UI_FILE.read_text(encoding="utf-8")


@app.post("/message")
def message(body: MessageIn):
    conn = connect()          # one connection per request
    try:
        svc = _service(conn, body.mode)
        result = svc.handle(body.conversation_id, body.phone, body.text, body.now or tu.SNAPSHOT_NOW,
                            external_message_id=body.external_message_id)
        result.setdefault("mode", getattr(svc, "mode", "workflow"))
        return result
    finally:
        conn.close()


@app.get("/health")
def health():
    from .agent import ACTIVE, get_extractor, get_llm, get_phraser
    get_extractor()
    llm = get_llm()
    return {"status": "ok", "database": "FDE Academy snapshot 2026-08-04", "message_reader": ACTIVE["name"],
            "model": ACTIVE["model"], "llm_replies": get_phraser() is not None,
            "agent_mode_available": bool(llm and hasattr(llm, "chat_tools"))}


@app.post("/reset")
def reset():
    """Restore the runtime database from the supplied snapshot."""
    reset_db()
    return {"status": "reset"}


@app.get("/drivers")
def drivers():
    """Drivers with their shipments, for the demo UI."""
    conn = connect()
    try:
        out = []
        for d in conn.execute("SELECT * FROM drivers ORDER BY driver_id"):
            ships = tools.shipments_for_driver(conn, d["driver_id"], open_only=False)
            out.append({"driver_id": d["driver_id"], "name": d["driver_name"], "phone": d["phone"],
                        "shipments": [{k: s[k] for k in ("shipment_id", "order_reference", "current_status",
                                                         "destination_facility_id", "required_dock_type",
                                                         "load_weight_kg", "expected_unload_min", "priority_code",
                                                         "reg_no")} for s in ships]})
        return out
    finally:
        conn.close()


@app.get("/board/{facility_id}")
def board(facility_id: str, now: str = tu.SNAPSHOT_NOW):
    """Every slot at a facility with its state, for the dock board."""
    now = tu.norm(now)
    conn = connect()
    try:
        docks = [dict(r) for r in conn.execute("SELECT * FROM docks WHERE facility_id=? ORDER BY dock_code", (facility_id,))]
        slots = conn.execute(f"""
            SELECT sl.slot_id, sl.dock_id, sl.slot_start_ts, sl.slot_end_ts, sl.slot_status, sl.block_reason,
              (SELECT a.shipment_id || ':' || a.appointment_status FROM appointments a
                 WHERE a.slot_id = sl.slot_id AND a.appointment_status IN {tools.ACTIVE_SQL}) AS booking,
              (SELECT h.shipment_id FROM slot_holds h WHERE h.slot_id = sl.slot_id AND h.status = 'ACTIVE'
                 AND h.expires_at > :now) AS held_by,
              (SELECT e.event_type || ': ' || e.reason FROM dock_status_events e WHERE e.dock_id = sl.dock_id
                 AND e.event_type IN {tools.BLOCKING_EVENTS}
                 AND e.event_start_ts < sl.slot_end_ts AND COALESCE(e.event_end_ts,'9999') > sl.slot_start_ts) AS event
            FROM appointment_slots sl WHERE sl.facility_id = :fac ORDER BY sl.dock_id, sl.slot_start_ts""",
                             {"fac": facility_id, "now": now}).fetchall()
        in_dock = [dict(r) for r in conn.execute(
            "SELECT fc.shipment_id, fc.actual_dock_id, fc.unload_start_ts, s.expected_unload_min FROM facility_checkins fc "
            "JOIN shipments s USING(shipment_id) WHERE fc.facility_id=? AND fc.queue_state='IN_DOCK'", (facility_id,))]
        yard = [dict(r) for r in conn.execute(
            "SELECT shipment_id, queue_state, queue_position, gate_in_ts FROM facility_checkins "
            "WHERE facility_id=? AND gate_out_ts IS NULL AND queue_state LIKE 'WAITING%' ORDER BY queue_position",
            (facility_id,))]
        return {"docks": docks, "slots": [dict(r) for r in slots], "in_dock": in_dock, "yard": yard}
    finally:
        conn.close()


@app.get("/schedule/{facility_id}")
def schedule(facility_id: str, now: str = tu.SNAPSHOT_NOW):
    from .scheduler import propose_schedule
    conn = connect()
    try:
        return propose_schedule(conn, facility_id, now)
    finally:
        conn.close()


@app.get("/shipments/{shipment_id}/options")
def options(shipment_id: str, earliest: str, latest_end: str | None = None, now: str = tu.SNAPSHOT_NOW):
    conn = connect()
    try:
        return tools.find_options(conn, shipment_id, earliest, latest_end, now=now)
    finally:
        conn.close()


@app.get("/data-issues")
def data_issues():
    """Run the data-issue checks against the supplied (unmodified) snapshot."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from analysis.run_issues import run
    from .db import PRISTINE_DB
    return [{"n": r["n"], "title": r["title"], "why": r["why"], "rows": len(r["rows"]), "columns": r["cols"],
             "evidence": [list(x) for x in r["rows"][:10]]} for r in run(PRISTINE_DB)]


# ---- evaluation on the server (uses the server's LLM key; capped) ----
EVAL = {"status": "idle", "mode": None, "started": None, "result": None, "error": None}
_eval_lock = threading.Lock()


@app.post("/admin/eval")
def start_eval(mode: str = "workflow", rounds: int = 3):
    """Run the ladder + race for one mode in the background. Max 5 rounds,
    one run at a time, at most one agent-mode run every 5 minutes."""
    if mode not in ("workflow", "agent"):
        return {"error": "mode must be workflow or agent"}
    rounds = max(1, min(rounds, 5))
    with _eval_lock:
        if EVAL["status"] == "running":
            return {"error": "an evaluation is already running", **EVAL}
        if mode == "agent" and EVAL.get("last_agent") and _time.time() - EVAL["last_agent"] < 300:
            return {"error": "agent-mode evaluation was run less than 5 minutes ago"}
        EVAL.update(status="running", mode=mode, started=_time.time(), result=None, error=None)
        if mode == "agent":
            EVAL["last_agent"] = _time.time()

    def work():
        try:
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            import eval as ev
            EVAL.update(status="done", result=ev.run_all(mode, rounds))
        except Exception as e:
            EVAL.update(status="error", error=f"{type(e).__name__}: {e}")
    threading.Thread(target=work, daemon=True).start()
    return {"status": "started", "mode": mode, "rounds": rounds}


@app.get("/admin/eval")
def eval_status():
    out = dict(EVAL)
    if out.get("started") and out["status"] == "running":
        out["running_for_s"] = round(_time.time() - out["started"])
    return out
