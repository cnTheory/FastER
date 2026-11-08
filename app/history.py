"""SPARCS historical summaries using the exact frontend-view schema supplied by the user."""
import asyncio
import os

from fastapi import APIRouter, HTTPException, Query

from app.databricks import execute, quote_table

router = APIRouter(prefix="/api/history", tags=["Historical hospital data"])
DEFAULT_VIEW = "patient-info.default.nys_sparcs_discharges_2024_frontend"


def history_view():
    return quote_table(os.getenv("DATABRICKS_HISTORY_VIEW", DEFAULT_VIEW))


@router.get("/hospitals")
async def historical_hospitals(limit: int = Query(250, ge=1, le=500), offset: int = Query(0, ge=0, le=10000)):
    rows = await execute(f"""
        SELECT permanent_facility_id AS hospital_id,
               MAX(facility_name) AS facility_name,
               COUNT(*) AS discharge_count
        FROM {history_view()}
        WHERE permanent_facility_id IS NOT NULL AND facility_name IS NOT NULL
        GROUP BY permanent_facility_id
        ORDER BY facility_name ASC, permanent_facility_id ASC
        LIMIT :row_limit OFFSET :row_offset
    """, [{"name": "row_limit", "value": str(limit), "type": "INT"},
          {"name": "row_offset", "value": str(offset), "type": "INT"}])
    return {"source": "Databricks SPARCS historical view", "dataset_year": 2024,
            "hospitals": rows, "limit": limit, "offset": offset,
            "possibly_more": len(rows) == limit}


@router.get("/hospitals/{facility_id}")
async def historical_summary(facility_id: str,
        age_group: str | None = Query(None, max_length=80),
        diagnosis_code: str | None = Query(None, max_length=80),
        emergency_department_indicator: str | None = Query(None, max_length=80),
        top_n: int = Query(10, ge=1, le=25)):
    if not facility_id.strip() or len(facility_id) > 128:
        raise HTTPException(422, "Invalid facility ID")
    filters = ["permanent_facility_id = :facility_id"]
    parameters = [{"name": "facility_id", "value": facility_id, "type": "STRING"}]
    for column, value in [("age_group", age_group), ("ccsr_diagnosis_code", diagnosis_code),
                           ("emergency_department_indicator", emergency_department_indicator)]:
        if value is not None:
            filters.append(f"{column} = :{column}")
            parameters.append({"name": column, "value": value, "type": "STRING"})
    where = " AND ".join(filters)
    view = history_view()
    summary = await execute(f"""
        SELECT MAX(facility_name) AS facility_name,
               COUNT(*) AS discharge_count,
               COUNT(CASE WHEN length_of_stay > 0 THEN 1 END) AS length_of_stay_record_count,
               AVG(CASE WHEN length_of_stay > 0 THEN length_of_stay END) AS average_inpatient_stay_days,
               percentile_approx(CASE WHEN length_of_stay > 0 THEN length_of_stay END, 0.5) AS median_inpatient_stay_days
        FROM {view} WHERE {where}
    """, parameters)
    if not summary or summary[0]["discharge_count"] == 0:
        raise HTTPException(404, "No historical discharges match this hospital and these filters")
    group_parameters = parameters + [{"name": "top_n", "value": str(top_n), "type": "INT"}]
    async def grouped(code, description):
        # Column names come only from these fixed server-side calls, never user input.
        return await execute(f"""
            SELECT {code} AS code, {description} AS description, COUNT(*) AS discharge_count
            FROM {view} WHERE {where}
            GROUP BY {code}, {description}
            ORDER BY discharge_count DESC, code ASC, description ASC
            LIMIT :top_n
        """, group_parameters)
    diagnoses, procedures, severity = await asyncio.gather(
        grouped("ccsr_diagnosis_code", "ccsr_diagnosis_description"),
        grouped("ccsr_procedure_code", "ccsr_procedure_description"),
        grouped("apr_severity_of_illness_code", "apr_severity_of_illness"))
    return {"hospital_id": facility_id, "dataset_year": 2024,
            "source": "Databricks SPARCS historical view",
            "filters": {"age_group": age_group, "diagnosis_code": diagnosis_code,
                        "emergency_department_indicator": emergency_department_indicator},
            "summary": summary[0], "diagnoses": diagnoses, "procedures": procedures, "severity": severity,
            "length_of_stay_unit": "days",
            "note": "Historical inpatient discharge summaries from the cleaned view. Length-of-stay summaries use positive non-null cleaned values; verify upstream handling of capped/redacted values. No current staffing, wait estimate, or outcome-quality ranking is inferred."}
