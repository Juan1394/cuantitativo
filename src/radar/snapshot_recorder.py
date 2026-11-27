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
from src.data_quality import log_validation, validate_event, validate_odds
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


def _unresolved_event_id(conn, odds_event: dict, competition_code: str) -> str | None:
    """Identifica un gap por horario y equipos conocidos, nunca autoriza cuotas.

    Exige una única coincidencia local en ±10 minutos y respeta el canonical de
    cada alias conocido. Considera también eventos fuera del plan para no atribuir
    al plan una respuesta que corresponde a otro partido de la competición.
    """
    kickoff_text = odds_event.get("commence_time")
    if not isinstance(kickoff_text, str):
        return None
    try:
        kickoff = _as_utc_datetime(kickoff_text)
    except (TypeError, ValueError, OverflowError):
        return None
    known_teams = {}
    for side in ("home_team", "away_team"):
        raw_name = odds_event.get(side)
        if not isinstance(raw_name, str):
            return None
        alias = conn.execute(
            "SELECT canonical_name FROM team_aliases "
            "WHERE raw_name = ? AND competition_code = ? AND source = 'the_odds_api'",
            (raw_name, competition_code),
        ).fetchone()
        if alias is not None and alias["canonical_name"]:
            known_teams[side] = alias["canonical_name"]
    candidates = []
    for event in conn.execute(
        "SELECT event_id, home_team, away_team, kickoff_utc FROM events "
        "WHERE competition_code = ?",
        (competition_code,),
    ):
        try:
            local_kickoff = _as_utc_datetime(event["kickoff_utc"])
        except (TypeError, ValueError, OverflowError):
            continue
        if abs(local_kickoff - kickoff) > timedelta(minutes=10):
            continue
        if all(event[side] == canonical for side, canonical in known_teams.items()):
            candidates.append(event["event_id"])
    return candidates[0] if len(candidates) == 1 else None


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
        """Calcula planes, registrando y omitiendo eventos que fallen la validación."""
        now = _as_utc_datetime(now_utc)
        near_window = timedelta(hours=self.settings.snapshot_near_window_h)
        near_interval = timedelta(hours=self.settings.snapshot_interval_near_h)
        far_interval = timedelta(hours=self.settings.snapshot_interval_far_h)
        eligible: list[SnapshotPlan] = []

        with closing(get_connection(self.db_path)) as conn:
            for event in events:
                validation = validate_event(event, now)
                if not validation.valid:
                    event_id = event.get("event_id") if isinstance(event, dict) else None
                    log_validation(conn, "event", event_id, validation, now)
                    logger.warning(
                        "invalid_event event_id=%s reasons=%s",
                        event_id,
                        [code for code, _ in validation.reasons],
                    )
                    continue
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
            conn.commit()

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
                        resolved_events: dict[str, dict] = {}
                        unresolved_events: set[str] = set()
                        for odds_event in odds_events:
                            if not isinstance(odds_event, dict):
                                continue
                            event_id = resolve_odds_event_to_local(
                                conn,
                                odds_event,
                                competition_code,
                                source="the_odds_api",
                            )
                            if event_id is None:
                                candidate_id = _unresolved_event_id(
                                    conn, odds_event, competition_code
                                )
                                if candidate_id in plan_ids:
                                    unresolved_events.add(candidate_id)
                            elif event_id in plan_ids:
                                resolved_events.setdefault(event_id, odds_event)

                        for plan in group:
                            event_id = plan.event_id
                            odds_event = resolved_events.get(event_id)
                            if odds_event is None:
                                results_by_event[event_id] = SnapshotResult(
                                    event_id=event_id,
                                    status="gap",
                                    rows_written=0,
                                    skip_reason=(
                                        "team_not_confirmed"
                                        if event_id in unresolved_events
                                        else "league_not_in_response"
                                    ),
                                    error=None,
                                )
                                continue

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
                                        market_captured_at = (
                                            market.get("last_update")
                                            or bookmaker.get("last_update")
                                            or captured_at
                                        )
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
                                                    market_captured_at,
                                                )
                                            )

                            if event_rows:
                                odds_by_market: dict[tuple[str, str], list[dict]] = defaultdict(list)
                                for row in event_rows:
                                    # Cada bookmaker aporta su propio mercado 1X2; sumar
                                    # sus selecciones entre fuentes duplicaría el overround.
                                    odds_by_market[(row[1], row[4])].append(
                                        {"odds": row[3], "captured_at_utc": row[7]}
                                    )
                                validation_results = [
                                    (market_name, validate_odds(market_odds, now))
                                    for (market_name, _source), market_odds in odds_by_market.items()
                                ]
                                rejected = [
                                    (market_name, result)
                                    for market_name, result in validation_results
                                    if not result.valid
                                ]
                                if rejected:
                                    for market_name, result in rejected:
                                        log_validation(
                                            conn, "odds", event_id, result, now
                                        )
                                        if any(code == "stale_data" for code, _ in result.reasons):
                                            conn.execute(
                                                insert_sql,
                                                (
                                                    event_id,
                                                    market_name,
                                                    "ALL",
                                                    0.0,
                                                    "system",
                                                    "gap",
                                                    "stale_data",
                                                    captured_at,
                                                ),
                                            )
                                    reason = (
                                        "stale_data"
                                        if any(
                                            code == "stale_data"
                                            for _, result in rejected
                                            for code, _ in result.reasons
                                        )
                                        else None
                                    )
                                    results_by_event[event_id] = SnapshotResult(
                                        event_id=event_id,
                                        status="gap",
                                        rows_written=1 if reason == "stale_data" else 0,
                                        skip_reason=reason,
                                        error=None,
                                    )
                                else:
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
