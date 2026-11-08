"""Optional remote execution of the existing demo ranking, without patient records."""
import json
import os
from fastapi import HTTPException
from . import scoring
from .capacity.databricks import execute, quote_table


async def rank(hospitals, incoming, outcome_fn, lvl, symptoms, ins=None, peds=False, trauma=False):
    mode = os.getenv("RANKING_BACKEND", "local").lower()
    if mode == "local":
        return scoring.rank(hospitals, incoming, outcome_fn, lvl, symptoms, ins, peds, trauma)
    if mode != "databricks":
        raise HTTPException(503, "RANKING_BACKEND must be local or databricks")
    # Explicit allowlist: no patient IDs, notes, names, ages or vitals in the payload.
    fields = ("id", "facility_id", "name", "city", "miles", "staff", "trauma",
              "peds", "insurance", "svc", "q", "boarding")
    cond = scoring.primary_condition(symptoms)
    indices = {str(h.get("facility_id", h["id"])):
               outcome_fn(h.get("facility_id", h["id"]), cond) if cond else 1.0
               for h in hospitals}
    payload = {"hospitals": [{k: h[k] for k in fields if k in h} for h in hospitals],
               "incoming": incoming, "outcomes": indices, "level": lvl,
               "symptoms": symptoms, "insurance": ins, "peds": peds, "trauma": trauma}
    function = quote_table(os.getenv("DATABRICKS_RANK_FUNCTION", "patient-info.default.faster_rank_v1"))
    rows = await execute(f"SELECT {function}(:payload) AS ranking_json", [
        {"name": "payload", "type": "STRING", "value": json.dumps(payload, allow_nan=False)}])
    try:
        result = json.loads(rows[0]["ranking_json"])
        if not isinstance(result, list) or len(result) != len(hospitals):
            raise ValueError("Unexpected ranking")
        if {r["hospital_id"] for r in result} != {h["id"] for h in hospitals}:
            raise ValueError("Unexpected hospitals")
        return result
    except (IndexError, KeyError, TypeError, ValueError):
        raise HTTPException(503, "Databricks ranking response was invalid")
