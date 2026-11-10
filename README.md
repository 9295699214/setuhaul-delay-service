# SetuHaul driver delay service

**Live demo:** https://setuhaul-6dsy.onrender.com · [API docs](https://setuhaul-6dsy.onrender.com/docs)
(free plan: the first request after idle can take about 50 seconds)

Drivers message SetuHaul when they run late. This service reads the message, finds dock
slots the truck can really use, holds them, and confirms one. It stays correct when several
drivers want the same slot at once. It runs on the FDE Academy snapshot
(`data/setuhaul_freight_operations.db`, 4 Aug 2026, Asia/Kolkata).

## Quick start

```bash
pip install -r requirements.txt
python -m pytest -q              # 75 tests on the real snapshot
python eval.py                   # 16 seeded cases + race -> reports/eval_report.md
python -m analysis.run_issues    # 23 data checks -> reports/data_issues_report.md
uvicorn app.api:app --reload     # web UI at http://127.0.0.1:8000, API at /docs
```

Optional LLM (without it, an offline reader is used and everything else works the same):
- **OpenRouter:** `OPENROUTER_API_KEY` (and optionally `OPENROUTER_MODEL`, default
  `anthropic/claude-haiku-4.5`). It reads messages, words replies in the driver's language
  (checked against the facts), and enables agent mode.
- **Anthropic direct:** `pip install anthropic` and set `ANTHROPIC_API_KEY`.

## Deliverables

| | Where |
|---|---|
| Data issues, each proven with a query | `analysis/data_issues.sql` (23 checks, 18 find problems) → `reports/data_issues_report.md`, `GET /data-issues` |
| Driver conversation service | `app/service.py` (workflow), `app/agent_mode.py` (LLM tool calling), `POST /message`, web UI |
| Concurrency proof | `tests/test_concurrency.py`, race in `eval.py` |
| Scheduling tool (optional) | `app/scheduler.py`, `GET /schedule/{facility}` |
| Evaluation on the guide's seeded cases | `eval.py` → `reports/eval_report.md` |
| Design decisions (incl. the guide's 7 questions) | `DECISIONS.md` |
| Architecture | `ARCHITECTURE.md` |

## What's in the box

| Path | Role |
|---|---|
| `app/desk.py` | Every action on a conversation; writes chat, ETA, exception, appointment and message rows |
| `app/tools.py` | Slot search with facility rules, holds, atomic confirm |
| `app/service.py` | Workflow mode: identify → on site? → ETA → offer → confirm |
| `app/agent_mode.py` | Agent mode: the LLM picks tools; code guards each one |
| `app/agent.py` | Message reading (OpenRouter / Anthropic / offline regex) |
| `app/phrasing.py` | LLM-worded replies, with every time/dock/order checked |
| `app/scheduler.py` | Facility-wide proposal: priorities, yard, in-dock, extensions |
| `app/timeutil.py` | `+05:30` time handling (SQLite's `datetime()` converts to UTC) |
| `app/db.py` | Runtime copy of the snapshot + the two added tables |
| `app/static/index.html` | Web UI: driver chat with the dataset's messages, dock board, scheduler, data issues |

## Results on the snapshot

- **Data:** 18 of 23 checks find real problems. Examples: a confirmed booking on a broken
  dock (APT1005), 21 t on a 20 t dock (APT1014A), three trucks booked to leave another city
  while still in Jaipur, one driver on two moving trucks, an escalation e-mail that FAILED.
- **Seeded cases:** 16 of 16 pass, from the tyre delay (SHP1012) to the cancelled shipment (SHP1019).
- **Race:** 30 rounds × 6 drivers for the same slots: zero promised twice, zero false
  promises, zero double bookings.
- **Live with the LLM on:** workflow 16/16, agent (LLM tool calling) 15/16, both with zero
  false promises; see `DECISIONS.md` and `reports/eval_report.md`.

## Deploy

`render.yaml` and `Dockerfile` are included. The runtime database is a copy of the snapshot
made at start-up, so it resets on every redeploy (`POST /reset` restores it at any time).
