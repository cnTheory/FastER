# FastER

HackRPI demo: a patient care tracker backed by FastAPI and SQLite. The backend from `faster_backend.zip` is integrated into the existing `src/faster` package. The patient tracker, SPARCS archive in `data/sparcs24.csv.xz`, and the newly attached `faster-starter` capacity/history dashboard are retained. The capacity/history dashboard is also integrated at `/capacity/`.

## Run locally

Use Python 3.12 or newer. From the extracted **FastER** directory:

```sh
python -m venv .venv
```

Activate the environment:

```powershell
# Windows PowerShell
.venv\Scripts\Activate.ps1
```

```sh
# macOS / Linux
source .venv/bin/activate
```

Then install and launch:

```sh
python -m pip install -e .
faster
```

Open **http://127.0.0.1:8000** for the tracker and **http://127.0.0.1:8000/docs** for the API. Serve the tracker through this address rather than opening the HTML file directly. FastAPI serves both the page and `/api`, so no separate frontend server is needed.

Alternatively, with uv installed:

```sh
uv sync
uv run faster
```

For auto-reload during development:

```sh
python -m uvicorn faster.app:app --reload
```

The first launch creates and seeds `data/faster.db`. Later launches preserve it. To explicitly delete and regenerate synthetic records:

```sh
python -m faster.init_db --reset
```

## Docker (both dashboards and backend)

From the top-level **FastER** directory, copy `.env.example` to `.env`, then run:

```sh
docker compose up --build
```

Open http://127.0.0.1:8000 for the tracker and http://127.0.0.1:8000/capacity/ for the capacity/history dashboard. SQLite and capacity CSV data are stored in the `faster-data` volume. Demo capacity snapshots reset on startup, matching the supplied starter's behavior; SQLite patients persist. The original `faster-starter/` remains available as a separate standalone scaffold with its original Docker setup. Use the top-level commands for the integrated app. Docker builds were not executed in this environment.

## Capacity and historical dashboard

The supplied capacity UI and history APIs are mounted at **`/capacity/`**. Its API documentation is at `/capacity/docs`; for example, its hospital comparison endpoint is `/capacity/api/hospitals`. The patient API keeps `/api/hospitals` as a hospital directory. Links connect the two pages.

The capacity cards retain the starter's fictional H1/H2/H3 hospitals and CSV snapshots. They are a separate simulation from the patient tracker's SQLite hospitals; staffing edits in one do not alter the other. Databricks history keeps its existing filters, pagination, parameterized SQL, and alphabetical hospital list. Set `DATABRICKS_HOST`, `DATABRICKS_TOKEN`, `DATABRICKS_WAREHOUSE_ID`, and optionally `DATABRICKS_HISTORY_VIEW` to enable history. `DATA_MODE=databricks` additionally switches capacity cards to `DATABRICKS_GOLD_TABLE`. Historical access without credentials returns a clear configuration error.

## Try the demo

1. Select a hospital and sign in with the default demo staff code **staff123**.
2. Choose a patient from the database-backed list.
3. Change their status and click **Save status**. Refresh reloads active patients and hospital totals; admitted/discharged patients leave the active list.
4. For a patient-only view, open `http://127.0.0.1:8000/?patient_id=PAT-...` with an actual patient ID from the staff view. Staff controls and AI insights require a staff session.
5. Try intake, hospital ranking, patient registration, and feed updates through `/docs`.

Staff sessions are kept in browser memory and expire after eight hours. Signing out revokes the token. A backend restart invalidates all sessions.

## Configuration and optional integrations

Copy `.env.example` to `.env` if you want to change settings. All integrations are optional; an empty `.env` works. A blank `DB_PATH` uses `data/faster.db`; relative paths are resolved from the project directory. `HOST` and `PORT` configure the `faster` command.

- **Offline:** SQLite, synthetic CSV outcomes, and keyword intake/ranking explanations work without credentials.
- **Gemini:** `python -m pip install -e ".[ai]"`, then set `GEMINI_API_KEY` and, optionally, `GEMINI_MODEL`. Clinical summaries require a configured AI provider; failures produce a visible unavailable message.
- **Databricks SQL:** `python -m pip install -e ".[databricks]"`, then set `DATABRICKS_HOST`, `DATABRICKS_TOKEN`, and `DATABRICKS_HTTP_PATH`. `databricks_gold.sql` contains the supplied SPARCS aggregation query. If SQL is unavailable, the packaged synthetic CSV is used.
- **Databricks-served Gemini:** install the AI extra and configure the host, token, and the model supported by your workspace. This integration is preserved from the supplied backend and requires verification in your workspace.
- **Staff/feed:** set `STAFF_CODE` and `LIVE_FEED_KEY` to override the demo defaults.
- **Separate frontends:** list their origins in comma-separated `CORS_ORIGINS`. The included page uses same-origin `/api` calls.

## API

| Method | Path | Access / purpose |
| --- | --- | --- |
| GET | `/health` | Health and outcomes source |
| GET | `/api/hospitals` | Hospital IDs and names |
| POST | `/api/intake` | Text to structured symptoms; offline keyword fallback |
| POST | `/api/triage` | Estimated severity and hospital ranking |
| POST | `/api/incoming` | Record an expected arrival |
| GET | `/api/patient/{patient_id}` | Patient tracking link |
| POST | `/api/live/{hospital_id}` | `X-Feed-Key`; staff count and optional five-level queue |
| POST | `/api/staff/login` | Hospital ID + shared demo code; returns token |
| POST | `/api/staff/logout` | Bearer token; revoke session |
| GET | `/api/staff/dashboard` | Bearer token; hospital-scoped patients, beds, incoming |
| POST | `/api/staff/patients` | Bearer token; register a patient |
| POST | `/api/staff/update-status` | Bearer token; update own hospital's patient |
| GET | `/api/staff/patient/{patient_id}/ai-summary` | Bearer token; staff-only summary |

Older `/api/patients` and `/api/patient/{patient_id}/ai-summary` paths remain as hospital-scoped, staff-authenticated compatibility routes. The tracker now uses the canonical staff endpoints. Fixed `PAT-001` selectors are replaced by real generated IDs. A discharge is handled as a terminal outcome without implying inpatient transfer.

Queues derive from patients in TRIAGE. A live-feed override lasts 15 minutes. Incoming entries expire after three hours. Bed occupancy is seeded demo data; changing patient status updates the boarding total but does not assign or release a specific bed.

The old `er_flow.db` schema is not automatically migrated. This backend uses a new hospital-scoped database; keep any old database separately.

## Validation

```sh
python -m pip install -e ".[dev]"
python -m pytest -q
```

Tests cover startup, offline intake/ranking, registration/status changes, derived queues, feed expiry, staff sessions, cross-hospital restrictions, and AI error handling. JavaScript syntax and wheel asset inclusion were also checked. An interactive browser check could not be completed because the browser download was unavailable. Capacity comparison and historical query routes are additionally exercised through the mounted app with mocked Databricks responses. External Gemini/Databricks calls require credentials and were not exercised as part of integration.

## Demo data and limits

This is a hackathon prototype with synthetic patients, mock admission predictions, synthetic outcome indices, approximate travel times, and an unvalidated scoring heuristic inherited from the supplied backend. The SPARCS archive is retained as source material; local rankings use the packaged CSV unless Databricks is configured. Seed hospital details, facility IDs, and insurance lists require replacement/verification before real use. Shared staff codes and public patient tracking links are demo access controls. Use synthetic data for the demo.
