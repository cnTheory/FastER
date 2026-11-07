"""Outcome index lookup (SPARCS-derived, 1.00 = state avg, lower is better).
Source order: Databricks SQL gold table -> local CSV fallback."""
import csv, os, re
from . import config  # load .env before loading outcomes


def _from_databricks():
    from databricks import sql
    host = os.environ["DATABRICKS_HOST"].replace("https://", "")
    with sql.connect(server_hostname=host, http_path=os.environ["DATABRICKS_HTTP_PATH"],
                     access_token=os.environ["DATABRICKS_TOKEN"]) as c, c.cursor() as cur:
        table = os.getenv("OUTCOMES_TABLE", "faster.gold.outcomes")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,2}", table):
            raise ValueError("OUTCOMES_TABLE must be a SQL table identifier")
        cur.execute(f"SELECT facility_id, cond, outcome_index FROM {table}")
        return {(str(r[0]), r[1]): float(r[2]) for r in cur.fetchall() if r[2] is not None}


def _from_csv(path=os.path.join(os.path.dirname(__file__), "data", "outcomes_gold.csv")):
    with open(path) as f:
        return {(r["facility_id"], r["cond"]): float(r["outcome_index"]) for r in csv.DictReader(f) if r["outcome_index"]}


def load():
    if os.getenv("DATABRICKS_HTTP_PATH") and os.getenv("DATABRICKS_TOKEN"):
        try:
            return _from_databricks(), "databricks"
        except Exception as e:
            print("Databricks outcomes unavailable, using CSV:", e)
    return _from_csv(), "csv"


TABLE, SOURCE = load()


def get(facility_id, cond):
    return TABLE.get((str(facility_id), cond), 1.0)  # unknown / suppressed small cells -> neutral
