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
