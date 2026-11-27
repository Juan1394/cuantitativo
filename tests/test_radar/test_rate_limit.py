"""Pruebas de presupuesto; la BD temporal usa el DDL de la migración 0006."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.db import get_connection
from src.migrations import apply_migrations
from src.radar.rate_limit import RateLimitBudgetManager

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def budget(tmp_path):
    db_path = tmp_path / "budget.db"
    conn = get_connection(db_path)
    apply_migrations(conn, ROOT / "migrations")
    conn.close()
    settings = SimpleNamespace(db_paths=SimpleNamespace(bets=db_path))
    return RateLimitBudgetManager(settings)


def test_consume_respeta_limite_y_devuelve_false_al_agotarse(budget):
    budget.reset("football_data_org", "minute")
    assert budget.consume("football_data_org", "minute", cost=9)
    assert budget.get_remaining("football_data_org", "minute") == 1
    assert budget.consume("football_data_org", "minute")
    assert not budget.consume("football_data_org", "minute")
    assert budget.get_remaining("football_data_org", "minute") == 0


def test_consume_resetea_ventana_expirada(budget):
    now = datetime.now(timezone.utc)
    conn = get_connection(budget.db_path)
    conn.execute(
        "INSERT INTO rate_limit_budget "
        "(provider, window, used, limit_value, reset_at_utc) VALUES (?, ?, ?, ?, ?)",
        ("football_data_org", "minute", 10, 10, (now - timedelta(seconds=1)).isoformat()),
    )
    conn.commit()
    conn.close()

    assert budget.consume("football_data_org", "minute")
    assert budget.get_remaining("football_data_org", "minute") == 9
    conn = get_connection(budget.db_path)
    row = conn.execute(
        "SELECT reset_at_utc FROM rate_limit_budget WHERE provider = ? AND window = ?",
        ("football_data_org", "minute"),
    ).fetchone()
    conn.close()
    assert datetime.fromisoformat(row[0]) > now


def test_concurrencia_consume_atomicamente(budget):
    budget.reset("football_data_org", "minute")
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(budget.consume, "football_data_org", "minute", 3),
            executor.submit(budget.consume, "football_data_org", "minute", 2),
        ]
        results = [future.result() for future in futures]

    assert results == [True, True]
    assert budget.get_remaining("football_data_org", "minute") == 5
    assert budget.consume("football_data_org", "minute", 5)
    assert not budget.consume("football_data_org", "minute")
