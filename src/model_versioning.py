"""Versionado de modelos Dixon-Coles sin sobrescribir versiones anteriores."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from src.db import transaction

SAVEPOINT_NAME = "model_versioning"
AUDIT_ACTOR = "system"
AUDIT_ACTION = "model_rollback"
AUDIT_OUTCOME = "success"


@contextmanager
def _atomic(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Agrupa operaciones y respeta una transacción externa con SAVEPOINT."""

    if conn.in_transaction:
        conn.execute(f"SAVEPOINT {SAVEPOINT_NAME}")
        try:
            yield conn
        except BaseException:
            try:
                conn.execute(f"ROLLBACK TO {SAVEPOINT_NAME}")
            finally:
                conn.execute(f"RELEASE {SAVEPOINT_NAME}")
            raise
        else:
            conn.execute(f"RELEASE {SAVEPOINT_NAME}")
    else:
        with transaction(conn):
            yield conn


def _fetch_one_as_dict(cursor: sqlite3.Cursor) -> dict | None:
    """Convierte una fila del cursor en dict, independiente de row_factory."""

    row = cursor.fetchone()
    if row is None:
        return None
    columns = [description[0] for description in cursor.description]
    return dict(zip(columns, row))


def _fetch_all_as_dicts(cursor: sqlite3.Cursor) -> list[dict]:
    """Convierte todas las filas del cursor en dicts."""

    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _canonical_json(value: dict) -> str:
    """Serializa un dict a JSON canónico sin dependencias externas."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _payload_hash(payload: dict) -> str:
    """Calcula el hash SHA-256 del payload canónico para ``audit_log``."""

    return hashlib.sha256(
        _canonical_json(payload).encode("utf-8")
    ).hexdigest()


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    """Indica si una tabla existe en el esquema SQLite actual."""

    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _recent_model_version_id(
    conn: sqlite3.Connection, competition_code: str
) -> int | None:
    """Devuelve el id más reciente de ``model_versions`` para una competición."""

    row = conn.execute(
        "SELECT model_version_id FROM model_versions "
        "WHERE competition_code = ? "
        "ORDER BY created_at_utc DESC, model_version_id DESC LIMIT 1",
        (competition_code,),
    ).fetchone()
    return None if row is None else int(row[0])


def register_model_version(
    conn: sqlite3.Connection,
    competition_code: str,
    train_until_utc: str,
    matches_count: int,
    hyperparams: dict,
    log_likelihood: float | None,
    model_path: Path,
) -> int:
    """Inserta una versión nueva y su artefacto, devolviendo el id generado."""

    hyperparams_json = _canonical_json(hyperparams)
    created_at_utc = datetime.now(timezone.utc).isoformat()

    with _atomic(conn):
        cursor = conn.execute(
            "INSERT INTO model_versions "
            "(competition_code, train_until_utc, matches_count, "
            "hyperparams_json, log_likelihood, created_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                competition_code,
                train_until_utc,
                matches_count,
                hyperparams_json,
                log_likelihood,
                created_at_utc,
            ),
        )
        model_version_id = int(cursor.lastrowid)
        conn.execute(
            "INSERT INTO model_artifacts "
            "(model_version_id, model_path) VALUES (?, ?)",
            (model_version_id, str(model_path)),
        )

    return model_version_id


def get_active_model_version(
    conn: sqlite3.Connection, competition_code: str
) -> int | None:
    """Devuelve la referencia activa o, si no existe, la versión más reciente."""

    if _table_exists(conn, "active_models"):
        row = conn.execute(
            "SELECT am.model_version_id FROM active_models AS am "
            "JOIN model_versions AS mv "
            "ON mv.model_version_id = am.model_version_id "
            "WHERE am.competition_code = ? AND mv.competition_code = ?",
            (competition_code, competition_code),
        ).fetchone()
        if row is not None:
            return int(row[0])

    return _recent_model_version_id(conn, competition_code)


def get_model_version(
    conn: sqlite3.Connection, model_version_id: int
) -> dict | None:
    """Devuelve todos los campos de una versión, incluido ``model_path``."""

    cursor = conn.execute(
        "SELECT mv.*, ma.model_path FROM model_versions AS mv "
        "LEFT JOIN model_artifacts AS ma "
        "ON ma.model_version_id = mv.model_version_id "
        "WHERE mv.model_version_id = ?",
        (model_version_id,),
    )
    return _fetch_one_as_dict(cursor)


def list_model_versions(
    conn: sqlite3.Connection, competition_code: str
) -> list[dict]:
    """Devuelve todas las versiones de una competición, de más reciente a antigua."""

    cursor = conn.execute(
        "SELECT mv.*, ma.model_path FROM model_versions AS mv "
        "LEFT JOIN model_artifacts AS ma "
        "ON ma.model_version_id = mv.model_version_id "
        "WHERE mv.competition_code = ? "
        "ORDER BY mv.created_at_utc DESC, mv.model_version_id DESC",
        (competition_code,),
    )
    return _fetch_all_as_dicts(cursor)


def rollback_active_model(
    conn: sqlite3.Connection,
    competition_code: str,
    target_model_version_id: int,
) -> None:
    """Cambia la referencia activa y audita la acción sin tocar ``model_versions``."""

    current_model_version_id = get_active_model_version(
        conn, competition_code
    )
    if current_model_version_id is None:
        raise ValueError(
            f"no hay version de modelo para la competicion {competition_code}"
        )

    target_exists = conn.execute(
        "SELECT 1 FROM model_versions "
        "WHERE model_version_id = ? AND competition_code = ?",
        (target_model_version_id, competition_code),
    ).fetchone()
    if target_exists is None:
        raise ValueError(
            "la version objetivo no existe para la competicion indicada"
        )

    payload = {
        "from": current_model_version_id,
        "to": target_model_version_id,
    }
    payload_hash = _payload_hash(payload)
    created_at_utc = datetime.now(timezone.utc).isoformat()

    with _atomic(conn):
        conn.execute(
            "INSERT INTO audit_log "
            "(actor, action, entity_id, payload_hash, outcome, created_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                AUDIT_ACTOR,
                AUDIT_ACTION,
                competition_code,
                payload_hash,
                AUDIT_OUTCOME,
                created_at_utc,
            ),
        )
        conn.execute(
            "INSERT INTO active_models "
            "(competition_code, model_version_id) VALUES (?, ?) "
            "ON CONFLICT(competition_code) DO UPDATE SET "
            "model_version_id = excluded.model_version_id",
            (competition_code, target_model_version_id),
        )
