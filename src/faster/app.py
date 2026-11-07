import logging, os, secrets, time
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime
from typing import Annotated, Literal, Optional
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import config, db, gemini_client, outcomes, scoring
from .capacity import main as capacity_main

log = logging.getLogger("faster")
@asynccontextmanager
async def lifespan(app):
    db.init()
    async with capacity_main.lifespan(capacity_main.app):
        yield

app = FastAPI(title="FastER API", version="2.0.0", lifespan=lifespan)
app.mount("/capacity", capacity_main.app)

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(Path(__file__).with_name("index.html"))
app.add_middleware(CORSMiddleware, allow_credentials=False, allow_methods=["*"], allow_headers=["*"],
                   allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:3000,http://localhost:5173,http://127.0.0.1:5500").split(","))
TOKENS: dict = {}  # demo-grade sessions; use a real identity provider for production
Status = Literal["TRIAGE", "WITH_DOCTOR", "BOARDING", "ADMITTED", "DISCHARGED"]
STEPS = {
    "TRIAGE": (1, "Triage Assessment", "Initial vitals logged. Waiting for full medical staff assignment."),
    "WITH_DOCTOR": (2, "Clinical Evaluation", "Undergoing active examinations, diagnostic tests, or lab reviews."),
    "BOARDING": (3, "Awaiting Inpatient Bed", "Admission approved. Waiting for a hospital bed to open upstairs."),
    "ADMITTED": (4, "Transferring", "Room preparation complete. Your care is moving upstairs."),
    "DISCHARGED": (5, "Discharged", "Your emergency visit is complete."),
}


class IntakeReq(BaseModel):
    text: str = Field(min_length=1, max_length=2000)

class TriageReq(BaseModel):
    symptoms: list[Literal["chest", "breath", "bleed", "unresp", "stroke", "fracture", "burn", "cut", "fever", "pain", "sprain", "cold"]] = Field(default_factory=list, max_length=12)
    age: Optional[int] = Field(None, ge=0, le=120)
    pain: Optional[int] = Field(None, ge=0, le=10)
    insurance: Optional[str] = None
    peds: bool = False
    trauma: bool = False
    explain: bool = False

class IncomingReq(BaseModel):
    hospital_id: str
    level: int = Field(ge=1, le=5)
    eta_min: int = Field(ge=0, le=300)
    label: str = Field("Patient (app)", max_length=40)  # keep PHI out of this field

class LiveReq(BaseModel):
    staff: int = Field(ge=0)
    q: Optional[list[Annotated[int, Field(ge=0)]]] = Field(None, min_length=5, max_length=5)

class LoginReq(BaseModel):
    hospital_id: str
    code: str

class RegisterReq(BaseModel):
    esi_level: int = Field(ge=1, le=5)
    chief_complaint: str = Field(min_length=1, max_length=200)
    age: Optional[int] = Field(None, ge=0, le=120)
    triage_heart_rate: Optional[float] = Field(None, ge=20, le=250)
    triage_o2_sat: Optional[float] = Field(None, ge=50, le=100)

class StatusReq(BaseModel):
    patient_id: str
    new_status: Status


def _waited(p):
    try:
        return max(0, int((datetime.now() - datetime.fromisoformat(p["arrival_time"])).total_seconds() // 60))
    except Exception:
        return None


@app.get("/health")
def health():
    return {"ok": True, "outcomes_source": outcomes.SOURCE, "db": os.path.basename(db.DB_PATH)}


@app.get("/api/hospitals")
def hospital_directory():
    return [{"id": h["id"], "name": h["name"], "city": h["city"]} for h in db.hospitals()]

# ---------- patient-facing ----------
@app.post("/api/intake")
def intake(r: IntakeReq):
    return gemini_client.extract_intake(r.text)


@app.post("/api/triage")
def triage(r: TriageReq):
    syms = list(dict.fromkeys(r.symptoms))
    lvl = scoring.severity(syms, r.age, r.pain)
    inc = {}
    for i in db.incoming():
        inc.setdefault(i["hospital_id"], []).append(i["lvl"])
    ranking = scoring.rank(db.hospitals(), inc, outcomes.get, lvl, syms, r.insurance,
                           r.peds or (r.age is not None and r.age < 18), r.trauma)
    res = {"level": lvl, "call_911": bool(scoring.RED_FLAGS & set(syms)) or lvl <= 2,
           "ranking": ranking, "disclaimer": "Estimates only. Not medical advice."}
    if r.explain:
        res["explanation"] = gemini_client.explain(ranking[:3])
    return res


@app.post("/api/incoming")
def incoming(r: IncomingReq):
    if r.hospital_id not in {h["id"] for h in db.hospitals()}:
        raise HTTPException(404, "unknown hospital")
    return db.add_incoming(r.hospital_id, r.level, r.eta_min, r.label)


@app.get("/api/patient/{patient_id}")  # patient pipeline tracker; IDs are random tokens
def tracker(patient_id: str):
    p = db.patient(patient_id)
    if not p:
        raise HTTPException(404, "Patient profile not found")
    step, label, text = STEPS[p["status"]]
    pred = p["admission_prediction"]
    hosp = next((h["name"] for h in db.hospitals() if h["id"] == p["hospital_id"]), None)
    return {"patient_id": patient_id, "hospital": hosp, "current_status": p["status"],
            "timeline_data": {"step": step, "label": label, "text": text},
            "high_admission_risk": (pred is not None and pred > 0.70) or (p["esi_level"] or 5) <= 2,
            "clinical_data": {"age": p["age"], "complaint": p["chief_complaint"], "heart_rate": p["triage_heart_rate"]}}


# ---------- hospital feed ----------
@app.post("/api/live/{hospital_id}")
def live(hospital_id: str, r: LiveReq, x_feed_key: str = Header(None)):
    if not x_feed_key or not secrets.compare_digest(x_feed_key, os.getenv("LIVE_FEED_KEY", "dev-feed-key")):
        raise HTTPException(401, "bad feed key")
    if not db.set_live(hospital_id, r.staff, r.q):
        raise HTTPException(404, "unknown hospital")
    return {"ok": True}


# ---------- staff (login required for everything below) ----------
@app.post("/api/staff/login")
def login(r: LoginReq):
    if r.hospital_id not in {h["id"] for h in db.hospitals()} or \
       not secrets.compare_digest(r.code, os.getenv("STAFF_CODE", "staff123")):
        raise HTTPException(401, "invalid credentials")
    t = secrets.token_urlsafe(24)
    TOKENS[t] = (r.hospital_id, time.time() + 8 * 3600)
    return {"token": t, "hospital_id": r.hospital_id}


def staff_hospital(authorization: str = Header(None)):
    s = TOKENS.get((authorization or "").removeprefix("Bearer ").strip())
    if not s or s[1] < time.time():
        raise HTTPException(401, "staff login required")
    return s[0]


@app.post("/api/staff/logout")
def logout(hid: str = Depends(staff_hospital), authorization: str = Header(None)):
    TOKENS.pop((authorization or "").removeprefix("Bearer ").strip(), None)
    return {"success": True}

@app.get("/api/staff/dashboard")
def dashboard(hid: str = Depends(staff_hospital)):
    h = next(x for x in db.hospitals() if x["id"] == hid)
    pts = sorted(db.patients(hid), key=lambda p: (p["esi_level"], p["arrival_time"]))
    for p in pts:
        p["waited_min"] = _waited(p)
    return {"hospital": {"id": hid, "name": h["name"], "staff": h["staff"], "queue_by_level": h["q"]},
            "beds": db.bed_summary(hid), "in_department": pts,
            "incoming": sorted(db.incoming(hid), key=lambda i: i["eta"])}


@app.get("/api/patients", include_in_schema=False)
def legacy_patients(hid: str = Depends(staff_hospital)):
    """Compatibility route; patient lists remain scoped to logged-in staff."""
    return db.patients(hid)

@app.post("/api/staff/patients")
def register(r: RegisterReq, hid: str = Depends(staff_hospital)):
    return {"patient_id": db.add_patient(hid, r.esi_level, r.chief_complaint, r.age, r.triage_heart_rate, r.triage_o2_sat)}


@app.post("/api/staff/update-status")
def update_status(r: StatusReq, hid: str = Depends(staff_hospital)):
    if not db.set_status(hid, r.patient_id, r.new_status):
        raise HTTPException(404, "patient not found at your hospital")
    return {"success": True, "beds": db.bed_summary(hid)}


@app.get("/api/patient/{patient_id}/ai-summary", include_in_schema=False)
@app.get("/api/staff/patient/{patient_id}/ai-summary")
def ai_summary(patient_id: str, hid: str = Depends(staff_hospital)):
    p = db.patient(patient_id)
    if not p or p["hospital_id"] != hid:
        raise HTTPException(404, "patient not found at your hospital")
    try:
        return {"success": True, "ai_brief": gemini_client.clinical_brief(p)}
    except Exception:
        log.exception("AI summary failed")
        return {"success": False, "error": "AI summary unavailable"}


if __name__ == "__main__":
    from . import main
    main()
