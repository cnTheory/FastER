"""Regional multi-hospital network simulation and patient routing orchestrator.

Coordinates synchronized simulation steps across multiple independent hospitals,
manages regional event bubbling, detects ambulance diversion, and computes
optimal destination recommendations based on travel time and dynamic ER wait times.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Set

from .engine import ERSimulation
from .events import EventBus, LiveEventBuffer, OperationalMetricsSink
from .models import (
    ESILevel,
    EventType,
    HospitalRoutingRecommendation,
    HospitalStateSnapshot,
    Patient,
    PatientStatus,
    RegionalNetworkSnapshot,
    SimulationEvent,
)


class HospitalNetworkSimulation:
    """Orchestrates multiple emergency departments in a regional healthcare network."""

    def __init__(
        self,
        hospitals: Optional[List[ERSimulation]] = None,
        start_time: Optional[datetime] = None,
    ):
        self.current_time = start_time or datetime(2026, 11, 8, 9, 0, 0)
        self.hospitals: Dict[str, ERSimulation] = {}
        
        # Unified telemetry bus and observers across all hospitals
        self.network_event_bus = EventBus()
        self.unified_metrics = OperationalMetricsSink()
        self.unified_live_events = LiveEventBuffer(max_size=300)
        
        self.network_event_bus.subscribe(self.unified_metrics.on_event)
        self.network_event_bus.subscribe(self.unified_live_events.on_event)

        if hospitals:
            for h in hospitals:
                self.add_hospital(h)

    def add_hospital(
        self,
        hospital: ERSimulation,
        travel_minutes: Optional[float] = None,
        capabilities: Optional[List[str]] = None,
    ) -> None:
        """Registers a hospital into the regional network without conflicts."""
        if travel_minutes is not None:
            hospital.travel_minutes = travel_minutes
        if capabilities is not None:
            hospital.capabilities = capabilities
            
        hospital.current_time = self.current_time
        self.hospitals[hospital.hospital_id] = hospital

        # Forward member hospital events up to the network event bus
        hospital.subscribe(self._forward_hospital_event)

    def _forward_hospital_event(self, event: SimulationEvent) -> None:
        """Bubbles an event from a member hospital to network subscribers."""
        self.network_event_bus.publish(event)

    def subscribe(
        self,
        handler: Callable[[SimulationEvent], None],
        event_types: Optional[Set[EventType]] = None,
    ) -> None:
        """Subscribes an observer to all events occurring across the regional network."""
        self.network_event_bus.subscribe(handler, event_types)

    # --- Synchronized Execution ---

    def step(self, delta_minutes: int = 1) -> None:
        """Advances all hospitals in synchronized lockstep and evaluates EMS diversions."""
        self.current_time += timedelta(minutes=delta_minutes)
        
        # Advance each hospital
        for hospital in self.hospitals.values():
            hospital.step(delta_minutes=delta_minutes)

        # Check for inter-hospital ambulance diversions
        self._evaluate_regional_diversions()

    def run_until(self, target_time: datetime, step_minutes: int = 1) -> None:
        """Runs the entire network forward to target_time in synchronized batch mode."""
        while self.current_time < target_time:
            self.step(delta_minutes=step_minutes)

    def _evaluate_regional_diversions(self) -> None:
        """Diverts newly arrived EMS ambulances away from overcrowded hospitals on diversion."""
        for hid, sim in self.hospitals.items():
            if not sim.is_on_diversion:
                continue

            # Identify newly arrived EMS ambulance patients waiting for intake at the diverting hospital
            divert_candidates = [
                p for p in sim.patients.values()
                if p.status in (PatientStatus.TRIAGE_WAIT, PatientStatus.TRIAGE_ACTIVE)
                and p.arrival_mode == "EMS_AMBULANCE"
                and p.esi in (ESILevel.EMERGENT, ESILevel.URGENT)
                and (sim.current_time - p.arrival_time).total_seconds() <= 300.0
            ]

            for patient in divert_candidates:
                target_hospital = self._find_best_alternate_hospital(exclude_id=hid, patient=patient)
                if not target_hospital:
                    # All hospitals on diversion or no matching capability -> must remain
                    continue

                # Remove from origin hospital and free triage staff
                sim.patients.pop(patient.id, None)
                sim._triage_completions.pop(patient.id, None)
                for staff_member in sim.staff.values():
                    if patient.id in staff_member.active_patient_ids:
                        staff_member.active_patient_ids.remove(patient.id)

                # Transfer to destination hospital
                target_hospital.receive_external_patient(patient)

                # Emit diversion event
                divert_event = SimulationEvent(
                    timestamp=self.current_time,
                    hospital_id=hid,
                    event_type=EventType.PATIENT_DIVERTED,
                    patient_id=patient.id,
                    data={
                        "origin_hospital_id": hid,
                        "origin_name": sim.name,
                        "destination_hospital_id": target_hospital.hospital_id,
                        "destination_name": target_hospital.name,
                        "esi": patient.esi.value,
                        "chief_complaint": patient.chief_complaint,
                        "reason": f"Ambulance Diversion (NEDOCS: {sim.get_snapshot().nedocs_score:.1f})",
                    },
                )
                self.network_event_bus.publish(divert_event)

    def _find_best_alternate_hospital(self, exclude_id: str, patient: Patient) -> Optional[ERSimulation]:
        """Finds candidate non-diverting hospital with shortest travel + wait time."""
        candidates = [
            h for h_id, h in self.hospitals.items()
            if h_id != exclude_id and not h.is_on_diversion
        ]
        if not candidates:
            return None

        # Sort by (travel_minutes + predicted_wait for patient's acuity)
        def score(h: ERSimulation) -> float:
            snap = h.get_snapshot()
            pred = snap.predicted_wait_times.get(patient.esi.value)
            wait = pred.estimated_wait_minutes if pred else 30.0
            return h.travel_minutes + wait

        candidates.sort(key=score)
        return candidates[0]

    # --- Regional Analytics & State Snapshot ---

    def get_network_snapshot(self) -> RegionalNetworkSnapshot:
        """Produces a comprehensive real-time comparison snapshot across all network hospitals."""
        hospital_snapshots: Dict[str, HospitalStateSnapshot] = {}
        recommendations: List[HospitalRoutingRecommendation] = []
        diverting_ids: List[str] = []

        total_waiting = 0
        total_boarders = 0
        total_treatment = 0

        for hid, sim in self.hospitals.items():
            snap = sim.get_snapshot()
            hospital_snapshots[hid] = snap

            total_waiting += snap.waiting_room_count
            total_boarders += snap.active_boarders_in_er
            total_treatment += snap.in_treatment_count

            if snap.is_on_diversion:
                diverting_ids.append(hid)

            # Benchmark wait time using ESI 3 (Urgent)
            urgent_pred = snap.predicted_wait_times.get(ESILevel.URGENT.value)
            wait_min = urgent_pred.estimated_wait_minutes if urgent_pred else 25.0
            total_time = snap.travel_minutes + wait_min

            recommendations.append(
                HospitalRoutingRecommendation(
                    hospital_id=hid,
                    name=snap.name,
                    travel_minutes=snap.travel_minutes,
                    estimated_wait_minutes=round(wait_min, 1),
                    total_minutes=round(total_time, 1),
                    bed_utilization_pct=snap.bed_utilization_pct,
                    active_boarders_in_er=snap.active_boarders_in_er,
                    is_on_diversion=snap.is_on_diversion,
                    nedocs_score=snap.nedocs_score,
                    overcrowding_level=snap.overcrowding_level,
                    capabilities=snap.capabilities,
                )
            )

        # Determine optimal destination hospital (eligible non-diverting with lowest total time)
        eligible = [r for r in recommendations if not r.is_on_diversion]
        best_id = min(eligible, key=lambda r: r.total_minutes).hospital_id if eligible else None

        # Sort recommendations by total travel + wait time
        recommendations.sort(key=lambda r: (r.is_on_diversion, r.total_minutes))

        return RegionalNetworkSnapshot(
            timestamp=self.current_time,
            hospitals=hospital_snapshots,
            routing_recommendations=recommendations,
            best_hospital_id=best_id,
            total_regional_waiting=total_waiting,
            total_regional_boarders=total_boarders,
            total_regional_in_treatment=total_treatment,
            hospitals_on_diversion=diverting_ids,
        )

    # --- Pre-configured Network Factory ---

    @classmethod
    def create_default_network(
        cls,
        start_time: Optional[datetime] = None,
        seed: int = 42,
    ) -> HospitalNetworkSimulation:
        """Instantiates a realistic 3-hospital regional emergency network."""
        t0 = start_time or datetime(2026, 11, 8, 9, 0, 0)
        
        # 1. Metro Trauma & Academic Center (Level 1 Trauma, high capacity, high volume)
        metro = ERSimulation(
            hospital_id="H1-METRO",
            name="Metro Trauma Center",
            er_beds_count=24,
            inpatient_beds_available=5,
            base_arrivals_per_hour=7.0,
            travel_minutes=14.0,
            capabilities=["general", "imaging", "trauma", "pediatric"],
            start_time=t0,
            seed=seed,
        )

        # 2. Riverside Community Hospital (Medium community hospital, close proximity)
        riverside = ERSimulation(
            hospital_id="H2-RIVERSIDE",
            name="Riverside Community ED",
            er_beds_count=16,
            inpatient_beds_available=3,
            base_arrivals_per_hour=4.5,
            travel_minutes=8.0,
            capabilities=["general", "imaging"],
            start_time=t0,
            seed=seed + 1,
        )

        # 3. Hillcrest Regional Hospital (Suburban community facility, slightly farther)
        hillcrest = ERSimulation(
            hospital_id="H3-HILLCREST",
            name="Hillcrest Regional ED",
            er_beds_count=12,
            inpatient_beds_available=2,
            base_arrivals_per_hour=3.2,
            travel_minutes=18.0,
            capabilities=["general", "imaging", "pediatric"],
            start_time=t0,
            seed=seed + 2,
        )

        network = cls(hospitals=[metro, riverside, hillcrest], start_time=t0)
        return network
