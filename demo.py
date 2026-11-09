"""Scripted demo: runs with no web server and no API key.

    python demo.py
"""
import tempfile
from pathlib import Path

from app.db import connect, init_db
from app.scheduler import format_schedule, propose_schedule
from app.service import ConversationService

db = Path(tempfile.mkdtemp()) / "demo.db"
init_db(db)
conn = connect(db)
svc = ConversationService(conn)
T = lambda hhmm: f"2026-08-04 {hhmm}"  # noqa: E731


def say(cid, phone, text, now):
    print(f"\n[{now[-5:]}] DRIVER ({phone[-4:]}): {text}")
    print("SETUHAUL: " + svc.handle(cid, phone, text, now)["reply"].replace("\n", "\n          "))


print("=" * 70, "\nPART 1 - one driver at a time\n" + "=" * 70)
say("c1", "+919800000001", "Stuck near Kotputli, 2 hrs late", T("08:30"))
say("c1", "+919800000001", "the second one", T("08:31"))

say("c3", "+919800000003", "running 1.5 hours late, anything after 1pm", T("09:00"))
say("c3", "+919800000003", "1", T("09:01"))

say("c2", "+919800000002", "around 3 hours late, need to leave by 4pm", T("09:30"))

say("c6", "+919800000006", "45 min late", T("10:00"))
say("c6", "+919800000006", "first", T("10:05"))          # hold expired

say("c15", "+919800000015", "I am 1 hour late", T("08:30"))  # gate says already inside

say("c18", "+919800000018", "late by 1 hour", T("11:00"))   # phone shared by two drivers
say("c18", "+919800000018", "GJ01 AB 1019", T("11:01"))
say("c18", "+919800000018", "1", T("11:02"))

print("\n" + "=" * 70, "\nPART 2 - scheduling tool: early, late and in-progress trucks\n" + "=" * 70)
print(format_schedule(propose_schedule(conn, "AMD-WH", T("13:55"))))
