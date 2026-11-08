"""Regenerate the deployable SQL after changing scoring.py. Run from project root."""
from pathlib import Path
root = Path(__file__).resolve().parents[1]
source = (root / "src/faster/scoring.py").read_text()
wrapper = """
import json
p = json.loads(payload)
result = rank(p["hospitals"], p["incoming"],
              lambda facility_id, condition: p["outcomes"].get(str(facility_id), 1.0),
              p["level"], p["symptoms"], p["insurance"], p["peds"], p["trauma"])
return json.dumps(result, allow_nan=False)
"""
(root / "databricks_ranking.sql").write_text(
    "-- Demo v1: preserves the existing heuristic; not a validated clinical model.\n"
    "CREATE OR REPLACE FUNCTION `patient-info`.`default`.`faster_rank_v1`(payload STRING)\n"
    "RETURNS STRING LANGUAGE PYTHON AS $$\n" + source + wrapper + "$$;\n")
