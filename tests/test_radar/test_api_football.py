"""Mocks estructurales JSON basados en la forma documentada; no producción."""

from pathlib import Path
from types import SimpleNamespace

from src.db import get_connection
from src.migrations import apply_migrations
from src.radar.api_football import APIFootballClient, LEAGUE_MAP
from src.radar.rate_limit import RateLimitBudgetManager

ROOT = Path(__file__).resolve().parents[2]


def test_get_fixtures_mapea_league_id(httpx_mock, tmp_path):
    path = tmp_path / "api-football.db"
    conn = get_connection(path)
    apply_migrations(conn, ROOT / "migrations")
    conn.close()
    budget = RateLimitBudgetManager(SimpleNamespace(db_paths=SimpleNamespace(bets=path)))
    settings = SimpleNamespace(api_football_key="test-key")
    client = APIFootballClient(settings, budget)
    # Forma documentada: objeto de respuesta con campo `response` tipo lista.
    httpx_mock.add_response(json={"get": "fixtures", "parameters": {}, "errors": [], "results": 0, "response": []})

    assert LEAGUE_MAP == {"SP1": 140, "E0": 39, "I1": 135}
    assert client.get_fixtures(LEAGUE_MAP["SP1"], 2030, "2030-01-01") == []
    request = httpx_mock.get_requests()[0]
    assert request.url.params["league"] == "140"
    assert request.url.params["season"] == "2030"
    assert request.headers["x-apisports-key"] == "test-key"
    assert budget.get_remaining("api_football", "day") == 99
