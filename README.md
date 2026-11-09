# SetuHaul driver delay service

**Live demo:** https://setuhaul-6dsy.onrender.com · [API docs](https://setuhaul-6dsy.onrender.com/docs)
(free plan: the first request after idle can take ~50 s)

Drivers message SetuHaul when they run late. This service reads the
message, finds compatible free dock slots, holds them, and confirms one. It
stays correct when ten drivers want the same slot at the same moment.

## Quick start

```bash
python -m app.db                 # build the stand-in snapshot (setuhaul.db)
python demo.py                   # sample conversations + scheduling tool
python -m pytest -q              # 41 tests
python eval.py                   # measures → reports/eval_report.md
python -m analysis.run_issues    # data issues → reports/data_issues_report.md
```

Only Python 3.10+ and `pydantic` are needed (plus `pytest` for the tests).
Optional extras:
- `pip install fastapi uvicorn`, then `uvicorn app.api:app --reload` for the HTTP API
- Message understanding by an LLM (optional; without a key the offline regex reader is used):
  - **OpenRouter:** set `OPENROUTER_API_KEY` (and optionally `OPENROUTER_MODEL`, default `anthropic/claude-haiku-4.5`). No extra package needed.
  - **Anthropic direct:** `pip install anthropic` and set `ANTHROPIC_API_KEY`.
  - `GET /health` shows which reader is active.

## What's in the box

| Path | What it is |
|---|---|
| `app/agent.py` | Free text → validated fields (Claude or regex fallback) |
| `app/service.py` | Conversation workflow: identify → ETA → offer → confirm |
| `app/tools.py` | `find_options`, `hold_slot`, `confirm`: all capacity logic |
| `app/scheduler.py` | Optional facility-wide scheduling tool |
| `app/db.py` | 18-table schema, unique indexes, stand-in snapshot |
| `app/api.py` | FastAPI endpoints |
| `app/static/index.html` | Web UI: driver chat, live dock board, scheduling tool |
| `analysis/data_issues.sql` | 15 data-quality checks, each a query |
| `analysis/run_issues.py` | Runs the checks, writes the evidence report |
| `eval.py` | Ladder scenarios + concurrency race, writes the measures report |
| `tests/` | Tools, scenarios, scheduler, data issues, concurrency |
| `reports/` | Generated reports |
| `DECISIONS.md` | Every design choice with reason and evidence |
| `ARCHITECTURE.md` | Diagrams, flow, tables |

## Results on the stand-in snapshot

- **Data issues:** 15 of 15 checks find evidence (see `reports/data_issues_report.md`).
- **Ladder:** 12 of 12 scenarios pass, from a simple delay up to a shared phone number.
- **Race:** 300 simultaneous conversations (30 rounds × 10 drivers). Zero slots
  promised twice, zero false promises, zero double bookings.

## About the database

The FDE Academy provides the real SQLite snapshot (4 Aug 2026, 18 tables,
2 facilities, 9 docks, 21 shipments). Until it arrives, `app/db.py` builds a
stand-in with the same shape and planted data problems. To switch:

1. Point `DEFAULT_DB` in `app/db.py` at the real file. Trim `SCHEMA` down to the
   tables the service adds (`slot_holds`, `conversations`, `messages`,
   `outbound_confirmations`, `appointment_history` if the real file has none),
   then call `init_db(seed=False)`.
2. Rename tables and columns in the SQL in `app/tools.py` and `analysis/data_issues.sql`.
3. Rerun `python -m analysis.run_issues path/to/real.db` and the tests.

## Run the API locally

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate     Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.api:app --reload
```
Open http://127.0.0.1:8000 for the web UI (driver chat, dock board, scheduler), or http://127.0.0.1:8000/docs and try `POST /message`:
```json
{"conversation_id": "c1", "phone": "+919800000001",
 "text": "Stuck near Kotputli, 2 hrs late", "now": "2026-08-04 08:30"}
```
Then send `"text": "2"` with `"now": "2026-08-04 08:31"` to confirm.
Other endpoints: `GET /health`, `POST /reset`, `GET /schedule/AMD-WH?now=2026-08-04 13:55`,
`GET /shipments/SH-01/options?earliest=2026-08-04 12:00&now=2026-08-04 08:00`.

## Deploy (Render, free tier)

1. Push this folder to a GitHub repository.
2. On render.com: New → Blueprint → pick the repo (it reads `render.yaml`).
3. Optionally set `ANTHROPIC_API_KEY` in the service's environment.
4. Open `https://<your-service>.onrender.com/docs`.

The SQLite file lives on the container's disk, so it resets on every redeploy
or restart. That's fine for a demo; production would use PostgreSQL.
