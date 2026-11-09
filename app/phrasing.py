"""LLM-written replies, with code-enforced facts.

The workflow builds the reply facts (action, ETA, options, confirmed slot)
and a safe template. The LLM only rewrites that template in the driver's
language and tone. Before sending, `check()` makes sure the LLM kept every
time, dock name and option number and added no new ones. If anything is off,
or the LLM call fails, the template is sent instead.
"""
import re

PHRASE_PROMPT = """You write WhatsApp replies from SetuHaul Logistics to truck drivers in India.
You get the driver's last message and a DRAFT reply written by our booking system.
Rewrite the draft so it sounds natural and friendly, in the SAME language and script the
driver used (English, Hindi in Devanagari, or Hinglish in Latin letters).

Hard rules:
- Keep every time EXACTLY as written in the draft (e.g. "12:00 PM"), including AM/PM.
- Keep every dock name EXACTLY as written (e.g. "Jaipur Dock 3 (reefer)"). Do not translate dock names.
- If the draft lists numbered options, keep the same numbers, order and one option per line.
- Do not add, remove or change any fact: no new times, docks, promises, apologies for things
  not in the draft, or instructions not in the draft.
- If the draft says something is confirmed, say confirmed. If it says not available, say so.
- At most 2 short sentences plus the option list. No greetings like "Dear driver". No emojis.
Return only the reply text."""

TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s?(?:AM|PM)\b", re.I)
ANY_CLOCK_RE = re.compile(r"\b\d{1,2}:\d{2}\b")
OPTION_RE = re.compile(r"^\s*(\d)\.", re.M)


def _norm_time(t: str) -> str:
    return re.sub(r"\s+", " ", t.upper().replace(" ", " ")).strip()


def facts_from(result: dict, template: str) -> dict:
    """What the LLM reply must contain, taken from the template and the result."""
    docks = sorted({o["dock_name"] for o in result.get("options", [])})
    m = re.search(r"at (.+?)\. Please report", template)       # confirmed reply
    if m:
        docks.append(m.group(1))
    return {"times": sorted({_norm_time(t) for t in TIME_RE.findall(template)}),
            "docks": docks,
            "options": OPTION_RE.findall(template),
            "truck_regs": re.findall(r"\b[A-Z]{2}\d{2}[A-Z]{2}\d{3,4}\b", template)}


def check(reply: str, facts: dict) -> tuple[bool, str]:
    """Return (ok, reason). The reply must keep all facts and invent none."""
    if not reply or len(reply) > 700:
        return False, "empty or too long"
    times = {_norm_time(t) for t in TIME_RE.findall(reply)}
    for t in facts["times"]:
        if t not in times:
            return False, f"missing time {t}"
    extra = times - set(facts["times"])
    if extra:
        return False, f"invented time {sorted(extra)}"
    if len(ANY_CLOCK_RE.findall(reply)) > len(TIME_RE.findall(reply)):
        return False, "time without AM/PM"
    for d in facts["docks"]:
        if d not in reply:
            return False, f"missing dock {d}"
    if OPTION_RE.findall(reply) != facts["options"]:
        return False, "option numbering changed"
    for r in facts["truck_regs"]:
        if r not in reply:
            return False, f"missing truck {r}"
    return True, "ok"


def phrase(llm, driver_text: str, result: dict) -> dict:
    """Rewrite result['reply'] with the LLM if it passes the check."""
    template = result["reply"]
    result["template_reply"] = template
    result["reply_source"] = "template"
    if llm is None:
        return result
    try:
        draft = llm.chat(PHRASE_PROMPT, f"Driver's message: {driver_text}\n\nDRAFT reply:\n{template}",
                         max_tokens=250).strip().strip('"')
    except Exception as e:                       # network, quota, timeout
        result["phrase_check"] = f"llm error: {type(e).__name__}"
        return result
    ok, reason = check(draft, facts_from(result, template))
    result["phrase_check"] = reason
    if ok:
        result["reply"] = draft
        result["reply_source"] = "llm"
    return result
