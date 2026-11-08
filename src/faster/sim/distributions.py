"""Stochastic distributions and clinical generative functions for ER simulation.

Models diurnal arrival waves, acuity distributions, complaint mappings,
log-normal clinical service times, and patient reneging (LWBS).
"""

from __future__ import annotations

import math
import random
from datetime import datetime
from typing import Tuple

from .models import ChiefComplaint, ESILevel, Patient, PatientStatus, VitalSigns


class ClinicalDistributions:
    """Provides validated clinical distributions for emergency care simulation."""

    def __init__(self, seed: int | None = None):
        self.rng = random.Random(seed)

    def get_hourly_arrival_multiplier(self, hour: int) -> float:
        """Returns diurnal arrival multiplier (1.0 = average rate).
        
        Ref: CDC National Hospital Ambulatory Medical Care Survey (NHAMCS).
        Lowest in early morning (03:00-06:00), peaks mid-afternoon to evening (12:00-20:00).
        """
        hourly_curve = [
            0.45, 0.35, 0.28, 0.22, 0.20, 0.25,  # 00:00 - 05:00
            0.40, 0.65, 0.95, 1.25, 1.45, 1.55,  # 06:00 - 11:00
            1.60, 1.58, 1.55, 1.52, 1.50, 1.48,  # 12:00 - 17:00
            1.42, 1.35, 1.20, 1.00, 0.78, 0.60   # 18:00 - 23:00
        ]
        return hourly_curve[hour % 24]

    def sample_arrival_count(self, base_arrivals_per_hour: float, current_time: datetime, delta_minutes: int) -> int:
        """Samples number of patient arrivals during a delta_minutes step using Poisson distribution."""
        multiplier = self.get_hourly_arrival_multiplier(current_time.hour)
        lambda_step = (base_arrivals_per_hour * multiplier) * (delta_minutes / 60.0)
        
        # Knuth's algorithm for Poisson random variable
        l_val = math.exp(-lambda_step)
        k = 0
        p = 1.0
        while p > l_val:
            k += 1
            p *= self.rng.random()
        return k - 1

    def sample_patient_profile(self, arrival_time: datetime, patient_index: int) -> Patient:
        """Generates a clinically realistic patient profile."""
        # 18% arrive via EMS ambulance; 82% walk in
        is_ems = self.rng.random() < 0.18
        arrival_mode = "EMS_AMBULANCE" if is_ems else "WALK_IN"

        # ESI level distribution differs strongly by arrival mode
        if is_ems:
            esi = self.rng.choices(
                [ESILevel.RESUSCITATION, ESILevel.EMERGENT, ESILevel.URGENT, ESILevel.LESS_URGENT, ESILevel.NON_URGENT],
                weights=[0.06, 0.38, 0.44, 0.10, 0.02]
            )[0]
        else:
            esi = self.rng.choices(
                [ESILevel.RESUSCITATION, ESILevel.EMERGENT, ESILevel.URGENT, ESILevel.LESS_URGENT, ESILevel.NON_URGENT],
                weights=[0.005, 0.12, 0.50, 0.26, 0.115]
            )[0]

        age = self._sample_age_for_esi(esi)
        gender = self.rng.choice(["M", "F"])
        complaint = self._sample_complaint_for_esi(esi)
        vitals = self._sample_vitals_for_esi(esi)

        # Resource needs & admission prediction
        requires_imaging = esi in (ESILevel.RESUSCITATION, ESILevel.EMERGENT, ESILevel.URGENT) and self.rng.random() < 0.65
        requires_labs = esi in (ESILevel.RESUSCITATION, ESILevel.EMERGENT, ESILevel.URGENT) and self.rng.random() < 0.85
        
        # Ground truth admission probability (Yale Emergency Department dataset benchmark)
        adm_prob = self._calculate_admission_probability(esi, age, vitals)
        will_admit = self.rng.random() < adm_prob

        return Patient(
            id=f"PAT-{arrival_time.strftime('%H%M')}-{patient_index:04d}",
            arrival_time=arrival_time,
            arrival_mode=arrival_mode,
            age=age,
            gender=gender,
            esi=esi,
            chief_complaint=complaint.value,
            vitals=vitals,
            status=PatientStatus.TRIAGE_WAIT,
            requires_imaging=requires_imaging,
            requires_labs=requires_labs,
            admission_probability=round(adm_prob, 2),
            will_admit=will_admit,
            deterioration_risk=0.15 if (esi == ESILevel.URGENT and vitals.is_abnormal) else 0.03
        )

    def _sample_age_for_esi(self, esi: ESILevel) -> int:
        if esi in (ESILevel.RESUSCITATION, ESILevel.EMERGENT):
            return int(self.rng.gauss(62, 14))
        if esi == ESILevel.URGENT:
            return int(self.rng.gauss(48, 18))
        return int(self.rng.gauss(34, 16))

    def _sample_complaint_for_esi(self, esi: ESILevel) -> ChiefComplaint:
        if esi == ESILevel.RESUSCITATION:
            return self.rng.choices(
                [ChiefComplaint.CARDIAC_ARREST, ChiefComplaint.SEVERE_TRAUMA, ChiefComplaint.RESPIRATORY_DISTRESS],
                weights=[0.50, 0.35, 0.15]
            )[0]
        if esi == ESILevel.EMERGENT:
            return self.rng.choices(
                [ChiefComplaint.STROKE_SYMPTOMS, ChiefComplaint.CHEST_PAIN, ChiefComplaint.RESPIRATORY_DISTRESS, ChiefComplaint.ABDOMINAL_PAIN],
                weights=[0.25, 0.40, 0.20, 0.15]
            )[0]
        if esi == ESILevel.URGENT:
            return self.rng.choices(
                [ChiefComplaint.ABDOMINAL_PAIN, ChiefComplaint.FRACTURE_DISLOCATION, ChiefComplaint.FEVER_INFECTION, ChiefComplaint.CHEST_PAIN],
                weights=[0.40, 0.25, 0.25, 0.10]
            )[0]
        if esi == ESILevel.LESS_URGENT:
            return self.rng.choices(
                [ChiefComplaint.LACERATION_MINOR, ChiefComplaint.MILD_HEADACHE, ChiefComplaint.FRACTURE_DISLOCATION],
                weights=[0.50, 0.30, 0.20]
            )[0]
        return self.rng.choices(
            [ChiefComplaint.MEDICATION_REFILL, ChiefComplaint.LACERATION_MINOR, ChiefComplaint.MILD_HEADACHE],
            weights=[0.60, 0.25, 0.15]
        )[0]

    def _sample_vitals_for_esi(self, esi: ESILevel) -> VitalSigns:
        pain = self.rng.randint(0, 3)
        if esi == ESILevel.RESUSCITATION:
            return VitalSigns(
                heart_rate=self.rng.choice([0, self.rng.randint(130, 175)]),
                systolic_bp=self.rng.randint(50, 85),
                diastolic_bp=self.rng.randint(30, 50),
                respiratory_rate=self.rng.randint(4, 38),
                o2_saturation=round(self.rng.uniform(70.0, 88.0), 1),
                pain_score=0,
                is_abnormal=True
            )
        if esi == ESILevel.EMERGENT:
            abnormal = self.rng.random() < 0.85
            return VitalSigns(
                heart_rate=self.rng.randint(105, 140) if abnormal else self.rng.randint(70, 95),
                systolic_bp=self.rng.randint(155, 210) if abnormal else self.rng.randint(110, 135),
                diastolic_bp=self.rng.randint(95, 120) if abnormal else self.rng.randint(70, 85),
                respiratory_rate=self.rng.randint(22, 32) if abnormal else 16,
                o2_saturation=round(self.rng.uniform(89.0, 93.0), 1) if abnormal else round(self.rng.uniform(96.0, 100.0), 1),
                pain_score=self.rng.randint(7, 10),
                is_abnormal=abnormal
            )
        if esi == ESILevel.URGENT:
            abnormal = self.rng.random() < 0.25
            return VitalSigns(
                heart_rate=self.rng.randint(95, 115) if abnormal else self.rng.randint(65, 88),
                systolic_bp=self.rng.randint(135, 150) if abnormal else self.rng.randint(115, 130),
                diastolic_bp=self.rng.randint(85, 95) if abnormal else self.rng.randint(70, 82),
                respiratory_rate=self.rng.randint(18, 22),
                o2_saturation=round(self.rng.uniform(94.0, 98.0), 1),
                pain_score=self.rng.randint(4, 7),
                is_abnormal=abnormal
            )
        # ESI 4 & 5
        return VitalSigns(
            heart_rate=self.rng.randint(60, 85),
            systolic_bp=self.rng.randint(110, 128),
            diastolic_bp=self.rng.randint(68, 80),
            respiratory_rate=14,
            o2_saturation=round(self.rng.uniform(98.0, 100.0), 1),
            pain_score=self.rng.randint(1, 4),
            is_abnormal=False
        )

    def _calculate_admission_probability(self, esi: ESILevel, age: int, vitals: VitalSigns) -> float:
        base = {
            ESILevel.RESUSCITATION: 0.95,
            ESILevel.EMERGENT: 0.72,
            ESILevel.URGENT: 0.32,
            ESILevel.LESS_URGENT: 0.07,
            ESILevel.NON_URGENT: 0.02
        }[esi]
        # Age modifier
        age_factor = 0.15 if age >= 65 else (-0.05 if age < 30 else 0.0)
        # Vitals modifier
        vitals_factor = 0.10 if vitals.is_abnormal else 0.0
        return max(0.01, min(0.99, base + age_factor + vitals_factor))

    # --- Service Duration Samplers (Log-normal distributions) ---

    def sample_triage_duration_minutes(self) -> float:
        """Triage assessment takes 4-9 minutes."""
        return max(3.0, self.rng.lognormvariate(math.log(6.0), 0.25))

    def sample_provider_initial_minutes(self, esi: ESILevel) -> float:
        """Physician bedside evaluation duration in minutes."""
        medians = {
            ESILevel.RESUSCITATION: 35.0,
            ESILevel.EMERGENT: 22.0,
            ESILevel.URGENT: 15.0,
            ESILevel.LESS_URGENT: 8.0,
            ESILevel.NON_URGENT: 5.0
        }
        med = medians[esi]
        return max(4.0, self.rng.lognormvariate(math.log(med), 0.30))

    def sample_diagnostics_duration_minutes(self, patient: Patient) -> float:
        """Turnaround time for lab results and imaging reads."""
        if not (patient.requires_labs or patient.requires_imaging):
            return 0.0
        median = 40.0
        if patient.requires_imaging:
            median += 35.0
        if patient.requires_labs:
            median += 20.0
        return max(15.0, self.rng.lognormvariate(math.log(median), 0.28))

    def sample_boarding_duration_minutes(self) -> float:
        """Time an admitted patient waits for an inpatient hospital bed."""
        # Mean 3 hours, wide distribution
        return max(30.0, self.rng.lognormvariate(math.log(180.0), 0.50))

    def sample_bed_turnover_minutes(self) -> float:
        """Time required by environmental services to clean and sanitize an ER bay."""
        return max(5.0, self.rng.lognormvariate(math.log(12.0), 0.30))

    # --- Patient Reneging (LWBS: Left Without Being Seen) ---

    def check_renege(self, patient: Patient, current_wait_minutes: float, delta_minutes: int) -> bool:
        """Evaluates whether an unassigned patient walks out due to excessive waiting time.
        
        ESI 1 and 2 almost never leave. ESI 4 & 5 leave with increasing hazard after 45-60 min.
        """
        if patient.esi in (ESILevel.RESUSCITATION, ESILevel.EMERGENT):
            return False

        # Tolerance thresholds (minutes)
        threshold = {
            ESILevel.URGENT: 150.0,
            ESILevel.LESS_URGENT: 60.0,
            ESILevel.NON_URGENT: 35.0
        }[patient.esi]

        if current_wait_minutes < threshold:
            return False

        # Increasing hazard rate as wait exceeds threshold
        excess = current_wait_minutes - threshold
        hazard_per_minute = 0.005 + (excess / 60.0) * 0.015
        hazard_step = min(0.40, hazard_per_minute * delta_minutes)

        return self.rng.random() < hazard_step
