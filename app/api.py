"""HTTP layer. Run with:  uvicorn app.api:app --reload
(needs `pip install fastapi uvicorn`)."""
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .db import connect, init_db
from .service import ConversationService
from . import tools

app = FastAPI(title="SetuHaul driver delay service")
init_db()


class MessageIn(BaseModel):
    conversation_id: str
    phone: str
    text: str
    now: str | None = None   # override clock for demos on the 2026-08-04 snapshot
    mode: str | None = None  # "workflow" | "agent"; default from AGENT_MODE env


def _service(conn, mode: str | None):
    import os
    mode = mode or ("agent" if os.getenv("AGENT_MODE", "").lower() in ("agent", "tools") else "workflow")
    if mode == "agent":
        try:
            from .agent_mode import AgentService
            return AgentService(conn)
        except RuntimeError:
            pass               # no tool-calling LLM configured: fall back to the workflow
    return ConversationService(conn)


UI_FILE = Path(__file__).resolve().parent / "static" / "index.html"


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def ui():
    return UI_FILE.read_text(encoding="utf-8")


@app.get("/drivers")
def drivers():
    """Shipments with their driver phone, for the demo UI."""
    conn = connect()
    try:
        rows = conn.execute("""
            SELECT sh.id, sh.facility_id, sh.status, sh.needs_reefer, sh.load_weight_kg,
                   sh.unload_minutes, d.name AS driver, d.phone, v.reg_no
            FROM shipments sh JOIN drivers d ON d.id = sh.driver_id
            LEFT JOIN vehicles v ON v.id = sh.vehicle_id ORDER BY sh.id""").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/board/{facility_id}")
def board(facility_id: str, now: str):
    """Every slot at a facility with its state: booked, held, blocked, past or free."""
    conn = connect()
    try:
        docks = [dict(r) for r in conn.execute(
            "SELECT id, name, supports_reefer, max_weight_kg, active FROM docks "
            "WHERE facility_id=? ORDER BY id", (facility_id,))]
        slots = conn.execute("""
            SELECT s.id, s.dock_id, s.start_at, s.end_at,
              (SELECT a.shipment_id FROM appointments a WHERE a.slot_id=s.id AND a.status='active') AS booked_by,
              (SELECT h.shipment_id FROM slot_holds h WHERE h.slot_id=s.id AND h.status='active'
                 AND h.expires_at > :now) AS held_by,
              (SELECT b.reason FROM dock_blocks b WHERE b.dock_id=s.dock_id
                 AND b.start_at < s.end_at AND b.end_at > s.start_at) AS blocked
            FROM slots s JOIN docks d ON d.id = s.dock_id
            WHERE d.facility_id = :fac AND s.end_at > s.start_at
            ORDER BY s.dock_id, s.start_at""",
            {"fac": facility_id, "now": now}).fetchall()
        return {"docks": docks, "slots": [dict(r) for r in slots]}
    finally:
        conn.close()


@app.get("/health")
def health():
    from .agent import ACTIVE, get_extractor
    get_extractor()                      # refresh which reader is active
    from .agent import get_llm, get_phraser
    llm = get_llm()
    return {"status": "ok", "message_reader": ACTIVE["name"], "model": ACTIVE["model"],
            "llm_replies": get_phraser() is not None,
            "agent_mode_available": bool(llm and hasattr(llm, "chat_tools"))}


# ---- evaluation on the server (uses the server's LLM key; capped) ----
import threading
import time as _time

EVAL = {"status": "idle", "mode": None, "started": None, "result": None, "error": None}
_eval_lock = threading.Lock()


@app.post("/admin/eval")
def start_eval(mode: str = "workflow", rounds: int = 3):
    """Run the ladder + race for one mode in the background. Max 5 rounds,
    one run at a time, at most one LLM-mode run every 5 minutes."""
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
            from pathlib import Path
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            import eval as ev
            res = ev.run_all(mode, rounds)
            EVAL.update(status="done", result=res)
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


@app.post("/reset")
def reset():
    """Rebuild the stand-in snapshot (demo use only)."""
    from .db import DEFAULT_DB
    for suffix in ("", "-wal", "-shm"):
        p = DEFAULT_DB.with_name(DEFAULT_DB.name + suffix)
        if p.exists():
            p.unlink()
    init_db()
    return {"status": "reset"}


@app.get("/schedule/{facility_id}")
def schedule(facility_id: str, now: str):
    from .scheduler import propose_schedule
    conn = connect()
    try:
        return propose_schedule(conn, facility_id, now)
    finally:
        conn.close()


@app.post("/message")
def message(body: MessageIn):
    conn = connect()          # one connection per request
    try:
        now = body.now or datetime.now().strftime(tools.FMT)
        svc = _service(conn, body.mode)
        result = svc.handle(body.conversation_id, body.phone, body.text, now)
        result.setdefault("mode", getattr(svc, "mode", "workflow"))
        return result
    finally:
        conn.close()


@app.get("/shipments/{shipment_id}/options")
def options(shipment_id: str, earliest: str, latest_end: str | None = None, now: str | None = None):
    conn = connect()
    try:
        return tools.find_options(conn, shipment_id, earliest, latest_end, now=now)
    finally:
        conn.close()
