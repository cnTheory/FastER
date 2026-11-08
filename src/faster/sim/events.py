"""Event bus, observers, and metrics collectors for simulation telemetry.

Decouples the core simulation mechanics from transport, storage, or presentation.
"""

from __future__ import annotations

import collections
from datetime import datetime
from typing import Callable, Deque, Dict, List, Optional, Set

from .models import EventType, Patient, PatientStatus, SimulationEvent


class EventBus:
    """Publish-subscribe event dispatcher for simulation telemetry."""

    def __init__(self):
        self._handlers: List[tuple[Callable[[SimulationEvent], None], Optional[Set[EventType]]]] = []

    def subscribe(
        self,
        handler: Callable[[SimulationEvent], None],
        event_types: Optional[Set[EventType]] = None,
    ) -> None:
        """Subscribes a callable to receive emitted events.
        
        If event_types is provided, only matching events will be forwarded.
        """
        self._handlers.append((handler, event_types))

    def publish(self, event: SimulationEvent) -> None:
        """Dispatches an event to all matching subscribers."""
        for handler, types in self._handlers:
            if types is None or event.event_type in types:
                handler(event)


class LiveEventBuffer:
    """Maintains a bounded sliding window of the latest simulation events for UI feeds."""

    def __init__(self, max_size: int = 100):
        self._buffer: Deque[SimulationEvent] = collections.deque(maxlen=max_size)

    def on_event(self, event: SimulationEvent) -> None:
        self._buffer.append(event)

    def get_latest(self, count: int = 20) -> List[SimulationEvent]:
        events = list(self._buffer)
        return events[-count:]


class OperationalMetricsSink:
    """Aggregates high-fidelity clinical throughput, wait times, and quality metrics."""

    def __init__(self):
        self.total_arrivals: int = 0
        self.total_discharges: int = 0
        self.total_admissions: int = 0
        self.total_lwbs: int = 0
        
        # Door-to-provider wait times by ESI (in minutes)
        self.door_to_provider_by_esi: Dict[int, List[float]] = collections.defaultdict(list)
        
        # Total length of stay (LOS) in minutes
        self.los_discharged: List[float] = []
        self.los_admitted: List[float] = []
        
        # Boarding times for admitted patients (in minutes)
        self.boarding_times: List[float] = []

    def on_event(self, event: SimulationEvent) -> None:
        et = event.event_type
        data = event.data

        if et == EventType.PATIENT_ARRIVED:
            self.total_arrivals += 1

        elif et == EventType.PROVIDER_ASSIGNED:
            esi = data.get("esi")
            wait_min = data.get("wait_minutes")
            if esi is not None and wait_min is not None:
                self.door_to_provider_by_esi[esi].append(wait_min)

        elif et == EventType.PATIENT_DISCHARGED:
            self.total_discharges += 1
            los = data.get("total_los_minutes")
            if los is not None:
                self.los_discharged.append(los)

        elif et == EventType.PATIENT_ADMITTED:
            self.total_admissions += 1
            los = data.get("total_los_minutes")
            if los is not None:
                self.los_admitted.append(los)
            board_time = data.get("boarding_minutes")
            if board_time is not None:
                self.boarding_times.append(board_time)

        elif et == EventType.PATIENT_LWBS:
            self.total_lwbs += 1

    def get_summary(self) -> Dict[str, any]:
        """Calculates summary statistics across the simulation run."""
        def calc_stats(values: List[float]) -> Dict[str, float]:
            if not values:
                return {"count": 0, "median": 0.0, "p90": 0.0, "mean": 0.0}
            s = sorted(values)
            n = len(s)
            return {
                "count": n,
                "median": round(s[n // 2], 1),
                "p90": round(s[int(n * 0.9)], 1),
                "mean": round(sum(s) / n, 1),
            }

        door_to_provider_stats = {
            f"ESI_{esi}": calc_stats(times)
            for esi, times in self.door_to_provider_by_esi.items()
        }

        lwbs_rate = (
            (self.total_lwbs / max(1, self.total_arrivals)) * 100.0
        )

        return {
            "volume": {
                "total_arrivals": self.total_arrivals,
                "total_discharges": self.total_discharges,
                "total_admissions": self.total_admissions,
                "total_lwbs": self.total_lwbs,
                "lwbs_rate_pct": round(lwbs_rate, 2),
            },
            "door_to_provider_wait_minutes": door_to_provider_stats,
            "length_of_stay_minutes": {
                "discharged_patients": calc_stats(self.los_discharged),
                "admitted_patients": calc_stats(self.los_admitted),
            },
            "boarding_duration_minutes": calc_stats(self.boarding_times),
        }
