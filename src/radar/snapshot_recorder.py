"""Planificación y registro de snapshots de cuotas agrupados por liga."""

from __future__ import annotations

import logging
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from src.db import get_connection
from src.radar.normalization import get_alias_status, resolve_odds_event_to_local
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
        """Obtiene cuotas por liga y procesa cada grupo en una transacción.

        Un evento con home_team o away_team pending/rejected/no registrado en
        team_aliases NO genera snapshots válidos. Se marca gap con
        team_not_confirmed. Esto protege contra aliases fuzzy no confirmados aunque
        el canonical tenga otros alias confirmed.
        """
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
                captured_at = now.isoformat()
                insert_sql = (
                    "INSERT INTO snapshots "
                    "(event_id, market, selection, odds, source, status, skip_reason, captured_at_utc) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                )
                with closing(get_connection(self.db_path)) as conn:
                    try:
                        conn.execute("BEGIN IMMEDIATE")
                        seen_event_ids: set[str] = set()
                        for odds_event in odds_events:
                            if not isinstance(odds_event, dict):
                                continue
                            event_id = resolve_odds_event_to_local(
                                conn,
                                odds_event,
                                competition_code,
                                source="the_odds_api",
                            )
                            if (
                                event_id is None
                                or event_id not in plan_ids
                                or event_id in seen_event_ids
                            ):
                                continue
                            seen_event_ids.add(event_id)

                            raw_home = odds_event.get("home_team")
                            raw_away = odds_event.get("away_team")
                            home_status = get_alias_status(
                                conn,
                                raw_home,
                                competition_code,
                                "the_odds_api",
                            )
                            away_status = get_alias_status(
                                conn,
                                raw_away,
                                competition_code,
                                "the_odds_api",
                            )
                            if home_status != "confirmed" or away_status != "confirmed":
                                results_by_event[event_id] = SnapshotResult(
                                    event_id=event_id,
                                    status="gap",
                                    rows_written=0,
                                    skip_reason="team_not_confirmed",
                                    error=None,
                                )
                                logger.warning(
                                    "team_not_confirmed event_id=%s home_status=%s "
                                    "away_status=%s home_team=%r away_team=%r",
                                    event_id,
                                    home_status,
                                    away_status,
                                    raw_home,
                                    raw_away,
                                )
                                continue

                            local_event = conn.execute(
                                "SELECT home_team, away_team FROM events WHERE event_id = ?",
                                (event_id,),
                            ).fetchone()
                            if local_event is None:
                                continue

                            event_rows: list[tuple] = []
                            bookmakers = odds_event.get("bookmakers")
                            if isinstance(bookmakers, list):
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
                                        if (
                                            not isinstance(market, dict)
                                            or market.get("key") != "h2h"
                                        ):
                                            continue
                                        outcomes = market.get("outcomes")
                                        if not isinstance(outcomes, list):
                                            continue
                                        for outcome in outcomes:
                                            if not isinstance(outcome, dict):
                                                continue
                                            outcome_name = outcome.get("name")
                                            price = outcome.get("price")
                                            if (
                                                not isinstance(outcome_name, str)
                                                or price is None
                                            ):
                                                continue
                                            selection = _selection_for_outcome(
                                                outcome_name,
                                                local_event["home_team"],
                                                local_event["away_team"],
                                            )
                                            if selection is None:
                                                continue
                                            event_rows.append(
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

                            if event_rows:
                                conn.executemany(insert_sql, event_rows)
                                results_by_event[event_id] = SnapshotResult(
                                    event_id=event_id,
                                    status="valid",
                                    rows_written=len(event_rows),
                                    skip_reason=None,
                                    error=None,
                                )
                            else:
                                results_by_event[event_id] = SnapshotResult(
                                    event_id=event_id,
                                    status="skipped",
                                    rows_written=0,
                                    skip_reason="no_odds_available",
                                    error=None,
                                )
                        conn.commit()
                    except BaseException:
                        if conn.in_transaction:
                            conn.rollback()
                        raise

                for plan in group:
                    if plan.event_id not in results_by_event:
                        results_by_event[plan.event_id] = SnapshotResult(
                            event_id=plan.event_id,
                            status="gap",
                            rows_written=0,
                            skip_reason="league_not_in_response",
                            error=None,
                        )
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
        """Resume estados recientes de un evento.

        valid = número de capturas temporales distintas (COUNT DISTINCT captured_at_utc),
        no de filas. Cada execute() sobre un evento puede escribir múltiples filas
        (bookmakers × outcomes) con el mismo captured_at_utc; eso cuenta como 1 captura.
        """
        cutoff = self._now_utc() - timedelta(days=7)
        with closing(get_connection(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT status, CASE WHEN status = 'valid' "
                "THEN COUNT(DISTINCT captured_at_utc) ELSE COUNT(*) END AS count "
                "FROM snapshots "
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
