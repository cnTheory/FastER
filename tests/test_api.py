"""Integration checks use a fresh synthetic SQLite database per test."""
import time
import pytest
from fastapi.testclient import TestClient
from faster import db, gemini_client
from faster.capacity import main as capacity_main, generate_data
from faster.app import app, TOKENS


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "demo.db"))
    monkeypatch.setenv("STAFF_CODE", "test-code")
    monkeypatch.setenv("LIVE_FEED_KEY", "test-feed")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
    monkeypatch.setattr(capacity_main, "MODE", "demo")
    monkeypatch.setattr(capacity_main, "ROOT", tmp_path / "capacity")
    monkeypatch.setattr(generate_data, "ROOT", tmp_path / "capacity")
    TOKENS.clear()
    with TestClient(app) as client:
        yield client
    TOKENS.clear()


def login(client, hospital="sam"):
    response = client.post("/api/staff/login", json={"hospital_id": hospital, "code": "test-code"})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_startup_serves_tracker_and_hospitals(client):
    assert client.get("/").status_code == 200
    assert 'const API_BASE = "/api"' in client.get("/").text
    assert client.get("/health").json()["outcomes_source"] == "csv"
    assert len(client.get("/api/hospitals").json()) == 5
    assert client.get("/openapi.json").status_code == 200


def test_staff_auth_and_logout(client):
    assert client.get("/api/staff/dashboard").status_code == 401
    assert client.get("/api/patients").status_code == 401
    assert client.post("/api/staff/login", json={"hospital_id": "sam", "code": "wrong"}).status_code == 401
    headers = login(client)
    assert client.get("/api/staff/dashboard", headers=headers).json()["hospital"]["id"] == "sam"
    assert client.post("/api/staff/logout", headers=headers).status_code == 200
    assert client.get("/api/staff/dashboard", headers=headers).status_code == 401


def test_expired_token(client):
    TOKENS["expired"] = ("sam", time.time() - 1)
    assert client.get("/api/staff/dashboard", headers={"Authorization": "Bearer expired"}).status_code == 401


def test_patient_lifecycle_and_queue(client):
    headers = login(client)
    before = client.get("/api/staff/dashboard", headers=headers).json()["hospital"]["queue_by_level"][2]
    result = client.post("/api/staff/patients", headers=headers, json={"esi_level": 3, "chief_complaint": "Synthetic demo", "age": 25})
    assert result.status_code == 200
    pid = result.json()["patient_id"]
    assert client.get(f"/api/patient/{pid}").json()["timeline_data"]["step"] == 1
    assert client.get("/api/staff/dashboard", headers=headers).json()["hospital"]["queue_by_level"][2] == before + 1
    for status, step in [("WITH_DOCTOR", 2), ("BOARDING", 3), ("ADMITTED", 4), ("DISCHARGED", 5)]:
        assert client.post("/api/staff/update-status", headers=headers, json={"patient_id": pid, "new_status": status}).status_code == 200
        assert client.get(f"/api/patient/{pid}").json()["timeline_data"]["step"] == step
    assert pid not in {p["id"] for p in client.get("/api/patients", headers=headers).json()}
    assert client.get("/api/staff/dashboard", headers=headers).json()["hospital"]["queue_by_level"][2] == before
    assert client.post("/api/staff/update-status", headers=headers, json={"patient_id": pid, "new_status": "INVALID"}).status_code == 422
    assert client.get("/api/patient/missing").status_code == 404


def test_cross_hospital_protection_and_summary_aliases(client, monkeypatch):
    sam, amc = login(client), login(client, "amc")
    pid = client.get("/api/patients", headers=sam).json()[0]["id"]
    assert client.post("/api/staff/update-status", headers=amc, json={"patient_id": pid, "new_status": "BOARDING"}).status_code == 404
    monkeypatch.setattr(gemini_client, "clinical_brief", lambda p: "Synthetic summary.")
    for path in [f"/api/staff/patient/{pid}/ai-summary", f"/api/patient/{pid}/ai-summary"]:
        assert client.get(path).status_code == 401
        assert client.get(path, headers=amc).status_code == 404
        assert client.get(path, headers=sam).json()["ai_brief"] == "Synthetic summary."


def test_ai_failure_is_clear(client, monkeypatch):
    headers = login(client)
    pid = client.get("/api/patients", headers=headers).json()[0]["id"]
    def fail(_):
        raise RuntimeError("provider failure")
    monkeypatch.setattr(gemini_client, "clinical_brief", fail)
    assert client.get(f"/api/staff/patient/{pid}/ai-summary", headers=headers).json() == {"success": False, "error": "AI summary unavailable"}


def test_offline_intake_and_ranking(client):
    intake = client.post("/api/intake", json={"text": "I am 25 years old with a cough"}).json()
    assert intake["source"] == "fallback"
    assert intake["age"] == 25 and "cold" in intake["symptoms"]
    result = client.post("/api/triage", json={"symptoms": intake["symptoms"], "age": intake["age"], "explain": True}).json()
    assert len(result["ranking"]) == 5 and result["explanation"]
    scores = [h["score"] for h in result["ranking"]]
    assert scores == sorted(scores)
    assert client.post("/api/triage", json={"symptoms": ["chest"]}).json()["call_911"] is True
    single = client.post("/api/triage", json={"symptoms": ["fever"]}).json()
    duplicate = client.post("/api/triage", json={"symptoms": ["fever", "fever"]}).json()
    assert single["level"] == duplicate["level"]
    assert client.post("/api/triage", json={"symptoms": ["unknown"]}).status_code == 422


def test_incoming_and_live_queue_override(client):
    headers = login(client)
    added = client.post("/api/incoming", json={"hospital_id": "sam", "level": 3, "eta_min": 10})
    assert added.status_code == 200
    assert added.json()["id"] in {p["id"] for p in client.get("/api/staff/dashboard", headers=headers).json()["incoming"]}
    assert client.post("/api/incoming", json={"hospital_id": "missing", "level": 3, "eta_min": 10}).status_code == 404
    payload = {"staff": 4, "q": [0, 1, 2, 3, 4]}
    assert client.post("/api/live/sam", json=payload).status_code == 401
    assert client.post("/api/live/sam", json=payload, headers={"X-Feed-Key": "test-feed"}).status_code == 200
    assert client.get("/api/staff/dashboard", headers=headers).json()["hospital"]["queue_by_level"] == payload["q"]
    with db.conn() as c:
        c.execute("UPDATE hospitals SET q_override_at=? WHERE id='sam'", (time.time() - db.OVERRIDE_TTL - 1,))
    assert client.get("/api/staff/dashboard", headers=headers).json()["hospital"]["queue_by_level"] != payload["q"]
    assert client.post("/api/live/sam", json={"staff": 4, "q": [-1, 0, 0, 0, 0]}, headers={"X-Feed-Key": "test-feed"}).status_code == 422


def test_initialization_preserves_records(client):
    headers = login(client)
    pid = client.post("/api/staff/patients", headers=headers, json={"esi_level": 4, "chief_complaint": "Demo"}).json()["patient_id"]
    db.init()
    assert db.patient(pid) is not None
