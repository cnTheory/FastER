import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ["DATA_MODE"] = "demo"
from fastapi.testclient import TestClient
from app import main
from app import triage as triage_mod

class FlowTests(unittest.TestCase):
    def setUp(self):
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

    def test_surge_redirects_away_from_closest_er(self):
        from app import scenarios
        with patch.dict(os.environ,{"GEMINI_API_KEY":""}):
            scenarios.set_scenario("normal")
            normal=self.client.post("/api/recommend",json={"injury_description":"sprained ankle","acuity":3}).json()
            scenarios.set_scenario("highway_accident")
            surge=self.client.post("/api/recommend",json={"injury_description":"sprained ankle","acuity":3}).json()
        scenarios.set_scenario("normal")
        self.assertEqual(normal["recommended_hospital_id"],"AMC")   # closest, normal load
        self.assertNotEqual(surge["recommended_hospital_id"],"AMC") # diverted by the surge
        self.assertEqual(surge["decision_source"],"algorithm")

    def test_trauma_still_goes_to_trauma_center_during_surge(self):
        from app import scenarios
        scenarios.set_scenario("highway_accident")
        with patch.dict(os.environ,{"GEMINI_API_KEY":""}):
            r=self.client.post("/api/recommend",json={"injury_description":"crash","acuity":2,"needs_trauma":True}).json()
        scenarios.set_scenario("normal")
        self.assertEqual(r["recommended_hospital_id"],"AMC")

    def test_priority_ordering_urgent_patients_skip_minor_queue(self):
        from app import triage
        waiting={1:0,2:0,3:0,4:20,5:20}
        self.assertEqual(triage.estimate_wait(waiting,4,2,1.0),0)   # minor patients don't delay acuity 2
        self.assertGreater(triage.estimate_wait(waiting,4,5,1.0),0)

    def test_gemini_pick_outside_eligible_set_is_rejected(self):
        from app import gemini
        async def fake(system,payload,max_tokens=900):
            return {"hospital_id":"H99","summary":"x","next_steps":["a","b","c"]}
        with patch.object(triage_mod.gemini,"generate_json",fake):
            r=self.client.post("/api/recommend",json={"injury_description":"cut","acuity":4}).json()
        self.assertEqual(r["decision_source"],"algorithm")

    def test_gemini_valid_pick_is_used(self):
        async def fake(system,payload,max_tokens=900):
            return {"hospital_id":"SPH","summary":"Go to St. Peter's.","next_steps":["a","b","c"]}
        with patch.object(triage_mod.gemini,"generate_json",fake):
            r=self.client.post("/api/recommend",json={"injury_description":"cut","acuity":4}).json()
        self.assertEqual((r["decision_source"],r["recommended_hospital_id"]),("gemini","SPH"))

    def test_acuity_one_flags_911(self):
        with patch.dict(os.environ,{"GEMINI_API_KEY":""}):
            r=self.client.post("/api/recommend",json={"injury_description":"not breathing","acuity":1}).json()
        self.assertTrue(r["call_911"])

    def test_gemini_failure_defaults_to_urgent_not_minor(self):
        with patch.dict(os.environ,{"GEMINI_API_KEY":""}):
            r=self.client.post("/api/recommend",json={"injury_description":"chest tightness"}).json()
        self.assertEqual(r["patient"]["acuity"],2)

    async def _csv_ref(self):
        return triage_mod._load_csv_reference(), "bundled_csv"

    def _db_rows(self, scenario="normal", edit=None):
        from app import scenarios
        rows=[r for r in scenarios.rows_for_export() if r["scenario"]==scenario]
        out=[]
        for r in rows:
            r=dict(r, observed_at="2026-01-01T00:00:00.000+00:00")
            if edit: edit(r)
            out.append({k:r[k] for k in ("hospital_id","acuity","waiting_count","active_clinicians","travel_minutes","surge","observed_at")})
        return out

    def test_databricks_status_drives_the_recommendation(self):
        from app import scenarios
        def make_amc_busy(r):
            if r["hospital_id"]=="AMC" and r["acuity"]==3: r["waiting_count"]=40
        async def fake_execute(statement, parameters=None):
            self.assertIn("WHERE scenario = :scenario", statement)
            return self._db_rows("normal", make_amc_busy)
        scenarios.set_scenario("normal")
        with patch.dict(os.environ,{"DATA_MODE":"databricks","GEMINI_API_KEY":""}), patch.object(scenarios,"execute",fake_execute), \
             patch.object(triage_mod,"load_reference",self._csv_ref):
            r=self.client.post("/api/recommend",json={"injury_description":"cut","acuity":3}).json()
        self.assertEqual(r["status_source"],"databricks")
        self.assertNotEqual(r["recommended_hospital_id"],"AMC")

    def test_databricks_failure_falls_back_and_says_so(self):
        from app import scenarios
        from fastapi import HTTPException
        async def boom(*a,**k): raise HTTPException(503,"down")
        async def csv_ref(): return triage_mod._load_csv_reference(), "bundled_csv"
        scenarios.set_scenario("normal")
        with patch.dict(os.environ,{"DATA_MODE":"databricks","GEMINI_API_KEY":""}), patch.object(scenarios,"execute",boom), \
             patch.object(triage_mod,"load_reference",csv_ref):
            r=self.client.post("/api/recommend",json={"injury_description":"cut","acuity":3}).json()
        self.assertIn("Databricks query failed",r["status_source"])
        self.assertEqual(r["recommended_hospital_id"],"AMC")

    def test_incomplete_databricks_scenario_is_rejected(self):
        from app import scenarios
        async def partial(statement, parameters=None):
            return [r for r in self._db_rows("normal") if r["acuity"]!=3]
        with patch.dict(os.environ,{"DATA_MODE":"databricks"}), patch.object(scenarios,"execute",partial):
            with self.assertRaises(ValueError):
                asyncio.run(scenarios.databricks_snapshot("normal"))

    def test_triage_page(self):
        self.assertIn("FastER",self.client.get("/triage").text)

    def test_homepage(self):
        self.assertIn("Synthetic demonstration only",self.client.get("/").text)

if __name__ == "__main__":
    unittest.main()
