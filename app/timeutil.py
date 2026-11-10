"""Time helpers. The database stores ISO-8601 text with a +05:30 offset
('2026-08-04T09:30:00+05:30'). Every row uses the same offset, so string
comparison orders times correctly; arithmetic is done here in Python because
SQLite's datetime() silently converts these values to UTC.
"""
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
SNAPSHOT_DATE = "2026-08-04"
# The snapshot spans 07:00-16:30, so there is no single "now" (data issue 18).
# Requests pass their own clock; this is only the default.
SNAPSHOT_NOW = f"{SNAPSHOT_DATE}T09:30:00+05:30"


def parse(s: str) -> datetime:
    """Accept '...T09:30:00+05:30', '2026-08-04 09:30', '2026-08-04T09:30' or '09:30'."""
    s = s.strip()
    if len(s) <= 5 and ":" in s:
        s = f"{SNAPSHOT_DATE} {s}"
    dt = datetime.fromisoformat(s.replace(" ", "T") if "T" not in s else s)
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)


def iso(dt: datetime) -> str:
    return dt.astimezone(IST).strftime("%Y-%m-%dT%H:%M:%S+05:30")


def norm(s: str) -> str:
    return iso(parse(s))


def add(s: str, minutes: float) -> str:
    return iso(parse(s) + timedelta(minutes=minutes))


def minutes(a: str, b: str) -> float:
    """b - a in minutes."""
    return (parse(b) - parse(a)).total_seconds() / 60


def hhmm(s: str) -> str:
    return parse(s).strftime("%I:%M %p").lstrip("0")


def simple(s: str) -> str:
    """'2026-08-04 09:30', the format the message extractor works in."""
    return parse(s).strftime("%Y-%m-%d %H:%M")


def on_date(date_of: str, hm: str) -> str:
    """'21:00' on the same date as date_of."""
    return f"{date_of[:10]}T{hm}:00+05:30"
