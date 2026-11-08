"""Queue prioritization algorithms and strategies for emergency department care.

Provides pluggable strategies ranging from standard strict ESI queuing
to dynamic multi-factor clinical scoring and fast-track routing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import List

from .models import ESILevel, Patient, PatientStatus


class BasePriorityStrategy(ABC):
    """Abstract interface for queue prioritization algorithms."""

    @abstractmethod
    def calculate_priority_score(self, patient: Patient, as_of: datetime) -> float:
        """Calculates a numerical priority score for the patient (higher = served first)."""
        pass

    def rank_queue(self, patients: List[Patient], as_of: datetime) -> List[Patient]:
        """Ranks a list of waiting patients according to the priority policy."""
        # Calculate and cache score on patient object for transparency/explainability
        for p in patients:
            p.priority_score = self.calculate_priority_score(p, as_of)
        
        # Sort descending by priority score; tie-break on earliest arrival time
        return sorted(patients, key=lambda p: (p.priority_score, -p.arrival_time.timestamp()), reverse=True)


class StrictESIPriorityStrategy(BasePriorityStrategy):
    """Classic ESI policy: Strict hierarchical ordering by triage tier (1 -> 5).
    
    Within each ESI tier, patients are served first-come-first-served (FCFS).
    """

    def calculate_priority_score(self, patient: Patient, as_of: datetime) -> float:
        base = {
            ESILevel.RESUSCITATION: 100000.0,
            ESILevel.EMERGENT: 10000.0,
            ESILevel.URGENT: 1000.0,
            ESILevel.LESS_URGENT: 100.0,
            ESILevel.NON_URGENT: 10.0,
        }[patient.esi]

        # Fractional tie-breaker based on waiting time (up to 9.99 points for 1000 mins)
        wait_min = patient.wait_minutes(as_of)
        return base + min(9.9, wait_min * 0.01)


class DynamicClinicalPriorityStrategy(BasePriorityStrategy):
    """Modern clinical priority policy balancing acuity, elapsed wait, and clinical risk.
    
    Prevents prolonged starvation of ESI 3/4 patients while strictly guaranteeing
    immediate resuscitation for ESI 1 and fast-track protection for unstable ESI 2.
    """

    def __init__(
        self,
        weight_esi: float = 1.0,
        weight_wait: float = 1.2,
        weight_vitals: float = 120.0,
        weight_pain: float = 5.0,
    ):
        self.w_esi = weight_esi
        self.w_wait = weight_wait
        self.w_vitals = weight_vitals
        self.w_pain = weight_pain

    def calculate_priority_score(self, patient: Patient, as_of: datetime) -> float:
        # Immediate preemption for ESI 1 (Life-threatening resuscitation)
        if patient.esi == ESILevel.RESUSCITATION:
            return 100000.0 + patient.wait_minutes(as_of)

        base_acuity = {
            ESILevel.EMERGENT: 1500.0,
            ESILevel.URGENT: 300.0,
            ESILevel.LESS_URGENT: 60.0,
            ESILevel.NON_URGENT: 10.0,
        }[patient.esi] * self.w_esi

        wait_min = patient.wait_minutes(as_of)
        
        # Target wait standards (in minutes): ESI 2: 10m, ESI 3: 45m, ESI 4: 90m, ESI 5: 120m
        target_wait = {
            ESILevel.EMERGENT: 10.0,
            ESILevel.URGENT: 45.0,
            ESILevel.LESS_URGENT: 90.0,
            ESILevel.NON_URGENT: 120.0,
        }[patient.esi]

        # Escalation acceleration: as wait time exceeds target, penalty grows non-linearly
        wait_ratio = max(0.0, wait_min / target_wait)
        wait_penalty = (wait_min * 1.5 + (wait_ratio ** 1.4) * 20.0) * self.w_wait

        # Clinical instability bonus
        vitals_bonus = self.w_vitals if patient.vitals.is_abnormal else 0.0
        
        # Pain distress bonus (for severe acute pain: score 7-10)
        pain_bonus = (patient.vitals.pain_score * self.w_pain) if patient.vitals.pain_score >= 7 else 0.0

        # Deterioration risk bonus
        deterioration_bonus = patient.deterioration_risk * 200.0

        return base_acuity + wait_penalty + vitals_bonus + pain_bonus + deterioration_bonus


class FastTrackPriorityStrategy(BasePriorityStrategy):
    """Dual-track queue policy.
    
    Routes ESI 4 & 5 to dedicated low-resource queues to prevent them
    from being perpetually blocked by acute arrivals.
    """

    def __init__(self, inner_strategy: BasePriorityStrategy | None = None):
        self.inner = inner_strategy or DynamicClinicalPriorityStrategy()

    def calculate_priority_score(self, patient: Patient, as_of: datetime) -> float:
        return self.inner.calculate_priority_score(patient, as_of)

    def separate_tracks(self, patients: List[Patient], as_of: datetime) -> tuple[List[Patient], List[Patient]]:
        """Splits queue into (acute_track, fast_track)."""
        acute = [p for p in patients if p.esi in (ESILevel.RESUSCITATION, ESILevel.EMERGENT, ESILevel.URGENT)]
        fast = [p for p in patients if p.esi in (ESILevel.LESS_URGENT, ESILevel.NON_URGENT)]
        return self.rank_queue(acute, as_of), self.rank_queue(fast, as_of)
