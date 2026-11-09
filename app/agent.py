"""Turns a free-text driver message into structured fields.

Two extractors with the same output:
  * LLMExtractor   - uses Claude if the `anthropic` package and
                     ANTHROPIC_API_KEY are available.
  * rule_extract   - regex fallback, so the service and tests run offline.

The output is validated with Pydantic either way, and nothing here
touches the database. The LLM understands text; it never decides capacity.
"""
import json
import os
import re
from datetime import datetime, timedelta

from pydantic import BaseModel, Field, ValidationError

FMT = "%Y-%m-%d %H:%M"


class Extraction(BaseModel):
    intent: str = Field("unknown", description="report_delay | choose_option | confirm | ask_status | unknown")
    delay_minutes: int | None = None
    new_eta: str | None = None            # 'YYYY-MM-DD HH:MM' if driver gave a clock time
    earliest: str | None = None           # "anything after 7"
    latest_end: str | None = None         # "need to leave by 9"
    choice_index: int | None = None       # 1-based: "the second one" -> 2


# ------------------------------------------------------------ rule based

_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "half": 0.5, "an": 1, "a": 1}
_ORDINAL = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3}


def _clock(hour: int, minute: int, ampm: str | None, now: datetime) -> str:
    """Resolve '7' / '7pm' / '19:30' to the next matching time after now."""
    if ampm:
        hour = hour % 12 + (12 if ampm.lower() == "pm" else 0)
        cand = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    else:
        cand = now.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)
        if cand < now and hour < 12:
            cand += timedelta(hours=12)     # "after 7" said at 2pm means 7pm
    if cand < now:
        cand += timedelta(days=1)
    return cand.strftime(FMT)


_TIME = r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?"


def rule_extract(text: str, now: str) -> Extraction:
    t = text.lower()
    n = datetime.strptime(now, FMT)
    out = Extraction()

    # delay: "2 hrs", "1.5 hours", "two hours", "45 min"
    m = re.search(r"\b(\d+(?:\.\d+)?|one|two|three|four|half|an|a)\s*(?:hrs?|hours?|h\b)", t)
    if m:
        v = m.group(1)
        out.delay_minutes = int(float(_WORD_NUM.get(v, v)) * 60)
    m = re.search(r"(\d+)\s*(?:mins?|minutes?)", t)
    if m:
        out.delay_minutes = (out.delay_minutes or 0) + int(m.group(1))

    m = re.search(r"(?:reach|arrive|there)\w*\s+(?:by|at|around)\s+" + _TIME, t)
    if m:
        out.new_eta = _clock(int(m.group(1)), int(m.group(2) or 0), m.group(3), n)
    m = re.search(r"(?:after|from)\s+" + _TIME, t)
    if m:
        out.earliest = _clock(int(m.group(1)), int(m.group(2) or 0), m.group(3), n)
    m = re.search(r"(?:leave|done|finish|out)\s+by\s+" + _TIME, t)
    if m:
        out.latest_end = _clock(int(m.group(1)), int(m.group(2) or 0), m.group(3), n)

    for word, idx in _ORDINAL.items():
        if re.search(rf"\b{word}\b", t):
            out.choice_index = idx
    m = re.fullmatch(r"\s*(?:option\s*)?([1-3])\s*", t)
    if m:
        out.choice_index = int(m.group(1))

    if out.choice_index:
        out.intent = "choose_option"
    elif out.delay_minutes or out.new_eta or "late" in t or "delay" in t or "stuck" in t:
        out.intent = "report_delay"
    elif re.search(r"\b(yes|ok|okay|haan|fine|sure)\b", t):
        out.intent = "confirm"
    elif "slot" in t or "status" in t:
        out.intent = "ask_status"
    return out


# ------------------------------------------------------------ LLM based

SYSTEM_PROMPT = """You extract fields from a truck driver's message to a logistics company.
Return ONLY a JSON object with keys: intent, delay_minutes, new_eta, earliest, latest_end, choice_index.
intent is one of report_delay, choose_option, confirm, ask_status, unknown.
Times must be absolute 'YYYY-MM-DD HH:MM' in Asia/Kolkata, resolved against the given current time.
Use null for anything the driver did not say. Never guess facts the driver did not state."""


class LLMExtractor:
    def __init__(self, model: str | None = None):
        import anthropic  # noqa: imported lazily so offline runs work
        self.client = anthropic.Anthropic()
        self.model = model or os.getenv("SETUHAUL_MODEL", "claude-haiku-5-5")

    def __call__(self, text: str, now: str, offered: list[dict] | None = None) -> Extraction:
        context = f"Current time: {now}\n"
        if offered:
            context += "Options already offered (1-based): " + json.dumps(
                [o["start_at"] + " " + o["dock_name"] for o in offered]) + "\n"
        msg = self.client.messages.create(
            model=self.model, max_tokens=300, system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": context + "Driver message: " + text}])
        raw = msg.content[0].text.strip().removeprefix("```json").removesuffix("```")
        return Extraction.model_validate_json(raw)


def get_extractor():
    """Use Claude when available, otherwise the regex fallback."""
    if os.getenv("ANTHROPIC_API_KEY"):
        try:
            llm = LLMExtractor()

            def extract(text, now, offered=None):
                try:
                    return llm(text, now, offered)
                except (ValidationError, Exception):   # bad JSON or API error
                    return rule_extract(text, now)
            return extract
        except ImportError:
            pass
    return lambda text, now, offered=None: rule_extract(text, now)
