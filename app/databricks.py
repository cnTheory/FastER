"""Small, server-only Databricks SQL adapter. Never send tokens to the browser."""
import os
import re

import httpx
from fastapi import HTTPException


def quote_table(name):
    parts = name.split(".")
    if len(parts) != 3 or not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", p) for p in parts):
        raise HTTPException(503, "Configure a catalog.schema.table name with simple identifier components")
    return ".".join(f"`{p}`" for p in parts)


def convert(value, data_type):
    if value is None:
        return None
    kind = data_type.upper()
    if kind in {"BYTE", "SHORT", "INT", "INTEGER", "LONG", "BIGINT"}:
        return int(value)
    if kind in {"FLOAT", "DOUBLE", "DECIMAL"}:
        return float(value)
    if kind == "BOOLEAN":
        if str(value).lower() not in {"true", "false"}:
            raise ValueError("Invalid Boolean result")
        return str(value).lower() == "true"
    return value


async def execute(statement, parameters=None):
    host = os.getenv("DATABRICKS_HOST", "").rstrip("/")
    token = os.getenv("DATABRICKS_TOKEN", "")
    warehouse = os.getenv("DATABRICKS_WAREHOUSE_ID", "")
    if not (host.startswith("https://") and token and warehouse):
        raise HTTPException(503, "Configure an HTTPS DATABRICKS_HOST, DATABRICKS_TOKEN and DATABRICKS_WAREHOUSE_ID")
    body = {"warehouse_id": warehouse, "statement": statement,
            "wait_timeout": "30s", "on_wait_timeout": "CANCEL",
            "disposition": "INLINE", "format": "JSON_ARRAY"}
    if parameters:
        body["parameters"] = parameters
    try:
        async with httpx.AsyncClient(timeout=40) as client:
            response = await client.post(f"{host}/api/2.0/sql/statements",
                headers={"Authorization": f"Bearer {token}"}, json=body)
            response.raise_for_status()
            payload = response.json()
        state = payload.get("status", {}).get("state")
        if state != "SUCCEEDED":
            raise HTTPException(503, "Databricks query did not succeed; check SQL Query History and warehouse status")
        if payload.get("manifest", {}).get("truncated") or payload.get("result", {}).get("next_chunk_internal_link"):
            raise HTTPException(503, "Databricks returned a partial result; reduce the query size")
        columns = payload["manifest"]["schema"]["columns"]
        rows = []
        for values in payload.get("result", {}).get("data_array", []):
            if len(values) != len(columns):
                raise ValueError("Column count mismatch")
            rows.append({c["name"]: convert(v, c.get("type_name", "STRING")) for c, v in zip(columns, values)})
        return rows
    except (httpx.HTTPError, KeyError, ValueError, TypeError):
        raise HTTPException(503, "Databricks connection or response failed; check server configuration and SQL Query History")
