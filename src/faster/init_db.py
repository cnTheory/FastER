"""Reset synthetic demo data: python -m faster.init_db."""
import argparse
from . import db

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reset FastER synthetic demo data.")
    parser.add_argument("--reset", action="store_true", help="Delete existing demo records and reseed")
    args = parser.parse_args()
    db.init()
    if args.reset:
        db.reset()
    print(f"Database ready at {db.DB_PATH}")
