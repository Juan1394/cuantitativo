"""Planificación y registro de snapshots de cuotas agrupados por liga."""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from src.db import get_connection
from src.radar.rate_limit import RateLimitBudgetManager
from src.radar.the_odds_api import SHARP_BOOKMAKERS, SPORT_KEY_MAP

logger = logging.getLogger(__name__)

SnapshotStatus = Literal["valid", "gap", "skipped"]


@dataclass(frozen=True)
class SnapshotPlan:
    event_id: str
    competition_code: str
    kickoff_utc: datetime
    next_snapshot_at_utc: datetime
    priority: int


@dataclass(frozen=True)
class SnapshotResult:
    event_id: str
    status: SnapshotStatus
    rows_written: int
    skip_reason: str | None
    error: str | None


def _as_utc_datetime(value: datetime | str) -> datetime:
    """Parsea una fecha y devuelve un datetime consciente en UTC."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _map_odds_event_to_local(
    odds_event: dict, events_by_id: dict[str, dict]
) -> str | None:
    """Mapeo provisional por kickoff y pareja de equipos, con tolerancia de cinco minutos.

    The Odds API `id` identifica el evento del proveedor, pero aún no existe un
    resolver canónico de equipos. En Fase 1.3 este emparejamiento temporal y
    textual se sustituirá por el resolver normalizado.
    """
    if not all(key in odds_event for key in ("id", "home_team", "away_team", "commence_time")):
        return None
    try:
        odds_kickoff = _as_utc_datetime(odds_event["commence_time"])
    except (TypeError, ValueError, OverflowError):
        return None

    odds_home = str(odds_event["home_team"]).strip().casefold()
    odds_away = str(odds_event["away_team"]).strip().casefold()
    candidates: list[str] = []
    tolerance = timedelta(minutes=5)
    for event_id, local_event in events_by_id.items():
        try:
            local_kickoff = _as_utc_datetime(local_event["kickoff_utc"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        local_home = str(local_event.get("home_team", "")).strip().casefold()
        local_away = str(local_event.get("away_team", "")).strip().casefold()
        if (
            abs(local_kickoff - odds_kickoff) <= tolerance
            and local_home == odds_home
            and local_away == odds_away
        ):
            candidates.append(event_id)
    return candidates[0] if len(candidates) == 1 else None


def _selection_for_outcome(outcome_name: str, home_team: str, away_team: str) -> str | None:
    normalized_name = outcome_name.strip().casefold()
    if normalized_name == "draw":
        return "DRAW"
    if normalized_name == home_team.strip().casefold():
        return "HOME"
    if normalized_name == away_team.strip().casefold():
        return "AWAY"
    return None


class SnapshotRecorder:
    """Agrupa capturas por liga para minimizar peticiones facturables a The Odds API."""

    def __init__(
        self,
        db_path: Path,
        odds_client,
        budget: RateLimitBudgetManager,
        settings,
    ):
        self.db_path = Path(db_path)
        self.odds_client = odds_client
        self.budget = budget
        self.settings = settings

    @staticmethod
    def _now_utc() -> datetime:
        return datetime.now(timezone.utc)

    def plan(self, events: list[dict], now_utc: datetime) -> list[SnapshotPlan]:
        """Calcula próximos planes sin llamar a proveedores ni escribir en SQLite."""
        now = _as_utc_datetime(now_utc)
        near_window = timedelta(hours=self.settings.snapshot_near_window_h)
        near_interval = timedelta(hours=self.settings.snapshot_interval_near_h)
        far_interval = timedelta(hours=self.settings.snapshot_interval_far_h)
        eligible: list[SnapshotPlan] = []

        with closing(get_connection(self.db_path)) as conn:
            for event in events:
                if event.get("match_status") != "scheduled":
                    continue
                competition_code = event.get("competition_code")
                if competition_code not in self.settings.active_competitions:
                    continue
                try:
                    kickoff = _as_utc_datetime(event["kickoff_utc"])
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
                if kickoff <= now:
                    continue

                time_to_kickoff = kickoff - now
                interval = near_interval if time_to_kickoff < near_window else far_interval
                latest = conn.execute(
                    "SELECT MAX(captured_at_utc) FROM snapshots "
                    "WHERE event_id = ? AND status = 'valid'",
                    (event["event_id"],),
                ).fetchone()[0]
                if latest is not None and _as_utc_datetime(latest) > now - interval:
                    continue

                eligible.append(
                    SnapshotPlan(
                        event_id=event["event_id"],
                        competition_code=competition_code,
                        kickoff_utc=kickoff,
                        next_snapshot_at_utc=now + interval,
                        priority=int(time_to_kickoff.total_seconds()),
                    )
                )

        return sorted(eligible, key=lambda item: (item.priority, item.kickoff_utc))

    def execute(
        self,
        plans: list[SnapshotPlan],
        now_utc: datetime,
        dry_run: bool = False,
    ) -> list[SnapshotResult]:
        """Obtiene una respuesta por liga y guarda cada lote en su propia transacción."""
        now = _as_utc_datetime(now_utc)
        if not dry_run and not getattr(self.settings, "snapshot_execution_enabled", False):
            raise RuntimeError(
                "snapshot_execution_enabled is False; refusing to hit The Odds API"
            )
        if dry_run:
            return [
                SnapshotResult(
                    event_id=plan.event_id,
                    status="skipped",
                    rows_written=0,
                    skip_reason="dry_run_disabled",
                    error=None,
                )
                for plan in plans
            ]

        grouped: dict[str, list[SnapshotPlan]] = defaultdict(list)
        for plan in plans:
            grouped[plan.competition_code].append(plan)

        results_by_event: dict[str, SnapshotResult] = {}
        group_order = sorted(grouped, key=lambda code: (code != "SP1", code))
        for competition_code in group_order:
            group = grouped[competition_code]
            try:
                if self.budget.get_remaining("the_odds_api", "month") < 1:
                    for plan in group:
                        results_by_event[plan.event_id] = SnapshotResult(
                            event_id=plan.event_id,
                            status="skipped",
                            rows_written=0,
                            skip_reason="budget_exhausted",
                            error=None,
                        )
                    continue

                sport_key = SPORT_KEY_MAP[competition_code]
                odds_events = self.odds_client.get_odds(
                    sport_key, markets="h2h", regions="eu"
                )
                if odds_events is None:
                    for plan in group:
                        results_by_event[plan.event_id] = SnapshotResult(
                            event_id=plan.event_id,
                            status="gap",
                            rows_written=0,
                            skip_reason="api_error",
                            error=None,
                        )
                    continue
                if not isinstance(odds_events, list):
                    raise TypeError("The Odds API response must be a list")

                plan_ids = {plan.event_id for plan in group}
                placeholders = ",".join("?" for _ in plan_ids)
                with closing(get_connection(self.db_path)) as conn:
                    local_rows = conn.execute(
                        "SELECT event_id, competition_code, home_team, away_team, kickoff_utc "
                        f"FROM events WHERE event_id IN ({placeholders})",
                        tuple(plan_ids),
                    ).fetchall()
                events_by_id = {
                    row["event_id"]: dict(row)
                    for row in local_rows
                }

                seen_event_ids: set[str] = set()
                rows_by_event: dict[str, list[tuple]] = defaultdict(list)
                captured_at = now.isoformat()
                for odds_event in odds_events:
                    if not isinstance(odds_event, dict):
                        continue
                    event_id = _map_odds_event_to_local(odds_event, events_by_id)
                    if event_id is None or event_id not in plan_ids:
                        continue
                    seen_event_ids.add(event_id)
                    local_event = events_by_id[event_id]
                    bookmakers = odds_event.get("bookmakers")
                    if not isinstance(bookmakers, list):
                        continue
                    for bookmaker in bookmakers:
                        if not isinstance(bookmaker, dict):
                            continue
                        bookmaker_key = bookmaker.get("key")
                        if bookmaker_key not in SHARP_BOOKMAKERS:
                            continue
                        markets = bookmaker.get("markets")
                        if not isinstance(markets, list):
                            continue
                        for market in markets:
                            if not isinstance(market, dict) or market.get("key") != "h2h":
                                continue
                            outcomes = market.get("outcomes")
                            if not isinstance(outcomes, list):
                                continue
                            for outcome in outcomes:
                                if not isinstance(outcome, dict):
                                    continue
                                outcome_name = outcome.get("name")
                                price = outcome.get("price")
                                if not isinstance(outcome_name, str) or price is None:
                                    continue
                                selection = _selection_for_outcome(
                                    outcome_name,
                                    local_event["home_team"],
                                    local_event["away_team"],
                                )
                                if selection is None:
                                    continue
                                rows_by_event[event_id].append(
                                    (
                                        event_id,
                                        "1X2",
                                        selection,
                                        price,
                                        bookmaker_key,
                                        "valid",
                                        None,
                                        captured_at,
                                    )
                                )

                insert_rows = [
                    row
                    for event_rows in rows_by_event.values()
                    for row in event_rows
                ]
                if insert_rows:
                    conn = get_connection(self.db_path)
                    try:
                        conn.execute("BEGIN IMMEDIATE")
                        conn.executemany(
                            "INSERT INTO snapshots "
                            "(event_id, market, selection, odds, source, status, skip_reason, captured_at_utc) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            insert_rows,
                        )
                        conn.commit()
                    except BaseException:
                        if conn.in_transaction:
                            conn.rollback()
                        raise
                    finally:
                        conn.close()

                for plan in group:
                    if plan.event_id not in seen_event_ids:
                        result = SnapshotResult(
                            event_id=plan.event_id,
                            status="gap",
                            rows_written=0,
                            skip_reason="league_not_in_response",
                            error=None,
                        )
                    else:
                        count = len(rows_by_event.get(plan.event_id, []))
                        if count == 0:
                            result = SnapshotResult(
                                event_id=plan.event_id,
                                status="skipped",
                                rows_written=0,
                                skip_reason="no_odds_available",
                                error=None,
                            )
                        else:
                            result = SnapshotResult(
                                event_id=plan.event_id,
                                status="valid",
                                rows_written=count,
                                skip_reason=None,
                                error=None,
                            )
                    results_by_event[plan.event_id] = result
            except Exception as exc:
                logger.exception(
                    "snapshot_group_failed competition_code=%s", competition_code
                )
                for plan in group:
                    results_by_event[plan.event_id] = SnapshotResult(
                        event_id=plan.event_id,
                        status="gap",
                        rows_written=0,
                        skip_reason="api_error",
                        error=str(exc),
                    )

        return [results_by_event[plan.event_id] for plan in plans]

    def get_event_status(self, event_id: str) -> dict:
        """Resume estados y capturas válidas recientes de un evento."""
        cutoff = self._now_utc() - timedelta(days=7)
        with closing(get_connection(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM snapshots "
                "WHERE event_id = ? AND (status != 'valid' OR captured_at_utc >= ?) "
                "GROUP BY status",
                (event_id, cutoff.isoformat()),
            ).fetchall()
            last_valid = conn.execute(
                "SELECT MAX(captured_at_utc) FROM snapshots "
                "WHERE event_id = ? AND status = 'valid' AND captured_at_utc >= ?",
                (event_id, cutoff.isoformat()),
            ).fetchone()[0]

        counts = {"valid": 0, "gap": 0, "skipped": 0}
        for row in rows:
            if row["status"] in counts:
                counts[row["status"]] = int(row["count"])
        return {
            **counts,
            "min_valid_met": counts["valid"] >= self.settings.snapshot_min_valid_per_event,
            "last_valid_at_utc": last_valid,
        }
