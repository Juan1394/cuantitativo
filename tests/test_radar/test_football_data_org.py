"""Mocks estructurales JSON basados en documentación; no representan producción."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.db import get_connection
from src.migrations import apply_migrations
from src.radar.football_data_org import COMPETITION_MAP, FootballDataOrgClient
from src.radar.rate_limit import RateLimitBudgetManager

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def setup_client(tmp_path):
    path = tmp_path / "football-data.db"
    conn = get_connection(path)
    apply_migrations(conn, ROOT / "migrations")
    conn.close()
    budget = RateLimitBudgetManager(SimpleNamespace(db_paths=SimpleNamespace(bets=path)))
    settings = SimpleNamespace(football_data_org_key="test-token")
    return FootballDataOrgClient(settings, budget), budget


def test_get_matches_mapea_competicion_a_endpoint_y_token(httpx_mock, setup_client):
    client, budget = setup_client
    # Forma documentada de la respuesta de /competitions/{code}/matches.
    payload = {"filters": {}, "resultSet": {}, "competition": {"code": "PD"}, "matches": []}
    httpx_mock.add_response(json=payload)

    assert COMPETITION_MAP == {"SP1": "PD", "E0": "PL", "I1": "SA"}
    assert client.get_matches("SP1", "2030-01-01", "2030-01-02") == []
    request = httpx_mock.get_requests()[0]
    assert request.url.path.endswith("/competitions/PD/matches")
    assert request.headers["X-Auth-Token"] == "test-token"
    assert budget.get_remaining("football_data_org", "minute") == 9


def test_429_devuelve_none_y_no_consumo_adicional(httpx_mock, setup_client):
    client, budget = setup_client
    httpx_mock.add_response(status_code=429, headers={"Retry-After": "30"})

    assert client.get_matches("E0", "2030-01-01", "2030-01-02") is None
    assert len(httpx_mock.get_requests()) == 1
    assert budget.get_remaining("football_data_org", "minute") == 9
