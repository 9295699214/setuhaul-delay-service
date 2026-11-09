"""HTTP layer. Run with:  uvicorn app.api:app --reload
(needs `pip install fastapi uvicorn`)."""
from datetime import datetime

from fastapi import FastAPI
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


@app.get("/health")
def health():
    return {"status": "ok"}


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
        return ConversationService(conn).handle(body.conversation_id, body.phone, body.text, now)
    finally:
        conn.close()


@app.get("/shipments/{shipment_id}/options")
def options(shipment_id: str, earliest: str, latest_end: str | None = None, now: str | None = None):
    conn = connect()
    try:
        return tools.find_options(conn, shipment_id, earliest, latest_end, now=now)
    finally:
        conn.close()
