from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.data_quality import (
    REASON_CODES,
    ValidationResult,
    get_data_health,
    log_validation,
    validate_decision,
    validate_event,
    validate_manual_odds,
    validate_odds,
    validate_reference_odds,
)
from src.db import get_connection
from src.migrations import apply_migrations
from src.radar.snapshot_recorder import SnapshotPlan, SnapshotRecorder

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
EXPECTED_REASONS = {
    "negative_ev", "below_ev_threshold", "sanity_check_failed",
    "missing_reference_odds", "insufficient_data", "invalid_manual_odds",
    "no_sharp_reference", "stale_data", "kickoff_in_past",
    "invalid_competition_code", "invalid_odds_range",
    "invalid_probability_range", "overround_out_of_bounds",
    "invalid_match_status", "unknown_team_alias",
}


def _event(**overrides):
    event = {
        "event_id": "evt-1",
        "competition_code": "SP1",
        "home_team": "Home",
        "away_team": "Away",
        "kickoff_utc": (NOW + timedelta(hours=2)).isoformat(),
        "match_status": "scheduled",
    }
    event.update(overrides)
    return event


def test_reason_codes_es_enum_cerrado_y_validation_result():
    assert isinstance(REASON_CODES, frozenset)
    assert REASON_CODES == EXPECTED_REASONS
    result = ValidationResult(False, [("stale_data", "stale")])
    assert result.reasons == [("stale_data", "stale")]


def test_validate_event_acepta_evento_sin_aliases_y_no_muta():
    event = _event()
    before = deepcopy(event)
    assert validate_event(event, NOW).valid
    assert event == before


def test_validate_event_rechaza_campos_estado_competicion_y_kickoff():
    result = validate_event(
        _event(home_team="", competition_code="XX", match_status="unknown",
               kickoff_utc="not-a-date"), NOW,
    )
    reasons = {code for code, _ in result.reasons}
    assert not result.valid
    assert {"insufficient_data", "invalid_competition_code",
            "invalid_match_status", "kickoff_in_past"}.issubset(reasons)


def test_validate_event_rechaza_kickoff_pasado():
    result = validate_event(_event(kickoff_utc=(NOW - timedelta(seconds=1)).isoformat()), NOW)
    assert "kickoff_in_past" in {code for code, _ in result.reasons}


def _odds(values, captured_at=None):
    captured_at = captured_at or NOW.isoformat()
    return [{"odds": value, "captured_at_utc": captured_at} for value in values]


def test_validate_odds_acepta_overround_en_limite_y_no_muta():
    odds = _odds([2.0, 2.0, 2.0])
    before = deepcopy(odds)
    assert validate_odds(odds, NOW).valid
    assert odds == before


def test_validate_odds_rechaza_precio_overround_y_cuotas_vacias():
    assert "missing_reference_odds" in {c for c, _ in validate_odds([], NOW).reasons}
    assert "invalid_odds_range" in {
        c for c, _ in validate_odds(_odds([1.0, 2.0, 2.0]), NOW).reasons
    }
    assert "overround_out_of_bounds" in {
        c for c, _ in validate_odds(_odds([1.9, 1.9, 1.9]), NOW).reasons
    }


def test_validate_odds_marca_datos_antiguos():
    old = (NOW - timedelta(minutes=61)).isoformat()
    assert "stale_data" in {c for c, _ in validate_odds(_odds([2, 2, 2], old), NOW).reasons}


def test_validate_decision_codigos_ev_probabilidad_y_sanity():
    assert validate_decision({"prob": .55, "ev": .03, "edge_pct": 5}).valid
    cases = [
        ({"prob": .55, "ev": -.01, "edge_pct": 5}, "negative_ev"),
        ({"prob": .55, "ev": .01, "edge_pct": 5}, "below_ev_threshold"),
        ({"prob": 1, "ev": .1, "edge_pct": 5}, "invalid_probability_range"),
        ({"prob": .5, "ev": .1, "edge_pct": 101}, "sanity_check_failed"),
    ]
    for decision, expected in cases:
        assert expected in {c for c, _ in validate_decision(decision).reasons}


def test_validate_manual_odds_detecta_typos_sin_autocorregir():
    assert validate_manual_odds(2.1, 2.0).valid
    assert "invalid_manual_odds" in {c for c, _ in validate_manual_odds(3.0, 2.0).reasons}
    assert "invalid_manual_odds" in {c for c, _ in validate_manual_odds(1.0, 2.0).reasons}
    assert validate_manual_odds(2.0, None).valid


def test_validate_reference_odds_distingue_ausencia_de_estructura_incompleta():
    assert "no_sharp_reference" in {c for c, _ in validate_reference_odds(None).reasons}
    assert "missing_reference_odds" in {
        c for c, _ in validate_reference_odds({"source": "pinnacle"}).reasons
    }
    complete = {
        "event_id": "evt-1", "source": "pinnacle", "market": "1X2",
        "selection": "HOME", "odds": 2.0, "captured_at_utc": NOW.isoformat(),
    }
    assert validate_reference_odds(complete).valid


def test_log_validation_y_get_data_health_cuentan_rechazos(tmp_path):
    conn = get_connection(tmp_path / "health.db")
    try:
        apply_migrations(conn, ROOT / "migrations")
        for index in range(4):
            conn.execute(
                "INSERT INTO events (event_id, competition_code, home_team, away_team, "
                "kickoff_utc, match_status, created_at_utc, updated_at_utc) "
                "VALUES (?, 'SP1', 'Home', 'Away', ?, 'scheduled', ?, ?)",
                (f"health-{index}", (NOW + timedelta(hours=1)).isoformat(),
                 NOW.isoformat(), NOW.isoformat()),
            )
        rejected = validate_event(_event(home_team="", competition_code="XX"), NOW)
        log_validation(conn, "event", "health-0", rejected, NOW)
        conn.commit()
        logs = conn.execute(
            "SELECT entity_type, entity_id, reason_code, message FROM data_quality_logs "
            "ORDER BY reason_code"
        ).fetchall()
        assert len(logs) == 2
        assert {row["entity_type"] for row in logs} == {"event"}
        assert {row["entity_id"] for row in logs} == {"health-0"}
        health = get_data_health(conn, NOW)
        assert health["total"] == 4
        assert health["total_logs"] == 2
        assert health["rejected"] == 1
        assert health["reason_counts"] == {
            "insufficient_data": 1, "invalid_competition_code": 1,
        }
        assert health["rejection_rate"] == .25
        assert health["degraded"] is True
    finally:
        conn.close()


def test_log_validation_no_escribe_validaciones_validas(tmp_path):
    conn = get_connection(tmp_path / "valid.db")
    try:
        apply_migrations(conn, ROOT / "migrations")
        log_validation(conn, "event", "evt-1", validate_event(_event(), NOW), NOW)
        assert conn.execute("SELECT count(*) FROM data_quality_logs").fetchone()[0] == 0
    finally:
        conn.close()


def test_snapshot_recorder_ignora_evento_invalido_y_marca_odds_stale(tmp_path):
    db_path = tmp_path / "recorder_quality.db"
    conn = get_connection(db_path)
    apply_migrations(conn, ROOT / "migrations")
    kickoff = NOW + timedelta(hours=2)
    event = {
        "event_id": "quality-event", "competition_code": "SP1",
        "home_team": "Home Quality", "away_team": "Away Quality",
        "kickoff_utc": kickoff.isoformat(), "match_status": "scheduled",
        "created_at_utc": NOW.isoformat(), "updated_at_utc": NOW.isoformat(),
    }
    conn.execute(
        "INSERT INTO events (event_id, competition_code, home_team, away_team, "
        "kickoff_utc, match_status, created_at_utc, updated_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        tuple(event[key] for key in (
            "event_id", "competition_code", "home_team", "away_team",
            "kickoff_utc", "match_status", "created_at_utc", "updated_at_utc",
        )),
    )
    for team in (event["home_team"], event["away_team"]):
        conn.execute(
            "INSERT INTO team_aliases (canonical_name, raw_name, competition_code, "
            "source, status, created_at_utc, verified_at_utc) "
            "VALUES (?, ?, 'SP1', 'the_odds_api', 'confirmed', ?, ?)",
            (team, team, NOW.isoformat(), NOW.isoformat()),
        )
    conn.commit()
    conn.close()

    settings = SimpleNamespace(
        snapshot_execution_enabled=True,
        snapshot_near_window_h=6,
        snapshot_interval_near_h=1,
        snapshot_interval_far_h=2,
        active_competitions=["SP1", "E0", "I1"],
    )
    client, budget = MagicMock(), MagicMock()
    budget.get_remaining.return_value = 10
    recorder = SnapshotRecorder(db_path, client, budget, settings)
    assert recorder.plan([{**event, "competition_code": "INVALID"}], NOW) == []
    conn = get_connection(db_path)
    assert conn.execute(
        "SELECT reason_code FROM data_quality_logs WHERE entity_type='event'"
    ).fetchone()[0] == "invalid_competition_code"
    conn.close()

    old_capture = (NOW - timedelta(minutes=61)).isoformat()
    client.get_odds.return_value = [{
        "id": "provider-event", "home_team": event["home_team"],
        "away_team": event["away_team"], "commence_time": kickoff.isoformat(),
        "bookmakers": [{"key": "pinnacle", "last_update": old_capture,
            "markets": [{"key": "h2h", "outcomes": [
                {"name": event["home_team"], "price": 2.1},
                {"name": "Draw", "price": 3.2},
                {"name": event["away_team"], "price": 3.4},
            ]}],
        }],
    }]
    plan = SnapshotPlan(
        event_id=event["event_id"], competition_code="SP1",
        kickoff_utc=kickoff, next_snapshot_at_utc=NOW + timedelta(hours=1), priority=1,
    )
    result = recorder.execute([plan], NOW)[0]
    assert result.status == "gap"
    assert result.skip_reason == "stale_data"
    conn = get_connection(db_path)
    snapshot = conn.execute(
        "SELECT status, skip_reason FROM snapshots WHERE event_id=?", (event["event_id"],)
    ).fetchone()
    assert tuple(snapshot) == ("gap", "stale_data")
    assert conn.execute(
        "SELECT reason_code FROM data_quality_logs WHERE entity_type='odds'"
    ).fetchone()[0] == "stale_data"
    conn.close()
