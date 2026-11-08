"""SYNTHETIC live ED status for the Capital Region hospitals. No real patient or real-time data.

waiting = patients waiting by acuity (1 = resuscitation ... 5 = non-urgent).
travel_minutes are illustrative drive times from a fictional patient location, not routing output.
"""
from datetime import datetime, timezone

HOSPITALS = {  # id: (travel_minutes, active_clinicians)
    "AMC": (6, 6), "SPH": (12, 5), "SAM": (14, 3), "ELL": (24, 4), "SAR": (32, 3),
}

SCENARIOS = {
    "normal": {
        "title": "Typical evening",
        "description": "No unusual events. Queues are moderate everywhere.",
        "waiting": {"AMC": {1: 0, 2: 2, 3: 6, 4: 6, 5: 4}, "SPH": {1: 0, 2: 2, 3: 6, 4: 5, 5: 3},
                    "SAM": {1: 0, 2: 1, 3: 3, 4: 3, 5: 2}, "ELL": {1: 0, 2: 1, 3: 4, 4: 3, 5: 2},
                    "SAR": {1: 0, 2: 0, 3: 2, 4: 3, 5: 2}},
        "clinicians": {},
    },
    "highway_accident": {
        "title": "Multi-vehicle highway accident",
        "description": "A major crash on the interstate sends a wave of trauma patients to Albany Medical "
                       "Center (the closest hospital). Two of its clinicians are pulled into trauma bays.",
        "waiting": {"AMC": {1: 3, 2: 9, 3: 14, 4: 6, 5: 2}, "SPH": {1: 0, 2: 2, 3: 5, 4: 4, 5: 3},
                    "SAM": {1: 0, 2: 1, 3: 3, 4: 3, 5: 2}, "ELL": {1: 0, 2: 1, 3: 4, 4: 3, 5: 2},
                    "SAR": {1: 0, 2: 0, 3: 2, 4: 3, 5: 2}},
        "clinicians": {"AMC": 4},
        "surge_hospitals": ["AMC"],
    },
}

_current = {"name": "normal"}


def set_scenario(name):
    if name not in SCENARIOS:
        raise KeyError(name)
    _current["name"] = name


def current_name():
    return _current["name"]


def snapshot(name=None):
    """Return (scenario_meta, list of per-hospital status dicts)."""
    name = name or _current["name"]
    sc = SCENARIOS[name]
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for hid, (travel, clinicians) in HOSPITALS.items():
        rows.append(dict(hospital_id=hid, travel_minutes=travel,
                         active_clinicians=sc["clinicians"].get(hid, clinicians),
                         waiting_by_acuity=dict(sc["waiting"][hid]),
                         surge=hid in sc.get("surge_hospitals", []), observed_at=now))
    return {"name": name, "title": sc["title"], "description": sc["description"]}, rows


# ---------------------------------------------------------------------------
# Databricks-backed status (DATA_MODE=databricks)
# ---------------------------------------------------------------------------
import os

from app.databricks import execute, quote_table

STATUS_TABLE_DEFAULT = "workspace.faster.gold_hospital_status_by_scenario"


def rows_for_export():
    """Flat rows (one per scenario/hospital/acuity) matching the Databricks table contract."""
    out = []
    for name, sc in SCENARIOS.items():
        for hid, (travel, clinicians) in HOSPITALS.items():
            for acuity in range(1, 6):
                out.append(dict(scenario=name, hospital_id=hid, acuity=acuity,
                                waiting_count=sc["waiting"][hid][acuity],
                                active_clinicians=sc["clinicians"].get(hid, clinicians),
                                travel_minutes=float(travel), surge=hid in sc.get("surge_hospitals", [])))
    return out


async def databricks_snapshot(name):
    """Read one scenario from the Gold table. Raises ValueError if the data is incomplete."""
    table = quote_table(os.getenv("DATABRICKS_STATUS_TABLE", STATUS_TABLE_DEFAULT))
    data = await execute(
        f"SELECT hospital_id, acuity, waiting_count, active_clinicians, travel_minutes, surge, observed_at "
        f"FROM {table} WHERE scenario = :scenario",
        [{"name": "scenario", "value": name, "type": "STRING"}])
    by_hospital = {}
    for r in data:
        h = by_hospital.setdefault(r["hospital_id"], dict(
            hospital_id=r["hospital_id"], travel_minutes=float(r["travel_minutes"]),
            active_clinicians=int(r["active_clinicians"]), waiting_by_acuity={},
            surge=bool(r["surge"]), observed_at=str(r["observed_at"])))
        if not 1 <= int(r["acuity"]) <= 5 or int(r["waiting_count"]) < 0:
            raise ValueError("Invalid acuity or negative count")
        h["waiting_by_acuity"][int(r["acuity"])] = int(r["waiting_count"])
    if set(by_hospital) != set(HOSPITALS):
        raise ValueError("Scenario is missing hospitals")
    # A missing acuity row is ambiguous (zero vs. missing), so require all five.
    if any(set(h["waiting_by_acuity"]) != {1, 2, 3, 4, 5} for h in by_hospital.values()):
        raise ValueError("Scenario is missing acuity rows")
    sc = SCENARIOS[name]
    meta = {"name": name, "title": sc["title"], "description": sc["description"]}
    return meta, [by_hospital[h] for h in HOSPITALS]


async def get_snapshot():
    """(meta, rows, source). Databricks in connected mode; labeled fallback to built-in scenarios."""
    name = _current["name"]
    if os.getenv("DATA_MODE", "demo") == "databricks":
        try:
            meta, rows = await databricks_snapshot(name)
            return meta, rows, "databricks"
        except Exception:  # HTTPException from execute(), ValueError, KeyError, TypeError
            meta, rows = snapshot(name)
            return meta, rows, "built-in scenarios (Databricks query failed)"
    meta, rows = snapshot(name)
    return meta, rows, "built-in scenarios"
