"""Mocks estructurales JSON basados en la forma de respuesta de documentación; no son datos de producción."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.db import get_connection
from src.migrations import apply_migrations
from src.radar.rate_limit import RateLimitBudgetManager
from src.radar.the_odds_api import SHARP_BOOKMAKERS, TheOddsAPIClient

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def manager(tmp_path):
    path = tmp_path / "odds.db"
    conn = get_connection(path)
    apply_migrations(conn, ROOT / "migrations")
    conn.close()
    settings = SimpleNamespace(db_paths=SimpleNamespace(bets=path))
    return RateLimitBudgetManager(settings)


@pytest.fixture
def client(manager):
    return TheOddsAPIClient(SimpleNamespace(sharp_odds_api_key="test-key"), manager)


def test_list_sports_no_consume_cuota(httpx_mock, client, manager):
    payload = [{"key": "soccer_test", "group": "Soccer", "title": "Soccer", "active": True}]
    httpx_mock.add_response(json=payload)
    before = manager.get_remaining("the_odds_api", "month")

    assert client.list_sports() == payload
    assert manager.get_remaining("the_odds_api", "month") == before == 500


def test_get_odds_consume_y_devuelve_none_sin_cuota(httpx_mock, client, manager, caplog):
    conn = get_connection(manager.db_path)
    conn.execute(
        "INSERT INTO rate_limit_budget VALUES (?, ?, ?, ?, ?)",
        ("the_odds_api", "month", 500, 500, "2999-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    assert client.get_odds("soccer_test") is None
    assert "monthly_quota_exhausted" in caplog.text
    assert not httpx_mock.get_requests()


def test_get_odds_filtra_bookmakers_sharp(httpx_mock, client, manager):
    # Respuesta estructural de documentación: evento con colección bookmakers.
    fixture = [{
        "id": "event-doc-example",
        "sport_key": "soccer_test",
        "commence_time": "2030-01-01T12:00:00Z",
        "home_team": "Home Example",
        "away_team": "Away Example",
        "bookmakers": [
            {"key": "pinnacle", "title": "Pinnacle", "markets": []},
            {"key": "betfair_ex_eu", "title": "Betfair Exchange", "markets": []},
            {"key": "bookmaker_example", "title": "Other", "markets": []},
        ],
    }]
    httpx_mock.add_response(json=fixture)

    result = client.get_odds("soccer_test")

    assert result is not None
    assert {item["key"] for item in result[0]["bookmakers"]} == SHARP_BOOKMAKERS
    assert manager.get_remaining("the_odds_api", "month") == 499
