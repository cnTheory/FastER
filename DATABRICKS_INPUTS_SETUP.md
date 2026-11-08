# Connect the ingested demo tables to FastER

This update is based on the last project archive supplied in this conversation.
The scoring.py formula is unchanged. Only the ranking data path and related API
metadata/guards change. The separate /capacity dashboard is unchanged.

## Files changed
- src/faster/ranking_data.py (new): fetch, decode and validate the Databricks demo inputs.
- src/faster/app.py: connect /api/triage and /api/hospitals to the selected source.
- .env.example: additional optional settings.
- tests/test_databricks_inputs.py and tests/fixtures/demo: integration and failure checks.

Keep your own .env and credentials. If your teammates have edited app.py since the
uploaded archive, merge these changes rather than overwriting their work. Do not
replace the older nested faster-starter/app/main.py: this update targets the root
project's src/faster/app.py.

## Environment
Add these to your root .env, keeping existing HTTPS host/token/warehouse settings:

```dotenv
RANKING_DATA_SOURCE=databricks_demo
RANKING_BACKEND=local
DATABRICKS_RANKING_VIEW=patient-info.default.demo_ranking_inputs
DATABRICKS_DEMO_OUTCOMES_TABLE=patient-info.default.demo_outcomes
DEMO_SCENARIO=baseline
DEMO_ORIGIN_ID=demo_troy_fixed
```

RANKING_DATA_SOURCE controls where the inputs come from.
RANKING_BACKEND controls where the same formula executes.
Start with local scoring and Databricks data so the data integration can be checked
without depending on UDF deployment. If faster_rank_v1 is already deployed, set:

```dotenv
RANKING_BACKEND=databricks
DATABRICKS_RANK_FUNCTION=patient-info.default.faster_rank_v1
```

The earlier databricks_ranking.sql installs that function. Do not rerun data ingestion.
The new REST read path uses DATABRICKS_WAREHOUSE_ID, not DATABRICKS_HTTP_PATH. The
legacy outcomes module may still initialize its old source at process startup; it
is never used for rankings in databricks_demo mode. Health reports both the legacy
outcomes source and the selected ranking data source; /health is not a live DB check.

Rebuild/recreate from the project root (do not remove the Docker data volume):

```bash
docker compose up -d --build --force-recreate
```

## Live connection checks

```bash
curl -sS http://localhost:8000/api/triage \
  -H 'Content-Type: application/json' \
  -d '{"symptoms":["fracture"],"age":25,"pain":5,"scenario_id":"baseline"}'
```

Expected: HTTP 200, data_source=databricks_demo, scenario_id=baseline,
static_snapshot=true, is_synthetic=true and Samaritan (sam) first.
Repeat with scenario_id=queue_surge: St. Peter's (sp) should be first.
Repeat with scenario_id=staff_reduction: sp should be first.
For symptoms=["fever"], age=10, pain=0: baseline should return sp first;
capability_change should return amc first. Full expected output is in
 tests/fixtures/demo/expected_rankings.json.
You can also run these POST requests interactively at http://localhost:8000/docs.
The existing HTML need not already expose a scenario picker; requests can select
scenario_id, or DEMO_SCENARIO sets the default when a request omits it.

Inspect Databricks SQL Query History to confirm each request reads the selected
view and outcome table. If remote scoring is enabled, an additional UDF call should
appear. A 503 is reported on failed queries, missing records, duplicates or malformed
inputs. There is no silent fallback to SQLite or neutral defaults for missing rows.

## Behavior and scope
- Exact five-hospital demo scope. IDs/conditions and synthetic labels are validated.
- Arrays/booleans are JSON-serialized in SQL and decoded before scoring; no string
  'false' truthiness bugs. Padded facility IDs remain strings.
- Original hospital order is restored for equal-score ties.
- No scoring weights, array indexing or urgency logic changed.
- Incoming levels are counted once, from the view, not also from SQLite.
- Outcome values are explicitly fetched from demo_outcomes for the same scenario.
- Results include source, scenario, origin, synthetic flag and source timestamps.
- These fixed snapshots are not live feeds. The existing formula does not enforce
  freshness; timestamps are exposed rather than rewritten to appear current.
- SQLite patient tracking/staff registration remains separate. Staff dashboard
  response declares staff_data_source=sqlite and staff_changes_affect_ranking=false
  in this mode. Registering or moving a patient does not update the Delta scenarios.
- /api/live and /api/incoming return 409 in this mode, because those SQLite changes
  would otherwise appear successful without affecting the remote ranking.
- /capacity retains its separate data settings and endpoints; its cards do not prove
  /api/triage uses the new source.
- No UI redesign or live travel integration is included.

## Validation performed
Local API tests use mocked Databricks REST results assembled from the supplied CSVs.
They compare all eight complete ranking fixtures, preserve the previous test suite,
and reject bad keys, bad arrays/booleans, nonfinite numbers, missing outcomes and
upstream outages. A live warehouse call and Docker execution still need to be run
on your machine; no credentials were provided or used here.
