"""Scripted demo on the FDE Academy snapshot (no server, no API key).

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
svc = ConversationService(conn, phraser=None)
T = lambda hhmm: f"2026-08-04T{hhmm}:00+05:30"   # noqa: E731
P = lambda n: f"+91-90000100{n:02d}"              # noqa: E731


def say(cid, n, text, hhmm):
    print(f"\n[{hhmm}] DRV{n:03d}: {text}")
    print("  SetuHaul: " + svc.handle(cid, P(n), text, T(hhmm))["reply"].replace("\n", "\n            "))


print("=" * 72, "\nDriver conversations (the guide's seeded messages)\n" + "=" * 72)
say("c3", 3, "I am already at the gate. Can I get a dock now instead of waiting for 9?", "08:22")
say("c5", 5, "Dock D3 is not working. What should I do?", "09:20")
say("c12", 12, "Tyre repaired. I can reach 11:10. I need to leave the warehouse before 1:30 for another pickup.", "09:29")
say("c12", 12, "1", "09:30")
say("c7", 7, "Heavy trailer ETA 12:00. Original heavy-bay slot will be missed. What is next?", "09:39")
say("c7", 7, "MH04HV4107", "09:40")
say("c14", 14, "Origin released me late. ETA 11:25. This load is urgent. Can I get the first possible dock?", "09:44")
say("c8", 8, "Is my 4 PM slot still active?", "10:12")
say("c15", 15, "Evening traffic. ETA 6:30. Can the reefer unload tonight?", "16:24")

print("\n" + "=" * 72, "\nScheduling tool: Jaipur at 10:15\n" + "=" * 72)
print(format_schedule(propose_schedule(conn, "FAC-JAI-01", T("10:15"))))
