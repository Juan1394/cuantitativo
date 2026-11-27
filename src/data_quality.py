"""Validación no correctiva de datos de eventos, cuotas y decisiones.

Los códigos de rechazo son cerrados para permitir métricas consistentes y
acciones explícitas aguas arriba:

- ``negative_ev``: la expectativa matemática de la apuesta es negativa.
- ``below_ev_threshold``: EV positivo, pero inferior al umbral configurado.
- ``sanity_check_failed``: EV o edge incoherente con límites básicos.
- ``missing_reference_odds``: faltan cuotas de referencia necesarias.
- ``insufficient_data``: faltan campos mínimos para validar un registro.
- ``invalid_manual_odds``: cuota manual inválida o desviada de la referencia.
- ``no_sharp_reference``: no existe una referencia sharp para comparar.
- ``stale_data``: la captura de cuotas excede la antigüedad admitida.
- ``kickoff_in_past``: el kickoff ya ocurrió o no se pudo interpretar.
- ``invalid_competition_code``: competición fuera del conjunto admitido.
- ``invalid_odds_range``: cuota no finita o menor o igual a uno.
- ``invalid_probability_range``: probabilidad fuera del intervalo abierto (0, 1).
- ``overround_out_of_bounds``: suma implícita fuera del intervalo 1.0–1.5.
- ``invalid_match_status``: estado del evento no reconocido.
- ``unknown_team_alias``: reservado para nombres de equipo sin alias conocido;
  esta capa no corrige ni confirma alias automáticamente.

Las funciones solo validan: no normalizan ni modifican los objetos recibidos.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

REASON_CODES = frozenset(
    {
        "negative_ev",
        "below_ev_threshold",
        "sanity_check_failed",
        "missing_reference_odds",
        "insufficient_data",
        "invalid_manual_odds",
        "no_sharp_reference",
        "stale_data",
        "kickoff_in_past",
        "invalid_competition_code",
        "invalid_odds_range",
        "invalid_probability_range",
        "overround_out_of_bounds",
        "invalid_match_status",
        "unknown_team_alias",
    }
)

_COMPETITIONS = frozenset({"SP1", "E0", "I1"})
_MATCH_STATUSES = frozenset(
    {"scheduled", "finished", "postponed", "cancelled", "suspended", "rescheduled"}
)
_ENTITY_TYPES = frozenset(
    {"event", "odds", "decision", "manual_odds", "reference_odds"}
)
_REQUIRED_REFERENCE_FIELDS = frozenset(
    {"event_id", "source", "market", "selection", "odds", "captured_at_utc"}
)


@dataclass(frozen=True)
class ValidationResult:
    """Resultado inmutable de una validación y sus rechazos explicados."""

    valid: bool
    reasons: list[tuple[str, str]]

    def __post_init__(self) -> None:
        if self.valid and self.reasons:
            raise ValueError("un resultado válido no puede contener rechazos")
        for reason_code, _message in self.reasons:
            if reason_code not in REASON_CODES:
                raise ValueError(f"reason_code no permitido: {reason_code}")


def _result(reasons: list[tuple[str, str]]) -> ValidationResult:
    return ValidationResult(valid=not reasons, reasons=reasons)


def _as_utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def validate_event(event: dict, now_utc: datetime) -> ValidationResult:
    """Valida estructura y temporalidad del evento, sin consultar team_aliases.

    La existencia o confirmación de aliases se comprueba por separado con
    ``get_alias_status``; esta función no autocorrige nombres ni estados.
    """
    reasons: list[tuple[str, str]] = []
    if not isinstance(event, dict):
        return _result([("insufficient_data", "event debe ser un diccionario")])

    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip():
        reasons.append(("insufficient_data", "event_id debe ser un string no vacío"))

    for key in ("home_team", "away_team"):
        value = event.get(key)
        if not isinstance(value, str) or not value.strip():
            reasons.append(("insufficient_data", f"{key} debe ser un string no vacío"))

    competition_code = event.get("competition_code")
    if not isinstance(competition_code, str) or competition_code not in _COMPETITIONS:
        reasons.append(
            ("invalid_competition_code", f"competición no admitida: {competition_code!r}")
        )

    match_status = event.get("match_status")
    if not isinstance(match_status, str) or match_status not in _MATCH_STATUSES:
        reasons.append(("invalid_match_status", f"estado no admitido: {match_status!r}"))

    try:
        kickoff = _as_utc(event.get("kickoff_utc"))
        now = _as_utc(now_utc)
        if kickoff <= now:
            reasons.append(("kickoff_in_past", "kickoff debe ser posterior a now_utc"))
    except (AttributeError, TypeError, ValueError, OverflowError):
        reasons.append(("kickoff_in_past", "kickoff_utc falta o no es una fecha válida"))

    return _result(reasons)


def validate_odds(
    odds_list: list[dict],
    now_utc: datetime,
    max_age_minutes: int = 60,
) -> ValidationResult:
    """Valida cuotas, antigüedad de captura y overround de un único mercado."""
    if not odds_list:
        return _result([("missing_reference_odds", "no hay cuotas de referencia")])

    now = _as_utc(now_utc)
    reasons: list[tuple[str, str]] = []
    numeric_odds: list[float] = []
    invalid_price = False

    for index, item in enumerate(odds_list):
        if not isinstance(item, dict):
            reasons.append(("invalid_odds_range", f"cuota {index} no es un diccionario"))
            invalid_price = True
            continue

        value = item.get("odds")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            reasons.append(("invalid_odds_range", f"cuota {index} debe ser numérica y > 1"))
            invalid_price = True
        elif not math.isfinite(float(value)) or float(value) <= 1.0:
            reasons.append(("invalid_odds_range", f"cuota {index} debe ser finita y > 1"))
            invalid_price = True
        else:
            numeric_odds.append(float(value))

        try:
            captured_at = _as_utc(item.get("captured_at_utc"))
            age_minutes = (now - captured_at).total_seconds() / 60
            if age_minutes > max_age_minutes:
                reasons.append(
                    (
                        "stale_data",
                        f"captured_at_utc de la cuota {index} excede {max_age_minutes} minutos",
                    )
                )
        except (AttributeError, TypeError, ValueError, OverflowError):
            reasons.append(
                ("stale_data", f"captured_at_utc de la cuota {index} falta o es inválido")
            )

    if not invalid_price and len(numeric_odds) == len(odds_list):
        overround = sum(1.0 / value for value in numeric_odds)
        if not 1.0 <= overround <= 1.5:
            reasons.append(
                (
                    "overround_out_of_bounds",
                    f"overround {overround:.4f} fuera del intervalo [1.0, 1.5]",
                )
            )

    return _result(reasons)


def _configured_ev_threshold(decision: dict) -> float:
    configured = decision.get("ev_threshold")
    if isinstance(configured, (int, float)) and not isinstance(configured, bool):
        return float(configured)
    try:
        from config import settings

        return float(settings.ev_threshold)
    except (ImportError, AttributeError, TypeError, ValueError):
        return 0.02


def validate_decision(decision: dict) -> ValidationResult:
    """Valida probabilidades, EV y edge sin cambiar la decisión recibida."""
    if not isinstance(decision, dict):
        return _result([("insufficient_data", "decision debe ser un diccionario")])

    reasons: list[tuple[str, str]] = []
    probability = decision.get("prob")
    if (
        isinstance(probability, bool)
        or not isinstance(probability, (int, float))
        or not math.isfinite(float(probability))
        or not 0.0 < float(probability) < 1.0
    ):
        reasons.append(
            ("invalid_probability_range", "prob debe estar estrictamente entre 0 y 1")
        )

    ev = decision.get("ev")
    if isinstance(ev, bool) or not isinstance(ev, (int, float)) or not math.isfinite(float(ev)):
        reasons.append(("insufficient_data", "ev debe ser un número finito"))
        return _result(reasons)
    ev = float(ev)
    if ev <= -1.0:
        reasons.append(("sanity_check_failed", "ev debe ser mayor que -1"))

    edge_pct = decision.get("edge_pct")
    if (
        isinstance(edge_pct, bool)
        or not isinstance(edge_pct, (int, float))
        or not math.isfinite(float(edge_pct))
    ):
        reasons.append(("insufficient_data", "edge_pct debe ser un número finito"))
        return _result(reasons)
    edge_pct = float(edge_pct)
    if not -100.0 <= edge_pct <= 100.0:
        reasons.append(("sanity_check_failed", "edge_pct fuera del intervalo [-100, 100]"))

    if reasons:
        return _result(reasons)
    if ev < 0.0:
        return _result([("negative_ev", "EV negativo")])
    if ev < _configured_ev_threshold(decision):
        return _result([("below_ev_threshold", "EV inferior al umbral configurado")])
    if edge_pct <= 0.0:
        return _result([("sanity_check_failed", "edge_pct debe ser positivo para aceptar")])
    return _result([])


def validate_manual_odds(
    manual_odds: float,
    reference_odds: float | None,
    deviation_pct: float = 0.15,
) -> ValidationResult:
    """Valida una cuota manual y alerta de desviaciones que sugieren un typo."""
    if (
        isinstance(manual_odds, bool)
        or not isinstance(manual_odds, (int, float))
        or not math.isfinite(float(manual_odds))
        or float(manual_odds) <= 1.0
    ):
        return _result([("invalid_manual_odds", "manual_odds debe ser finita y > 1")])
    if reference_odds is None:
        return _result([])
    if (
        isinstance(reference_odds, bool)
        or not isinstance(reference_odds, (int, float))
        or not math.isfinite(float(reference_odds))
        or float(reference_odds) <= 1.0
    ):
        return _result([("invalid_manual_odds", "reference_odds no es válida para comparar")])

    deviation = abs(float(manual_odds) - float(reference_odds)) / float(reference_odds)
    if deviation > deviation_pct:
        return _result(
            [
                (
                    "invalid_manual_odds",
                    f"possible_typo: desviación {deviation:.1%} supera {deviation_pct:.1%}",
                )
            ]
        )
    return _result([])


def validate_reference_odds(reference: dict | None) -> ValidationResult:
    """Comprueba que exista una referencia sharp estructuralmente completa."""
    if reference is None:
        return _result([("no_sharp_reference", "no existe referencia sharp")])
    if not isinstance(reference, dict) or not _REQUIRED_REFERENCE_FIELDS.issubset(reference):
        return _result(
            [("missing_reference_odds", "faltan campos requeridos en reference_odds")]
        )
    return _result([])


def log_validation(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: str | None,
    result: ValidationResult,
    now_utc: datetime,
) -> None:
    """Persiste un log por cada razón de rechazo; no registra resultados válidos."""
    if entity_type not in _ENTITY_TYPES:
        raise ValueError(f"entity_type no admitido: {entity_type}")
    if result.valid:
        return

    created_at = _as_utc(now_utc).isoformat()
    for reason_code, message in result.reasons:
        if reason_code not in REASON_CODES:
            raise ValueError(f"reason_code no permitido: {reason_code}")
        conn.execute(
            "INSERT INTO data_quality_logs "
            "(entity_type, entity_id, reason_code, message, created_at_utc) "
            "VALUES (?, ?, ?, ?, ?)",
            (entity_type, entity_id, reason_code, str(message), created_at),
        )


def get_data_health(
    conn: sqlite3.Connection,
    now_utc: datetime | None = None,
    window_hours: int = 24,
) -> dict[str, Any]:
    """Devuelve conteos recientes y una tasa de rechazo por entidades evaluadas.

    El denominador se obtiene de las tablas de origen correspondientes a los
    tipos de entidad registrados; cada entidad rechazada cuenta una vez aunque
    haya producido varios reason codes.
    """
    now = _as_utc(now_utc or datetime.now(timezone.utc))
    since = (now.timestamp() - window_hours * 3600)
    since_utc = datetime.fromtimestamp(since, tz=timezone.utc).isoformat()
    rows = conn.execute(
        "SELECT reason_code, COUNT(*) AS count FROM data_quality_logs "
        "WHERE created_at_utc >= ? AND created_at_utc <= ? GROUP BY reason_code",
        (since_utc, now.isoformat()),
    ).fetchall()
    reason_counts = {row["reason_code"]: int(row["count"]) for row in rows}
    log_count_row = conn.execute(
        "SELECT COUNT(*) FROM data_quality_logs "
        "WHERE created_at_utc >= ? AND created_at_utc <= ?",
        (since_utc, now.isoformat()),
    ).fetchone()
    log_count = int(log_count_row[0]) if log_count_row else 0
    entity_rows = conn.execute(
        "SELECT COUNT(DISTINCT entity_type || ':' || COALESCE(entity_id, rowid)) "
        "FROM data_quality_logs WHERE created_at_utc >= ? AND created_at_utc <= ?",
        (since_utc, now.isoformat()),
    ).fetchone()
    rejected = int(entity_rows[0]) if entity_rows else 0
    logged_types = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT entity_type FROM data_quality_logs "
            "WHERE created_at_utc >= ? AND created_at_utc <= ?",
            (since_utc, now.isoformat()),
        ).fetchall()
    }
    source_tables = {
        "event": ("events",),
        "odds": ("snapshots",),
        "decision": ("decision_logs",),
        "manual_odds": ("manual_odds",),
        "reference_odds": ("reference_odds",),
    }
    total = 0
    for entity_type in logged_types:
        for table_name in source_tables.get(entity_type, ()):
            count_row = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
            total += int(count_row[0]) if count_row else 0
    total = max(total, rejected)
    rejection_rate = (rejected / total) if total else 0.0
    return {
        "total": total,
        "total_logs": log_count,
        "rejected": rejected,
        "rejection_rate": rejection_rate,
        "reason_counts": reason_counts,
        "degraded": rejection_rate > 0.20,
        "window_hours": window_hours,
    }
