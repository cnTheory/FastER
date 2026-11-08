"""Core discrete-event simulation engine for hospital emergency departments.

Integrates arrival generation, clinical staging, queue prioritization,
clinician workload balancing, inpatient boarding, and telemetry streaming.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

from .distributions import ClinicalDistributions
from .events import EventBus, LiveEventBuffer, OperationalMetricsSink
from .models import (
    BedStatus,
    BedType,
    ERBed,
    ESILevel,
    EventType,
    HospitalStateSnapshot,
    Patient,
    PatientStatus,
    QueueItem,
    SimulationEvent,
    StaffMember,
    StaffRole,
)
from .prediction import WaitTimePredictor
from .priorities import BasePriorityStrategy, DynamicClinicalPriorityStrategy


class ERSimulation:
    """Independent, stack-agnostic emergency department simulation engine."""

    def __init__(
        self,
        hospital_id: str = "H-DEMO-01",
        name: str = "Metro General Emergency Center",
        er_beds_count: int = 24,
        inpatient_beds_available: int = 5,
        base_arrivals_per_hour: float = 4.5,
        priority_strategy: Optional[BasePriorityStrategy] = None,
        start_time: Optional[datetime] = None,
        seed: Optional[int] = 42,
    ):
        self.hospital_id = hospital_id
        self.name = name
        self.er_beds_count = er_beds_count
        self.inpatient_beds_available = inpatient_beds_available
        self.base_arrivals_per_hour = base_arrivals_per_hour
        
        self.current_time = start_time or datetime(2026, 11, 8, 9, 0, 0)
        self.strategy = priority_strategy or DynamicClinicalPriorityStrategy()
        self.dist = ClinicalDistributions(seed=seed)
        
        # Telemetry & observers
        self.event_bus = EventBus()
        self.metrics = OperationalMetricsSink()
        self.live_events = LiveEventBuffer(max_size=100)
        
        # Wire default observers
        self.event_bus.subscribe(self.metrics.on_event)
        self.event_bus.subscribe(self.live_events.on_event)
        
        # State registries
        self.patients: Dict[str, Patient] = {}
        self.staff: Dict[str, StaffMember] = {}
        self.beds: Dict[str, ERBed] = {}
        
        # Internal scheduling book-keeping (patient_id -> completion_time)
        self._triage_completions: Dict[str, datetime] = {}
        self._eval_completions: Dict[str, datetime] = {}
        self._diag_completions: Dict[str, datetime] = {}
        self._boarding_completions: Dict[str, datetime] = {}
        
        self._patient_sequence: int = 0
        
        # Initialize default facilities and roster
        self._initialize_default_resources()

    def _initialize_default_resources(self):
        """Builds default baseline ER beds and clinical shift roster."""
        # Setup beds: 2 Resuscitation bays, 18 Acute beds, 4 Fast-Track chairs
        for i in range(1, self.er_beds_count + 1):
            if i <= 2:
                btype = BedType.RESUSCITATION
                bname = f"Trauma Bay {i}"
            elif i > self.er_beds_count - 4:
                btype = BedType.FAST_TRACK
                bname = f"Fast Track {i - (self.er_beds_count - 4)}"
            else:
                btype = BedType.ACUTE
                bname = f"Acute Bed {i}"

            self.beds[f"BED-{i:02d}"] = ERBed(
                id=f"BED-{i:02d}",
                name=bname,
                bed_type=btype,
                status=BedStatus.AVAILABLE,
            )

        # Baseline clinical staffing: 3 Attendings/PAs, 2 Triage Nurses, 6 Bedside Nurses
        for i in range(1, 4):
            self.staff[f"MD-{i:02d}"] = StaffMember(
                id=f"MD-{i:02d}",
                name=f"Dr. Clinician {i}",
                role=StaffRole.PHYSICIAN,
                max_concurrent_patients=4,
            )
        for i in range(1, 3):
            self.staff[f"RN-TRIAGE-{i:02d}"] = StaffMember(
                id=f"RN-TRIAGE-{i:02d}",
                name=f"Triage Nurse {i}",
                role=StaffRole.TRIAGE_NURSE,
                max_concurrent_patients=1,
            )
        for i in range(1, 7):
            self.staff[f"RN-BED-{i:02d}"] = StaffMember(
                id=f"RN-BED-{i:02d}",
                name=f"Bedside RN {i}",
                role=StaffRole.BEDSIDE_NURSE,
                max_concurrent_patients=3,
            )

    # --- Public Telemetry API ---

    def subscribe(
        self,
        handler: Callable[[SimulationEvent], None],
        event_types: Optional[set[EventType]] = None,
    ) -> None:
        """Attaches an external event consumer (e.g. WebSocket streamer, logger)."""
        self.event_bus.subscribe(handler, event_types)

    # --- Core Simulation Tick / Step ---

    def step(self, delta_minutes: int = 1) -> None:
        """Advances simulation by delta_minutes, processing all clinical queues."""
        self.current_time += timedelta(minutes=delta_minutes)

        self._process_bed_cleaning()
        self._generate_arrivals(delta_minutes)
        self._process_triage()
        self._process_reneges(delta_minutes)
        self._process_queue_and_bed_assignment()
        self._process_clinical_evaluations()
        self._process_diagnostics()
        self._process_disposition_decisions()
        self._process_boarding_departures()
        self._process_stochastic_fluctuations(delta_minutes)

    def _process_stochastic_fluctuations(self, delta_minutes: int) -> None:
        """Generates realistic background hospital variance: upstairs bed turnover and EMS clusters."""
        # Upstairs inpatient bed discharge: ~0.9 beds opened per hour on average
        prob_discharge = (0.9 / 60.0) * delta_minutes
        if self.dist.rng.random() < prob_discharge:
            if self.inpatient_beds_available < 10:
                self.inpatient_beds_available += 1
                self._emit(
                    EventType.INPATIENT_CAPACITY_CHANGE,
                    data={
                        "change": "+1",
                        "inpatient_beds_available": self.inpatient_beds_available,
                        "reason": "Upstairs inpatient discharged",
                    },
                )

        # Upstairs surgical/ICU admission consuming a bed: ~0.6 beds per hour
        prob_intake = (0.6 / 60.0) * delta_minutes
        if self.dist.rng.random() < prob_intake and self.inpatient_beds_available > 0:
            self.inpatient_beds_available -= 1
            self._emit(
                EventType.INPATIENT_CAPACITY_CHANGE,
                data={
                    "change": "-1",
                    "inpatient_beds_available": self.inpatient_beds_available,
                    "reason": "Direct surgical/ICU intake upstairs",
                },
            )

        # Occasional ambulance cluster (e.g. 2-car MVA or multi-patient EMS run): ~1.5% chance per step
        if self.dist.rng.random() < 0.015:
            cluster_count = self.dist.rng.choice([2, 3])
            acuity = self.dist.rng.choice([ESILevel.EMERGENT, ESILevel.URGENT])
            self.inject_surge(patient_count=cluster_count, acuity=acuity)

    def run_until(self, target_time: datetime, step_minutes: int = 1) -> None:
        """Runs the simulation forward in fast-forward batch mode until target_time."""
        while self.current_time < target_time:
            self.step(delta_minutes=step_minutes)

    # --- Internal Lifecycle Stages ---

    def _emit(
        self,
        event_type: EventType,
        patient_id: Optional[str] = None,
        staff_id: Optional[str] = None,
        bed_id: Optional[str] = None,
        data: Optional[Dict[str, any]] = None,
    ) -> None:
        event = SimulationEvent(
            timestamp=self.current_time,
            hospital_id=self.hospital_id,
            event_type=event_type,
            patient_id=patient_id,
            staff_id=staff_id,
            bed_id=bed_id,
            data=data or {},
        )
        self.event_bus.publish(event)

    def _generate_arrivals(self, delta_minutes: int) -> None:
        count = self.dist.sample_arrival_count(
            self.base_arrivals_per_hour, self.current_time, delta_minutes
        )
        for _ in range(count):
            self._patient_sequence += 1
            patient = self.dist.sample_patient_profile(self.current_time, self._patient_sequence)
            self.patients[patient.id] = patient

            self._emit(
                EventType.PATIENT_ARRIVED,
                patient_id=patient.id,
                data={
                    "esi": patient.esi.value,
                    "chief_complaint": patient.chief_complaint,
                    "arrival_mode": patient.arrival_mode,
                    "age": patient.age,
                    "vitals_abnormal": patient.vitals.is_abnormal,
                },
            )

            # ESI 1: Immediate resuscitation preemption (skips standard triage queue)
            if patient.esi == ESILevel.RESUSCITATION:
                patient.status = PatientStatus.WAITING_ROOM
                patient.triage_end_time = self.current_time
            else:
                patient.status = PatientStatus.TRIAGE_WAIT

    def _process_bed_cleaning(self) -> None:
        for bed in self.beds.values():
            if bed.status == BedStatus.DIRTY and bed.cleaning_until:
                if self.current_time >= bed.cleaning_until:
                    bed.status = BedStatus.AVAILABLE
                    bed.cleaning_until = None
                    bed.occupant_patient_id = None

    def _process_triage(self) -> None:
        # Check ongoing triages
        active_triage_patients = [
            p for p in self.patients.values() if p.status == PatientStatus.TRIAGE_ACTIVE
        ]
        for p in active_triage_patients:
            completion = self._triage_completions.get(p.id)
            if completion and self.current_time >= completion:
                p.status = PatientStatus.WAITING_ROOM
                p.triage_end_time = self.current_time
                self._triage_completions.pop(p.id, None)

                # Free up triage nurse
                for staff in self.staff.values():
                    if staff.role == StaffRole.TRIAGE_NURSE and p.id in staff.active_patient_ids:
                        staff.active_patient_ids.remove(p.id)

                self._emit(
                    EventType.TRIAGE_COMPLETED,
                    patient_id=p.id,
                    data={"esi": p.esi.value, "chief_complaint": p.chief_complaint},
                )

        # Assign available triage nurses to waiting patients
        free_nurses = [
            s for s in self.staff.values()
            if s.role == StaffRole.TRIAGE_NURSE and s.is_available
        ]
        waiting_triage = [
            p for p in self.patients.values() if p.status == PatientStatus.TRIAGE_WAIT
        ]

        for nurse, patient in zip(free_nurses, waiting_triage):
            nurse.active_patient_ids.append(patient.id)
            patient.status = PatientStatus.TRIAGE_ACTIVE
            patient.triage_start_time = self.current_time
            duration = self.dist.sample_triage_duration_minutes()
            self._triage_completions[patient.id] = self.current_time + timedelta(minutes=duration)

            self._emit(
                EventType.TRIAGE_STARTED,
                patient_id=patient.id,
                staff_id=nurse.id,
            )

    def _process_reneges(self, delta_minutes: int) -> None:
        waiting = [
            p for p in self.patients.values() if p.status == PatientStatus.WAITING_ROOM
        ]
        for p in waiting:
            wait_min = p.wait_minutes(self.current_time)
            if self.dist.check_renege(p, wait_min, delta_minutes):
                p.status = PatientStatus.LWBS
                p.departure_time = self.current_time
                self._emit(
                    EventType.PATIENT_LWBS,
                    patient_id=p.id,
                    data={"wait_minutes": round(wait_min, 1), "esi": p.esi.value},
                )

    def _process_queue_and_bed_assignment(self) -> None:
        waiting_room = [
            p for p in self.patients.values() if p.status == PatientStatus.WAITING_ROOM
        ]
        if not waiting_room:
            return

        # Prioritize waiting queue using active strategy
        ranked = self.strategy.rank_queue(waiting_room, self.current_time)

        # Available physicians with spare capacity
        available_mds = [
            s for s in self.staff.values()
            if s.role == StaffRole.PHYSICIAN and s.is_available
        ]

        for patient in ranked:
            if not available_mds:
                break

            # Find matching available bed
            bed = self._find_available_bed_for(patient)
            if not bed:
                # No suitable bed available right now (e.g., all resuscitation or acute beds occupied)
                continue

            # Assign best available physician (least loaded first)
            available_mds.sort(key=lambda md: len(md.active_patient_ids))
            assigned_md = available_mds[0]

            # Transition state
            bed.status = BedStatus.OCCUPIED
            bed.occupant_patient_id = patient.id
            assigned_md.active_patient_ids.append(patient.id)

            patient.assigned_bed_id = bed.id
            patient.assigned_provider_id = assigned_md.id
            patient.bed_assigned_time = self.current_time
            patient.provider_start_time = self.current_time
            patient.status = PatientStatus.IN_BED_EVAL

            # Schedule physician initial bedside evaluation completion
            eval_dur = self.dist.sample_provider_initial_minutes(patient.esi)
            self._eval_completions[patient.id] = self.current_time + timedelta(minutes=eval_dur)

            door_to_provider = patient.door_to_provider_minutes()

            self._emit(
                EventType.BED_ASSIGNED,
                patient_id=patient.id,
                bed_id=bed.id,
                staff_id=assigned_md.id,
                data={"bed_name": bed.name, "bed_type": bed.bed_type.value},
            )

            self._emit(
                EventType.PROVIDER_ASSIGNED,
                patient_id=patient.id,
                staff_id=assigned_md.id,
                data={
                    "provider_name": assigned_md.name,
                    "esi": patient.esi.value,
                    "wait_minutes": round(door_to_provider or 0.0, 1),
                },
            )

            if not assigned_md.is_available:
                available_mds.remove(assigned_md)

    def _find_available_bed_for(self, patient: Patient) -> Optional[ERBed]:
        free_beds = [b for b in self.beds.values() if b.status == BedStatus.AVAILABLE]
        if not free_beds:
            return None

        # ESI 1 prefers Resuscitation Bay, then Acute Bed
        if patient.esi == ESILevel.RESUSCITATION:
            resus = [b for b in free_beds if b.bed_type == BedType.RESUSCITATION]
            return resus[0] if resus else free_beds[0]

        # ESI 4 & 5 can use Fast-Track chairs
        if patient.esi in (ESILevel.LESS_URGENT, ESILevel.NON_URGENT):
            fast = [b for b in free_beds if b.bed_type == BedType.FAST_TRACK]
            if fast:
                return fast[0]

        # Standard acute beds
        acute = [b for b in free_beds if b.bed_type == BedType.ACUTE]
        return acute[0] if acute else free_beds[0]

    def _process_clinical_evaluations(self) -> None:
        evaluating = [
            p for p in self.patients.values() if p.status == PatientStatus.IN_BED_EVAL
        ]
        for p in evaluating:
            comp = self._eval_completions.get(p.id)
            if comp and self.current_time >= comp:
                self._eval_completions.pop(p.id, None)

                if p.requires_labs or p.requires_imaging:
                    p.status = PatientStatus.DIAGNOSTICS
                    diag_mins = self.dist.sample_diagnostics_duration_minutes(p)
                    self._diag_completions[p.id] = self.current_time + timedelta(minutes=diag_mins)
                    self._emit(
                        EventType.DIAGNOSTICS_ORDERED,
                        patient_id=p.id,
                        data={
                            "requires_labs": p.requires_labs,
                            "requires_imaging": p.requires_imaging,
                            "estimated_minutes": round(diag_mins, 1),
                        },
                    )
                else:
                    p.status = PatientStatus.DECISION_PENDING

    def _process_diagnostics(self) -> None:
        in_diag = [
            p for p in self.patients.values() if p.status == PatientStatus.DIAGNOSTICS
        ]
        for p in in_diag:
            comp = self._diag_completions.get(p.id)
            if comp and self.current_time >= comp:
                self._diag_completions.pop(p.id, None)
                p.workup_complete_time = self.current_time
                p.status = PatientStatus.DECISION_PENDING
                self._emit(
                    EventType.DIAGNOSTICS_COMPLETED,
                    patient_id=p.id,
                )

    def _process_disposition_decisions(self) -> None:
        pending = [
            p for p in self.patients.values() if p.status == PatientStatus.DECISION_PENDING
        ]
        for p in pending:
            p.decision_time = self.current_time

            if not p.will_admit:
                # Discharged home
                self._complete_discharge(p)
            else:
                # Admitted: check upstairs hospital inpatient bed capacity
                if self.inpatient_beds_available > 0:
                    self.inpatient_beds_available -= 1
                    self._complete_admission(p, boarding_minutes=0.0)
                else:
                    # Inpatient bed gridlock -> ER Boarding
                    p.status = PatientStatus.BOARDING
                    board_dur = self.dist.sample_boarding_duration_minutes()
                    self._boarding_completions[p.id] = self.current_time + timedelta(minutes=board_dur)
                    
                    self._emit(
                        EventType.BOARDING_STARTED,
                        patient_id=p.id,
                        bed_id=p.assigned_bed_id,
                        data={
                            "reason": "No upstairs inpatient beds available",
                            "assigned_bed": p.assigned_bed_id,
                        },
                    )

    def _process_boarding_departures(self) -> None:
        boarding = [
            p for p in self.patients.values() if p.status == PatientStatus.BOARDING
        ]
        for p in boarding:
            comp = self._boarding_completions.get(p.id)
            # Boarding completes either when upstairs bed opens or simulated duration completes
            if comp and (self.current_time >= comp or self.inpatient_beds_available > 0):
                self._boarding_completions.pop(p.id, None)
                board_mins = (self.current_time - (p.decision_time or self.current_time)).total_seconds() / 60.0
                self._complete_admission(p, boarding_minutes=board_mins)

    def _complete_discharge(self, p: Patient) -> None:
        p.status = PatientStatus.DISCHARGED
        p.departure_time = self.current_time
        self._release_resources(p)

        self._emit(
            EventType.PATIENT_DISCHARGED,
            patient_id=p.id,
            bed_id=p.assigned_bed_id,
            data={
                "door_to_provider_minutes": round(p.door_to_provider_minutes() or 0.0, 1),
                "total_los_minutes": round(p.total_los_minutes() or 0.0, 1),
                "esi": p.esi.value,
            },
        )

    def _complete_admission(self, p: Patient, boarding_minutes: float) -> None:
        p.status = PatientStatus.ADMITTED
        p.departure_time = self.current_time
        self._release_resources(p)

        self._emit(
            EventType.PATIENT_ADMITTED,
            patient_id=p.id,
            bed_id=p.assigned_bed_id,
            data={
                "door_to_provider_minutes": round(p.door_to_provider_minutes() or 0.0, 1),
                "total_los_minutes": round(p.total_los_minutes() or 0.0, 1),
                "boarding_minutes": round(boarding_minutes, 1),
                "esi": p.esi.value,
            },
        )

    def _release_resources(self, p: Patient) -> None:
        # Free clinician
        if p.assigned_provider_id and p.assigned_provider_id in self.staff:
            md = self.staff[p.assigned_provider_id]
            if p.id in md.active_patient_ids:
                md.active_patient_ids.remove(p.id)

        # Mark ER bed as DIRTY and start sanitation turnover
        if p.assigned_bed_id and p.assigned_bed_id in self.beds:
            bed = self.beds[p.assigned_bed_id]
            bed.status = BedStatus.DIRTY
            clean_mins = self.dist.sample_bed_turnover_minutes()
            bed.cleaning_until = self.current_time + timedelta(minutes=clean_mins)

    # --- Interactive Scenario Injections for Live Demos ---

    def inject_surge(self, patient_count: int = 5, acuity: ESILevel = ESILevel.EMERGENT) -> List[str]:
        """Simulates an immediate multi-casualty accident or ambulance surge."""
        new_ids = []
        for _ in range(patient_count):
            self._patient_sequence += 1
            p = self.dist.sample_patient_profile(self.current_time, self._patient_sequence)
            p.esi = acuity
            p.arrival_mode = "EMS_AMBULANCE"
            p.chief_complaint = "Severe Trauma / Collision Surge"
            self.patients[p.id] = p
            new_ids.append(p.id)

            if p.esi == ESILevel.RESUSCITATION:
                p.status = PatientStatus.WAITING_ROOM
                p.triage_end_time = self.current_time
            else:
                p.status = PatientStatus.TRIAGE_WAIT

        self._emit(
            EventType.SURGE_TRIGGERED,
            data={"count": patient_count, "acuity": acuity.value, "patient_ids": new_ids},
        )
        return new_ids

    def set_physician_count(self, active_count: int) -> None:
        """Simulates a doctor call-out, shift change, or surge relief staffing."""
        mds = [s for s in self.staff.values() if s.role == StaffRole.PHYSICIAN]
        for i, md in enumerate(mds):
            md.on_shift = i < active_count
        self._emit(
            EventType.STAFF_SHIFT_CHANGE,
            data={"role": "PHYSICIAN", "active_physicians": active_count},
        )

    def set_inpatient_beds_available(self, count: int) -> None:
        """Simulates an upstairs hospital bed crunch or bed clearance."""
        old = self.inpatient_beds_available
        self.inpatient_beds_available = max(0, count)
        self._emit(
            EventType.INPATIENT_CAPACITY_CHANGE,
            data={"previous": old, "current": self.inpatient_beds_available},
        )

    def set_priority_strategy(self, strategy: BasePriorityStrategy) -> None:
        """Swaps queue sorting policy on the fly."""
        self.strategy = strategy

    # --- State Snapshot Generation ---

    def get_snapshot(self) -> HospitalStateSnapshot:
        """Generates a complete, structured snapshot of hospital operations."""
        all_patients = list(self.patients.values())
        
        # Bed calculations
        total_beds = len(self.beds)
        occupied_beds = sum(1 for b in self.beds.values() if b.status == BedStatus.OCCUPIED)
        dirty_beds = sum(1 for b in self.beds.values() if b.status == BedStatus.DIRTY)
        available_beds = sum(1 for b in self.beds.values() if b.status == BedStatus.AVAILABLE)
        utilization = (occupied_beds / max(1, total_beds)) * 100.0

        # Queues
        triage_queue = [p for p in all_patients if p.status in (PatientStatus.TRIAGE_WAIT, PatientStatus.TRIAGE_ACTIVE)]
        waiting_room = [p for p in all_patients if p.status == PatientStatus.WAITING_ROOM]
        in_treatment = [p for p in all_patients if p.status in (PatientStatus.IN_BED_EVAL, PatientStatus.DIAGNOSTICS, PatientStatus.DECISION_PENDING)]
        boarders = [p for p in all_patients if p.status == PatientStatus.BOARDING]

        # Prioritize waiting queue
        ranked_waiting = self.strategy.rank_queue(waiting_room, self.current_time)
        queue_items = [
            QueueItem(
                patient_id=p.id,
                esi=p.esi.value,
                chief_complaint=p.chief_complaint,
                wait_minutes=round(p.wait_minutes(self.current_time), 1),
                priority_score=round(p.priority_score, 1),
                arrival_mode=p.arrival_mode,
                vitals_abnormal=p.vitals.is_abnormal,
            )
            for p in ranked_waiting
        ]

        # Staffing
        on_shift_mds = [s for s in self.staff.values() if s.role == StaffRole.PHYSICIAN and s.on_shift]
        total_md_slots = sum(s.max_concurrent_patients for s in on_shift_mds)
        used_md_slots = sum(len(s.active_patient_ids) for s in on_shift_mds)
        avail_md_slots = max(0, total_md_slots - used_md_slots)
        on_shift_rns = sum(1 for s in self.staff.values() if s.role in (StaffRole.BEDSIDE_NURSE, StaffRole.TRIAGE_NURSE) and s.on_shift)

        # Overcrowding calculation (NEDOCS)
        longest_wait_hours = (max((p.wait_minutes(self.current_time) for p in waiting_room), default=0.0)) / 60.0
        longest_board_hours = (
            max(
                ((self.current_time - (p.decision_time or self.current_time)).total_seconds() / 3600.0 for p in boarders),
                default=0.0,
            )
        )
        total_in_ed = len(waiting_room) + len(in_treatment) + len(boarders)
        nedocs_score, crowding_lvl = WaitTimePredictor.calculate_nedocs_score(
            total_er_beds=total_beds,
            active_er_patients=total_in_ed,
            inpatient_beds_total=60,
            active_boarders=len(boarders),
            longest_wait_hours=longest_wait_hours,
            longest_board_hours=longest_board_hours,
        )

        # Dynamic wait time predictions
        predicted_waits = WaitTimePredictor.estimate_wait_times_by_esi(
            patients=all_patients,
            staff=list(self.staff.values()),
            er_beds_total=total_beds,
            er_beds_occupied=occupied_beds,
            boarding_count=len(boarders),
            as_of=self.current_time,
        )

        return HospitalStateSnapshot(
            hospital_id=self.hospital_id,
            name=self.name,
            timestamp=self.current_time,
            total_er_beds=total_beds,
            occupied_er_beds=occupied_beds,
            dirty_er_beds=dirty_beds,
            available_er_beds=available_beds,
            bed_utilization_pct=round(utilization, 1),
            inpatient_beds_available=self.inpatient_beds_available,
            active_boarders_in_er=len(boarders),
            triage_queue_count=len(triage_queue),
            waiting_room_count=len(waiting_room),
            in_treatment_count=len(in_treatment),
            on_shift_physicians=len(on_shift_mds),
            available_physician_slots=avail_md_slots,
            on_shift_nurses=on_shift_rns,
            nedocs_score=nedocs_score,
            overcrowding_level=crowding_lvl,
            waiting_queue=queue_items,
            predicted_wait_times=predicted_waits,
        )
