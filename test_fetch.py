"""Паузы и повторы без реальных запросов и ожидания: python test_fetch.py."""
from unittest.mock import patch
from requests import Response
import checker


def response(status):
    r = Response()
    r.status_code, r.url = status, 'https://example.org/book'
    r.headers['Retry-After'] = '3'
    return r, b'page'


def run():
    with patch('checker.get', side_effect=[response(429), response(200)]) as get, patch('checker.defer') as defer:
        assert checker.fetch('https://example.org/book')[0].status_code == 200
        assert get.call_count == 2
        defer.assert_called_once_with('https://example.org/book', 3)
    with patch('checker.get', return_value=response(403)) as get:
        assert checker.fetch('https://example.org/book')[0].status_code == 403
        assert get.call_count == 1
    assert checker.retry_delay('invalid', 1) == 4
    assert checker.retry_delay('120', 0) == 120
    clock, sleeps = [100.0], []
    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
    with patch('checker.time.monotonic', side_effect=lambda: clock[0]), patch('checker.time.sleep', side_effect=sleep), patch('checker.random.uniform', return_value=2):
        checker._next_request.clear()
        checker.pace('https://example.org/a', 200)
        checker.pace('https://s4.example.org/b', 200)
        assert sleeps == [2]
        checker.pace('https://different.org/a', 200)
        assert sleeps == [2]
    checker._next_request.clear()
    print('fetch checks passed')


if __name__ == '__main__':
    run()
