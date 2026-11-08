"""Read the validated Capital Region demo from Databricks; no SQLite fallback."""
import asyncio
import json
import math
import os
from fastapi import HTTPException
from .databricks import execute, quote_table

SCENARIOS = ('baseline', 'queue_surge', 'staff_reduction', 'capability_change')
IDS = {'sam': '000756', 'sp': '000005', 'amc': '000001', 'ell': '000829', 'sar': '000818'}
CONDITIONS = {'abdominal', 'cardiac', 'infection', 'neuro', 'respiratory', 'trauma'}


def source():
    value = os.getenv('RANKING_DATA_SOURCE', 'sqlite').lower()
    if value not in ('sqlite', 'databricks_demo'):
        raise HTTPException(503, 'RANKING_DATA_SOURCE must be sqlite or databricks_demo')
    return value


def selected_scenario(requested=None):
    value = requested or os.getenv('DEMO_SCENARIO', 'baseline')
    if value not in SCENARIOS:
        raise HTTPException(422, 'Unknown demo scenario')
    return value


def _number(value, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('Invalid numeric input')
    if not math.isfinite(value) or value < 0 or (integer and type(value) is not int):
        raise ValueError('Invalid numeric range')


async def load(scenario):
    if scenario not in SCENARIOS:
        raise HTTPException(422, 'Unknown demo scenario')
    origin = os.getenv('DEMO_ORIGIN_ID', 'demo_troy_fixed')
    view = quote_table(os.getenv('DATABRICKS_RANKING_VIEW', 'patient-info.default.demo_ranking_inputs'))
    table = quote_table(os.getenv('DATABRICKS_DEMO_OUTCOMES_TABLE', 'patient-info.default.demo_outcomes'))
    # Serialize complete typed rows in SQL: preserve arrays, booleans and string IDs
    # without relying on nested-type coercion in the existing REST adapter.
    fields = ('scenario_id,id,facility_id,ny_facility_id,name,city,latitude,longitude,'
              'origin_id,miles,staff,q,svc,boarding,peds,trauma,insurance,incoming_levels,'
              'is_synthetic,observed_at').split(',')
    struct = ', '.join(f"'{f}', `{f}`" for f in fields)
    params = [{'name': 'scenario', 'type': 'STRING', 'value': scenario}]
    h_rows, o_rows = await asyncio.gather(
        execute(f'SELECT to_json(named_struct({struct})) AS row_json FROM {view} '
                'WHERE scenario_id = :scenario AND origin_id = :origin LIMIT 6',
                params + [{'name': 'origin', 'type': 'STRING', 'value': origin}]),
        execute(f"SELECT to_json(named_struct('facility_id',facility_id,'cond',cond,"
                "'outcome_index',outcome_index,'is_synthetic',is_synthetic,"
                f"'outcome_source',outcome_source)) AS row_json FROM {table} "
                'WHERE scenario_id = :scenario LIMIT 31', params))
    try:
        hospitals = [json.loads(r['row_json']) for r in h_rows]
        if len(hospitals) != 5 or {h['id'] for h in hospitals} != set(IDS):
            raise ValueError('Expected exactly five demo hospitals')
        incoming = {}
        for h in hospitals:
            if (h['scenario_id'] != scenario or h['origin_id'] != origin or
                h['facility_id'] != IDS[h['id']] or not isinstance(h['ny_facility_id'], str) or
                h['ny_facility_id'].zfill(6) != h['facility_id'] or
                h['is_synthetic'] is not True or not h['observed_at']):
                raise ValueError('Invalid demo metadata or mapping')
            for f in ('name', 'city'):
                if not isinstance(h[f], str) or not h[f].strip():
                    raise ValueError('Missing hospital identity')
            for f in ('staff', 'boarding'):
                _number(h[f], integer=True)
            _number(h['miles'])
            for f in ('q', 'svc'):
                if not isinstance(h[f], list) or len(h[f]) != 5:
                    raise ValueError('Expected five entries')
                for value in h[f]:
                    _number(value, integer=True)
            if type(h['peds']) is not bool or type(h['trauma']) is not bool:
                raise ValueError('Invalid capability flags')
            if not isinstance(h['insurance'], list) or not all(isinstance(v, str) for v in h['insurance']):
                raise ValueError('Invalid insurance list')
            levels = h.pop('incoming_levels')
            if not isinstance(levels, list) or not all(type(v) is int and 1 <= v <= 5 for v in levels):
                raise ValueError('Invalid incoming urgency')
            incoming[h['id']] = levels
        indices = {}
        if len(o_rows) != 30:
            raise ValueError('Expected thirty outcome placeholders')
        for row in o_rows:
            r = json.loads(row['row_json'])
            key = (r['facility_id'], r['cond'])
            _number(r['outcome_index'])
            if (key in indices or r['is_synthetic'] is not True or r['outcome_index'] != 1.0 or
                r['outcome_source'] != 'neutral_demo_placeholder_not_measured'):
                raise ValueError('Invalid neutral outcome placeholder')
            indices[key] = r['outcome_index']
        if set(indices) != {(fid, c) for fid in IDS.values() for c in CONDITIONS}:
            raise ValueError('Incomplete outcome coverage')
        # Python sort is stable; preserve the previous order for equal scores.
        hospitals.sort(key=lambda h: list(IDS).index(h['id']))
        metadata = {'data_source': 'databricks_demo', 'scenario_id': scenario,
                    'origin_id': origin, 'is_synthetic': True, 'static_snapshot': True,
                    'observed_at_by_hospital': {h['id']: h['observed_at'] for h in hospitals},
                    'outcome_source': 'neutral_demo_placeholder_not_measured'}
        return hospitals, incoming, lambda fid, cond: indices[(str(fid), cond)], metadata
    except (ValueError, TypeError, KeyError, IndexError):
        raise HTTPException(503, 'Invalid or incomplete Databricks demo inputs; verify tables, scenario and origin')
