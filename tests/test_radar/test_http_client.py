"""Pruebas HTTP simuladas; no realizan peticiones externas."""

from src.radar import http_client
from src.radar.http_client import get_json


def test_before_attempt_called_per_retry(httpx_mock, monkeypatch):
    monkeypatch.setattr(http_client.time, "sleep", lambda _seconds: None)
    httpx_mock.add_response(status_code=503)
    httpx_mock.add_response(status_code=503)
    httpx_mock.add_response(json={"ok": True})
    calls = 0

    def before_attempt():
        nonlocal calls
        calls += 1
        return True

    result = get_json("https://provider.example/resource", {}, {}, before_attempt=before_attempt)

    assert result == {"ok": True}
    assert calls == 3
    assert len(httpx_mock.get_requests()) == 3


def test_before_attempt_false_aborts(httpx_mock, monkeypatch):
    monkeypatch.setattr(http_client.time, "sleep", lambda _seconds: None)
    httpx_mock.add_response(status_code=503, is_optional=True)
    httpx_mock.add_response(status_code=503, is_optional=True)
    httpx_mock.add_response(json={"ok": True}, is_optional=True)
    calls = 0

    def before_attempt():
        nonlocal calls
        calls += 1
        return calls == 1

    result = get_json("https://provider.example/resource", {}, {}, before_attempt=before_attempt)

    assert result is None
    assert calls == 2
    assert len(httpx_mock.get_requests()) == 1
