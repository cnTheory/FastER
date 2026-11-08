"""Remote boundary and generated UDF parity; no live workspace credentials."""
import asyncio
from pathlib import Path
import pytest
from fastapi import HTTPException
from faster import scoring, ranking_backend
H = dict(id='test', facility_id='42', name='Test Hospital', city='Demo', miles=3,
         staff=4, trauma=True, peds=True, insurance=['Private'],
         svc=[0, 0, 22, 16, 10], q=[0, 1, 3, 5, 4], boarding=2)
def udf():
    body = (Path(__file__).parents[1] / 'databricks_ranking.sql').read_text().split('$$')[1]
    ns = {}
    exec('def invoke(payload):\n' + '\n'.join('    '+s for s in body.splitlines()), ns)
    return ns['invoke']
@pytest.mark.parametrize('level', [1, 2, 3, 4, 5])
def test_remote_matches_original(monkeypatch, level):
    monkeypatch.setenv('RANKING_BACKEND', 'databricks')
    async def execute(sql, params):
        assert '`patient-info`.`default`.`faster_rank_v1`(:payload)' in sql
        assert params[0]['type'] == 'STRING'
        assert 'private_note' not in params[0]['value']
        return [{'ranking_json': udf()(params[0]['value'])}]
    monkeypatch.setattr(ranking_backend, 'execute', execute)
    hospitals = [dict(H, private_note='not sent'), dict(H, id='b', staff=0, miles=8)]
    args = (hospitals, {'test': [1, 3, 5]}, lambda *_: 1.2, level, ['fracture'], 'Private', True, True)
    assert asyncio.run(ranking_backend.rank(*args)) == scoring.rank(*args)
def test_local_does_not_call_databricks(monkeypatch):
    monkeypatch.setenv('RANKING_BACKEND', 'local')
    async def fail(*_):
        raise AssertionError('remote called')
    monkeypatch.setattr(ranking_backend, 'execute', fail)
    assert asyncio.run(ranking_backend.rank([H], {}, lambda *_: 1, 4, ['cut']))
def test_failure_does_not_silently_fallback(monkeypatch):
    monkeypatch.setenv('RANKING_BACKEND', 'databricks')
    async def fail(*_):
        raise HTTPException(503, 'Unavailable')
    monkeypatch.setattr(ranking_backend, 'execute', fail)
    with pytest.raises(HTTPException):
        asyncio.run(ranking_backend.rank([H], {}, lambda *_: 1, 4, ['cut']))
