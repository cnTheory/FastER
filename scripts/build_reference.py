"""Build per-hospital reference stats from the NYS SPARCS 2024 discharge file.

SPARCS is HISTORICAL INPATIENT data. It has NO triage times or ED wait times, so we use it
only for hospital-level context: acuity mix, trauma volume, pediatric share, and typical
inpatient length of stay (mean, capped at 30 days) (a rough proxy for how long beds stay occupied -> ED boarding pressure).

Usage (from faster-starter/):
    python scripts/build_reference.py path/to/sparcs24.csv.xz
"""
import sys
from pathlib import Path

import pandas as pd

OUT = Path(__file__).resolve().parents[1] / "app" / "reference" / "hospital_reference.csv"
# Capital Region hospitals used by the surge scenario. Names must match SPARCS facility_name.
HOSPITALS = {
    "ALBANY MEDICAL CENTER HOSPITAL": "AMC",
    "ST. PETER'S HOSPITAL": "SPH",
    "SAMARITAN HOSPITAL": "SAM",
    "ELLIS HOSPITAL": "ELL",
    "SARATOGA HOSPITAL": "SAR",
}


def build(path):
    df = pd.read_csv(path, low_memory=False)
    # length_of_stay is text because very long stays are capped as "120 +"
    df["los"] = pd.to_numeric(df["length_of_stay"].astype(str).str.replace(r"\D", "", regex=True), errors="coerce")
    df["los"] = df["los"].clip(upper=30)  # cap so extreme long stays don't dominate the mean
    system_mean = df["los"].mean()
    rows = []
    for name, hid in HOSPITALS.items():
        h = df[df["facility_name"] == name]
        if h.empty:
            raise SystemExit(f"{name!r} not found in SPARCS file")
        rows.append(dict(
            hospital_id=hid, facility_name=name, discharge_count=len(h),
            ed_admit_share=round((h["emergency_department_indicator"] == "Y").mean(), 3),
            severe_share=round(h["apr_severity_of_illness"].isin(["Major", "Extreme"]).mean(), 3),
            trauma_admissions=int((h["type_of_admission"] == "Trauma").sum()),
            pediatric_share=round((h["age_group"] == "0-17").mean(), 3),
            mean_los_days=round(float(h["los"].mean()), 2),
            # >1 means beds stay occupied longer than the state median (more boarding pressure)
            boarding_factor=round(min(max(h["los"].mean() / system_mean, 0.8), 1.5), 3),
        ))
    out = pd.DataFrame(rows)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUT, index=False)
    print(out.to_string(index=False))
    print(f"\nsystem mean LOS = {system_mean:.2f} days; wrote {OUT}")


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "../data/sparcs24.csv.xz")
