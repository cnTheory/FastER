import os
import sqlite3
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from google import genai
from dotenv import load_dotenv

# Load environmental configs from .env
load_dotenv()

app = FastAPI(title="FastER API Engine", version="1.0.0")

# Enable CORS cross-origin resource sharing for hackathon UIs
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize Gemini Client (automatically imports GEMINI_API_KEY from .env)
try:
    client = genai.Client()
except Exception as e:
    print(f"⚠️ Warning: Gemini Client initialization failed. Check your API key. Error: {e}")
    client = None

class StatusUpdate(BaseModel):
    patient_id: str
    new_status: str

@app.get("/api/patients")
def get_all_patients():
    """Returns active patients for a coordinator dashboard."""
    conn = sqlite3.connect('er_flow.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM patients WHERE status NOT IN ('DISCHARGED', 'ADMITTED')")
    patients = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return patients

@app.get("/api/patient/{patient_id}")
def get_patient_progress(patient_id: str):
    """Feeds step metrics directly into the patient pipeline tracking interface."""
    conn = sqlite3.connect('er_flow.db')
    cursor = conn.cursor()
    cursor.execute("SELECT status, admission_prediction, esi_level, chief_complaint, age, triage_heart_rate FROM patients WHERE id = ?", (patient_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        raise HTTPException(status_code=404, detail="Patient profile not found")
        
    status, prediction, esi, complaint, age, hr = row
    
    steps = {
        'TRIAGE': {"step": 1, "label": "Triage Assessment", "text": "Initial vitals logged. Waiting for full medical staff assignment."},
        'WITH_DOCTOR': {"step": 2, "label": "Clinical Evaluation", "text": "Undergoing active examinations, diagnostic tests, or lab reviews."},
        'BOARDING': {"step": 3, "label": "Awaiting Inpatient Bed", "text": "Admission approved! Waiting for a specialized hospital bed to open up upstairs."},
        'ADMITTED': {"step": 4, "label": "Transferring", "text": "Room preparation complete. Your care is being transitioned upstairs."}
    }
    
    return {
        "patient_id": patient_id,
        "current_status": status,
        "timeline_data": steps.get(status, {"step": 1, "label": "Processing", "text": "Updating status details..."}),
        "high_admission_risk": True if prediction > 0.70 or esi <= 2 else False,
        "clinical_data": {"age": age, "complaint": complaint, "heart_rate": hr}
    }

@app.get("/api/patient/{patient_id}/ai-summary")
def get_ai_triage_summary(patient_id: str):
    """Uses Gemini 2.5 Flash to generate clinical insights from triage data."""
    if not client:
        return {"success": False, "error": "Gemini Client is uninitialized."}
        
    conn = sqlite3.connect('er_flow.db')
    cursor = conn.cursor()
    cursor.execute("SELECT chief_complaint, age, triage_heart_rate, triage_o2_sat, esi_level FROM patients WHERE id = ?", (patient_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        raise HTTPException(status_code=404, detail="Patient not found")
        
    complaint, age, hr, o2, esi = row
    
    prompt = f"""
    You are an expert Emergency Room Triage assistant.
    Analyze this patient data:
    - Chief Complaint: {complaint}
    - Age: {age}
    - Heart Rate: {hr} bpm
    - O2 Saturation: {o2}%
    - Emergency Severity index (ESI): {esi}
    
    Provide a concise 2-sentence clinical brief for the nursing team outlining immediate care suggestions or order sets (e.g., labs, imaging, EKG) required. Do not give direct treatment advice.
    """
    
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        return {"success": True, "ai_brief": response.text.strip()}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/staff/update-status")
def update_status(payload: StatusUpdate):
    """Updates operational patient position changes across the workflow columns."""
    conn = sqlite3.connect('er_flow.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE patients SET status = ? WHERE id = ?", (payload.new_status, payload.patient_id))
    cursor.execute("SELECT COUNT(*) FROM patients WHERE status = 'BOARDING'")
    boarding_count = cursor.fetchone()[0]
    conn.commit()
    conn.close()
    return {"success": True, "system_boarding_load": boarding_count}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
