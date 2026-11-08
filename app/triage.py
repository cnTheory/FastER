"""Acuity-aware hospital ranking + Gemini recommendation.

Pipeline:  injury text -> (Gemini) acuity + needs -> (algorithm) score every hospital
           -> (Gemini) choose among eligible hospitals + write the run-down -> backend validates the choice.

NOT a medical device. Acuity levels follow the idea of the 5-level ESI scale but are NOT a validated triage.
"""
from typing import Literal
import csv
import math
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app import gemini, scenarios
from app.databricks import execute

router = APIRouter(prefix="/api", tags=["Triage recommendation"])
REFERENCE_CSV = Path(__file__).parent / "reference" / "hospital_reference.csv"

# ASSUMPTION (not from SPARCS): clinician-minutes before a clinician can start the next patient, by acuity.
SERVICE_MINUTES = {1: 30, 2: 20, 3: 15, 4: 10, 5: 8}
TRAUMA_MIN_ADMISSIONS = 500   # proxy: hospital handled >=500 "Trauma" admissions in SPARCS 2024
PEDIATRIC_MIN_SHARE = 0.15    # proxy: >=15% of discharges aged 0-17
PEDIATRIC_PENALTY_MIN = 20    # soft penalty for a child going to a low-pediatric-volume hospital

NAMES = {"AMC": "Albany Medical Center Hospital", "SPH": "St. Peter's Hospital",
         "SAM": "Samaritan Hospital", "ELL": "Ellis Hospital", "SAR": "Saratoga Hospital"}

_ref_cache = {}


async def load_reference(source=None):
    if source == "databricks":
        try:
            return await _load_databricks_reference(), "databricks"
        except HTTPException:
            raise
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(502, f"Databricks SPARCS reference data is incomplete or invalid: {exc}")
    if source == "built_in":
        return _load_csv_reference(), "bundled_csv"
    if os.getenv("DATA_MODE", "demo") == "databricks":    
        with REFERENCE_CSV.open(newline="") as f:
            return {r["hospital_id"]: {**r, "trauma_admissions": int(r["trauma_admissions"]),
                                    "pediatric_share": float(r["pediatric_share"]),
                                    "boarding_factor": float(r["boarding_factor"]),
                                    "severe_share": float(r["severe_share"])} for r in csv.DictReader(f)}


async def _load_databricks_reference():
    """Same stats as scripts/build_reference.py, computed live from the Databricks SPARCS view."""
    from app.history import history_view
    by_name = {r["facility_name"]: hid for hid, r in _load_csv_reference().items()}
    names = list(by_name)
    params = [{"name": f"n{i}", "value": n, "type": "STRING"} for i, n in enumerate(names)]
    in_list = ",".join(f":n{i}" for i in range(len(names)))
    view = history_view()
    rows = await execute(f"""
        WITH sys AS (SELECT AVG(LEAST(length_of_stay, 30)) AS m FROM {view} WHERE length_of_stay > 0)
        SELECT facility_name,
               COUNT(*) AS discharge_count,
               SUM(CASE WHEN type_of_admission = 'Trauma' THEN 1 ELSE 0 END) AS trauma_admissions,
               AVG(CASE WHEN age_group = '0-17' THEN 1.0 ELSE 0.0 END) AS pediatric_share,
               AVG(CASE WHEN apr_severity_of_illness IN ('Major','Extreme') THEN 1.0 ELSE 0.0 END) AS severe_share,
               AVG(LEAST(length_of_stay, 30)) / FIRST(sys.m) AS boarding_ratio
        FROM {view} CROSS JOIN sys
        WHERE facility_name IN ({in_list}) AND length_of_stay > 0
        GROUP BY facility_name""", params)
    out = {}
    for r in rows:
        hid = by_name[r["facility_name"]]
        out[hid] = {"hospital_id": hid, "facility_name": r["facility_name"],
                    "trauma_admissions": int(r["trauma_admissions"]),
                    "pediatric_share": float(r["pediatric_share"]), "severe_share": float(r["severe_share"]),
                    "boarding_factor": round(min(max(float(r["boarding_ratio"]), 0.8), 1.5), 3)}
    if set(out) != set(by_name.values()):
        raise ValueError("Databricks reference missing hospitals")
    return out


async def load_reference():
    """Databricks when DATA_MODE=databricks (falls back to the bundled CSV, and says so)."""
    if os.getenv("DATA_MODE", "demo") == "databricks":
        try:
            return await _load_databricks_reference(), "databricks"
        except Exception:  # HTTPException from execute(), ValueError, KeyError...
            return _load_csv_reference(), "bundled_csv (Databricks query failed)"
    if "csv" not in _ref_cache:
        _ref_cache["csv"] = _load_csv_reference()
    return _ref_cache["csv"], "bundled_csv"


def estimate_wait(waiting, clinicians, acuity, boarding_factor):
    """Priority queue: only patients of equal or higher urgency (lower number) are served first."""
    if clinicians <= 0:
        return None
    ahead_minutes = sum(waiting.get(a, 0) * SERVICE_MINUTES[a] for a in range(1, acuity + 1))
    return math.ceil(ahead_minutes / clinicians * boarding_factor)


def score_hospitals(status_rows, reference, acuity, needs_trauma, pediatric):
    results = []
    for s in status_rows:
        ref = reference[s["hospital_id"]]
        waiting = s["waiting_by_acuity"]
        wait = estimate_wait(waiting, s["active_clinicians"], acuity, ref["boarding_factor"])
        trauma_ok = ref["trauma_admissions"] >= TRAUMA_MIN_ADMISSIONS
        ped_ok = ref["pediatric_share"] >= PEDIATRIC_MIN_SHARE
        reason = None
        if wait is None:
            reason = "No clinicians on shift reported"
        elif needs_trauma and not trauma_ok:
            reason = "Low trauma volume in SPARCS reference data"
        penalty = PEDIATRIC_PENALTY_MIN if pediatric and not ped_ok else 0
        total = None if reason else s["travel_minutes"] + wait + penalty
        results.append(dict(
            hospital_id=s["hospital_id"], name=NAMES[s["hospital_id"]], travel_minutes=s["travel_minutes"],
            estimated_wait_minutes=wait, pediatric_penalty_minutes=penalty, total_minutes=total,
            patients_waiting=sum(waiting.values()), more_urgent_or_equal_ahead=sum(waiting.get(a, 0) for a in range(1, acuity + 1)),
            active_clinicians=s["active_clinicians"], surge=s["surge"],
            trauma_capable=trauma_ok, pediatric_capable=ped_ok,
            baseline_severe_case_share=ref["severe_share"], boarding_factor=ref["boarding_factor"],
            eligible=reason is None, excluded_reason=reason))
    return sorted(results, key=lambda h: (h["total_minutes"] is None, h["total_minutes"] or 0))


class RecommendRequest(BaseModel):
    injury_description: str = Field(min_length=3, max_length=600)
    age: int | None = Field(default=None, ge=0, le=120)
    # Optional manual override (e.g. from a dropdown) if Gemini is unavailable.
    acuity: int | None = Field(default=None, ge=1, le=5)
    needs_trauma: bool | None = None
    source: Literal["databricks", "built_in"] | None = None


CLASSIFY_SYSTEM = (
    "You assign an emergency-department URGENCY LEVEL from 1 to 5 (1 = needs immediate life-saving care, "
    "2 = high risk, 3 = urgent, 4 = less urgent, 5 = non-urgent) to a short injury description, in the spirit of "
    "the ESI scale. Also say whether major trauma capability is likely needed (e.g. high-speed crash, penetrating "
    "injury, major bleeding, head/spine injury) and give a one-sentence summary. When unsure, choose the MORE urgent "
    'level. Do not diagnose. Reply as JSON: {"acuity": int 1-5, "needs_trauma": bool, "summary": string}.')

RECOMMEND_SYSTEM = (
    "You help patients reach an emergency department that can treat them quickly, so that no single ER is overwhelmed. "
    "Choose exactly one hospital_id from options (all eligible). Prefer the lowest total_minutes, but if a hospital has "
    "surge=true or many more_urgent_or_equal_ahead patients, mention that as the reason to go elsewhere. Use ONLY the "
    "supplied numbers; do not diagnose or give treatment. All numbers are simulated estimates. "
    'Reply as JSON: {"hospital_id": string, "summary": string (max 3 sentences, plain language), '
    '"next_steps": [string, string, string] (short, practical: e.g. bring ID/med list, call ahead, what to tell triage)}.')


@router.get("/scenarios")
def list_scenarios():
    return {"current": scenarios.current_name(),
            "scenarios": {k: {"title": v["title"], "description": v["description"]} for k, v in scenarios.SCENARIOS.items()}}


@router.post("/scenario/{name}")
def choose_scenario(name: str):
    try:
        scenarios.set_scenario(name)
    except KeyError:
        raise HTTPException(404, "Unknown scenario")
    return {"current": name}


@router.get("/status")
async def live_status(source: Literal["databricks", "built_in"] | None = None,
                      acuity: int = Query(3, ge=1, le=5)):
    meta, rows, status_source = await scenarios.get_snapshot(source)
    reference, ref_source = await load_reference(source)
    preview = {h["hospital_id"]: h for h in score_hospitals(rows, reference, acuity, False, False)}
    return {"synthetic": True, "scenario": meta, "reference_source": ref_source, "status_source": status_source,
            "preview_acuity": acuity,
            "hospitals": [{**r, "name": NAMES[r["hospital_id"]], "reference": reference[r["hospital_id"]],
                           "preview_wait_minutes": preview[r["hospital_id"]]["estimated_wait_minutes"],
                           "preview_total_minutes": preview[r["hospital_id"]]["total_minutes"]} for r in rows]}


@router.post("/recommend")
async def recommend(body: RecommendRequest):
    meta, rows, status_source = await scenarios.get_snapshot(body.source)
    reference, ref_source = await load_reference(body.source)

    # 1) Urgency: manual override > Gemini > conservative default
    triage_source, summary = "manual", body.injury_description[:200]
    acuity, needs_trauma = body.acuity, bool(body.needs_trauma)
    if acuity is None:
        c = await gemini.generate_json(CLASSIFY_SYSTEM, {"injury": body.injury_description, "age": body.age}, 300)
        try:
            acuity, needs_trauma = int(c["acuity"]), bool(c["needs_trauma"])
            summary, triage_source = str(c.get("summary", ""))[:300], "gemini"
            if not 1 <= acuity <= 5:
                raise ValueError
        except (TypeError, KeyError, ValueError):
            acuity, needs_trauma, triage_source = 2, False, "default (Gemini unavailable; assumed urgent)"
    pediatric = body.age is not None and body.age < 18

    # 2) Deterministic scoring using SPARCS reference + (synthetic) live queue
    scored = score_hospitals(rows, reference, acuity, needs_trauma, pediatric)
    eligible = [h for h in scored if h["eligible"]]
    patient = {"acuity": acuity, "needs_trauma": needs_trauma, "pediatric": pediatric,
               "summary": summary, "triage_source": triage_source}
    base = {"scenario": meta, "patient": patient, "reference_source": ref_source,
            "status_source": status_source, "synthetic": True,
            "call_911": acuity == 1,
            "safety_notice": "Simulated demo, not medical advice. If this may be life-threatening, call 911 now.",
            "hospitals": scored}
    if not eligible:
        return {**base, "recommended_hospital_id": None, "decision_source": "none",
                "summary": "No hospital in the simulation can take this patient. Call 911.", "next_steps": ["Call 911."]}

    # 3) Gemini picks among eligible hospitals; backend validates
    best = eligible[0]
    fallback = {**base, "recommended_hospital_id": best["hospital_id"], "recommended_name": best["name"],
                "decision_source": "algorithm",
                "summary": f"{best['name']} has the lowest estimated travel-plus-wait ({best['total_minutes']} min).",
                "next_steps": ["Bring photo ID and a list of medications.", "Tell triage staff exactly what happened.",
                               "Call 911 instead if symptoms worsen."]}
    options = [{k: h[k] for k in ("hospital_id", "name", "travel_minutes", "estimated_wait_minutes", "total_minutes",
                                  "more_urgent_or_equal_ahead", "patients_waiting", "active_clinicians", "surge",
                                  "trauma_capable", "pediatric_capable")} for h in eligible]
    choice = await gemini.generate_json(RECOMMEND_SYSTEM, {"scenario": meta["description"], "patient": patient,
                                                           "options": options})
    try:
        picked = next(h for h in eligible if h["hospital_id"] == choice["hospital_id"])
        steps = [str(x)[:200] for x in choice["next_steps"]][:5]
        return {**base, "recommended_hospital_id": picked["hospital_id"], "recommended_name": picked["name"],
                "decision_source": "gemini", "algorithm_best_hospital_id": best["hospital_id"],
                "summary": str(choice["summary"])[:600], "next_steps": steps}
    except (TypeError, KeyError, StopIteration):
        return fallback
