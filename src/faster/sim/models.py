"""Core domain models for the FastER emergency department simulation.

Defines clinical states, patient profiles, staffing entities, beds,
and telemetry events. Independent of database or transport layers.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class ESILevel(int, Enum):
    """Emergency Severity Index (ESI) triage level (1 = highest urgency, 5 = lowest).
    
    Ref: Agency for Healthcare Research and Quality (AHRQ) ESI Handbook.
    """
    RESUSCITATION = 1  # Immediate life-saving intervention needed
    EMERGENT = 2       # High risk, confusional state, severe pain/distress
    URGENT = 3         # Normal vitals, but requires multiple clinical resources
    LESS_URGENT = 4    # Requires one diagnostic/therapeutic resource (e.g. X-ray)
    NON_URGENT = 5     # Requires zero resources (e.g. suture removal, refill)


class PatientStatus(str, Enum):
    """Lifecycle states of a patient in the emergency department."""
    ARRIVED = "ARRIVED"                  # Walked in or delivered via EMS
    TRIAGE_WAIT = "TRIAGE_WAIT"          # Waiting for initial triage assessment
    TRIAGE_ACTIVE = "TRIAGE_ACTIVE"      # Currently being triaged by triage RN
    WAITING_ROOM = "WAITING_ROOM"        # Triaged, waiting for an ER bed / physician
    IN_BED_EVAL = "IN_BED_EVAL"          # Placed in bed, clinical evaluation / MD exam
    DIAGNOSTICS = "DIAGNOSTICS"          # Awaiting labs, X-ray, CT, or specialty consult
    DECISION_PENDING = "DECISION_PENDING"# Workup complete, MD finalizing disposition
    BOARDING = "BOARDING"                # Admission approved, waiting for upstairs inpatient bed
    DISCHARGED = "DISCHARGED"            # Safely discharged home
    ADMITTED = "ADMITTED"                # Transferred upstairs to hospital ward
    LWBS = "LWBS"                        # Left Without Being Seen (patient reneged due to wait)


class ChiefComplaint(str, Enum):
    """Standard emergency department chief complaints categorized by acuity."""
    CARDIAC_ARREST = "Cardiac Arrest"
    SEVERE_TRAUMA = "Severe Polytrauma"
    STROKE_SYMPTOMS = "Acute Neurologic Deficit / Stroke"
    CHEST_PAIN = "Acute Chest Pain"
    RESPIRATORY_DISTRESS = "Severe Dyspnea / Respiratory Distress"
    ABDOMINAL_PAIN = "Acute Severe Abdominal Pain"
    FRACTURE_DISLOCATION = "Bone Fracture / Deformity"
    LACERATION_MINOR = "Simple Laceration"
    FEVER_INFECTION = "Fever / Systemic Infection"
    MILD_HEADACHE = "Mild Headache"
    MEDICATION_REFILL = "Prescription Refill / Routine"


class VitalSigns(BaseModel):
    """Baseline vital signs captured during triage."""
    heart_rate: int = 75
    systolic_bp: int = 120
    diastolic_bp: int = 80
    respiratory_rate: int = 16
    o2_saturation: float = 98.0
    temperature_f: float = 98.6
    pain_score: int = Field(default=0, ge=0, le=10)
    is_abnormal: bool = False


class Patient(BaseModel):
    """Clinical representation of an ER patient throughout their hospital journey."""
    id: str
    arrival_time: datetime
    arrival_mode: str = "WALK_IN"  # "WALK_IN" or "EMS_AMBULANCE"
    age: int
    gender: str = "U"
    esi: ESILevel
    chief_complaint: str
    vitals: VitalSigns = Field(default_factory=VitalSigns)
    
    # State tracking
    status: PatientStatus = PatientStatus.ARRIVED
    assigned_bed_id: Optional[str] = None
    assigned_provider_id: Optional[str] = None
    
    # Clinical milestones (UTC / simulation time)
    triage_start_time: Optional[datetime] = None
    triage_end_time: Optional[datetime] = None
    bed_assigned_time: Optional[datetime] = None
    provider_start_time: Optional[datetime] = None
    workup_complete_time: Optional[datetime] = None
    decision_time: Optional[datetime] = None
    departure_time: Optional[datetime] = None
    
    # Clinical workup requirements
    requires_imaging: bool = False
    requires_labs: bool = False
    admission_probability: float = 0.20  # Model prediction (Yale-style)
    will_admit: bool = False             # Ground truth simulated outcome
    
    # Queue dynamics
    priority_score: float = 0.0
    deterioration_risk: float = 0.0      # Likelihood of vitals degrading in waiting room

    def wait_minutes(self, as_of: datetime) -> float:
        """Minutes spent waiting from initial arrival."""
        end = self.departure_time or as_of
        return max(0.0, (end - self.arrival_time).total_seconds() / 60.0)

    def door_to_provider_minutes(self) -> Optional[float]:
        """Minutes from arrival to first being seen by an MD/PA."""
        if not self.provider_start_time:
            return None
        return max(0.0, (self.provider_start_time - self.arrival_time).total_seconds() / 60.0)

    def total_los_minutes(self) -> Optional[float]:
        """Total Length of Stay (LOS) in minutes from arrival to departure."""
        if not self.departure_time:
            return None
        return max(0.0, (self.departure_time - self.arrival_time).total_seconds() / 60.0)


class StaffRole(str, Enum):
    """Clinical staff specializations."""
    TRIAGE_NURSE = "TRIAGE_NURSE"      # Conducts initial triage interviews & vitals
    BEDSIDE_NURSE = "BEDSIDE_NURSE"    # Manages in-bed patient care (ratio 1:3 or 1:4)
    PHYSICIAN = "PHYSICIAN"            # Attending/Resident MD or Emergency PA
    CHARGE_NURSE = "CHARGE_NURSE"      # Flow coordinator


class StaffMember(BaseModel):
    """Clinical provider or nurse on shift."""
    id: str
    name: str
    role: StaffRole
    on_shift: bool = True
    max_concurrent_patients: int = 4   # MD can manage ~4 concurrent patients; RN ~3 beds
    active_patient_ids: List[str] = Field(default_factory=list)

    @property
    def is_available(self) -> bool:
        return self.on_shift and len(self.active_patient_ids) < self.max_concurrent_patients

    @property
    def capacity_remaining(self) -> int:
        if not self.on_shift:
            return 0
        return max(0, self.max_concurrent_patients - len(self.active_patient_ids))


class BedType(str, Enum):
    """Physical bay or bed classification."""
    RESUSCITATION = "RESUSCITATION"  # Trauma bay / code room for ESI 1
    ACUTE = "ACUTE"                  # Standard ER monitored acute bed (ESI 2 & 3)
    FAST_TRACK = "FAST_TRACK"        # Recliner / chair for low-acuity ESI 4 & 5
    TRIAGE_BAY = "TRIAGE_BAY"        # Dedicated intake triage interview chair


class BedStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    OCCUPIED = "OCCUPIED"
    DIRTY = "DIRTY"  # Undergoing cleaning/turnover


class ERBed(BaseModel):
    """A physical ER treatment location."""
    id: str
    name: str
    bed_type: BedType = BedType.ACUTE
    status: BedStatus = BedStatus.AVAILABLE
    occupant_patient_id: Optional[str] = None
    cleaning_until: Optional[datetime] = None


class EventType(str, Enum):
    """Simulation lifecycle events emitted for streaming and monitoring."""
    SIMULATION_START = "SIMULATION_START"
    PATIENT_ARRIVED = "PATIENT_ARRIVED"
    TRIAGE_STARTED = "TRIAGE_STARTED"
    TRIAGE_COMPLETED = "TRIAGE_COMPLETED"
    BED_ASSIGNED = "BED_ASSIGNED"
    PROVIDER_ASSIGNED = "PROVIDER_ASSIGNED"
    DIAGNOSTICS_ORDERED = "DIAGNOSTICS_ORDERED"
    DIAGNOSTICS_COMPLETED = "DIAGNOSTICS_COMPLETED"
    DISPOSITION_DECIDED = "DISPOSITION_DECIDED"
    BOARDING_STARTED = "BOARDING_STARTED"
    PATIENT_DISCHARGED = "PATIENT_DISCHARGED"
    PATIENT_ADMITTED = "PATIENT_ADMITTED"
    PATIENT_LWBS = "PATIENT_LWBS"
    STAFF_SHIFT_CHANGE = "STAFF_SHIFT_CHANGE"
    SURGE_TRIGGERED = "SURGE_TRIGGERED"
    INPATIENT_CAPACITY_CHANGE = "INPATIENT_CAPACITY_CHANGE"


class SimulationEvent(BaseModel):
    """Atomic telemetry event emitted by the simulation engine."""
    timestamp: datetime
    hospital_id: str
    event_type: EventType
    patient_id: Optional[str] = None
    staff_id: Optional[str] = None
    bed_id: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)


class QueueItem(BaseModel):
    """Summary of a patient's position in the waiting queue."""
    patient_id: str
    esi: int
    chief_complaint: str
    wait_minutes: float
    priority_score: float
    arrival_mode: str
    vitals_abnormal: bool


class WaitTimeEstimate(BaseModel):
    """Estimated door-to-provider wait time breakdown by triage tier."""
    esi_level: int
    estimated_wait_minutes: float
    confidence_interval_low: float
    confidence_interval_high: float
    queue_depth_ahead: int


class HospitalStateSnapshot(BaseModel):
    """High-level snapshot of the emergency department at a single point in time."""
    hospital_id: str
    name: str
    timestamp: datetime
    
    # Capacity metrics
    total_er_beds: int
    occupied_er_beds: int
    dirty_er_beds: int
    available_er_beds: int
    bed_utilization_pct: float
    
    # Inpatient boarding metrics (the critical gridlock metric)
    inpatient_beds_available: int
    active_boarders_in_er: int
    
    # Queues
    triage_queue_count: int
    waiting_room_count: int
    in_treatment_count: int
    
    # Staffing
    on_shift_physicians: int
    available_physician_slots: int
    on_shift_nurses: int
    
    # Operational scores
    nedocs_score: float                  # National ED Overcrowding Scale (0-200)
    overcrowding_level: str              # Normal, Busy, Extremely Busy, Overcrowded, Severely Overcrowded, Disastrous
    
    # Prioritized waiting room list
    waiting_queue: List[QueueItem]
    
    # Dynamic wait time predictions
    predicted_wait_times: Dict[int, WaitTimeEstimate]
