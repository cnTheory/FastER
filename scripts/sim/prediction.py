"""Clinical prediction models for ER wait times and crowding metrics.

Computes real-time dynamic door-to-provider wait time estimates by ESI tier,
expected length-of-stay (LOS), and the National Emergency Department
Overcrowding Scale (NEDOCS) score.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Dict, List, Optional

from .models import (
    ESILevel,
    HospitalStateSnapshot,
    Patient,
    PatientStatus,
    QueueItem,
    StaffMember,
    StaffRole,
    WaitTimeEstimate,
)


class WaitTimePredictor:
    """Predicts wait times and systemic delays conditioned on current ER state."""

    @staticmethod
    def estimate_wait_times_by_esi(
        patients: List[Patient],
        staff: List[StaffMember],
        er_beds_total: int,
        er_beds_occupied: int,
        boarding_count: int,
        as_of: datetime,
    ) -> Dict[int, WaitTimeEstimate]:
        """Calculates expected door-to-provider wait times for each ESI triage tier.
        
        Accounts for priority preemption, active clinician capacity, bed gridlock,
        and current boarding burden.
        """
        # Active physicians and current free capacity
        on_shift_mds = [s for s in staff if s.role == StaffRole.PHYSICIAN and s.on_shift]
        total_md_capacity = sum(s.max_concurrent_patients for s in on_shift_mds)
        total_md_active = sum(len(s.active_patient_ids) for s in on_shift_mds)
        available_md_slots = max(0, total_md_capacity - total_md_active)
        effective_physicians = max(1, len(on_shift_mds))

        # Available physical beds
        available_beds = max(0, er_beds_total - er_beds_occupied)
        
        # Boarding impact factor: boarders consume ER acute beds, locking throughput
        boarding_ratio = boarding_count / max(1, er_beds_total)
        boarding_multiplier = 1.0 + (boarding_ratio ** 1.3) * 2.5

        # Triage and waiting room patients
        waiting_patients = [
            p for p in patients
            if p.status in (PatientStatus.TRIAGE_WAIT, PatientStatus.TRIAGE_ACTIVE, PatientStatus.WAITING_ROOM)
        ]

        # Count queue depth by ESI
        counts_by_esi = {
            level: sum(1 for p in waiting_patients if p.esi == level)
            for level in ESILevel
        }

        # Average assessment start interval per clinician in minutes
        avg_service_mins = {
            ESILevel.RESUSCITATION: 5.0,   # Immediate resuscitation team response
            ESILevel.EMERGENT: 14.0,       # Initial emergent workup
            ESILevel.URGENT: 22.0,         # Detailed clinical exam
            ESILevel.LESS_URGENT: 12.0,    # Focused minor care
            ESILevel.NON_URGENT: 8.0,      # Rapid discharge
        }

        estimates: Dict[int, WaitTimeEstimate] = {}

        # 1. ESI 1: Immediate resuscitation (near zero wait time)
        resus_wait = 0.5 if counts_by_esi[ESILevel.RESUSCITATION] == 0 else 2.0
        estimates[ESILevel.RESUSCITATION.value] = WaitTimeEstimate(
            esi_level=1,
            estimated_wait_minutes=round(resus_wait, 1),
            confidence_interval_low=0.0,
            confidence_interval_high=round(resus_wait + 2.0, 1),
            queue_depth_ahead=counts_by_esi[ESILevel.RESUSCITATION]
        )

        # 2. ESI 2: Emergent
        # Preempts lower acuity, only waits behind other ESI 1 & ESI 2 patients
        esi2_queue = counts_by_esi[ESILevel.EMERGENT]
        if available_beds > 0 and available_md_slots > 0 and esi2_queue == 0:
            esi2_est = 4.0  # Triage transit time
        else:
            base_turnover = (esi2_queue * avg_service_mins[ESILevel.EMERGENT]) / effective_physicians
            esi2_est = max(5.0, (base_turnover * boarding_multiplier))
            
        estimates[ESILevel.EMERGENT.value] = WaitTimeEstimate(
            esi_level=2,
            estimated_wait_minutes=round(esi2_est, 1),
            confidence_interval_low=round(max(2.0, esi2_est * 0.7), 1),
            confidence_interval_high=round(esi2_est * 1.4, 1),
            queue_depth_ahead=counts_by_esi[ESILevel.RESUSCITATION] + esi2_queue
        )

        # 3. ESI 3: Urgent (The vast middle class of the ER - 45-50% of patients)
        # Waits behind ESI 1, ESI 2, and other ESI 3s. Vulnerable to ongoing ESI 2 arrivals.
        esi3_queue = counts_by_esi[ESILevel.URGENT]
        preempting_queue = counts_by_esi[ESILevel.EMERGENT] + esi3_queue
        work_units = (counts_by_esi[ESILevel.EMERGENT] * 18.0) + (esi3_queue * 20.0)
        esi3_est = max(10.0, (work_units / effective_physicians) * boarding_multiplier)
        
        # Add bed bottleneck penalty if hospital ER is above 90% bed capacity
        if available_beds <= 1:
            esi3_est += 25.0 * (1.0 + boarding_ratio)

        estimates[ESILevel.URGENT.value] = WaitTimeEstimate(
            esi_level=3,
            estimated_wait_minutes=round(esi3_est, 1),
            confidence_interval_low=round(max(5.0, esi3_est * 0.75), 1),
            confidence_interval_high=round(esi3_est * 1.5, 1),
            queue_depth_ahead=preempting_queue
        )

        # 4. ESI 4: Less Urgent
        # Heavy preemption risk from higher acuities
        esi4_queue = counts_by_esi[ESILevel.LESS_URGENT]
        higher_work = (counts_by_esi[ESILevel.EMERGENT] * 20.0) + (counts_by_esi[ESILevel.URGENT] * 18.0)
        esi4_est = max(15.0, ((higher_work * 0.8 + esi4_queue * 12.0) / effective_physicians) * (boarding_multiplier ** 1.2))
        
        estimates[ESILevel.LESS_URGENT.value] = WaitTimeEstimate(
            esi_level=4,
            estimated_wait_minutes=round(esi4_est, 1),
            confidence_interval_low=round(max(10.0, esi4_est * 0.70), 1),
            confidence_interval_high=round(esi4_est * 1.6, 1),
            queue_depth_ahead=preempting_queue + esi4_queue
        )

        # 5. ESI 5: Non-Urgent
        esi5_queue = counts_by_esi[ESILevel.NON_URGENT]
        esi5_est = max(20.0, esi4_est + (esi5_queue * 8.0) / effective_physicians)
        
        estimates[ESILevel.NON_URGENT.value] = WaitTimeEstimate(
            esi_level=5,
            estimated_wait_minutes=round(esi5_est, 1),
            confidence_interval_low=round(max(15.0, esi5_est * 0.65), 1),
            confidence_interval_high=round(esi5_est * 1.7, 1),
            queue_depth_ahead=len(waiting_patients)
        )

        return estimates

    @staticmethod
    def calculate_nedocs_score(
        total_er_beds: int,
        active_er_patients: int,
        inpatient_beds_total: int,
        active_boarders: int,
        longest_wait_hours: float,
        longest_board_hours: float,
        ventilator_count: int = 1,
    ) -> tuple[float, str]:
        """Calculates the National Emergency Department Overcrowding Scale (NEDOCS) score.
        
        Standard formula used nationally across hospital incident command centers.
        """
        be = max(1, total_er_beds)
        bh = max(1, inpatient_beds_total)
        pt = active_er_patients
        pa = active_boarders
        w = max(0.0, longest_wait_hours)
        a = max(0.0, longest_board_hours)
        v = ventilator_count

        raw_score = (
            (pt / be) * 85.8 +
            (pa / bh) * 600.0 +
            w * 5.64 +
            a * 0.93 +
            v * 13.4 -
            20.0
        )
        score = max(0.0, round(raw_score, 1))

        if score < 20:
            level = "Normal"
        elif score <= 60:
            level = "Busy"
        elif score <= 100:
            level = "Extremely Busy"
        elif score <= 140:
            level = "Overcrowded"
        elif score <= 180:
            level = "Severely Overcrowded"
        else:
            level = "Disastrous (Ambulance Diversion)"

        return score, level
