"""SQLite persistence for FastER. Single source of truth: queues are DERIVED from the patients table
(patients waiting in TRIAGE, grouped by ESI level), unless a hospital feed pushes a fresh override."""
import json, os, random, secrets, sqlite3, time
from contextlib import contextmanager
from datetime import datetime, timedelta

from pathlib import Path
from .config import PROJECT_ROOT

configured_path = os.getenv("DB_PATH", "").strip()
database_path = Path(configured_path).expanduser() if configured_path else Path("data/faster.db")
DB_PATH = str(database_path if database_path.is_absolute() else PROJECT_ROOT / database_path)
STATUSES = ("TRIAGE", "WITH_DOCTOR", "BOARDING", "ADMITTED", "DISCHARGED")
OVERRIDE_TTL = 15 * 60  # a pushed live queue is trusted for 15 minutes, then we fall back to derived

SCHEMA = """
CREATE TABLE IF NOT EXISTS hospitals (
  id TEXT PRIMARY KEY, facility_id TEXT, name TEXT, city TEXT, miles REAL, staff INTEGER,
  trauma INTEGER, peds INTEGER, insurance TEXT, svc TEXT, q_override TEXT, q_override_at REAL);
CREATE TABLE IF NOT EXISTS patients (
  id TEXT PRIMARY KEY, hospital_id TEXT NOT NULL, arrival_time TEXT, age INTEGER,
  esi_level INTEGER CHECK(esi_level BETWEEN 1 AND 5), chief_complaint TEXT,
  triage_heart_rate REAL, triage_o2_sat REAL,
  status TEXT NOT NULL DEFAULT 'TRIAGE'
    CHECK(status IN ('TRIAGE','WITH_DOCTOR','BOARDING','ADMITTED','DISCHARGED')),
  admission_prediction REAL);  -- MOCK until a real model exists
CREATE INDEX IF NOT EXISTS ix_pat ON patients(hospital_id, status);
CREATE TABLE IF NOT EXISTS beds (
  id TEXT PRIMARY KEY, hospital_id TEXT NOT NULL, ward_name TEXT,
  status TEXT CHECK(status IN ('AVAILABLE','OCCUPIED','DIRTY')) DEFAULT 'AVAILABLE');
CREATE TABLE IF NOT EXISTS incoming (
  id TEXT PRIMARY KEY, hospital_id TEXT NOT NULL, lvl INTEGER, eta INTEGER, label TEXT, created REAL);
"""

# facility_id: replace with real SPARCS facility IDs so outcomes join correctly
SEED_HOSPITALS = [
 ("sam", "Samaritan Hospital", "Troy", 2, 5, 0, 0, ["Medicare","Medicaid","Private","Uninsured"], [0,1,3,5,4]),
 ("sp", "St. Peter's Hospital", "Albany", 9, 7, 0, 1, ["Medicare","Medicaid","Private"], [0,1,2,3,2]),
 ("amc", "Albany Medical Center", "Albany", 11, 12, 1, 1, ["Medicare","Medicaid","Private","Uninsured"], [0,3,6,8,5]),
 ("ell", "Ellis Medicine", "Schenectady", 15, 6, 0, 0, ["Medicare","Private","Medicaid"], [0,0,1,2,1]),
 ("sar", "Saratoga Hospital", "Saratoga Springs", 27, 5, 0, 0, ["Medicare","Private"], [0,0,0,1,1]),
]
COMPLAINTS = ["Chest Pain", "Shortness of Breath", "Abdominal Pain", "Fever", "Laceration", "Altered Mental Status"]


@contextmanager
def conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def new_id(prefix="PAT"):
    return f"{prefix}-{secrets.token_urlsafe(6)}"  # unguessable, unlike PAT-001


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        empty = c.execute("SELECT COUNT(*) FROM hospitals").fetchone()[0] == 0
    if empty:
        seed()


def reset():
    with conn() as c:
        for t in ("hospitals", "patients", "beds", "incoming"):
            c.execute(f"DELETE FROM {t}")
    seed()


def _now():
    return datetime.now()


def seed():
    with conn() as c:
        for hid, name, city, miles, staff, trauma, peds, ins, q in SEED_HOSPITALS:
            c.execute("INSERT INTO hospitals(id,facility_id,name,city,miles,staff,trauma,peds,insurance,svc) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (hid, hid, name, city, miles, staff, trauma, peds, json.dumps(ins), json.dumps([0, 0, 22, 16, 10])))
            def add(esi, status):
                arr = (_now() - timedelta(minutes=random.randint(5, 120))).isoformat(timespec="seconds")
                c.execute("INSERT INTO patients VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (new_id(), hid, arr, random.randint(18, 85), esi, random.choice(COMPLAINTS),
                           random.randint(60, 120), round(random.uniform(92, 100), 1), status,
                           round(random.uniform(0.7, 0.98), 2) if esi <= 2 else round(random.uniform(0.05, 0.45), 2)))
            for i, n in enumerate(q):
                for _ in range(n):
                    add(i + 1, "TRIAGE")
            for _ in range(3):
                add(random.choice([2, 3, 4]), "WITH_DOCTOR")
            for _ in range(random.randint(0, 3)):
                add(random.choice([2, 3]), "BOARDING")
            for i in range(1, 9):
                c.execute("INSERT INTO beds VALUES (?,?,?,?)", (f"{hid.upper()}-BED-{i:02d}", hid,
                          random.choice(["ICU", "Med-Surg", "Pediatrics", "Cardiac"]),
                          "OCCUPIED" if random.random() > 0.3 else "AVAILABLE"))


def hospitals():
    with conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM hospitals ORDER BY miles").fetchall()]
        for h in rows:
            h["trauma"], h["peds"] = bool(h["trauma"]), bool(h["peds"])
            h["insurance"], h["svc"] = json.loads(h["insurance"]), json.loads(h["svc"])
            ov, at = h.pop("q_override"), h.pop("q_override_at")
            q = [0] * 5
            for r in c.execute("SELECT esi_level, COUNT(*) n FROM patients WHERE hospital_id=? AND status='TRIAGE' GROUP BY esi_level", (h["id"],)).fetchall():
                q[r["esi_level"] - 1] = r["n"]
            h["q"] = json.loads(ov) if ov and at and time.time() - at < OVERRIDE_TTL else q
            h["boarding"] = c.execute("SELECT COUNT(*) FROM patients WHERE hospital_id=? AND status='BOARDING'", (h["id"],)).fetchone()[0]
        return rows


def set_live(hid, staff, q=None):
    with conn() as c:
        if not c.execute("SELECT 1 FROM hospitals WHERE id=?", (hid,)).fetchone():
            return False
        c.execute("UPDATE hospitals SET staff=? WHERE id=?", (staff, hid))
        if q is not None:
            c.execute("UPDATE hospitals SET q_override=?, q_override_at=? WHERE id=?", (json.dumps(q), time.time(), hid))
        return True


def incoming(hid=None):
    with conn() as c:
        sql, args = "SELECT * FROM incoming WHERE created > ?", [time.time() - 3 * 3600]  # stale entries expire
        if hid:
            sql, args = sql + " AND hospital_id=?", args + [hid]
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def add_incoming(hid, lvl, eta, label):
    d = dict(id=new_id("PT"), hospital_id=hid, lvl=lvl, eta=eta, label=label, created=time.time())
    with conn() as c:
        c.execute("INSERT INTO incoming VALUES (:id,:hospital_id,:lvl,:eta,:label,:created)", d)
    return d


def add_patient(hid, esi, complaint, age=None, hr=None, o2=None):
    pid = new_id()
    with conn() as c:
        c.execute("INSERT INTO patients(id,hospital_id,arrival_time,age,esi_level,chief_complaint,triage_heart_rate,triage_o2_sat,status) VALUES (?,?,?,?,?,?,?,?,'TRIAGE')",
                  (pid, hid, _now().isoformat(timespec="seconds"), age, esi, complaint, hr, o2))
    return pid


def patient(pid):
    with conn() as c:
        r = c.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None


def patients(hid):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM patients WHERE hospital_id=? AND status NOT IN ('DISCHARGED','ADMITTED')", (hid,)).fetchall()]


def set_status(hid, pid, status):
    with conn() as c:
        return c.execute("UPDATE patients SET status=? WHERE id=? AND hospital_id=?", (status, pid, hid)).rowcount > 0


def bed_summary(hid):
    with conn() as c:
        s = {"available": 0, "occupied": 0, "dirty": 0}
        for r in c.execute("SELECT status, COUNT(*) n FROM beds WHERE hospital_id=? GROUP BY status", (hid,)).fetchall():
            s[r["status"].lower()] = r["n"]
        s["boarding"] = c.execute("SELECT COUNT(*) FROM patients WHERE hospital_id=? AND status='BOARDING'", (hid,)).fetchone()[0]
        return s
