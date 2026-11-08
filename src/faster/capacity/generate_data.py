"""Generate fictional hospital snapshots. No real patient data is used."""
import csv
from datetime import datetime, timezone, timedelta
from pathlib import Path

from ..config import PROJECT_ROOT

ROOT = PROJECT_ROOT / "data" / "capacity"

def generate():
    ROOT.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    stamp = now.isoformat()
    hospitals = [
        dict(hospital_id="H1", name="Demo Riverside", travel_minutes=8, capabilities="general|imaging", service_minutes=12, observed_at=stamp),
        dict(hospital_id="H2", name="Demo Hillcrest", travel_minutes=18, capabilities="general|imaging|pediatric", service_minutes=12, observed_at=stamp),
        dict(hospital_id="H3", name="Demo Central", travel_minutes=12, capabilities="general|pediatric", service_minutes=12, observed_at=stamp),
    ]
    visits, staff = [], []
    for h, waiting, clinicians in [("H1", 12, 3), ("H2", 4, 2), ("H3", 8, 2)]:
        for i in range(waiting + 3):
            visits.append(dict(visit_id=f"{h}-V{i}", hospital_id=h,
                status="WAITING" if i < waiting else "IN_CARE",
                arrival_at=(now-timedelta(minutes=10+i)).isoformat(), updated_at=stamp))
        for i in range(clinicians + 1):
            staff.append(dict(staff_id=f"{h}-S{i}", hospital_id=h,
                role="clinician" if i < clinicians else "nurse", on_shift="true", updated_at=stamp))
    for filename, rows in [("hospitals.csv", hospitals), ("visits.csv", visits), ("staff.csv", staff)]:
        with (ROOT / filename).open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
    return ROOT

if __name__ == "__main__":
    print(generate())
