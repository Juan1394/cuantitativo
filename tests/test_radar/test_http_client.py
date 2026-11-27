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


def test_429_returns_none(httpx_mock, caplog):
    httpx_mock.add_response(
        status_code=429,
        headers={"Retry-After": "30"},
        is_optional=True,
    )

    assert get_json("https://provider.example/resource", {}, {}) is None
    assert "retry_after_seconds=30" in caplog.text
    assert len(httpx_mock.get_requests()) == 1


def test_429_without_retry_after(httpx_mock):
    httpx_mock.add_response(status_code=429, is_optional=True)

    assert get_json("https://provider.example/resource", {}, {}) is None
    assert len(httpx_mock.get_requests()) == 1


def test_5xx_exhausts_retries(httpx_mock, monkeypatch):
    monkeypatch.setattr(http_client.time, "sleep", lambda _seconds: None)
    for _ in range(3):
        httpx_mock.add_response(status_code=503, is_optional=True)

    assert get_json("https://provider.example/resource", {}, {}) is None
    assert len(httpx_mock.get_requests()) == 3


def test_5xx_recovery(httpx_mock, monkeypatch):
    monkeypatch.setattr(http_client.time, "sleep", lambda _seconds: None)
    httpx_mock.add_response(status_code=503, is_optional=True)
    httpx_mock.add_response(json={"recovered": True}, is_optional=True)

    assert get_json("https://provider.example/resource", {}, {}) == {"recovered": True}
    assert len(httpx_mock.get_requests()) == 2


def test_4xx_no_retry(httpx_mock):
    httpx_mock.add_response(status_code=400, is_optional=True)

    assert get_json("https://provider.example/resource", {}, {}) is None
    assert len(httpx_mock.get_requests()) == 1


def test_timeout_returns_none(httpx_mock):
    import httpx

    httpx_mock.add_exception(httpx.TimeoutException("timeout"), is_optional=True)

    assert get_json("https://provider.example/resource", {}, {}) is None


def test_connection_error_returns_none(httpx_mock):
    import httpx

    httpx_mock.add_exception(httpx.ConnectError("connection refused"), is_optional=True)

    assert get_json("https://provider.example/resource", {}, {}) is None


def test_invalid_json_returns_none(httpx_mock, caplog):
    httpx_mock.add_response(status_code=200, text="<html>not json</html>", is_optional=True)

    assert get_json("https://provider.example/resource", {}, {}) is None
    assert "http_invalid_json" in caplog.text


def test_empty_body_returns_none(httpx_mock):
    httpx_mock.add_response(status_code=200, text="", is_optional=True)

    assert get_json("https://provider.example/resource", {}, {}) is None


def test_before_attempt_exception_propagates():
    def raise_error():
        raise RuntimeError("boom")

    import pytest

    with pytest.raises(RuntimeError, match="boom"):
        get_json(
            "https://provider.example/resource",
            {},
            {},
            before_attempt=raise_error,
        )


def test_retry_after_http_date(httpx_mock, caplog):
    httpx_mock.add_response(
        status_code=429,
        headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"},
        is_optional=True,
    )

    assert get_json("https://provider.example/resource", {}, {}) is None
    assert "retry_after_seconds=" in caplog.text
    logged_seconds = caplog.text.split("retry_after_seconds=")[-1].split()[0]
    assert int(logged_seconds) >= 0


def test_retry_after_invalid_format(httpx_mock, caplog):
    httpx_mock.add_response(
        status_code=429,
        headers={"Retry-After": "garbage"},
        is_optional=True,
    )

    assert get_json("https://provider.example/resource", {}, {}) is None
    assert "retry_after_seconds=None" in caplog.text or "retry_after_seconds=" not in caplog.text


def test_3xx_returns_none(httpx_mock, caplog):
    httpx_mock.add_response(
        status_code=302,
        headers={"Location": "/other"},
        is_optional=True,
    )

    assert get_json("https://provider.example/resource", {}, {}) is None
    assert "http_unexpected_status" in caplog.text


def test_json_string_shape_returns_none(httpx_mock, caplog):
    httpx_mock.add_response(status_code=200, text='"hello"', is_optional=True)

    assert get_json("https://provider.example/resource", {}, {}) is None
    assert "http_json_unexpected_shape" in caplog.text


def test_json_null_shape_returns_none(httpx_mock, caplog):
    httpx_mock.add_response(status_code=200, text="null", is_optional=True)

    assert get_json("https://provider.example/resource", {}, {}) is None
    assert "http_json_unexpected_shape" in caplog.text


def test_last_status_code_helper(httpx_mock):
    from src.radar.http_client import get_last_status_code

    httpx_mock.add_response(status_code=200, json={"ok": True}, is_optional=True)
    httpx_mock.add_response(status_code=429, is_optional=True)

    assert get_json("https://provider.example/resource", {}, {}) == {"ok": True}
    assert get_last_status_code() == 200
    assert get_json("https://provider.example/resource", {}, {}) is None
    assert get_last_status_code() == 429
