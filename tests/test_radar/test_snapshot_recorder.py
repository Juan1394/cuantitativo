"""Pruebas de planificación y persistencia del snapshot recorder con datos controlados."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.db import get_connection
from src.migrations import apply_migrations
from src.radar.normalization import normalize_team_name
from src.radar.rate_limit import RateLimitBudgetManager
from src.radar.snapshot_recorder import SnapshotPlan, SnapshotRecorder

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)


def _event(event_id: str, competition_code: str, kickoff: datetime, status="scheduled"):
    return {
        "event_id": event_id,
        "competition_code": competition_code,
        "home_team": f"Home {event_id}",
        "away_team": f"Away {event_id}",
        "kickoff_utc": kickoff.isoformat(),
        "match_status": status,
        "created_at_utc": "2029-12-01T00:00:00+00:00",
        "updated_at_utc": "2029-12-01T00:00:00+00:00",
    }


def _insert_event(conn, event):
    conn.execute(
        "INSERT INTO events (event_id, competition_code, home_team, away_team, kickoff_utc, "
        "match_status, created_at_utc, updated_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event["event_id"],
            event["competition_code"],
            event["home_team"],
            event["away_team"],
            event["kickoff_utc"],
            event["match_status"],
            event["created_at_utc"],
            event["updated_at_utc"],
        ),
    )


def _odds_event(event, index: int, sharp=True):
    bookmakers = []
    outcomes = [
        {"name": event["home_team"], "price": 2.1},
        {"name": "Draw", "price": 3.2},
        {"name": event["away_team"], "price": 3.4},
    ]
    if sharp:
        for key in ("pinnacle", "betfair_ex_eu"):
            bookmakers.append(
                {
                    "key": key,
                    "markets": [{"key": "h2h", "outcomes": outcomes}],
                }
            )
    else:
        bookmakers.append(
            {
                "key": "bookmaker_other",
                "markets": [{"key": "h2h", "outcomes": outcomes}],
            }
        )
    kickoff = datetime.fromisoformat(event["kickoff_utc"].replace("Z", "+00:00"))
    return {
        "id": f"{index:032x}",
        "home_team": event["home_team"],
        "away_team": event["away_team"],
        "commence_time": kickoff.isoformat().replace("+00:00", "Z"),
        "bookmakers": bookmakers,
    }


@pytest.fixture
def recorder_env(tmp_path):
    db_path = tmp_path / "snapshots.db"
    conn = get_connection(db_path)
    apply_migrations(conn, ROOT / "migrations")
    events = [
        _event("sp1-1h", "SP1", NOW + timedelta(hours=1)),
        _event("sp1-2h", "SP1", NOW + timedelta(hours=2)),
        _event("sp1-8h", "SP1", NOW + timedelta(hours=8)),
        _event("e0-3h", "E0", NOW + timedelta(hours=3)),
        _event("e0-12h", "E0", NOW + timedelta(hours=12)),
    ]
    for event in events:
        _insert_event(conn, event)
        for team_name in (event["home_team"], event["away_team"]):
            conn.execute(
                "INSERT INTO team_aliases "
                "(canonical_name, raw_name, competition_code, source, status, "
                "created_at_utc, verified_at_utc) "
                "VALUES (?, ?, ?, 'the_odds_api', 'confirmed', ?, ?)",
                (
                    team_name,
                    team_name,
                    event["competition_code"],
                    event["created_at_utc"],
                    event["created_at_utc"],
                ),
            )
    conn.commit()
    conn.close()

    settings = SimpleNamespace(
        db_paths=SimpleNamespace(bets=db_path),
        active_competitions=["SP1", "E0", "I1"],
        snapshot_interval_near_h=1,
        snapshot_interval_far_h=2,
        snapshot_near_window_h=6,
        snapshot_min_valid_per_event=3,
        snapshot_execution_enabled=True,
    )
    budget = RateLimitBudgetManager(settings)
    odds_client = MagicMock()
    recorder = SnapshotRecorder(db_path, odds_client, budget, settings)
    return recorder, budget, odds_client, settings, events, db_path


def _plan_for(recorder, event, now=NOW):
    plans = recorder.plan([event], now)
    assert len(plans) == 1
    return plans[0]


def _valid_rows(conn, event_id=None):
    query = "SELECT * FROM snapshots WHERE status = 'valid'"
    params = ()
    if event_id is not None:
        query += " AND event_id = ?"
        params = (event_id,)
    return conn.execute(query, params).fetchall()


def test_plan_filtra_status(recorder_env):
    recorder, _, _, _, events, _ = recorder_env
    candidates = [
        {**events[0], "match_status": "cancelled"},
        {**events[1], "match_status": "finished"},
        events[2],
    ]

    plans = recorder.plan(candidates, NOW)

    assert [plan.event_id for plan in plans] == ["sp1-8h"]


def test_plan_filtra_kickoff_pasado(recorder_env):
    recorder, _, _, _, events, _ = recorder_env
    past = {**events[0], "kickoff_utc": (NOW - timedelta(seconds=1)).isoformat()}

    assert recorder.plan([past], NOW) == []


def test_plan_ordena_por_prioridad(recorder_env):
    recorder, _, _, _, events, _ = recorder_env

    plans = recorder.plan(events, NOW)

    assert [plan.event_id for plan in plans] == [
        "sp1-1h",
        "sp1-2h",
        "e0-3h",
        "sp1-8h",
        "e0-12h",
    ]


def test_plan_descarta_evento_con_snapshot_reciente(recorder_env):
    recorder, _, _, settings, events, db_path = recorder_env
    settings.snapshot_interval_near_h = 0.5
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO snapshots "
        "(event_id, market, selection, odds, source, status, captured_at_utc) "
        "VALUES (?, '1X2', 'HOME', 2.0, 'pinnacle', 'valid', ?)",
        (events[0]["event_id"], (NOW - timedelta(minutes=10)).isoformat()),
    )
    conn.commit()
    conn.close()

    assert recorder.plan([events[0]], NOW) == []


def test_plan_ventana_cerca_vs_lejos(recorder_env):
    recorder, _, _, _, events, _ = recorder_env
    far_event = {
        **events[0],
        "event_id": "far-event",
        "kickoff_utc": (NOW + timedelta(hours=10)).isoformat(),
    }

    plans = recorder.plan([events[0], far_event], NOW)
    plans_by_id = {plan.event_id: plan for plan in plans}

    assert plans_by_id[events[0]["event_id"]].next_snapshot_at_utc == NOW + timedelta(hours=1)
    assert plans_by_id["far-event"].next_snapshot_at_utc == NOW + timedelta(hours=2)


def test_execute_dry_run_no_llama_api(recorder_env):
    recorder, _, odds_client, _, events, _ = recorder_env
    plans = recorder.plan(events, NOW)

    results = recorder.execute(plans, NOW, dry_run=True)

    assert odds_client.get_odds.call_count == 0
    assert all(result.status == "skipped" for result in results)
    assert all(result.skip_reason == "dry_run_disabled" for result in results)


def test_execute_bloquea_si_flag_false(recorder_env):
    recorder, _, odds_client, settings, events, _ = recorder_env
    settings.snapshot_execution_enabled = False
    plans = recorder.plan(events, NOW)

    with pytest.raises(RuntimeError, match="snapshot_execution_enabled is False"):
        recorder.execute(plans, NOW)

    assert odds_client.get_odds.call_count == 0


def test_execute_agrupa_por_liga(recorder_env):
    recorder, _, odds_client, _, events, _ = recorder_env
    plans = recorder.plan(events, NOW)
    odds_client.get_odds.side_effect = lambda sport_key, **kwargs: [
        _odds_event(event, index)
        for index, event in enumerate(events, start=1)
        if (sport_key == "soccer_spain_la_liga" and event["competition_code"] == "SP1")
        or (sport_key == "soccer_epl" and event["competition_code"] == "E0")
    ]

    recorder.execute(plans, NOW)

    assert odds_client.get_odds.call_count == 2
    assert {call.args[0] for call in odds_client.get_odds.call_args_list} == {
        "soccer_spain_la_liga",
        "soccer_epl",
    }


def test_execute_budget_exhausted(recorder_env):
    recorder, _, odds_client, _, events, db_path = recorder_env
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO rate_limit_budget VALUES (?, ?, ?, ?, ?)",
        ("the_odds_api", "month", 500, 500, "2999-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()
    plans = recorder.plan(events, NOW)

    results = recorder.execute(plans, NOW)

    assert odds_client.get_odds.call_count == 0
    assert all(result.status == "skipped" for result in results)
    assert all(result.skip_reason == "budget_exhausted" for result in results)


def test_execute_api_error(recorder_env):
    recorder, _, odds_client, _, events, _ = recorder_env
    plans = recorder.plan(events[:3], NOW)
    odds_client.get_odds.return_value = None

    results = recorder.execute(plans, NOW)

    assert all(result.status == "gap" for result in results)
    assert all(result.skip_reason == "api_error" for result in results)


def test_execute_evento_no_en_respuesta(recorder_env):
    recorder, _, odds_client, _, events, _ = recorder_env
    plans = recorder.plan(events[:3], NOW)
    odds_client.get_odds.return_value = [
        _odds_event(events[0], 1),
        _odds_event(events[1], 2),
    ]

    results = recorder.execute(plans, NOW)
    results_by_id = {result.event_id: result for result in results}

    assert results_by_id["sp1-8h"].status == "gap"
    assert results_by_id["sp1-8h"].skip_reason == "league_not_in_response"


def test_execute_evento_sin_sharp(recorder_env):
    recorder, _, odds_client, _, events, _ = recorder_env
    plans = recorder.plan(events[:1], NOW)
    odds_client.get_odds.return_value = [_odds_event(events[0], 1, sharp=False)]

    result = recorder.execute(plans, NOW)[0]

    assert result.status == "skipped"
    assert result.skip_reason == "no_odds_available"
    assert result.rows_written == 0


def test_execute_escribe_filas_valid(recorder_env):
    recorder, _, odds_client, _, events, db_path = recorder_env
    plans = recorder.plan(events[:1], NOW)
    odds_client.get_odds.return_value = [_odds_event(events[0], 1)]

    result = recorder.execute(plans, NOW)[0]

    conn = get_connection(db_path)
    rows = _valid_rows(conn, events[0]["event_id"])
    conn.close()
    assert result.status == "valid"
    assert result.rows_written == 6
    assert len(rows) == 6
    assert {row["source"] for row in rows} == {"pinnacle", "betfair_ex_eu"}
    assert {row["selection"] for row in rows} == {"HOME", "DRAW", "AWAY"}


def test_execute_transaccional_por_grupo(recorder_env):
    recorder, _, odds_client, _, events, db_path = recorder_env
    plans = recorder.plan(events, NOW)
    odds_client.get_odds.side_effect = [
        [_odds_event(event, index) for index, event in enumerate(events[:3], start=1)],
        RuntimeError("simulated E0 provider failure"),
    ]

    results = recorder.execute(plans, NOW)

    conn = get_connection(db_path)
    sp1_rows = _valid_rows(conn)
    e0_rows = conn.execute(
        "SELECT * FROM snapshots WHERE event_id IN ('e0-3h', 'e0-12h')"
    ).fetchall()
    conn.close()
    assert len(sp1_rows) == 18
    assert all(row["event_id"].startswith("sp1-") for row in sp1_rows)
    assert e0_rows == []
    results_by_id = {result.event_id: result for result in results}
    assert results_by_id["e0-3h"].skip_reason == "api_error"
    assert results_by_id["e0-12h"].skip_reason == "api_error"
    assert odds_client.get_odds.call_args_list[0].args[0] == "soccer_spain_la_liga"
    assert odds_client.get_odds.call_args_list[1].args[0] == "soccer_epl"


def test_get_event_status(recorder_env, monkeypatch):
    recorder, _, _, settings, events, db_path = recorder_env
    monkeypatch.setattr(recorder, "_now_utc", lambda: NOW)
    conn = get_connection(db_path)
    capture_times = (
        NOW - timedelta(hours=2),
        NOW - timedelta(hours=1),
        NOW - timedelta(minutes=30),
    )
    for index, captured_at in enumerate(capture_times):
        conn.execute(
            "INSERT INTO snapshots "
            "(event_id, market, selection, odds, source, status, captured_at_utc) "
            "VALUES (?, '1X2', ?, ?, 'pinnacle', 'valid', ?)",
            (
                events[0]["event_id"],
                f"SELECTION_{index}",
                2.0 + index,
                captured_at.isoformat(),
            ),
        )
    conn.execute(
        "INSERT INTO snapshots "
        "(event_id, market, selection, odds, source, status, skip_reason, captured_at_utc) "
        "VALUES (?, '1X2', 'HOME', 0, 'system', 'gap', 'api_error', ?)",
        (events[0]["event_id"], (NOW - timedelta(hours=4)).isoformat()),
    )
    conn.commit()
    conn.close()

    settings.snapshot_min_valid_per_event = 3
    status = recorder.get_event_status(events[0]["event_id"])
    assert status == {
        "valid": 3,
        "gap": 1,
        "skipped": 0,
        "min_valid_met": True,
        "last_valid_at_utc": capture_times[-1].isoformat(),
    }
    settings.snapshot_min_valid_per_event = 4
    assert recorder.get_event_status(events[0]["event_id"])["min_valid_met"] is False


def test_get_event_status_una_sola_captura_no_cumple_minimo(
    recorder_env, monkeypatch
):
    recorder, _, _, settings, events, db_path = recorder_env
    monkeypatch.setattr(recorder, "_now_utc", lambda: NOW)
    captured_at = NOW.isoformat()
    conn = get_connection(db_path)
    for source in ("pinnacle", "betfair_ex_eu"):
        for selection in ("HOME", "DRAW", "AWAY"):
            conn.execute(
                "INSERT INTO snapshots "
                "(event_id, market, selection, odds, source, status, captured_at_utc) "
                "VALUES (?, '1X2', ?, 2.0, ?, 'valid', ?)",
                (events[0]["event_id"], selection, source, captured_at),
            )
    conn.commit()
    conn.close()

    settings.snapshot_min_valid_per_event = 3
    status = recorder.get_event_status(events[0]["event_id"])
    assert status["valid"] == 1
    assert status["min_valid_met"] is False


def test_get_event_status_tres_capturas_cumplen_minimo(recorder_env, monkeypatch):
    recorder, _, _, settings, events, db_path = recorder_env
    monkeypatch.setattr(recorder, "_now_utc", lambda: NOW)
    capture_times = (
        NOW - timedelta(hours=2),
        NOW - timedelta(hours=1),
        NOW - timedelta(minutes=30),
    )
    conn = get_connection(db_path)
    for index, captured_at in enumerate(capture_times):
        conn.execute(
            "INSERT INTO snapshots "
            "(event_id, market, selection, odds, source, status, captured_at_utc) "
            "VALUES (?, '1X2', ?, 2.0, 'pinnacle', 'valid', ?)",
            (events[0]["event_id"], f"SELECTION_{index}", captured_at.isoformat()),
        )
    conn.commit()
    conn.close()

    settings.snapshot_min_valid_per_event = 3
    status = recorder.get_event_status(events[0]["event_id"])
    assert status["valid"] == 3
    assert status["min_valid_met"] is True


def test_get_event_status_evento_inexistente(recorder_env, monkeypatch):
    recorder, _, _, _, _, _ = recorder_env
    monkeypatch.setattr(recorder, "_now_utc", lambda: NOW)

    assert recorder.get_event_status("missing-event") == {
        "valid": 0,
        "gap": 0,
        "skipped": 0,
        "min_valid_met": False,
        "last_valid_at_utc": None,
    }


def test_execute_bloquea_alias_pending_aunque_canonical_confirmed(
    recorder_env, caplog
):
    recorder, _, odds_client, _, _, db_path = recorder_env
    event = _event("pending-home", "E0", NOW + timedelta(hours=3))
    event.update(home_team="Manchester United", away_team="Arsenal")
    conn = get_connection(db_path)
    _insert_event(conn, event)
    assert normalize_team_name(
        "Manchester Untd", "E0", source="the_odds_api", conn=conn
    ) == ("Manchester United", "fuzzy")
    conn.commit()
    conn.close()

    plan = _plan_for(recorder, event)
    odds_event = _odds_event({**event, "home_team": "Manchester Untd"}, 1)
    odds_client.get_odds.return_value = [odds_event]

    result = recorder.execute([plan], NOW)[0]

    conn = get_connection(db_path)
    row_count = conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE event_id = ?", (event["event_id"],)
    ).fetchone()[0]
    conn.close()
    assert result.status == "gap"
    assert result.skip_reason == "team_not_confirmed"
    assert row_count == 0
    assert "team_not_confirmed" in caplog.text


def test_execute_alias_no_existente_bloquea(recorder_env):
    recorder, _, odds_client, _, _, db_path = recorder_env
    event = _event("unknown-home", "E0", NOW + timedelta(hours=3))
    event.update(home_team="Manchester United", away_team="Arsenal")
    conn = get_connection(db_path)
    _insert_event(conn, event)
    conn.commit()
    conn.close()
    plan = _plan_for(recorder, event)
    odds_client.get_odds.return_value = [
        _odds_event({**event, "home_team": "Equipo Desconocido XYZ"}, 1)
    ]

    result = recorder.execute([plan], NOW)[0]

    assert result.status == "gap"
    assert result.skip_reason == "league_not_in_response"


def test_execute_ambos_confirmed_escribe_normal(recorder_env):
    recorder, _, odds_client, _, _, db_path = recorder_env
    event = _event("confirmed-pair", "E0", NOW + timedelta(hours=3))
    event.update(home_team="Manchester United", away_team="Arsenal")
    conn = get_connection(db_path)
    _insert_event(conn, event)
    conn.commit()
    conn.close()
    plan = _plan_for(recorder, event)
    odds_client.get_odds.return_value = [_odds_event(event, 1)]

    result = recorder.execute([plan], NOW)[0]

    conn = get_connection(db_path)
    row_count = conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE event_id = ?", (event["event_id"],)
    ).fetchone()[0]
    conn.close()
    assert result.status == "valid"
    assert result.rows_written > 0
    assert row_count > 0


def test_execute_un_equipo_pending_otro_confirmed_bloquea(recorder_env):
    recorder, _, odds_client, _, _, db_path = recorder_env
    event = _event("pending-away", "E0", NOW + timedelta(hours=3))
    event.update(home_team="Arsenal", away_team="Manchester United")
    conn = get_connection(db_path)
    _insert_event(conn, event)
    conn.commit()
    conn.close()
    plan = _plan_for(recorder, event)
    odds_client.get_odds.return_value = [
        _odds_event({**event, "away_team": "Manchester Untd"}, 1)
    ]

    result = recorder.execute([plan], NOW)[0]

    conn = get_connection(db_path)
    row_count = conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE event_id = ?", (event["event_id"],)
    ).fetchone()[0]
    conn.close()
    assert result.status == "gap"
    assert result.skip_reason == "team_not_confirmed"
    assert row_count == 0


def test_execute_transaccion_compartida_validacion_escritura(recorder_env):
    recorder, _, odds_client, _, _, db_path = recorder_env
    confirmed_event = _event("txn-confirmed", "E0", NOW + timedelta(hours=3))
    confirmed_event.update(home_team="Manchester United", away_team="Arsenal")
    pending_event = _event("txn-pending", "E0", NOW + timedelta(hours=4))
    pending_event.update(home_team="Manchester United", away_team="Arsenal")
    conn = get_connection(db_path)
    _insert_event(conn, confirmed_event)
    _insert_event(conn, pending_event)
    conn.commit()
    conn.close()
    plans = [
        _plan_for(recorder, confirmed_event),
        _plan_for(recorder, pending_event),
    ]
    confirmed_odds = _odds_event(confirmed_event, 1)
    pending_odds = _odds_event(
        {**pending_event, "home_team": "Manchester Untd"}, 2
    )
    odds_client.get_odds.return_value = [confirmed_odds, pending_odds]

    first_results = recorder.execute(plans, NOW)
    first_by_id = {result.event_id: result for result in first_results}
    conn = get_connection(db_path)
    confirmed_rows = conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE event_id = ?",
        (confirmed_event["event_id"],),
    ).fetchone()[0]
    pending_rows = conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE event_id = ?",
        (pending_event["event_id"],),
    ).fetchone()[0]
    conn.execute("DELETE FROM snapshots")
    conn.execute(
        "CREATE TRIGGER fail_confirmed_snapshot_insert "
        "BEFORE INSERT ON snapshots "
        "WHEN NEW.event_id = 'txn-confirmed' AND NEW.selection = 'DRAW' "
        "BEGIN SELECT RAISE(ABORT, 'simulated database failure'); END"
    )
    conn.commit()
    conn.close()
    assert first_by_id[confirmed_event["event_id"]].status == "valid"
    assert first_by_id[pending_event["event_id"]].status == "gap"
    assert first_by_id[pending_event["event_id"]].skip_reason == "team_not_confirmed"
    assert confirmed_rows > 0
    assert pending_rows == 0

    second_results = recorder.execute(plans, NOW)
    second_by_id = {result.event_id: result for result in second_results}
    conn = get_connection(db_path)
    remaining_rows = conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE event_id IN (?, ?)",
        (confirmed_event["event_id"], pending_event["event_id"]),
    ).fetchone()[0]
    conn.close()
    assert remaining_rows == 0
    assert all(result.skip_reason == "api_error" for result in second_by_id.values())
