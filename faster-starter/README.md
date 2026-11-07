# FastER Docker starter

A single-container, mobile-responsive web dashboard and FastAPI backend. It runs immediately with synthetic CSV snapshots and can later read the same aggregate contract from Databricks. Gemini explanations are optional. Fish Audio is an extension point, not implemented in this starter.

This is a programming scaffold for a hackathon demonstration. It does not assess symptoms, perform medical triage, identify clinically appropriate destinations, or predict real hospital waits. All hospitals, travel times, and records are fictional. Databricks mode is also intended for synthetic/test data only.

## Run in Docker

Install/start Docker Desktop, unzip this folder, and open a terminal inside it:

```bash
cp .env.example .env
docker compose up --build
```

Open http://localhost:8000. Stop with Ctrl+C, then `docker compose down`.

The image includes both the HTML/CSS/JavaScript frontend and Python API. No Node installation, database server, or external keys are needed for local mode. Credentials are runtime environment variables; `.env` is excluded from the image and Git. The published port binds to localhost. The demo mutation endpoint has no authentication; do not expose this starter publicly.

To build/run without Compose:

```bash
docker build -t faster-demo:local .
docker run --rm -p 127.0.0.1:8000:8000 --env-file .env faster-demo:local
```

You can run the image with this command on an ordinary Docker host. Each process startup in local demo mode generates fresh example snapshots; edits are deliberately temporary. To regenerate CSV files for upload:

```bash
python scripts/generate_data.py
```

Use one worker in this scaffold: local CSV mutation is process-locked, not multi-process transactional storage.

## What to try

1. The initial general comparison selects Demo Hillcrest: 18 fixed travel minutes + 24 illustrative wait minutes = 42 minutes.
2. Choose Demo Hillcrest in the staffing panel and set on-shift clinicians to 0. Apply the change.
3. Hillcrest becomes unranked and Demo Riverside has the lowest total. Zero capacity is not treated as zero wait.
4. Choose the pediatric filter. Riverside is excluded because it lacks that demo capability.
5. Request an explanation. Without Gemini configuration, an explicitly labeled template works.
6. Inspect the backend JSON. It contains aggregate hospital counts, not patient or staff identifiers.

The calculation is `ceil(waiting_visits * service_minutes / on_shift_clinicians)`. `service_minutes` is an invented 12-minute interval between new assessments per clinician, NOT a measured average visit duration. In-care visits are excluded from the queue but their residual work is not modeled. On-shift clinicians are not necessarily free. Triage, admission boarding, beds, nurses, and treatment complexity are not modeled. The result is an engineering demonstration, not a validated predictor. Travel times are fixed examples, not a routing API.

## How the data fits together

Each source is a separate table. Join using a canonical `hospital_id`, not hospital names. A real ingestion adapter must map source facility codes to that ID.

| CSV | Grain | Essential fields |
| --- | --- | --- |
| hospitals.csv | One hospital per complete snapshot | hospital_id, name, travel_minutes, capabilities, service_minutes, observed_at |
| visits.csv | One visit state per update | visit_id, hospital_id, status, arrival_at, updated_at |
| staff.csv | One staff state per update | staff_id, hospital_id, role, on_shift, updated_at |

The sample generator writes complete snapshots with timezone-aware UTC timestamps. Visits have WAITING, IN_CARE, or DISCHARGED states. Staff roles are clinician and nurse; only clinician counts are used by the toy formula. Staff counts mean on shift, not idle or guaranteed available. Capabilities are pipe-separated strings.

Local mode reads the snapshots, keeps the latest update per visit/staff ID, aggregates the records, and returns the API contract. The Databricks notebook performs the corresponding batch processing with raw, clean, and aggregate Delta tables.

Never join raw patient visits directly to raw staff rows and then count: 10 visits joined to 4 staff can produce 40 rows. Aggregate visits and staff independently by hospital first; then join those two summaries.

For real event feeds, preserve raw history and handle late updates, discharge events, staff shift endings, missing data, and source outages explicitly. A quiet source is not proof of an empty hospital. The batch demo assumes complete snapshots; a production feed needs separate source heartbeats and completeness checks. These are future integration requirements, not features provided here.

## Connect Databricks

1. Run `python scripts/generate_data.py` to create the sample CSVs. Run it just before upload so source timestamps are fresh.
2. Import `databricks_pipeline.py` into your Databricks workspace as a Python notebook.
3. Set the notebook's catalog widget to an existing writable catalog. The default `workspace` may need changing for your workspace. The notebook creates the `faster` schema and `incoming` volume if permitted.
4. Run the first notebook cell, then upload all three CSVs into `/Volumes/<catalog>/faster/incoming/` through the Catalog UI. Upload these controlled synthetic snapshots as one batch and run downstream cells after all uploads complete.
5. Run the remaining cells. They create bronze raw tables, silver cleaned tables, rejection tables, and `gold_hospital_status`.
6. Select an available SQL warehouse and obtain its warehouse ID. Configure an appropriate workspace credential (the starter uses bearer-token authentication). The credential needs permission to use the warehouse and SELECT the Gold table, with USE CATALOG / USE SCHEMA as needed. Use the workspace-supported authentication method; do not weaken account controls to obtain a token.
7. Edit `.env`:

```dotenv
DATA_MODE=databricks
DATABRICKS_HOST=https://your-workspace-host
DATABRICKS_TOKEN=your-server-side-workspace-token
DATABRICKS_WAREHOUSE_ID=your-warehouse-id
DATABRICKS_GOLD_TABLE=workspace.faster.gold_hospital_status
```

8. Recreate the container so it receives the new environment:

```bash
docker compose up --build --force-recreate
```

The backend sends a fixed SELECT to `/api/2.0/sql/statements` with INLINE JSON_ARRAY results. It checks SQL execution state as well as HTTP success. Connected mode never silently falls back to demo CSVs if the query fails. Staff controls are disabled in connected mode. To change connected results, regenerate/upload the synthetic snapshots and rerun the notebook. Nothing is pushed from this demo's staff control to Databricks.

This is deliberately batch ingestion, not a live production feed. Auto Loader/Lakeflow can be a later step for new files. Re-running the notebook replaces the current tables. Do not rerun its overwrite steps on a production dataset.

Gold table contract:

| Column | Type | Meaning |
| --- | --- | --- |
| hospital_id | STRING | Canonical facility ID |
| name | STRING | Fictional display name |
| travel_minutes | DOUBLE | Fixed demo travel duration |
| capabilities | STRING | e.g. general\|imaging |
| service_minutes | DOUBLE | Invented assessment-start interval |
| queue_count | BIGINT | Waiting visit count |
| active_clinicians | BIGINT | On-shift clinician count |
| observed_at | STRING | UTC ISO 8601 source snapshot time |

The app suppresses estimates after 15 minutes. This is a chosen demo threshold, not a clinical standard. Refreshing the page or rerunning the notebook does not make old source data fresh. Generate new synthetic snapshots to refresh the source timestamps. Real feeds must preserve original observation times.

## Connect Gemini

Add a Gemini API key and an enabled, generateContent-compatible model ID to `.env`, then recreate the container:

```dotenv
GEMINI_API_KEY=your-server-side-key
GEMINI_MODEL=your-enabled-model-id
```

The API uses the REST `generateContent` endpoint. It sends aggregate comparison JSON, not raw visit/staff records, and asks Gemini for a short explanation. It falls back to a template on a provider failure. The interface labels the explanation source.

Gemini does not store the hospital data, calculate the rankings, certify hospital suitability, or infer medical urgency here. Prompt instructions do not guarantee factual correctness. The numeric cards remain the authoritative backend result. A deployed explanation system would need output validation and clinical review appropriate to its scope. Choose synthetic data while testing; do not send real health records to external model services without an approved data-handling setup.

An agent can be added later using function calls such as `get_hospital_status` and `compare_hospitals`. Validate all tool arguments on the server; avoid unrestricted model-generated SQL. A bounded query/API contract is enough for this prototype.

## Add Fish Audio later

Add a server-side `/api/speech` endpoint that takes the approved explanation text, calls Fish Audio's TTS API, and returns an audio response. Play that response using an HTML audio element. Keep the Fish key on the server. Patient spoken input is a separate speech-to-text step; TTS alone does not listen or transcribe. This starter does not include a Fish Audio integration and should not be submitted as if it does.

## API endpoints

| Method | Route | Purpose |
| --- | --- | --- |
| GET | / | Dashboard |
| GET | /api/health | Application liveness, not upstream health |
| GET | /api/hospitals?capability=general | Aggregate comparisons |
| POST | /api/demo/staff | Change synthetic local staffing only |
| POST | /api/explain | Gemini or template explanation |
| GET | /docs | Interactive FastAPI API reference |

## Local development and verification

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

For tests, install httpx (already in requirements) and run:

```bash
python -m unittest discover -s tests -v
```

Verified locally: eight API/logic tests covering queue aggregation, staffing-driven comparison change, capability filtering, stale source handling, zero staffing, invalid requests, aggregate-only responses, template fallback, and refusal to silently use demo data when Databricks is unavailable. Python and frontend JavaScript syntax were checked. The Databricks notebook and authenticated provider calls require your workspace and credentials and were not executed against live accounts. Docker was unavailable in the authoring environment, so an actual image build was not run. Automated browser rendering was blocked by that environment's process/socket restrictions; the layout still needs a visual check in your browser.

## Reference documentation

- Databricks SQL statements: https://docs.databricks.com/api/statement-execution/v1/execute-statement
- Databricks Auto Loader: https://docs.databricks.com/aws/en/ingestion/cloud-object-storage/auto-loader/
- Gemini API: https://ai.google.dev/gemini-api/docs
- Fish Audio: https://docs.fish.audio/developer-guide/getting-started/quickstart
