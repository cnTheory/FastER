import sqlite3
import random
from datetime import datetime, timedelta

def init_and_seed_db():
    conn = sqlite3.connect('er_flow.db')
    cursor = conn.cursor()
    
    # Create Patients Table matching Yale ED features
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS patients (
        id TEXT PRIMARY KEY,
        arrival_time TEXT,
        age INTEGER,
        gender TEXT,
        esi_level INTEGER,
        chief_complaint TEXT,
        triage_heart_rate REAL,
        triage_o2_sat REAL,
        past_year_admissions INTEGER,
        medication_count INTEGER,
        status TEXT CHECK(status IN ('TRIAGE', 'WITH_DOCTOR', 'BOARDING', 'ADMITTED', 'DISCHARGED')) DEFAULT 'TRIAGE',
        admission_prediction REAL
    )
    ''')
    
    # Create Inpatient Beds Table to track boarding capacity
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS beds (
        id TEXT PRIMARY KEY,
        ward_name TEXT,
        status TEXT CHECK(status IN ('AVAILABLE', 'OCCUPIED', 'DIRTY')) DEFAULT 'AVAILABLE'
    )
    ''')
    
    cursor.execute("DELETE FROM patients")
    cursor.execute("DELETE FROM beds")
    
    # Seed Mock Beds
    wards = ['ICU', 'Med-Surg', 'Pediatrics', 'Cardiac']
    for i in range(1, 21):
        ward = random.choice(wards)
        status = 'OCCUPIED' if random.random() > 0.3 else 'AVAILABLE'
        cursor.execute("INSERT INTO beds (id, ward_name, status) VALUES (?, ?, ?)", (f"BED-{i:02d}", ward, status))
        
    # Seed Mock Patients using Yale clinical profiles
    complaints = ['Chest Pain', 'Shortness of Breath', 'Abdominal Pain', 'Fever', 'Laceration', 'Altered Mental Status']
    genders = ['M', 'F']
    
    base_time = datetime.now()
    for i in range(1, 26):
        p_id = f"PAT-{i:03d}"
        esi = random.choice([1, 2, 3, 4, 5])
        
        if esi <= 2:
            prediction = round(random.uniform(0.70, 0.98), 2)
            status = random.choice(['WITH_DOCTOR', 'BOARDING'])
        else:
            prediction = round(random.uniform(0.05, 0.45), 2)
            status = random.choice(['TRIAGE', 'WITH_DOCTOR', 'DISCHARGED'])
            
        arrival = (base_time - timedelta(minutes=random.randint(10, 240))).strftime('%Y-%m-%d %H:%M:%S')
        
        cursor.execute('''
            INSERT INTO patients VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            p_id, arrival, random.randint(18, 85), random.choice(genders), esi,
            random.choice(complaints), random.randint(60, 120), round(random.uniform(92.0, 100.0), 1),
            random.randint(0, 4), random.randint(0, 15), status, prediction
        ))
        
    conn.commit()
    conn.close()
    print("🚀 Database initialized and seeded with 25 Yale-structured patient records!")

if __name__ == "__main__":
    init_and_seed_db()
