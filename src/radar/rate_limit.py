"""Presupuesto persistente y seguro frente a concurrencia para APIs externas."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from src.db import get_connection

Window = Literal["minute", "day", "month"]

PROVIDER_LIMITS: dict[tuple[str, str], int] = {
    ("the_odds_api", "month"): 500,
    ("football_data_org", "minute"): 10,
    ("api_football", "day"): 100,
}


class RateLimitBudgetManager:
    """Administra contadores SQLite por proveedor y ventana, con `BEGIN IMMEDIATE`.

    The Odds API considera gratuitos `list_sports`, `get_events` y
    `odds_api_quota`; el consumo de presupuesto de este proyecto solo se aplica
    a `get_odds`.

    `settings` se usa para obtener la ruta de la base principal
    (`settings.db_paths.bets`). La migración 0006 debe haberse aplicado antes.
    """

    def __init__(self, settings: object):
        self.db_path = Path(settings.db_paths.bets)  # type: ignore[attr-defined]

    @staticmethod
    def _now_utc() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _limit(provider: str, window: str) -> int:
        try:
            return PROVIDER_LIMITS[(provider, window)]
        except KeyError as exc:
            raise ValueError(f"ventana/proveedor no admitido: {provider}/{window}") from exc

    @staticmethod
    def _next_reset(window: str, now: datetime) -> datetime:
        now = now.astimezone(timezone.utc)
        if window == "minute":
            return now + timedelta(seconds=60)
        if window == "day":
            next_day = now.date() + timedelta(days=1)
            return datetime.combine(next_day, datetime.min.time(), timezone.utc)
        if window == "month":
            year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
            return datetime(year, month, 1, tzinfo=timezone.utc)
        raise ValueError(f"ventana no admitida: {window}")

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _get_or_reset(
        self, conn: sqlite3.Connection, provider: str, window: str, now: datetime
    ) -> tuple[int, int]:
        limit_value = self._limit(provider, window)
        row = conn.execute(
            "SELECT used, limit_value, reset_at_utc FROM rate_limit_budget "
            "WHERE provider = ? AND window = ?",
            (provider, window),
        ).fetchone()
        if row is None:
            reset_at = self._next_reset(window, now)
            conn.execute(
                "INSERT INTO rate_limit_budget "
                "(provider, window, used, limit_value, reset_at_utc) "
                "VALUES (?, ?, 0, ?, ?)",
                (provider, window, limit_value, reset_at.isoformat()),
            )
            return 0, limit_value

        used, stored_limit, reset_at_utc = row
        if self._parse_timestamp(reset_at_utc) <= now:
            reset_at = self._next_reset(window, now)
            conn.execute(
                "UPDATE rate_limit_budget SET used = 0, limit_value = ?, reset_at_utc = ? "
                "WHERE provider = ? AND window = ?",
                (limit_value, reset_at.isoformat(), provider, window),
            )
            return 0, limit_value
        if stored_limit != limit_value:
            conn.execute(
                "UPDATE rate_limit_budget SET limit_value = ? WHERE provider = ? AND window = ?",
                (limit_value, provider, window),
            )
            stored_limit = limit_value
        return used, stored_limit

    def consume(self, provider: str, window: str, cost: int = 1) -> bool:
        """Consume `cost` units atomically, returning False if over the limit."""
        self._limit(provider, window)
        if not isinstance(cost, int) or isinstance(cost, bool) or cost <= 0:
            raise ValueError("cost debe ser un entero positivo")
        now = self._now_utc()
        conn = get_connection(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            used, limit_value = self._get_or_reset(conn, provider, window, now)
            if used + cost > limit_value:
                conn.commit()
                return False
            conn.execute(
                "UPDATE rate_limit_budget SET used = ? WHERE provider = ? AND window = ?",
                (used + cost, provider, window),
            )
            conn.commit()
            return True
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def get_remaining(self, provider: str, window: str) -> int:
        """Devuelve el presupuesto disponible y materializa un reset vencido."""
        self._limit(provider, window)
        now = self._now_utc()
        conn = get_connection(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            used, limit_value = self._get_or_reset(conn, provider, window, now)
            conn.commit()
            return max(0, limit_value - used)
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def reset(self, provider: str, window: str) -> None:
        """Limpia el contador indicado (utilidad para pruebas)."""
        limit_value = self._limit(provider, window)
        now = self._now_utc()
        conn = get_connection(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM rate_limit_budget WHERE provider = ? AND window = ?",
                (provider, window),
            )
            conn.execute(
                "INSERT INTO rate_limit_budget "
                "(provider, window, used, limit_value, reset_at_utc) "
                "VALUES (?, ?, 0, ?, ?)",
                (provider, window, limit_value, self._next_reset(window, now).isoformat()),
            )
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()
