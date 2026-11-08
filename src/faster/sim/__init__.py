"""FastER Discrete-Event Simulation Package.

A library-first simulation engine for modeling emergency department
patient flow, clinical queue priorities, staffing capacity, and wait times.
"""

from .distributions import ClinicalDistributions
from .engine import ERSimulation
from .events import EventBus, LiveEventBuffer, OperationalMetricsSink
from .models import (
    BedStatus,
    BedType,
    ChiefComplaint,
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
    VitalSigns,
    WaitTimeEstimate,
)
from .prediction import WaitTimePredictor
from .priorities import (
    BasePriorityStrategy,
    DynamicClinicalPriorityStrategy,
    FastTrackPriorityStrategy,
    StrictESIPriorityStrategy,
)

__all__ = [
    "ERSimulation",
    "ESILevel",
    "PatientStatus",
    "ChiefComplaint",
    "VitalSigns",
    "Patient",
    "StaffRole",
    "StaffMember",
    "BedType",
    "BedStatus",
    "ERBed",
    "EventType",
    "SimulationEvent",
    "QueueItem",
    "WaitTimeEstimate",
    "HospitalStateSnapshot",
    "BasePriorityStrategy",
    "StrictESIPriorityStrategy",
    "DynamicClinicalPriorityStrategy",
    "FastTrackPriorityStrategy",
    "WaitTimePredictor",
    "ClinicalDistributions",
    "EventBus",
    "LiveEventBuffer",
    "OperationalMetricsSink",
]
