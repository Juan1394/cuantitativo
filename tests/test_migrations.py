import inspect
import shutil
import sqlite3
from pathlib import Path

import pytest

from src.db import get_connection
from src.migrations import apply_migrations, get_current_version, list_migrations

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "migrations"

EXPECTED_TABLE_NAMES = {
    "league_mappings",
    "events",
    "snapshots",
    "reference_odds",
    "model_versions",
    "model_probabilities",
    "manual_odds",
    "bets",
    "decision_explanation",
    "decision_logs",
    "scheduled_closures",
    "closing_odds",
    "clv_records",
    "bankroll_history",
    "audit_log",
    "performance_snapshots",
    "model_artifacts",
    "active_models",
}


def _table_names(conn: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def _index_names(conn: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def test_bd_vacia_migra_de_0_a_ultima_version(tmp_path):
    conn = get_connection(tmp_path / "vacia.db")
    try:
        migrations = list_migrations(MIGRATIONS_DIR)
        versions = [version for version, _ in migrations]
        expected_names = [path.name for _, path in migrations]

        assert versions == sorted(versions)
        assert get_current_version(conn) == 0

        applied = apply_migrations(conn, MIGRATIONS_DIR)

        assert applied == expected_names
        assert get_current_version(conn) == versions[-1]
        assert set(_table_names(conn)) == EXPECTED_TABLE_NAMES
        assert len(_table_names(conn)) == 18
        assert len(_index_names(conn)) == 7
    finally:
        conn.close()


def test_aplicar_dos_veces_no_rompe_ni_duplica(tmp_path):
    conn = get_connection(tmp_path / "doble.db")
    try:
        first = apply_migrations(conn, MIGRATIONS_DIR)
        second = apply_migrations(conn, MIGRATIONS_DIR)

        assert len(first) == len(list_migrations(MIGRATIONS_DIR))
        assert second == []
        assert get_current_version(conn) == list_migrations(MIGRATIONS_DIR)[-1][0]
        assert set(_table_names(conn)) == EXPECTED_TABLE_NAMES
        assert len(_table_names(conn)) == 18
        assert len(_index_names(conn)) == 7
        assert len(_index_names(conn)) == len(set(_index_names(conn)))
    finally:
        conn.close()


def test_migracion_invalida_hace_rollback_y_deja_version_intacta(tmp_path):
    migrations_dir = tmp_path / "migrations_fallo"
    migrations_dir.mkdir()
    (migrations_dir / "0002_bad.sql").write_text(
        "CREATE TABLE tabla_parcial(x);\nESTO NO ES SQL;\n",
        encoding="utf-8",
    )

    conn = get_connection(tmp_path / "fallo.db")
    try:
        conn.execute("PRAGMA user_version = 1")
        conn.commit()

        with pytest.raises(RuntimeError, match="0002_bad.sql"):
            apply_migrations(conn, migrations_dir)

        assert get_current_version(conn) == 1
        assert "tabla_parcial" not in _table_names(conn)
    finally:
        conn.close()


def test_migracion_invalida_desde_cero_deja_version_cero(tmp_path):
    migrations_dir = tmp_path / "migrations_cero"
    migrations_dir.mkdir()
    (migrations_dir / "0001_bad.sql").write_text(
        "ESTO NO ES SQL;\n",
        encoding="utf-8",
    )

    conn = get_connection(tmp_path / "cero.db")
    try:
        with pytest.raises(RuntimeError, match="0001_bad.sql"):
            apply_migrations(conn, migrations_dir)

        assert get_current_version(conn) == 0
    finally:
        conn.close()


def test_get_connection_activa_pragmas(tmp_path):
    conn = get_connection(tmp_path / "pragmas.db")
    try:
        assert conn.row_factory is sqlite3.Row
        assert (
            conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        )
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()


def test_checks_de_bets_rechazan_valores_invalidos(tmp_path):
    conn = get_connection(tmp_path / "checks.db")
    try:
        apply_migrations(conn, MIGRATIONS_DIR)
        conn.execute(
            "INSERT INTO events "
            "(event_id, competition_code, home_team, away_team, kickoff_utc, "
            "match_status, created_at_utc, updated_at_utc) "
            "VALUES ('event-1', 'SP1', 'Home', 'Away', '2026-01-01T00:00:00Z', "
            "'scheduled', '2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z')"
        )
        invalid_values = [
            ("manual_odds", 0.5),
            ("stake", -10),
            ("model_probability", 7.5),
        ]
        for index, (field, value) in enumerate(invalid_values):
            values = {
                "manual_odds": 2.0,
                "stake": 10.0,
                "model_probability": 0.5,
            }
            values[field] = value
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO bets "
                    "(event_id, market, selection, manual_odds, stake, ev, "
                    "model_probability, status, idempotency_key, created_at_utc) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "event-1",
                        "1x2",
                        "home",
                        values["manual_odds"],
                        values["stake"],
                        0.1,
                        values["model_probability"],
                        "open",
                        f"invalid-{index}",
                        "2025-01-01T00:00:00Z",
                    ),
                )
    finally:
        conn.close()


def test_begin_immediate_precede_lectura_user_version():
    source = inspect.getsource(apply_migrations)
    assert source.index('conn.execute("BEGIN IMMEDIATE")') < source.index(
        "get_current_version(conn)"
    )


def test_migracion_acepta_comentario_inline(tmp_path):
    migrations_dir = tmp_path / "migrations_comentarios"
    migrations_dir.mkdir()
    (migrations_dir / "0001_comments.sql").write_text(
        "CREATE TABLE comment_test(value INTEGER); -- comentario inline\n"
        "INSERT INTO comment_test(value) VALUES (7); -- comentario final\n",
        encoding="utf-8",
    )

    conn = get_connection(tmp_path / "comments.db")
    try:
        assert apply_migrations(conn, migrations_dir) == ["0001_comments.sql"]
        assert conn.execute("SELECT value FROM comment_test").fetchone()[0] == 7
    finally:
        conn.close()


def _create_v4_migrations_dir(parent: Path) -> Path:
    migrations_dir = parent / "migrations_v4"
    migrations_dir.mkdir()
    for version in range(1, 5):
        source = next(MIGRATIONS_DIR.glob(f"{version:04d}_*.sql"))
        shutil.copy2(source, migrations_dir / source.name)
    return migrations_dir


def _insert_valid_bet(conn: sqlite3.Connection, event_id: str, key: str) -> None:
    conn.execute(
        "INSERT INTO events "
        "(event_id, competition_code, home_team, away_team, kickoff_utc, "
        "match_status, created_at_utc, updated_at_utc) "
        "VALUES (?, 'SP1', 'Home', 'Away', '2026-01-01T00:00:00Z', "
        "'scheduled', '2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z')",
        (event_id,),
    )
    conn.execute(
        "INSERT INTO bets "
        "(event_id, market, selection, manual_odds, stake, ev, "
        "model_probability, status, idempotency_key, created_at_utc) "
        "VALUES (?, '1x2', 'home', 2.0, 10.0, 0.1, 0.5, 'open', ?, ?)",
        (event_id, key, "2025-01-01T00:00:00Z"),
    )


def test_0005_upgrade_desde_v4_rechaza_datos_invalidos(tmp_path):
    v4_dir = _create_v4_migrations_dir(tmp_path)
    conn = get_connection(tmp_path / "v4.db")
    try:
        apply_migrations(conn, v4_dir)
        assert get_current_version(conn) == 4
        _insert_valid_bet(conn, "event-v4", "valid-v4")
        conn.commit()

        migration_5_dir = tmp_path / "migration_5"
        migration_5_dir.mkdir()
        migration_5 = next(MIGRATIONS_DIR.glob("0005_*.sql"))
        shutil.copy2(migration_5, migration_5_dir / migration_5.name)
        assert apply_migrations(conn, migration_5_dir) == [migration_5.name]
        assert get_current_version(conn) == 5
        assert conn.execute("SELECT count(*) FROM bets").fetchone()[0] == 1

        invalid_values = [
            ("manual_odds", 0.5),
            ("stake", -5),
            ("model_probability", 1.4),
            ("manual_odds", "texto"),
        ]
        for index, (field, value) in enumerate(invalid_values):
            values = {
                "manual_odds": 2.0,
                "stake": 10.0,
                "model_probability": 0.5,
            }
            values[field] = value
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO bets "
                    "(event_id, market, selection, manual_odds, stake, ev, "
                    "model_probability, status, idempotency_key, created_at_utc) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "event-v4",
                        "1x2",
                        "home",
                        values["manual_odds"],
                        values["stake"],
                        0.1,
                        values["model_probability"],
                        "open",
                        f"invalid-v4-{index}",
                        "2025-01-01T00:00:00Z",
                    ),
                )
    finally:
        conn.close()


def test_0005_nueva_igual_a_migrada(tmp_path):
    fresh = get_connection(tmp_path / "fresh.db")
    migrated = get_connection(tmp_path / "migrated.db")
    try:
        apply_migrations(fresh, MIGRATIONS_DIR)
        v4_dir = _create_v4_migrations_dir(tmp_path)
        apply_migrations(migrated, v4_dir)
        apply_migrations(migrated, MIGRATIONS_DIR)

        fresh_ddl = fresh.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='bets'"
        ).fetchone()[0]
        migrated_ddl = migrated.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='bets'"
        ).fetchone()[0]
        assert fresh_ddl == migrated_ddl

        fresh_columns = [
            tuple(row) for row in fresh.execute("PRAGMA table_info(bets)")
        ]
        migrated_columns = [
            tuple(row) for row in migrated.execute("PRAGMA table_info(bets)")
        ]
        assert fresh_columns == migrated_columns
    finally:
        fresh.close()
        migrated.close()


def test_comentario_dentro_de_string_no_corta(tmp_path):
    migrations_dir = tmp_path / "migrations_string_comment"
    migrations_dir.mkdir()
    (migrations_dir / "0001_team.sql").write_text(
        "CREATE TABLE teams(name TEXT);\n"
        "INSERT INTO teams(name) VALUES ('Team -- A');\n",
        encoding="utf-8",
    )

    conn = get_connection(tmp_path / "team.db")
    try:
        apply_migrations(conn, migrations_dir)
        assert conn.execute("SELECT name FROM teams").fetchone()[0] == "Team -- A"
    finally:
        conn.close()


def test_0005_con_filas_hijas(tmp_path):
    v4_dir = _create_v4_migrations_dir(tmp_path)
    conn = get_connection(tmp_path / "v4_children.db")
    try:
        apply_migrations(conn, v4_dir)
        _insert_valid_bet(conn, "event-child", "bet-child")
        bet_id = conn.execute(
            "SELECT bet_id FROM bets WHERE idempotency_key = 'bet-child'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO model_versions "
            "(competition_code, train_until_utc, matches_count, hyperparams_json, "
            "created_at_utc) VALUES ('SP1', '2025-12-31T23:59:59Z', 10, '{}', "
            "'2026-01-01T00:00:00Z')"
        )
        model_version_id = conn.execute(
            "SELECT model_version_id FROM model_versions WHERE competition_code='SP1'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO scheduled_closures (bet_id, scheduled_for_utc, status) "
            "VALUES (?, '2026-01-01T00:00:00Z', 'pending')",
            (bet_id,),
        )
        conn.execute(
            "INSERT INTO closing_odds "
            "(bet_id, closing_odds, source, fallback_reason, captured_at_utc) "
            "VALUES (?, 2.1, 'book', NULL, '2026-01-01T00:00:00Z')",
            (bet_id,),
        )
        conn.execute(
            "INSERT INTO clv_records "
            "(bet_id, clv_pct, entry_implied_prob, closing_fair_prob, "
            "beat_closing_line, calculated_at_utc) "
            "VALUES (?, 0.1, 0.5, 0.45, 1, '2026-01-01T00:00:00Z')",
            (bet_id,),
        )
        conn.execute(
            "INSERT INTO bankroll_history "
            "(amount_delta, balance_after, reason, bet_id, created_at_utc) "
            "VALUES (-10, 90, 'bet placed', ?, '2026-01-01T00:00:00Z')",
            (bet_id,),
        )
        conn.execute(
            "INSERT INTO decision_explanation "
            "(bet_id, model_version_id, decision_rules_version, devig_method, "
            "model_probability, implied_probability, ev, edge_pct, decision, "
            "reasons_json) VALUES (?, ?, 'v1', 'multiplicative', 0.5, 0.48, "
            "0.1, 0.02, 'bet', '[]')",
            (bet_id, model_version_id),
        )
        conn.commit()
        bet_before = tuple(
            conn.execute("SELECT * FROM bets WHERE bet_id = ?", (bet_id,)).fetchone()
        )

        applied = apply_migrations(conn, MIGRATIONS_DIR)

        assert applied == ["0005_bets_constraints.sql"]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert tuple(
            conn.execute("SELECT * FROM bets WHERE bet_id = ?", (bet_id,)).fetchone()
        ) == bet_before
        for table in (
            "scheduled_closures",
            "closing_odds",
            "clv_records",
            "bankroll_history",
            "decision_explanation",
        ):
            row = conn.execute(
                f"SELECT bet_id FROM {table} WHERE bet_id = ?", (bet_id,)
            ).fetchone()
            assert row is not None
            assert row[0] == bet_id
    finally:
        conn.close()


def test_0005_conserva_secuencia_ids(tmp_path):
    v4_dir = _create_v4_migrations_dir(tmp_path)
    conn = get_connection(tmp_path / "v4_sequence.db")
    try:
        apply_migrations(conn, v4_dir)
        for index in range(1, 4):
            _insert_valid_bet(conn, f"event-seq-{index}", f"bet-seq-{index}")
        conn.execute("DELETE FROM bets WHERE bet_id = 2")
        conn.commit()

        apply_migrations(conn, MIGRATIONS_DIR)
        _insert_valid_bet(conn, "event-seq-4", "bet-seq-4")
        new_bet_id = conn.execute(
            "SELECT bet_id FROM bets WHERE idempotency_key = 'bet-seq-4'"
        ).fetchone()[0]
        assert new_bet_id == 4
    finally:
        conn.close()


def test_0005_rollback_si_fk_check_falla(tmp_path):
    v4_dir = _create_v4_migrations_dir(tmp_path)
    conn = get_connection(tmp_path / "v4_fk_failure.db")
    try:
        apply_migrations(conn, v4_dir)
        bad_migrations = tmp_path / "bad_migrations"
        bad_migrations.mkdir()
        (bad_migrations / "0005_orphan.sql").write_text(
            "INSERT INTO scheduled_closures "
            "(bet_id, scheduled_for_utc, status) "
            "VALUES (999999, '2026-01-01T00:00:00Z', 'pending');\n",
            encoding="utf-8",
        )

        with pytest.raises(RuntimeError, match="foreign key violations"):
            apply_migrations(conn, bad_migrations)

        assert get_current_version(conn) == 4
        assert conn.execute("SELECT count(*) FROM scheduled_closures").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()
