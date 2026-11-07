# Databricks notebook source
# SYNTHETIC SNAPSHOT DEMO ONLY. Import this file as a Python notebook.
# This batch pipeline recomputes from three full snapshots. It is not streaming CDC.
from pyspark.sql import functions as F, Window

dbutils.widgets.text("catalog", "workspace")
dbutils.widgets.text("schema", "faster")
catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")
import re
assert all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n) for n in (catalog, schema))
prefix = f"{catalog}.{schema}"
spark.conf.set("spark.sql.session.timeZone", "UTC")
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {prefix}")
spark.sql(f"CREATE VOLUME IF NOT EXISTS {prefix}.incoming")
source = f"/Volumes/{catalog}/{schema}/incoming"
# COMMAND ----------
# FIRST RUN: upload generated data/hospitals.csv, visits.csv, staff.csv to this volume.
# Then run from this cell onward. Replace all 3 together when refreshing a snapshot.
required = {
    "hospitals": ["hospital_id", "name", "travel_minutes", "capabilities", "service_minutes", "observed_at"],
    "visits": ["visit_id", "hospital_id", "status", "arrival_at", "updated_at"],
    "staff": ["staff_id", "hospital_id", "role", "on_shift", "updated_at"],
}
for name, fields in required.items():
    raw = spark.read.option("header", True).csv(f"{source}/{name}.csv")
    assert set(fields).issubset(raw.columns), f"{name}: missing columns"
    raw.write.mode("overwrite").format("delta").saveAsTable(f"{prefix}.bronze_{name}")
# COMMAND ----------
h = (spark.table(f"{prefix}.bronze_hospitals")
     .withColumn("travel_minutes", F.expr("try_cast(travel_minutes as double)"))
     .withColumn("service_minutes", F.expr("try_cast(service_minutes as double)"))
     .withColumn("observed_at", F.expr("try_cast(observed_at as timestamp)")))
valid_h = (F.col("hospital_id").isNotNull() & F.col("name").isNotNull() &
           F.col("capabilities").isNotNull() & F.col("observed_at").isNotNull() &
           (F.col("travel_minutes") >= 0) & (F.col("service_minutes") > 0))
h.filter(~F.coalesce(valid_h, F.lit(False))).write.mode("overwrite").saveAsTable(f"{prefix}.rejected_hospitals")
h = h.filter(valid_h)
# One hospital row per snapshot; duplicate hospital IDs must be resolved before ingestion.
assert h.count() == h.select("hospital_id").distinct().count(), "Duplicate hospital IDs"
ids = h.select("hospital_id")
clean = {}
for name, key in [("visits", "visit_id"), ("staff", "staff_id")]:
    frame = (spark.table(f"{prefix}.bronze_{name}")
             .withColumn("updated_at", F.expr("try_cast(updated_at as timestamp)")))
    valid = F.col(key).isNotNull() & F.col("updated_at").isNotNull() & F.col("hospital_id").isNotNull()
    if name == "visits":
        frame = frame.withColumn("arrival_at", F.expr("try_cast(arrival_at as timestamp)"))
        valid = valid & F.col("status").isin("WAITING", "IN_CARE", "DISCHARGED") & F.col("arrival_at").isNotNull()
    else:
        frame = frame.withColumn("on_shift", F.expr("try_cast(on_shift as boolean)"))
        valid = valid & F.col("on_shift").isNotNull() & F.col("role").isin("clinician", "nurse")
    rejected = frame.filter(~F.coalesce(valid, F.lit(False)))
    valid_rows = frame.filter(valid)
    unknown = valid_rows.join(ids, "hospital_id", "left_anti")
    rejected.unionByName(unknown).write.mode("overwrite").saveAsTable(f"{prefix}.rejected_{name}")
    known = valid_rows.join(ids, "hospital_id", "inner")
    window = Window.partitionBy(key).orderBy(F.col("updated_at").desc())
    clean[name] = known.withColumn("_rank", F.row_number().over(window)).filter("_rank = 1").drop("_rank")
    clean[name].write.mode("overwrite").saveAsTable(f"{prefix}.silver_{name}")
h.write.mode("overwrite").saveAsTable(f"{prefix}.silver_hospitals")
# COMMAND ----------
# Aggregate each fact table BEFORE joining: otherwise visits x staff multiplies counts.
v = clean["visits"].groupBy("hospital_id").agg(
    F.sum(F.when(F.col("status") == "WAITING", 1).otherwise(0)).alias("queue_count"),
    F.min("updated_at").alias("visit_snapshot_at"))
s = clean["staff"].groupBy("hospital_id").agg(
    F.sum(F.when((F.col("role") == "clinician") & F.col("on_shift"), 1).otherwise(0)).alias("active_clinicians"),
    F.min("updated_at").alias("staff_snapshot_at"))
gold = (h.join(v, "hospital_id", "left").join(s, "hospital_id", "left")
    .fillna({"queue_count": 0, "active_clinicians": 0})
    .withColumn("observed_at", F.date_format(
        F.least("observed_at", "visit_snapshot_at", "staff_snapshot_at"), "yyyy-MM-dd'T'HH:mm:ss.SSSXXX"))
    .drop("visit_snapshot_at", "staff_snapshot_at"))
# Zero-fill is valid only because these are complete, synthetic snapshots.
# Real feeds need a source heartbeat and completeness checks to distinguish zero from missing.
gold.write.mode("overwrite").format("delta").saveAsTable(f"{prefix}.gold_hospital_status")
display(gold)
# COMMAND ----------
# Query gold_hospital_status using the app's SQL warehouse identity.
# Minimum privileges: USE CATALOG, USE SCHEMA, SELECT on gold table, and warehouse use.
# No patient rows are returned to Gemini or the frontend.
