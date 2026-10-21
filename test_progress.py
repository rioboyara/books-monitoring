"""Быстрая ссылка появляется до медленной, API сохраняет совместимость."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

from fastapi.testclient import TestClient
from app import app
from checker import check_many, inspect_link


def run():
    release, delivered = Event(), Event()
    def check(url, *args):
        if url == 'slow':
            assert release.wait(3)
        if url == 'broken':
            raise ValueError('bad page')
        return {'url': url, 'status': 1}
    with patch('checker.check', side_effect=check), ThreadPoolExecutor(1) as pool:
        batch = pool.submit(check_many, ['slow', 'fast'], [], on_result=lambda r: delivered.set() if r['url'] == 'fast' else None)
        try:
            assert delivered.wait(1), 'fast result must not wait for slow URL'
            assert not batch.done()
        finally:
            release.set()
        assert [r['url'] for r in batch.result()] == ['slow', 'fast']
        rows = check_many(['broken', 'fast'], [])
        assert [r['status'] for r in rows] == [2, 1]
    with patch('checker.fetch', side_effect=ValueError) as fetch:
        assert inspect_link('https://example.org/download') is None
        fetch.assert_called_once_with('https://example.org/download', deadline=8)

    def emit(urls, catalog, on_result=None):
        rows = [{'url': u, 'status': 1} for u in urls]
        if on_result:
            for row in reversed(rows):
                on_result(row)
        return rows
    with TestClient(app) as client, patch('app.check_many', side_effect=emit):
        payload = {'urls': ['https://example.org/a', 'https://example.org/b', 'https://example.org/a']}
        response = client.post('/api/check?stream=true', json=payload)
        assert response.status_code == 200
        assert response.headers['X-Accel-Buffering'] == 'no'
        events = [json.loads(line) for line in response.iter_lines() if line]
        assert events[0] == {'total': 2} and events[-1] == {'done': True}
        assert [e['result']['url'] for e in events if 'result' in e] == payload['urls'][1::-1]
        assert [r['url'] for r in client.post('/api/check', json=payload).json()] == payload['urls'][:2]
        assert client.post('/api/check?stream=true', json={'urls': []}).status_code == 400
    with TestClient(app) as client, patch('app.check_many', side_effect=RuntimeError):
        for _ in range(3):
            response = client.post('/api/check?stream=true', json={'urls': ['https://example.org']})
            assert response.status_code == 200, 'failed worker must release capacity'
            assert any('error' in json.loads(line) for line in response.iter_lines() if line)
    print('Progress, ordering, partial failure, legacy API and capacity checks passed')


if __name__ == '__main__':
    run()
