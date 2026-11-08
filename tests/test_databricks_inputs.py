import csv,json
from pathlib import Path
import pytest
from fastapi import HTTPException
from test_api import client
from faster import db, ranking_data, ranking_backend, scoring, outcomes

ROOT=Path(__file__).parent/'fixtures/demo'
EXPECTED=json.loads((ROOT/'expected_rankings.json').read_text())
def read(name):return list(csv.DictReader((ROOT/(name+'.csv')).open()))
def records(scenario):
    rows=[]
    for o in read('demo_hospital_operations'):
        if o['scenario_id'] != scenario: continue
        hid=o['hospital_id']
        d=next(r for r in read('demo_hospital_directory') if r['hospital_id']==hid)
        m=next(r for r in read('demo_hospital_id_map') if r['hospital_id']==hid)
        t=next(r for r in read('demo_travel') if r['hospital_id']==hid and r['scenario_id']==scenario)
        h=dict(scenario_id=scenario,id=hid,facility_id=m['permanent_facility_id'],ny_facility_id=d['ny_facility_id'],
               name=d['facility_name'],city=d['city'],latitude=float(d['latitude']),longitude=float(d['longitude']),
               origin_id=t['origin_id'],miles=float(t['miles']),staff=int(o['staff']),q=[int(o[f'q_{n}']) for n in range(1,6)],
               svc=[int(o[f'svc_{n}']) for n in range(5)],boarding=int(o['boarding']),peds=o['peds']=='True',trauma=o['trauma']=='True',
               insurance=o['insurance'].split('|'),is_synthetic=True,observed_at=o['observed_at'],
               incoming_levels=[int(r['lvl']) for r in read('demo_incoming') if r['hospital_id']==hid and r['scenario_id']==scenario])
        rows.append(h)
    indices=[dict(facility_id=r['facility_id'],cond=r['cond'],outcome_index=float(r['outcome_index']),
                  is_synthetic=True,outcome_source=r['outcome_source']) for r in read('demo_outcomes') if r['scenario_id']==scenario]
    return rows[::-1],indices

def remote(monkeypatch, mutation=None):
    monkeypatch.setenv('RANKING_DATA_SOURCE','databricks_demo')
    monkeypatch.setenv('RANKING_BACKEND','local')
    async def execute(sql,parameters):
        params={p['name']:p['value'] for p in parameters}
        assert ':scenario' in sql and params['scenario'] not in sql
        hs,os=records(params['scenario'])
        if mutation: mutation(hs,os)
        if 'demo_ranking_inputs' in sql:
            assert ':origin' in sql and params['origin']=='demo_troy_fixed'
            data=hs
        else:data=os
        return [dict(row_json=json.dumps(r)) for r in data]
    monkeypatch.setattr(ranking_data,'execute',execute)

@pytest.mark.parametrize('case', EXPECTED)
@pytest.mark.parametrize('backend', ['local', 'databricks'])
def test_endpoint_matches_unchanged_algorithm(client,monkeypatch,case,backend):
    remote(monkeypatch)
    monkeypatch.setenv('RANKING_BACKEND',backend)
    async def udf(sql, parameters):
        p=json.loads(parameters[0]['value'])
        result=scoring.rank(p['hospitals'],p['incoming'],lambda fid,c:p['outcomes'][fid],
                            p['level'],p['symptoms'],p['insurance'],p['peds'],p['trauma'])
        return [{'ranking_json':json.dumps(result)}]
    monkeypatch.setattr(ranking_backend,'execute',udf)
    def forbidden(*a):raise AssertionError('SQLite/legacy outcomes fallback used')
    monkeypatch.setattr(db,'hospitals',forbidden)
    monkeypatch.setattr(db,'incoming',forbidden)
    monkeypatch.setattr(outcomes,'get',forbidden)
    r=client.post('/api/triage',json={**case['intake'],'scenario_id':case['scenario_id']})
    assert r.status_code==200,r.text
    assert r.json()['ranking']==case['ranking']
    assert r.json()['level']==case['level']
    assert r.json()['data_source']=='databricks_demo'
    assert r.json()['static_snapshot'] is True
    assert r.json()['observed_at_by_hospital']['sam']=='2026-11-08T13:00:00Z'

@pytest.mark.parametrize('mutation',[
    lambda h,o:h.pop(),
    lambda h,o:h.append(h[0]),
    lambda h,o:h[0].update(q=[0]),
    lambda h,o:h[0].update(peds='false'),
    lambda h,o:h[0].update(facility_id='1'),
    lambda h,o:h[0].update(miles=float('nan')),
    lambda h,o:o.pop(),
    lambda h,o:o[0].update(outcome_index=0.5),
    lambda h,o:h[0].update(is_synthetic=False),
])
def test_bad_data_fails_closed(client,monkeypatch,mutation):
    remote(monkeypatch,mutation)
    assert client.post('/api/triage',json={'symptoms':['fracture']}).status_code==503

def test_outage_no_fallback(client,monkeypatch):
    remote(monkeypatch)
    async def fail(*a):raise HTTPException(503,'Warehouse unavailable')
    monkeypatch.setattr(ranking_data,'execute',fail)
    assert client.post('/api/triage',json={'symptoms':['fracture']}).status_code==503

def test_directory_and_read_only_feed(client,monkeypatch):
    remote(monkeypatch)
    assert len(client.get('/api/hospitals').json())==5
    assert client.post('/api/incoming',json={'hospital_id':'sam','level':3,'eta_min':5}).status_code==409
    assert client.post('/api/live/sam',json={'staff':4},headers={'X-Feed-Key':'test-feed'}).status_code==409
    assert client.post('/api/triage',json={'symptoms':['cold'],'scenario_id':'unknown'}).status_code==422
