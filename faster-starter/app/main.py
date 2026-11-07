"""Local synthetic demo; not a medical triage or production hospital system."""
import csv
import json
import math
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from scripts.generate_data import ROOT, generate

MODE = os.getenv("DATA_MODE", "demo")
LOCK = threading.Lock()
STALE_SECONDS = 15 * 60

@asynccontextmanager
async def lifespan(app):
    if MODE == "demo":
        generate()
    elif MODE != "databricks":
        raise RuntimeError("DATA_MODE must be demo or databricks")
    yield

app = FastAPI(title="FastER synthetic demo", lifespan=lifespan)

def read_csv(name):
    with (ROOT / name).open(newline="") as f:
        return list(csv.DictReader(f))

def latest(rows, key):
    """Deduplicate event updates by ID and timestamp."""
    result = {}
    for row in rows:
        timestamp = datetime.fromisoformat(row["updated_at"])
        if timestamp.tzinfo is None:
            raise ValueError("Timestamps must include a timezone")
        old = result.get(row[key])
        if old is None or timestamp > datetime.fromisoformat(old["updated_at"]):
            result[row[key]] = row
    return list(result.values())

def demo_status():
    with LOCK:
        hospitals = read_csv("hospitals.csv")
        visits = latest(read_csv("visits.csv"), "visit_id")
        staff = latest(read_csv("staff.csv"), "staff_id")
    rows = []
    for h in hospitals:
        hid = h["hospital_id"]
        hv = [v for v in visits if v["hospital_id"] == hid]
        hs = [s for s in staff if s["hospital_id"] == hid]
        clinicians = sum(s["role"] == "clinician" and s["on_shift"].lower() == "true" for s in hs)
        # This demo treats a source snapshot as one atomic batch.
        stamps = [h["observed_at"]] + [v["updated_at"] for v in hv] + [s["updated_at"] for s in hs]
        rows.append(dict(hospital_id=hid, name=h["name"],
            travel_minutes=float(h["travel_minutes"]), capabilities=h["capabilities"],
            service_minutes=float(h["service_minutes"]),
            queue_count=sum(v["status"] == "WAITING" for v in hv),
            active_clinicians=clinicians,
            observed_at=min(stamps, key=lambda s: datetime.fromisoformat(s))))
    return rows

async def databricks_status():
    """Read a tiny aggregate table using the SQL Statement Execution REST API."""
    host = os.getenv("DATABRICKS_HOST", "").rstrip("/")
    token = os.getenv("DATABRICKS_TOKEN", "")
    warehouse = os.getenv("DATABRICKS_WAREHOUSE_ID", "")
    table = os.getenv("DATABRICKS_GOLD_TABLE", "workspace.faster.gold_hospital_status")
    if not (host.startswith("https://") and token and warehouse):
        raise HTTPException(503, "Databricks connection settings are incomplete")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){2}", table):
        raise HTTPException(503, "Use a three-part catalog.schema.table name")
    statement = f"SELECT hospital_id,name,travel_minutes,capabilities,service_minutes,queue_count,active_clinicians,observed_at FROM {table} LIMIT 100"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=40) as client:
            response = await client.post(f"{host}/api/2.0/sql/statements", headers=headers,
                json={"warehouse_id": warehouse, "statement": statement,
                      "wait_timeout": "30s", "on_wait_timeout": "CANCEL",
                      "disposition": "INLINE", "format": "JSON_ARRAY"})
            response.raise_for_status()
            payload = response.json()
        if payload.get("status", {}).get("state") != "SUCCEEDED":
            raise HTTPException(503, "Databricks query did not complete; check warehouse status")
        if payload.get("manifest", {}).get("truncated"):
            raise HTTPException(503, "Hospital query was truncated")
        columns = [c["name"] for c in payload["manifest"]["schema"]["columns"]]
        return [dict(zip(columns, row)) for row in payload.get("result", {}).get("data_array", [])]
    except (httpx.HTTPError, KeyError, ValueError):
        # Never return provider error bodies or credentials to the browser.
        raise HTTPException(503, "Databricks unavailable; no synthetic fallback in connected mode")

def calculate(rows, capability):
    now = datetime.now(timezone.utc)
    result = []
    for raw in rows:
        h = dict(raw)
        h["queue_count"] = int(h["queue_count"])
        h["active_clinicians"] = int(h["active_clinicians"])
        h["travel_minutes"] = float(h["travel_minutes"])
        h["service_minutes"] = float(h["service_minutes"])
        observed = datetime.fromisoformat(h["observed_at"].replace("Z", "+00:00"))
        if observed.tzinfo is None:
            raise ValueError("observed_at must include timezone")
        if min(h["queue_count"], h["active_clinicians"], h["travel_minutes"]) < 0 or h["service_minutes"] <= 0:
            raise ValueError("Negative counts or invalid service interval")
        age = (now - observed).total_seconds()
        h["stale"] = age > STALE_SECONDS or age < -60
        h["matches_capability"] = capability in h["capabilities"].split("|")
        eligible = not h["stale"] and h["active_clinicians"] > 0 and h["matches_capability"]
        # Illustrative queue model only: a clinician begins one new assessment every N minutes.
        # Does not model triage, inpatient boarding, current residual service time or nursing bottlenecks.
        h["estimated_wait_minutes"] = math.ceil(h["queue_count"] * h["service_minutes"] / h["active_clinicians"]) if eligible else None
        h["total_minutes"] = h["travel_minutes"] + h["estimated_wait_minutes"] if eligible else None
        h["reason"] = ("Status is stale" if h["stale"] else
            "Requested demo capability unavailable" if not h["matches_capability"] else
            "No on-shift clinicians reported" if h["active_clinicians"] == 0 else "Eligible for demo comparison")
        result.append(h)
    return sorted(result, key=lambda h: (h["total_minutes"] is None, h["total_minutes"] or 0))

async def comparison(capability):
    rows = demo_status() if MODE == "demo" else await databricks_status()
    try:
        hospitals = calculate(rows, capability)
    except (ValueError, TypeError, KeyError):
        raise HTTPException(503, "Hospital data does not match the required contract")
    eligible = [h for h in hospitals if h["total_minutes"] is not None]
    return {"mode": MODE, "synthetic": True, "capability": capability,
        "model": "Illustrative queue arithmetic; not a clinically validated wait prediction",
        "best_hospital_id": eligible[0]["hospital_id"] if eligible else None,
        "hospitals": hospitals}

@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")

@app.get("/api/health")
def health():
    return {"status": "ok", "mode": MODE}

@app.get("/api/hospitals")
async def hospitals(capability: str = "general"):
    if capability not in {"general", "imaging", "pediatric"}:
        raise HTTPException(422, "Unknown demo capability")
    return await comparison(capability)

class DemoChange(BaseModel):
    hospital_id: str = Field(pattern=r"^H[1-3]$")
    active_clinicians: int = Field(ge=0, le=10)

@app.post("/api/demo/staff")
def change_staff(change: DemoChange):
    if MODE != "demo":
        raise HTTPException(403, "Local controls are disabled in connected mode")
    with LOCK:
        rows = read_csv("staff.csv")
        rows = [r for r in rows if r["hospital_id"] != change.hospital_id]
        now = datetime.now(timezone.utc).isoformat()
        for i in range(change.active_clinicians):
            rows.append(dict(staff_id=f"{change.hospital_id}-S{i}", hospital_id=change.hospital_id,
                role="clinician", on_shift="true", updated_at=now))
        # Preserve the header even when a hospital has zero clinicians.
        with (ROOT / "staff.csv").open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["staff_id", "hospital_id", "role", "on_shift", "updated_at"])
            writer.writeheader()
            writer.writerows(rows)
    return {"updated": True}

class ExplainRequest(BaseModel):
    capability: str = Field(default="general", pattern=r"^(general|imaging|pediatric)$")

@app.post("/api/explain")
async def explain(body: ExplainRequest):
    evidence = await comparison(body.capability)
    best = next((h for h in evidence["hospitals"] if h["hospital_id"] == evidence["best_hospital_id"]), None)
    fallback = (f"In this fictional scenario, {best['name']} has the lowest illustrative travel-plus-wait total of {best['total_minutes']:g} minutes. Actual clinical priority and waits can differ." if best else
        "No hospital has fresh, matching data with reported clinician capacity. No comparison is available.")
    key, model = os.getenv("GEMINI_API_KEY"), os.getenv("GEMINI_MODEL")
    if not key or not model or not best:
        return {"provider": "template", "text": fallback}
    if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise HTTPException(503, "Invalid Gemini model name")
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": key},
                json={"systemInstruction": {"parts": [{"text":
                    "Explain only this fictional hospital queue demonstration in at most 3 sentences. "
                    "Use only supplied facts. Do not infer diagnoses, treatment, staff specialty, safety, or real clinical suitability. "
                    "Do not change the backend-selected best_hospital_id. Say the numbers are simulated and actual waits may differ."}]},
                    "contents": [{"role": "user", "parts": [{"text": json.dumps(evidence)}]}],
                    "generationConfig": {"temperature": 0.1, "maxOutputTokens": 400}})
            response.raise_for_status()
            payload = response.json()
        text = " ".join(p.get("text", "") for p in payload["candidates"][0]["content"]["parts"] if not p.get("thought"))
        if not text.strip():
            raise ValueError("No text returned")
        return {"provider": "gemini", "text": text,
                "notice": "AI-generated explanation of simulated aggregate data; verify against the cards."}
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        return {"provider": "template (Gemini unavailable)", "text": fallback}
