import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ["DATA_MODE"] = "demo"
from fastapi.testclient import TestClient
from faster.capacity import main, generate_data
import tempfile
from pathlib import Path

class FlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for target, attribute, value in [(main, "MODE", "demo"), (main, "ROOT", Path(self.temp.name)), (generate_data, "ROOT", Path(self.temp.name))]:
            patcher = patch.object(target, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client_context = TestClient(main.app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)

    def test_aggregate_and_staffing_change(self):
        initial = self.client.get("/api/hospitals").json()
        self.assertEqual(initial["best_hospital_id"], "H2")
        h1 = next(h for h in initial["hospitals"] if h["hospital_id"] == "H1")
        self.assertEqual(h1["queue_count"], 12)
        self.assertEqual(h1["active_clinicians"], 3)  # nurse excluded
        self.assertEqual(h1["estimated_wait_minutes"], 48)
        self.assertEqual(self.client.post("/api/demo/staff", json={"hospital_id":"H2", "active_clinicians":0}).status_code, 200)
        updated = self.client.get("/api/hospitals").json()
        self.assertEqual(updated["best_hospital_id"], "H1")
        h2 = next(h for h in updated["hospitals"] if h["hospital_id"] == "H2")
        self.assertIsNone(h2["estimated_wait_minutes"])

    def test_capability_is_a_constraint(self):
        rows = self.client.get("/api/hospitals?capability=pediatric").json()["hospitals"]
        self.assertIsNone(next(h for h in rows if h["hospital_id"]=="H1")["total_minutes"])

    def test_stale_snapshots_suppress_estimates(self):
        rows = main.demo_status()
        for h in rows:
            h["observed_at"]=(datetime.now(timezone.utc)-timedelta(minutes=16)).isoformat()
        with patch.object(main, "demo_status", return_value=rows):
            result = self.client.get("/api/hospitals").json()
        self.assertIsNone(result["best_hospital_id"])
        self.assertTrue(all(h["estimated_wait_minutes"] is None for h in result["hospitals"]))

    def test_no_patient_records_leak_to_browser(self):
        text=self.client.get("/api/hospitals").text
        self.assertNotIn("visit_id", text)
        self.assertNotIn("staff_id", text)

    def test_invalid_staff_change_is_rejected(self):
        self.assertEqual(self.client.post("/api/demo/staff", json={"hospital_id":"H1", "active_clinicians":-1}).status_code,422)
        self.assertEqual(self.client.get("/api/hospitals?capability=invalid").status_code,422)

    def test_explanation_without_keys(self):
        with patch.dict(os.environ,{"GEMINI_API_KEY":"", "GEMINI_MODEL":""}):
            r=self.client.post("/api/explain",json={"capability":"general"}).json()
        self.assertEqual(r["provider"], "template")
        self.assertIn("Demo Hillcrest",r["text"])

    def test_connected_failure_does_not_fall_back_to_demo(self):
        with patch.object(main, "MODE", "databricks"), patch.dict(os.environ, {"DATABRICKS_HOST":""}):
            self.assertEqual(self.client.get("/api/hospitals").status_code,503)
            self.assertEqual(self.client.post("/api/demo/staff",json={"hospital_id":"H1","active_clinicians":1}).status_code,403)

    def test_homepage(self):
        self.assertIn("Synthetic demonstration only",self.client.get("/").text)

if __name__ == "__main__":
    unittest.main()
