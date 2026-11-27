import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.db import get_connection
from src.migrations import apply_migrations, get_current_version, list_migrations
from src.radar.normalization import (
    get_alias_status,
    normalize_market,
    normalize_team_name,
    normalize_text,
    resolve_fixture,
    resolve_odds_event_to_local,
)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
CREATED_AT = "2026-01-01T00:00:00+00:00"


@pytest.fixture
def normalization_db(tmp_path):
    conn = get_connection(tmp_path / "normalization.db")
    apply_migrations(conn, MIGRATIONS_DIR)
    yield conn
    conn.close()


@pytest.fixture
def fixture_events(normalization_db):
    rows = (
        ("fixture-a", "Arsenal", "Liverpool", "2030-01-01T15:00:00+00:00"),
        ("fixture-b", "Chelsea", "Manchester City", "2030-01-02T16:00:00+00:00"),
        ("fixture-c", "Liverpool", "Chelsea", "2030-01-03T17:00:00+00:00"),
    )
    for event_id, home, away, kickoff in rows:
        _insert_event(normalization_db, event_id, home, away, "E0", kickoff)
    normalization_db.commit()
    return rows


def _insert_event(conn, event_id, home, away, competition, kickoff):
    conn.execute(
        "INSERT INTO events "
        "(event_id, competition_code, home_team, away_team, kickoff_utc, "
        "match_status, created_at_utc, updated_at_utc) "
        "VALUES (?, ?, ?, ?, ?, 'scheduled', ?, ?)",
        (event_id, competition, home, away, kickoff, CREATED_AT, CREATED_AT),
    )


def test_normalize_text_elimina_acentos():
    assert normalize_text("Atlético Madrid") == "atletico madrid"


def test_normalize_text_colapsa_espacios():
    assert normalize_text("Real   Betis  ") == "real betis"


def test_normalize_team_name_exact_match(normalization_db):
    assert normalize_team_name(
        "Man Utd", "E0", source="football_data_co_uk", conn=normalization_db
    ) == ("Manchester United", "exact")


def test_normalize_team_name_suffix_strip(normalization_db):
    assert normalize_team_name(
        "Arsenal FC", "E0", conn=normalization_db
    ) == ("Arsenal", "exact")


def test_normalize_team_name_abbreviation(normalization_db):
    result = normalize_team_name("Newcastle Utd", "E0", conn=normalization_db)
    assert result in {
        (None, None),
        ("Newcastle United", "exact"),
        ("Newcastle United", "fuzzy"),
    }
    row = normalization_db.execute(
        "SELECT status FROM team_aliases WHERE source = 'manual' "
        "AND competition_code = 'E0' AND raw_name = 'Newcastle Utd'"
    ).fetchone()
    if result == (None, None):
        assert row["status"] == "pending"


def test_normalize_team_name_unknown_inserts_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("API_FOOTBALL_KEY", "test-football")
    monkeypatch.setenv("FOOTBALL_DATA_ORG_KEY", "test-data-org")
    monkeypatch.setenv("SHARP_ODDS_API_KEY", "test-sharp")
    import config

    db_path = tmp_path / "unknown_team.db"
    setup_conn = get_connection(db_path)
    apply_migrations(setup_conn, MIGRATIONS_DIR)
    setup_conn.close()
    monkeypatch.setattr(
        config,
        "settings",
        SimpleNamespace(db_paths=SimpleNamespace(bets=db_path)),
    )

    assert normalize_team_name(
        "Random Team", "E0", source="the_odds_api"
    ) == (None, None)
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT canonical_name, status FROM team_aliases "
            "WHERE source = 'the_odds_api' AND competition_code = 'E0' "
            "AND raw_name = 'Random Team'"
        ).fetchone()
        assert tuple(row) == ("", "pending")
    finally:
        conn.close()

    with pytest.raises(ValueError, match="source no admitido"):
        normalize_team_name("Random Team", "E0", source="unlisted")


def test_normalize_team_name_ambiguous_returns_none(normalization_db):
    for canonical in ("Liverpool United", "Liverpool Unite"):
        normalization_db.execute(
            "INSERT INTO team_aliases "
            "(canonical_name, raw_name, competition_code, source, status, "
            "created_at_utc, verified_at_utc) "
            "VALUES (?, ?, 'E0', 'manual', 'confirmed', ?, ?)",
            (canonical, f"alias-{canonical}", CREATED_AT, CREATED_AT),
        )
    normalization_db.commit()

    assert normalize_team_name(
        "Liverpool Uniter", "E0", conn=normalization_db
    ) == (None, None)


@pytest.mark.parametrize(
    ("market", "selection", "expected"),
    [
        ("1X2", "Home", ("1X2", "HOME")),
        ("Match Winner", "Draw", ("1X2", "DRAW")),
        ("Moneyline", "Away", ("1X2", "AWAY")),
        ("Over/Under 2.5", "Over", ("OU", "OVER_2.5")),
        ("Weird Market", "Foo", None),
    ],
)
def test_normalize_market(market, selection, expected):
    assert normalize_market(market, selection) == expected


def test_resolve_fixture_unique_match(normalization_db, fixture_events):
    kickoff = "2026-01-01T15:00:00+00:00"
    _insert_event(
        normalization_db,
        "event-manchester-arsenal",
        "Manchester United",
        "Arsenal",
        "E0",
        kickoff,
    )
    normalization_db.commit()

    assert resolve_fixture(
        normalization_db,
        "Man Utd",
        "Arsenal",
        "E0",
        kickoff,
    ) == "event-manchester-arsenal"


def test_resolve_fixture_kickoff_tolerance(normalization_db, fixture_events):
    event_time = "2026-01-01T15:05:00+00:00"
    _insert_event(
        normalization_db,
        "event-tolerance",
        "Manchester United",
        "Arsenal",
        "E0",
        event_time,
    )
    normalization_db.commit()

    within_tolerance = resolve_fixture(
        normalization_db,
        "Man Utd",
        "Arsenal",
        "E0",
        "2026-01-01T15:00:00+00:00",
    )
    outside_tolerance = resolve_fixture(
        normalization_db,
        "Man Utd",
        "Arsenal",
        "E0",
        "2026-01-01T14:50:00+00:00",
    )
    assert within_tolerance == "event-tolerance"
    assert outside_tolerance is None


def test_resolve_fixture_multiple_matches_returns_none(
    normalization_db, fixture_events
):
    kickoff = "2026-01-01T15:00:00+00:00"
    for event_id in ("event-duplicate-1", "event-duplicate-2"):
        _insert_event(
            normalization_db,
            event_id,
            "Manchester United",
            "Arsenal",
            "E0",
            kickoff,
        )
    normalization_db.commit()

    assert resolve_fixture(
        normalization_db,
        "Man Utd",
        "Arsenal",
        "E0",
        kickoff,
    ) is None


def test_resolve_fixture_no_match_returns_none(normalization_db, fixture_events):
    assert resolve_fixture(
        normalization_db,
        "Man Utd",
        "Arsenal",
        "E0",
        "2026-01-01T15:00:00+00:00",
    ) is None


def test_resolve_odds_event_to_local(normalization_db, fixture_events):
    event_time = "2026-01-01T15:00:00+00:00"
    _insert_event(
        normalization_db,
        "event-odds",
        "Manchester United",
        "Arsenal",
        "E0",
        event_time,
    )
    normalization_db.commit()

    odds_event = {
        "home_team": "Manchester United",
        "away_team": "Arsenal",
        "commence_time": event_time,
    }
    assert resolve_odds_event_to_local(
        normalization_db, odds_event, "E0"
    ) == "event-odds"


def test_resolve_odds_event_to_local_unknown_team(normalization_db, fixture_events):
    odds_event = {
        "home_team": "Unknown FC",
        "away_team": "Arsenal",
        "commence_time": "2026-01-01T15:00:00+00:00",
    }
    assert resolve_odds_event_to_local(
        normalization_db, odds_event, "E0"
    ) is None


def test_normalize_team_name_persists_fuzzy_match(normalization_db):
    result = normalize_team_name(
        "Manchester Untd", "E0", source="the_odds_api", conn=normalization_db
    )
    assert result == ("Manchester United", "fuzzy")
    row = normalization_db.execute(
        "SELECT canonical_name, status, fuzzy_score FROM team_aliases "
        "WHERE source = 'the_odds_api' AND competition_code = 'E0' "
        "AND raw_name = 'Manchester Untd'"
    ).fetchone()
    assert row["canonical_name"] == "Manchester United"
    assert row["status"] == "pending"
    assert row["fuzzy_score"] >= 92


def test_migration_0008_seed_minimo(normalization_db):
    expected_final = max(v for v, _ in list_migrations(MIGRATIONS_DIR))
    assert get_current_version(normalization_db) == expected_final
    assert normalization_db.execute(
        "SELECT COUNT(*) FROM team_aliases "
        "WHERE competition_code = 'E0' AND status = 'confirmed'"
    ).fetchone()[0] == 15


def test_normalize_exact_post_strip_no_fuzzy_score(normalization_db):
    result = normalize_team_name(
        "Arsenal FC", "E0", conn=normalization_db
    )
    assert result == ("Arsenal", "exact")
    row = normalization_db.execute(
        "SELECT status, fuzzy_score FROM team_aliases "
        "WHERE source = 'manual' AND competition_code = 'E0' "
        "AND raw_name = 'Arsenal FC'"
    ).fetchone()
    assert row["status"] == "confirmed"
    assert row["fuzzy_score"] is None


def test_get_alias_status_confirmed(normalization_db):
    assert get_alias_status(
        normalization_db, "Man Utd", "E0", "football_data_co_uk"
    ) == "confirmed"


def test_get_alias_status_pending(normalization_db):
    normalization_db.execute(
        "INSERT INTO team_aliases "
        "(canonical_name, raw_name, competition_code, source, status, "
        "fuzzy_score, created_at_utc) "
        "VALUES ('Manchester United', 'Pending United', 'E0', 'the_odds_api', "
        "'pending', 94, ?)",
        (CREATED_AT,),
    )
    assert get_alias_status(
        normalization_db, "Pending United", "E0", "the_odds_api"
    ) == "pending"


def test_get_alias_status_nonexistent(normalization_db):
    assert get_alias_status(
        normalization_db, "Not Inserted", "E0", "the_odds_api"
    ) is None
    uninitialized_conn = sqlite3.connect(":memory:")
    try:
        assert get_alias_status(
            uninitialized_conn, "Not Inserted", "E0", "the_odds_api"
        ) is None
    finally:
        uninitialized_conn.close()


def test_get_alias_status_wrong_competition(normalization_db):
    assert get_alias_status(
        normalization_db, "Man Utd", "SP1", "football_data_co_uk"
    ) is None


def test_get_alias_status_wrong_source(normalization_db):
    assert get_alias_status(
        normalization_db, "Manchester United", "E0", "football_data_org"
    ) is None


def test_get_alias_status_prioriza_el_alias_no_el_canonical(normalization_db):
    result = normalize_team_name(
        "Manchester Untd", "E0", source="the_odds_api", conn=normalization_db
    )
    assert result == ("Manchester United", "fuzzy")
    assert get_alias_status(
        normalization_db, "Manchester Untd", "E0", "the_odds_api"
    ) == "pending"


def test_resolve_odds_event_to_local_default_source_the_odds_api(
    normalization_db, fixture_events
):
    event_time = "2026-01-01T15:00:00+00:00"
    _insert_event(
        normalization_db,
        "event-odds-default-source",
        "Manchester United",
        "Arsenal",
        "E0",
        event_time,
    )
    normalization_db.commit()
    odds_event = {
        "home_team": "Manchester United",
        "away_team": "Arsenal",
        "commence_time": event_time,
    }

    assert resolve_odds_event_to_local(
        normalization_db, odds_event, "E0"
    ) == "event-odds-default-source"


def test_resolve_odds_event_to_local_source_explicita(
    normalization_db, fixture_events
):
    event_time = "2026-01-01T15:00:00+00:00"
    _insert_event(
        normalization_db,
        "event-odds-explicit-source",
        "Manchester United",
        "Arsenal",
        "E0",
        event_time,
    )
    normalization_db.commit()
    odds_event = {
        "home_team": "Manchester United",
        "away_team": "Arsenal",
        "commence_time": event_time,
    }

    assert resolve_odds_event_to_local(
        normalization_db, odds_event, "E0", source="api_football"
    ) == "event-odds-explicit-source"
