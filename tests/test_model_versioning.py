import hashlib
import json
import shutil
from datetime import datetime as real_datetime
from itertools import count
from pathlib import Path

import pytest

from src import model_versioning
from src.db import get_connection
from src.migrations import apply_migrations, get_current_version
from src.model_versioning import (
    get_active_model_version,
    get_model_version,
    list_model_versions,
    promote_model_version,
    register_model_version,
    rollback_active_model,
)

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "migrations"

MODEL_VERSION_FIELDS = {
    "model_version_id",
    "competition_code",
    "train_until_utc",
    "matches_count",
    "hyperparams_json",
    "log_likelihood",
    "created_at_utc",
}


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    connection = get_connection(tmp_path / "model_versioning.db")
    apply_migrations(connection, MIGRATIONS_DIR)
    yield connection
    connection.close()


def _controlled_datetime(*iso_values):
    instants = [real_datetime.fromisoformat(value) for value in iso_values]
    counter = count()

    class _ControlledDateTime:
        @staticmethod
        def now(tz):
            return instants[next(counter) % len(instants)]

    return _ControlledDateTime


def _query_dicts(connection, sql, params=()):
    cursor = connection.execute(sql, params)
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _register(
    connection,
    competition_code="SP1",
    train_until_utc="2025-12-31T23:59:59+00:00",
    matches_count=100,
    hyperparams=None,
    log_likelihood=None,
    model_path=None,
):
    artifact_path = Path("models/sp1.pkl") if model_path is None else model_path
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.touch(exist_ok=True)
    return register_model_version(
        connection,
        competition_code,
        train_until_utc,
        matches_count,
        {"xi": 0.003, "alpha": 1} if hyperparams is None else hyperparams,
        log_likelihood,
        artifact_path,
    )


def test_register_model_version_inserta_fila_y_devuelve_id(monkeypatch, conn):
    monkeypatch.setattr(
        model_versioning,
        "datetime",
        _controlled_datetime("2026-01-01T00:00:00+00:00"),
    )
    model_path = Path("models/sp1_v1.pkl")

    result = _register(
        conn,
        matches_count=120,
        hyperparams={"xi": 0.003, "alpha": 1},
        log_likelihood=-123.5,
        model_path=model_path,
    )

    assert result == 1
    rows = _query_dicts(
        conn, "SELECT * FROM model_versions WHERE model_version_id = ?", (result,)
    )
    assert len(rows) == 1
    assert rows[0]["competition_code"] == "SP1"
    assert rows[0]["train_until_utc"] == "2025-12-31T23:59:59+00:00"
    assert rows[0]["matches_count"] == 120
    assert rows[0]["hyperparams_json"] == '{"alpha":1,"xi":0.003}'
    assert rows[0]["log_likelihood"] == -123.5
    assert rows[0]["created_at_utc"] == "2026-01-01T00:00:00+00:00"

    artifact = conn.execute(
        "SELECT model_path FROM model_artifacts WHERE model_version_id = ?",
        (result,),
    ).fetchone()
    assert artifact[0] == str(model_path)

    version = get_model_version(conn, result)
    assert version is not None
    assert set(version) == MODEL_VERSION_FIELDS | {"model_path"}
    assert version["model_path"] == str(model_path)
    assert conn.execute("SELECT count(*) FROM active_models").fetchone()[0] == 0


def test_dos_registros_no_se_sobrescriben_y_activa_es_la_mas_reciente(
    monkeypatch, conn
):
    monkeypatch.setattr(
        model_versioning,
        "datetime",
        _controlled_datetime(
            "2026-01-01T00:00:00+00:00",
            "2026-01-02T00:00:00+00:00",
        ),
    )
    first = _register(
        conn,
        hyperparams={"xi": 0.1},
        log_likelihood=-10.0,
        model_path=Path("models/sp1_v1.pkl"),
    )
    second = _register(
        conn,
        hyperparams={"xi": 0.2},
        log_likelihood=None,
        model_path=Path("models/sp1_v2.pkl"),
    )

    assert first != second
    rows = _query_dicts(
        conn, "SELECT * FROM model_versions ORDER BY model_version_id"
    )
    assert len(rows) == 2
    assert [row["model_version_id"] for row in rows] == [first, second]
    assert [row["hyperparams_json"] for row in rows] == [
        '{"xi":0.1}',
        '{"xi":0.2}',
    ]
    assert rows[1]["log_likelihood"] is None

    assert get_active_model_version(conn, "SP1") == second
    assert get_active_model_version(conn, "E0") is None
    assert conn.execute("SELECT count(*) FROM active_models").fetchone()[0] == 0

    versions = list_model_versions(conn, "SP1")
    assert [version["model_version_id"] for version in versions] == [
        second,
        first,
    ]
    assert [version["created_at_utc"] for version in versions] == [
        "2026-01-02T00:00:00+00:00",
        "2026-01-01T00:00:00+00:00",
    ]
    assert versions[0]["model_path"] == str(Path("models/sp1_v2.pkl"))
    assert versions[1]["model_path"] == str(Path("models/sp1_v1.pkl"))


def test_rollback_active_model_cambia_active_models_sin_tocar_model_versions(
    monkeypatch, conn
):
    monkeypatch.setattr(
        model_versioning,
        "datetime",
        _controlled_datetime(
            "2026-01-01T00:00:00+00:00",
            "2026-01-02T00:00:00+00:00",
            "2026-01-03T00:00:00+00:00",
        ),
    )
    first = _register(
        conn,
        hyperparams={"xi": 0.1},
        model_path=Path("models/sp1_v1.pkl"),
    )
    second = _register(
        conn,
        hyperparams={"xi": 0.2},
        model_path=Path("models/sp1_v2.pkl"),
    )

    before_versions = _query_dicts(
        conn, "SELECT * FROM model_versions ORDER BY model_version_id"
    )
    before_artifacts = _query_dicts(
        conn, "SELECT * FROM model_artifacts ORDER BY model_version_id"
    )
    assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 0

    rollback_active_model(conn, "SP1", first)

    after_versions = _query_dicts(
        conn, "SELECT * FROM model_versions ORDER BY model_version_id"
    )
    after_artifacts = _query_dicts(
        conn, "SELECT * FROM model_artifacts ORDER BY model_version_id"
    )
    assert after_versions == before_versions
    assert after_artifacts == before_artifacts

    pointer = conn.execute(
        "SELECT model_version_id FROM active_models WHERE competition_code = ?",
        ("SP1",),
    ).fetchone()
    assert pointer[0] == first
    assert get_active_model_version(conn, "SP1") == first

    payload = {"from": second, "to": first}
    expected_hash = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    audit_rows = _query_dicts(conn, "SELECT * FROM audit_log ORDER BY audit_id")
    assert len(audit_rows) == 1
    assert audit_rows[0]["actor"] == "system"
    assert audit_rows[0]["action"] == "model_rollback"
    assert audit_rows[0]["entity_id"] == "SP1"
    assert audit_rows[0]["payload_hash"] == expected_hash
    assert audit_rows[0]["outcome"] == "success"
    assert audit_rows[0]["created_at_utc"] == "2026-01-03T00:00:00+00:00"


def test_rollback_hash_audita_el_modelo_activo_leido_en_transaccion(conn):
    first = _register(conn, hyperparams={"version": 1})
    second = _register(conn, hyperparams={"version": 2})
    conn.execute(
        "INSERT INTO active_models (competition_code, model_version_id) "
        "VALUES (?, ?)",
        ("SP1", second),
    )
    conn.commit()

    rollback_active_model(conn, "SP1", first)

    payload = {"from": second, "to": first}
    expected_hash = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    row = conn.execute(
        "SELECT payload_hash FROM audit_log WHERE action = 'model_rollback'"
    ).fetchone()
    assert row[0] == expected_hash


def test_get_model_version_con_id_inexistente_devuelve_none(conn):
    assert get_model_version(conn, 999999) is None


def test_rollback_rechaza_objetivo_inexistente_o_de_otra_competicion(conn):
    first = _register(conn, competition_code="SP1")
    other = _register(conn, competition_code="E0")

    with pytest.raises(ValueError, match="no existe"):
        rollback_active_model(conn, "SP1", 999999)
    with pytest.raises(ValueError, match="no existe"):
        rollback_active_model(conn, "SP1", other)

    assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM active_models").fetchone()[0] == 0
    assert get_active_model_version(conn, "SP1") == first


def test_migraciones_incrementales_preservan_filas_de_model_versions(tmp_path):
    migrations_v2 = tmp_path / "migrations_v2"
    migrations_v2.mkdir()
    for name in (
        "0001_initial_schema.sql",
        "0002_add_indexes_if_needed.sql",
    ):
        shutil.copy(MIGRATIONS_DIR / name, migrations_v2 / name)

    connection = get_connection(tmp_path / "incremental.db")
    try:
        apply_migrations(connection, migrations_v2)
        assert get_current_version(connection) == 2

        connection.execute(
            "INSERT INTO model_versions "
            "(competition_code, train_until_utc, matches_count, "
            "hyperparams_json, log_likelihood, created_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "SP1",
                "2025-12-31T23:59:59+00:00",
                90,
                '{"xi":0.003}',
                -42.0,
                "2026-01-01T00:00:00+00:00",
            ),
        )
        connection.commit()
        before = _query_dicts(
            connection, "SELECT * FROM model_versions ORDER BY model_version_id"
        )

        applied = apply_migrations(connection, MIGRATIONS_DIR)

        assert applied == [
            "0003_model_artifacts.sql",
            "0004_active_models.sql",
            "0005_bets_constraints.sql",
        ]
        assert get_current_version(connection) == 5
        assert _query_dicts(
            connection, "SELECT * FROM model_versions ORDER BY model_version_id"
        ) == before
        assert (
            connection.execute("SELECT count(*) FROM active_models").fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_promote_model_version_despues_de_rollback_y_registro(conn):
    first = _register(conn, hyperparams={"version": 1})
    second = _register(conn, hyperparams={"version": 2})
    rollback_active_model(conn, "SP1", first)
    third = _register(conn, hyperparams={"version": 3})

    assert get_active_model_version(conn, "SP1") == first
    promote_model_version(conn, "SP1", third)
    assert get_active_model_version(conn, "SP1") == third
    assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 2

    with pytest.raises(ValueError, match="no existe"):
        promote_model_version(conn, "SP1", 999999)


def test_promote_activa_version_sin_artefacto_falla(conn):
    previous_path = Path("models/sp1_v1.pkl")
    missing_path = Path("models/sp1_v2.pkl")
    previous = _register(conn, model_path=previous_path)
    missing = _register(conn, model_path=missing_path)

    promote_model_version(conn, "SP1", previous)
    missing_path.unlink()

    with pytest.raises(FileNotFoundError, match="sp1_v2.pkl"):
        promote_model_version(conn, "SP1", missing)

    active = conn.execute(
        "SELECT model_version_id FROM active_models WHERE competition_code = ?",
        ("SP1",),
    ).fetchone()
    assert active[0] == previous
    assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 1
