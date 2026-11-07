"""Verify mounted dashboard routing and preserve the supplied history contract."""
from pathlib import Path
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from faster import db
from faster.app import app
from faster.capacity import main, generate_data, history


def test_combined_dashboards_and_history(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "demo.db"))
    monkeypatch.setattr(main, "MODE", "demo")
    monkeypatch.setattr(main, "ROOT", tmp_path / "capacity")
    monkeypatch.setattr(generate_data, "ROOT", tmp_path / "capacity")
    with TestClient(app) as client:
        assert '/capacity/' in client.get('/').text
        page = client.get('/capacity/')
        assert page.status_code == 200
        assert 'fetch("/capacity"+path,options)' in page.text
        assert isinstance(client.get('/api/hospitals').json(), list)
        comparison = client.get('/capacity/api/hospitals').json()
        assert comparison['best_hospital_id'] == 'H2'
        assert client.get('/capacity/api/health').status_code == 200
        assert client.get('/capacity/docs').status_code == 200
        assert client.post('/capacity/api/demo/staff', json={'hospital_id': 'H2', 'active_clinicians': 0}).status_code == 200
        assert client.get('/capacity/api/hospitals').json()['best_hospital_id'] == 'H1'
        assert client.post('/capacity/api/explain', json={}).json()['text']
        execute = AsyncMock(return_value=[{'hospital_id': '123', 'facility_name': 'Test Facility', 'discharge_count': 40}])
        monkeypatch.setattr(history, 'execute', execute)
        result = client.get('/capacity/api/history/hospitals?limit=10&offset=20')
        assert result.status_code == 200
        assert result.json()['hospitals'][0]['facility_name'] == 'Test Facility'
        sql, parameters = execute.call_args.args
        assert 'ORDER BY facility_name ASC' in sql
        assert parameters == [{'name': 'row_limit', 'value': '10', 'type': 'INT'}, {'name': 'row_offset', 'value': '20', 'type': 'INT'}]
        assert client.get('/capacity/api/history/hospitals?limit=0').status_code == 422
        execute.reset_mock()
        execute.return_value = [{'facility_name': 'Test Facility', 'discharge_count': 40}]
        response = client.get('/capacity/api/history/hospitals/123?age_group=18%20to%2029&diagnosis_code=ABC')
        assert response.status_code == 200
        assert response.json()['length_of_stay_unit'] == 'days'
        assert execute.await_count == 4
        sql, parameters = execute.call_args_list[0].args
        assert ':facility_id' in sql and ':age_group' in sql and ':ccsr_diagnosis_code' in sql
        assert {'name': 'facility_id', 'value': '123', 'type': 'STRING'} in parameters
