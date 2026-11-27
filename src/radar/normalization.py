"""Normalización y resolución local de nombres de equipos y mercados.

La normalización de equipos consulta y actualiza ``team_aliases``. Los nombres
nuevos se guardan como pendientes para revisión; una coincidencia fuzzy nunca
confirma por sí sola un alias.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator

from rapidfuzz import fuzz

from src.db import get_connection
from src.logging_config import get_logger

logger = get_logger(__name__)

ALLOWED_SOURCES = frozenset(
    {
        "football_data_co_uk",
        "football_data_org",
        "the_odds_api",
        "api_football",
        "manual",
    }
)

SUFFIXES_TO_STRIP = {
    "fc",
    "cf",
    "afc",
    "cd",
    "ud",
    "sc",
    "ac",
    "ss",
    "ssc",
    "as",
    "us",
}

ABBREVIATION_EXPANSIONS = {
    "utd": "united",
    "ath": "athletic",
    "atl": "atletico",
    "dep": "deportivo",
    "bet": "betis",
    "jun": "juventus",
}

MARKET_VOCABULARY = {
    ("1x2", "home"): ("1X2", "HOME"),
    ("1x2", "draw"): ("1X2", "DRAW"),
    ("1x2", "away"): ("1X2", "AWAY"),
    ("match winner", "home"): ("1X2", "HOME"),
    ("match winner", "draw"): ("1X2", "DRAW"),
    ("match winner", "away"): ("1X2", "AWAY"),
    ("moneyline", "home"): ("1X2", "HOME"),
    ("moneyline", "draw"): ("1X2", "DRAW"),
    ("moneyline", "away"): ("1X2", "AWAY"),
    ("over/under 2.5", "over"): ("OU", "OVER_2.5"),
    ("over/under 2.5", "under"): ("OU", "UNDER_2.5"),
    ("total goals over 2.5", "over"): ("OU", "OVER_2.5"),
    ("total goals over 2.5", "under"): ("OU", "UNDER_2.5"),
    ("ou 2.5", "over"): ("OU", "OVER_2.5"),
    ("ou 2.5", "under"): ("OU", "UNDER_2.5"),
}


def normalize_text(text: str) -> str:
    """Pasa a minúsculas, elimina diacríticos y puntuación y colapsa espacios."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    without_marks = "".join(
        character
        for character in decomposed
        if not unicodedata.category(character).startswith("M")
    )
    alphanumeric = "".join(
        character if character.isalnum() or character.isspace() else " "
        for character in without_marks
    )
    return " ".join(alphanumeric.split())


_NORMALIZED_MARKET_VOCABULARY = {
    (normalize_text(market), normalize_text(selection)): canonical
    for (market, selection), canonical in MARKET_VOCABULARY.items()
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _connection_scope(conn=None) -> Iterator[object]:
    """Usa la conexión recibida o abre y administra la DB principal de apuestas."""
    if conn is not None:
        yield conn
        return

    from config import settings

    owned_connection = get_connection(settings.db_paths.bets)
    try:
        yield owned_connection
        owned_connection.commit()
    except Exception:
        owned_connection.rollback()
        raise
    finally:
        owned_connection.close()


def _candidate_name(normalized: str) -> str:
    words = normalized.split()
    if len(words) >= 2 and words[-1] in SUFFIXES_TO_STRIP:
        words.pop()

    candidate = " ".join(words)
    if candidate in {"bet", "jun"}:
        return ABBREVIATION_EXPANSIONS[candidate]

    return " ".join(ABBREVIATION_EXPANSIONS.get(word, word) for word in words)


def _canonical_matches(conn, candidate: str, competition_code: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT canonical_name FROM team_aliases "
        "WHERE competition_code = ? AND status = 'confirmed'",
        (competition_code,),
    ).fetchall()
    return sorted(
        {
            row["canonical_name"]
            for row in rows
            if normalize_text(row["canonical_name"]) == candidate
        }
    )


def _store_alias(
    conn,
    *,
    canonical_name: str,
    raw_name: str,
    competition_code: str,
    source: str,
    status: str,
    fuzzy_score: float | None,
    verified_at_utc: str | None,
) -> None:
    """Inserta o actualiza un alias; nunca reemplaza uno rechazado."""
    existing = conn.execute(
        "SELECT alias_id, status FROM team_aliases "
        "WHERE source = ? AND competition_code = ? AND raw_name = ?",
        (source, competition_code, raw_name),
    ).fetchone()
    if existing is not None:
        if existing["status"] == "rejected":
            return
        if existing["status"] == "confirmed" and status == "pending":
            return
        conn.execute(
            "UPDATE team_aliases SET canonical_name = ?, status = ?, fuzzy_score = ?, "
            "verified_at_utc = ? WHERE alias_id = ?",
            (
                canonical_name,
                status,
                fuzzy_score,
                verified_at_utc,
                existing["alias_id"],
            ),
        )
        return

    conn.execute(
        "INSERT INTO team_aliases "
        "(canonical_name, raw_name, competition_code, source, status, fuzzy_score, "
        "created_at_utc, verified_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            canonical_name,
            raw_name,
            competition_code,
            source,
            status,
            fuzzy_score,
            _utc_now_iso(),
            verified_at_utc,
        ),
    )


def get_alias_status(
    conn, raw_name: str, competition_code: str, source: str
) -> str | None:
    """Devuelve el estado del alias de entrada, no el del nombre canónico.

    Consulta por ``(raw_name, competition_code, source)`` y no tiene efectos
    laterales. Devuelve ``None`` si no hay alias o la tabla aún no existe.
    """
    try:
        row = conn.execute(
            "SELECT status FROM team_aliases "
            "WHERE raw_name = ? AND competition_code = ? AND source = ? LIMIT 1",
            (raw_name, competition_code, source),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower() and "team_aliases" in str(exc).lower():
            return None
        raise
    return row["status"] if row is not None else None


def normalize_team_name(
    raw_name: str,
    competition_code: str,
    source: str = "manual",
    *,
    conn=None,
) -> tuple[str | None, str | None]:
    """Resuelve un equipo y registra alias nuevos como pendientes.

    Tiene efectos laterales: puede insertar o actualizar filas en ``team_aliases``.
    ``source`` identifica la procedencia del nombre; si no se conoce se usa
    ``manual``. Se puede pasar una conexión existente para compartir la transacción.
    Las puntuaciones positivas se guardan solo para fuentes no manuales.
    """
    normalized = normalize_text(raw_name)
    if not normalized:
        return "", None
    if source not in ALLOWED_SOURCES:
        raise ValueError(f"source no admitido: {source}")

    with _connection_scope(conn) as db:
        exact = db.execute(
            "SELECT canonical_name FROM team_aliases "
            "WHERE source = ? AND competition_code = ? AND raw_name = ? "
            "AND status = 'confirmed'",
            (source, competition_code, raw_name),
        ).fetchone()
        if exact is not None:
            return exact["canonical_name"], "exact"

        # resolve_fixture no conoce el proveedor que originó cada nombre. Si el
        # mismo raw_name está confirmado en otra fuente, sirve como fallback exacto
        # después de intentar la equivalencia normalizada.
        known = db.execute(
            "SELECT DISTINCT canonical_name FROM team_aliases "
            "WHERE competition_code = ? AND raw_name = ? AND status = 'confirmed'",
            (competition_code, raw_name),
        ).fetchall()
        known_canonicals = sorted({row["canonical_name"] for row in known})

        candidate = _candidate_name(normalized)
        canonical_matches = _canonical_matches(db, candidate, competition_code)
        if len(canonical_matches) == 1:
            canonical = canonical_matches[0]
            _store_alias(
                db,
                canonical_name=canonical,
                raw_name=raw_name,
                competition_code=competition_code,
                source=source,
                status="confirmed",
                fuzzy_score=None,
                verified_at_utc=_utc_now_iso(),
            )
            return canonical, "exact"
        if len(canonical_matches) > 1:
            logger.warning(
                "ambiguous_normalized_alias raw_name=%s competition_code=%s "
                "canonicals=%s",
                raw_name,
                competition_code,
                canonical_matches,
            )
            return None, None

        if len(known_canonicals) == 1:
            canonical = known_canonicals[0]
            if source != "manual":
                _store_alias(
                    db,
                    canonical_name=canonical,
                    raw_name=raw_name,
                    competition_code=competition_code,
                    source=source,
                    status="confirmed",
                    fuzzy_score=None,
                    verified_at_utc=_utc_now_iso(),
                )
            return canonical, "exact"
        if len(known_canonicals) > 1:
            logger.warning(
                "ambiguous_exact_alias raw_name=%s competition_code=%s canonicals=%s",
                raw_name,
                competition_code,
                known_canonicals,
            )
            return None, None

        confirmed = db.execute(
            "SELECT DISTINCT canonical_name FROM team_aliases "
            "WHERE competition_code = ? AND status = 'confirmed'",
            (competition_code,),
        ).fetchall()
        scored = [
            (
                float(fuzz.ratio(candidate, normalize_text(row["canonical_name"]))),
                row["canonical_name"],
            )
            for row in confirmed
        ]
        matches = sorted(
            {canonical: score for score, canonical in scored if score >= 92}.items()
        )
        if len(matches) == 1:
            score, canonical = matches[0][1], matches[0][0]
            _store_alias(
                db,
                canonical_name=canonical,
                raw_name=raw_name,
                competition_code=competition_code,
                source=source,
                status="pending",
                fuzzy_score=score if source != "manual" else None,
                verified_at_utc=None,
            )
            logger.warning(
                "pending_fuzzy_team_alias raw_name=%s canonical_name=%s "
                "competition_code=%s score=%.2f source=%s",
                raw_name,
                canonical,
                competition_code,
                score,
                source,
            )
            return canonical, "fuzzy"
        if len(matches) > 1:
            logger.warning(
                "ambiguous_fuzzy_match raw_name=%s competition_code=%s candidates=%s",
                raw_name,
                competition_code,
                matches,
            )
            return None, None

        _store_alias(
            db,
            canonical_name="",
            raw_name=raw_name,
            competition_code=competition_code,
            source=source,
            status="pending",
            fuzzy_score=None,
            verified_at_utc=None,
        )
        logger.warning(
            "pending_team_alias raw_name=%s competition_code=%s source=%s",
            raw_name,
            competition_code,
            source,
        )
        return None, None


def normalize_market(raw_market: str, raw_selection: str) -> tuple[str, str] | None:
    """Convierte una pareja de mercado/selección a vocabulario canónico."""
    key = (normalize_text(raw_market), normalize_text(raw_selection))
    canonical = _NORMALIZED_MARKET_VOCABULARY.get(key)
    if canonical is None:
        logger.debug(
            "unknown_market raw_market=%r raw_selection=%r",
            raw_market,
            raw_selection,
        )
    return canonical


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def resolve_fixture(
    conn,
    raw_home: str,
    raw_away: str,
    competition_code: str,
    kickoff_utc: str,
    source: str = "manual",
) -> str | None:
    """Resuelve un fixture local si ambos equipos y el kickoff son inequívocos."""
    home, _ = normalize_team_name(
        raw_home, competition_code, source=source, conn=conn
    )
    away, _ = normalize_team_name(
        raw_away, competition_code, source=source, conn=conn
    )
    if not home or not away:
        return None

    try:
        kickoff = _parse_utc(kickoff_utc)
    except (TypeError, ValueError):
        return None
    lower = (kickoff - timedelta(minutes=10)).isoformat()
    upper = (kickoff + timedelta(minutes=10)).isoformat()
    rows = conn.execute(
        "SELECT event_id FROM events WHERE home_team = ? AND away_team = ? "
        "AND competition_code = ? AND kickoff_utc BETWEEN ? AND ?",
        (home, away, competition_code, lower, upper),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["event_id"]
    if len(rows) > 1:
        event_ids = [row["event_id"] for row in rows]
        logger.warning(
            "multiple_events_matched competition_code=%s home=%s away=%s event_ids=%s",
            competition_code,
            home,
            away,
            event_ids,
        )
    return None


def resolve_odds_event_to_local(
    conn,
    odds_event: dict,
    competition_code: str,
    source: str = "the_odds_api",
) -> str | None:
    """Mapea un evento de cuotas a un event_id local.

    ``source`` documenta la procedencia para el log; la resolución usa el mismo
    algoritmo de aliases y fixtures independientemente de ese valor.
    """
    home = odds_event.get("home_team")
    away = odds_event.get("away_team")
    kickoff = odds_event.get("commence_time")
    if (
        not isinstance(home, str)
        or not isinstance(away, str)
        or not isinstance(kickoff, str)
    ):
        return None
    logger.debug(
        "resolving_odds_event source=%s competition_code=%s",
        source,
        competition_code,
    )
    return resolve_fixture(
        conn,
        home,
        away,
        competition_code,
        kickoff,
        source="the_odds_api",
    )
