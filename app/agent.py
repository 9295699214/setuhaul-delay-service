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

from pydantic import BaseModel, Field

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
Drivers write in English, Hindi or Hinglish. Return ONLY a JSON object with keys:
intent, delay_minutes, new_eta, earliest, latest_end, choice_index.

- intent: report_delay | choose_option | confirm | ask_status | unknown
- delay_minutes: integer, when the driver says HOW LATE they are ("2 hrs late",
  "do ghante late", "45 min der"). Do NOT convert this into new_eta; the backend
  adds it to the planned arrival time, which you do not know.
- new_eta: only when the driver states a CLOCK TIME of arrival ("reach by 3pm",
  "8:15 tak pahunchunga"). Otherwise null.
- earliest: start of a window the driver accepts ("anything after 7", "1 baje ke baad").
- latest_end: when the driver must be finished ("leave by 9", "10 se pehle nikalna hai").
- choice_index: 1-based option the driver picks ("2", "the second one", "doosra wala").
Clock times are absolute 'YYYY-MM-DD HH:MM' in Asia/Kolkata, resolved against the given
current time; a bare hour means the next such time after now. Use null for anything the
driver did not say. Never guess facts the driver did not state."""


def _user_content(text: str, now: str, offered: list[dict] | None) -> str:
    context = f"Current time: {now}\n"
    if offered:
        context += "Options already offered (1-based): " + json.dumps(
            [o["start_at"] + " " + o["dock_name"] for o in offered]) + "\n"
    return context + "Driver message: " + text


def _parse(raw: str) -> Extraction:
    """Models sometimes wrap JSON in prose or ``` fences; keep the first {...}."""
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError("no JSON object in model reply")
    return Extraction.model_validate_json(m.group(0))


class LLMExtractor:
    """Claude through the Anthropic SDK (needs `pip install anthropic`)."""
    name = "anthropic"

    def __init__(self, model: str | None = None):
        import anthropic  # noqa: imported lazily so offline runs work
        self.client = anthropic.Anthropic()
        self.model = model or os.getenv("SETUHAUL_MODEL", "claude-haiku-5-5")

    def chat(self, system: str, user: str, json_mode: bool = False, max_tokens: int = 300) -> str:
        msg = self.client.messages.create(
            model=self.model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}])
        return msg.content[0].text

    def __call__(self, text: str, now: str, offered: list[dict] | None = None) -> Extraction:
        return _parse(self.chat(SYSTEM_PROMPT, _user_content(text, now, offered), json_mode=True))


class OpenRouterExtractor:
    """Any model on OpenRouter (OpenAI-compatible chat completions API).
    Uses only the standard library, so no extra package is needed."""
    name = "openrouter"
    URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, api_key: str | None = None, model: str | None = None, timeout: float = 15):
        self.api_key = api_key or os.environ["OPENROUTER_API_KEY"]
        self.model = model or os.getenv("OPENROUTER_MODEL", "anthropic/claude-haiku-4.5")
        self.timeout = timeout

    def chat(self, system: str, user: str, json_mode: bool = False, max_tokens: int = 300) -> str:
        import urllib.request
        payload = {"model": self.model, "max_tokens": max_tokens, "temperature": 0,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}]}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(self.URL, data=json.dumps(payload).encode(), method="POST", headers={
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/9295699214/setuhaul-delay-service",
            "X-Title": "SetuHaul driver delay service",
        })
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"]

    def chat_tools(self, messages: list[dict], tools: list[dict], max_tokens: int = 400) -> dict:
        """One tool-calling turn (OpenAI format). Returns the assistant message:
        {"role": "assistant", "content": str|None, "tool_calls": [...]|None}."""
        import urllib.request
        payload = {"model": self.model, "max_tokens": max_tokens, "temperature": 0,
                   "messages": messages, "tools": tools, "tool_choice": "auto"}
        req = urllib.request.Request(self.URL, data=json.dumps(payload).encode(), method="POST", headers={
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/9295699214/setuhaul-delay-service",
            "X-Title": "SetuHaul driver delay service",
        })
        with urllib.request.urlopen(req, timeout=max(self.timeout, 30)) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]

    def __call__(self, text: str, now: str, offered: list[dict] | None = None) -> Extraction:
        return _parse(self.chat(SYSTEM_PROMPT, _user_content(text, now, offered), json_mode=True))


ACTIVE = {"name": "regex", "model": None}


def get_llm():
    """The configured LLM backend, or None (OpenRouter > Anthropic > none)."""
    if os.getenv("OPENROUTER_API_KEY"):
        return OpenRouterExtractor()
    if os.getenv("ANTHROPIC_API_KEY"):
        try:
            return LLMExtractor()
        except ImportError:
            return None
    return None


def get_phraser():
    """LLM that rewrites the code-built reply in the driver's language, or None.
    Turn off with LLM_REPLIES=0."""
    if os.getenv("LLM_REPLIES", "1") == "0":
        return None
    return get_llm()


def get_extractor():
    """Pick the message reader: OpenRouter if OPENROUTER_API_KEY is set, else
    Claude via ANTHROPIC_API_KEY, else the offline regex reader. Any LLM
    failure (network, bad JSON, invalid fields) falls back to regex."""
    llm = get_llm()
    if llm is None:
        ACTIVE.update(name="regex", model=None)
        return lambda text, now, offered=None: rule_extract(text, now)
    ACTIVE.update(name=llm.name, model=llm.model)

    def extract(text, now, offered=None):
        try:
            return llm(text, now, offered)
        except Exception:          # network error, bad JSON, validation error
            return rule_extract(text, now)
    return extract
