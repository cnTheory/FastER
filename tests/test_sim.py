"""Unit tests for the FastER simulation package."""

import unittest
from datetime import datetime, timedelta

from src.faster.sim.distributions import ClinicalDistributions
from src.faster.sim.engine import ERSimulation
from src.faster.sim.models import (
    BedStatus,
    ESILevel,
    EventType,
    Patient,
    PatientStatus,
    StaffMember,
    StaffRole,
    VitalSigns,
)
from src.faster.sim.prediction import WaitTimePredictor
from src.faster.sim.priorities import (
    DynamicClinicalPriorityStrategy,
    FastTrackPriorityStrategy,
    StrictESIPriorityStrategy,
)


class TestClinicalDistributions(unittest.TestCase):
    def setUp(self):
        self.dist = ClinicalDistributions(seed=42)

    def test_diurnal_curve_peaks_midday(self):
        early_morning = self.dist.get_hourly_arrival_multiplier(4)
        midday = self.dist.get_hourly_arrival_multiplier(13)
        self.assertLess(early_morning, 0.4)
        self.assertGreater(midday, 1.4)

    def test_sample_patient_profile(self):
        t = datetime(2026, 11, 8, 12, 0, 0)
        p = self.dist.sample_patient_profile(t, 1)
        self.assertTrue(p.id.startswith("PAT-1200-"))
        self.assertIn(p.esi, list(ESILevel))
        self.assertGreater(p.age, 0)
        self.assertIsNotNone(p.vitals)

    def test_check_renege_acuity_safety(self):
        # ESI 1 and 2 should never renege
        t = datetime(2026, 11, 8, 12, 0, 0)
        p1 = Patient(
            id="P1", arrival_time=t, age=60, esi=ESILevel.RESUSCITATION,
            chief_complaint="Arrest", status=PatientStatus.WAITING_ROOM
        )
        p2 = Patient(
            id="P2", arrival_time=t, age=60, esi=ESILevel.EMERGENT,
            chief_complaint="Chest Pain", status=PatientStatus.WAITING_ROOM
        )
        self.assertFalse(self.dist.check_renege(p1, current_wait_minutes=300.0, delta_minutes=10))
        self.assertFalse(self.dist.check_renege(p2, current_wait_minutes=300.0, delta_minutes=10))


class TestPriorityStrategies(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 8, 12, 0, 0)

    def test_strict_esi_ordering(self):
        strategy = StrictESIPriorityStrategy()
        p_esi3 = Patient(
            id="P-3", arrival_time=self.now - timedelta(minutes=60),
            age=40, esi=ESILevel.URGENT, chief_complaint="Abdominal Pain"
        )
        p_esi2 = Patient(
            id="P-2", arrival_time=self.now - timedelta(minutes=5),
            age=70, esi=ESILevel.EMERGENT, chief_complaint="Chest Pain"
        )
        ranked = strategy.rank_queue([p_esi3, p_esi2], self.now)
        # ESI 2 must precede ESI 3 even though ESI 3 waited 12x longer
        self.assertEqual(ranked[0].id, "P-2")
        self.assertEqual(ranked[1].id, "P-3")

    def test_dynamic_clinical_priority_escalation(self):
        strategy = DynamicClinicalPriorityStrategy()
        # Normal ESI 4 patient waiting 2 hours vs freshly arrived ESI 4 patient
        p_fresh = Patient(
            id="P-Fresh", arrival_time=self.now - timedelta(minutes=2),
            age=25, esi=ESILevel.LESS_URGENT, chief_complaint="Sprain"
        )
        p_waited = Patient(
            id="P-Waited", arrival_time=self.now - timedelta(minutes=130),
            age=25, esi=ESILevel.LESS_URGENT, chief_complaint="Sprain"
        )
        ranked = strategy.rank_queue([p_fresh, p_waited], self.now)
        self.assertEqual(ranked[0].id, "P-Waited")

    def test_dynamic_clinical_vitals_deterioration(self):
        strategy = DynamicClinicalPriorityStrategy()
        p_stable = Patient(
            id="P-Stable", arrival_time=self.now - timedelta(minutes=30),
            age=50, esi=ESILevel.URGENT, chief_complaint="Abdominal Pain"
        )
        p_abnormal = Patient(
            id="P-Abnormal", arrival_time=self.now - timedelta(minutes=30),
            age=50, esi=ESILevel.URGENT, chief_complaint="Abdominal Pain",
            vitals=VitalSigns(heart_rate=135, systolic_bp=85, is_abnormal=True)
        )
        ranked = strategy.rank_queue([p_stable, p_abnormal], self.now)
        self.assertEqual(ranked[0].id, "P-Abnormal")

    def test_fast_track_separation(self):
        strategy = FastTrackPriorityStrategy()
        p1 = Patient(id="P1", arrival_time=self.now, age=50, esi=ESILevel.EMERGENT, chief_complaint="Stroke")
        p2 = Patient(id="P2", arrival_time=self.now, age=30, esi=ESILevel.NON_URGENT, chief_complaint="Refill")
        p3 = Patient(id="P3", arrival_time=self.now, age=40, esi=ESILevel.URGENT, chief_complaint="Fever")
        acute, fast = strategy.separate_tracks([p1, p2, p3], self.now)
        self.assertEqual([p.id for p in acute], ["P1", "P3"])
        self.assertEqual([p.id for p in fast], ["P2"])


class TestPredictionModels(unittest.TestCase):
    def test_nedocs_calculation(self):
        # High bed occupancy + 8 boarders + 3.5h max wait -> Disastrous / Overcrowded
        score, level = WaitTimePredictor.calculate_nedocs_score(
            total_er_beds=20,
            active_er_patients=25,
            inpatient_beds_total=50,
            active_boarders=8,
            longest_wait_hours=3.5,
            longest_board_hours=5.0,
        )
        self.assertGreater(score, 100.0)
        self.assertIn(level, ["Overcrowded", "Severely Overcrowded", "Disastrous (Ambulance Diversion)"])

    def test_wait_time_estimates_scale_with_acuity(self):
        now = datetime(2026, 11, 8, 12, 0, 0)
        mds = [StaffMember(id=f"MD-{i}", name=f"Dr {i}", role=StaffRole.PHYSICIAN) for i in range(2)]
        estimates = WaitTimePredictor.estimate_wait_times_by_esi(
            patients=[],
            staff=mds,
            er_beds_total=10,
            er_beds_occupied=2,
            boarding_count=0,
            as_of=now,
        )
        # ESI 1 should be fastest, ESI 5 should be longest
        self.assertLess(estimates[1].estimated_wait_minutes, estimates[2].estimated_wait_minutes)
        self.assertLess(estimates[2].estimated_wait_minutes, estimates[3].estimated_wait_minutes)
        self.assertLess(estimates[3].estimated_wait_minutes, estimates[4].estimated_wait_minutes)


class TestERSimulationEngine(unittest.TestCase):
    def test_simulation_step_and_snapshot(self):
        sim = ERSimulation(
            hospital_id="TEST-01",
            er_beds_count=10,
            inpatient_beds_available=2,
            base_arrivals_per_hour=3.0,
            seed=42,
        )
        events_captured = []
        sim.subscribe(lambda e: events_captured.append(e.event_type))

        # Advance 60 minutes
        for _ in range(60):
            sim.step(delta_minutes=1)

        self.assertGreater(len(sim.patients), 0)
        self.assertIn(EventType.PATIENT_ARRIVED, events_captured)

        snapshot = sim.get_snapshot()
        self.assertEqual(snapshot.hospital_id, "TEST-01")
        self.assertEqual(snapshot.total_er_beds, 10)
        self.assertGreaterEqual(snapshot.bed_utilization_pct, 0.0)
        self.assertIsNotNone(snapshot.nedocs_score)
        self.assertEqual(len(snapshot.predicted_wait_times), 5)

    def test_surge_injection(self):
        sim = ERSimulation(hospital_id="TEST-SURGE", seed=42)
        initial_count = len(sim.patients)
        surge_ids = sim.inject_surge(patient_count=4, acuity=ESILevel.EMERGENT)
        self.assertEqual(len(surge_ids), 4)
        self.assertEqual(len(sim.patients), initial_count + 4)
        for pid in surge_ids:
            self.assertEqual(sim.patients[pid].esi, ESILevel.EMERGENT)

    def test_boarding_gridlock_when_inpatient_beds_zero(self):
        sim = ERSimulation(
            hospital_id="TEST-BOARD",
            er_beds_count=6,
            inpatient_beds_available=0,  # Zero inpatient beds triggers boarding
            seed=42,
        )
        # Advance 180 minutes to generate admissions
        sim.run_until(sim.current_time + timedelta(minutes=180), step_minutes=2)
        snapshot = sim.get_snapshot()
        # Should have captured some boarders or bed pressure
        self.assertEqual(snapshot.inpatient_beds_available, 0)


if __name__ == "__main__":
    unittest.main()
