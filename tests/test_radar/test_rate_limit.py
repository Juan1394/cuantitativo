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


def test_persistencia_entre_procesos(budget):
    import subprocess
    import sys

    code = (
        "from pathlib import Path; from types import SimpleNamespace; "
        "from src.radar.rate_limit import RateLimitBudgetManager; "
        f"m=RateLimitBudgetManager(SimpleNamespace(db_paths=SimpleNamespace(bets=Path({str(budget.db_path)!r})))); "
        "assert m.consume('football_data_org', 'minute', 5)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert budget.get_remaining("football_data_org", "minute") == 5


def test_reset_diario_cruzando_anio(budget, monkeypatch):
    from datetime import datetime, timezone

    conn = get_connection(budget.db_path)
    conn.execute(
        "INSERT INTO rate_limit_budget VALUES (?, ?, ?, ?, ?)",
        ("api_football", "day", 5, 100, "2025-12-31T23:59:59+00:00"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        budget,
        "_now_utc",
        lambda: datetime(2025, 12, 31, 23, 59, 58, tzinfo=timezone.utc),
    )
    assert budget.consume("api_football", "day")

    monkeypatch.setattr(
        budget,
        "_now_utc",
        lambda: datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
    )
    assert budget.get_remaining("api_football", "day") == 100


def test_reset_mensual_febrero_bisiesto(budget, monkeypatch):
    from datetime import datetime, timezone

    conn = get_connection(budget.db_path)
    conn.execute(
        "INSERT INTO rate_limit_budget VALUES (?, ?, ?, ?, ?)",
        ("the_odds_api", "month", 7, 500, "2024-02-28T23:59:59+00:00"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        budget,
        "_now_utc",
        lambda: datetime(2024, 2, 28, 23, 59, 58, tzinfo=timezone.utc),
    )
    assert budget.consume("the_odds_api", "month")

    monkeypatch.setattr(
        budget,
        "_now_utc",
        lambda: datetime(2024, 2, 29, 0, 0, 0, tzinfo=timezone.utc),
    )
    assert budget.get_remaining("the_odds_api", "month") == 492

    monkeypatch.setattr(
        budget,
        "_now_utc",
        lambda: datetime(2024, 3, 1, 0, 0, 0, tzinfo=timezone.utc),
    )
    assert budget.get_remaining("the_odds_api", "month") == 500


def test_concurrencia_agotamiento(budget, monkeypatch):
    from threading import Barrier

    from src.radar import rate_limit

    monkeypatch.setitem(rate_limit.PROVIDER_LIMITS, ("football_data_org", "minute"), 5)
    budget.reset("football_data_org", "minute")
    barrier = Barrier(20)

    def consume_together():
        barrier.wait()
        return budget.consume("football_data_org", "minute")

    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(executor.map(lambda _index: consume_together(), range(20)))

    assert sum(results) == 5
    assert budget.get_remaining("football_data_org", "minute") == 0
