import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta

import pytest

from src.audit import audit

DDL = (
    "CREATE TABLE audit_log("
    "audit_id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "actor TEXT NOT NULL, "
    "action TEXT NOT NULL, "
    "entity_id TEXT, "
    "payload_hash TEXT, "
    "outcome TEXT NOT NULL, "
    "created_at_utc TEXT NOT NULL)"
)


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.execute(DDL)
    connection.commit()
    yield connection
    try:
        connection.close()
    except sqlite3.ProgrammingError:
        pass


def test_audit_inserta_hash_y_timestamp_utc(conn):
    payload = {"b": 2, "a": 1}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    expected_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    assert audit(conn, "usuario", "crear", "bet-1", payload, "ok") is None
    assert conn.in_transaction is False

    row = conn.execute(
        "SELECT actor, action, entity_id, payload_hash, outcome, created_at_utc "
        "FROM audit_log"
    ).fetchone()
    assert row is not None
    assert row[0] == "usuario"
    assert row[1] == "crear"
    assert row[2] == "bet-1"
    assert row[3] == expected_hash
    assert row[4] == "ok"
    assert canonical not in row
    assert row[3] != canonical

    timestamp = datetime.fromisoformat(row[5])
    assert timestamp.tzinfo is not None
    assert timestamp.utcoffset() == timedelta(0)


def test_audit_payload_none_usa_objeto_vacio(conn):
    expected_hash = hashlib.sha256(b"{}").hexdigest()

    assert audit(conn, "usuario", "crear", None, None, "ok") is None

    row = conn.execute(
        "SELECT entity_id, payload_hash, outcome FROM audit_log"
    ).fetchone()
    assert row == (None, expected_hash, "ok")


def test_audit_conexion_cerrada_no_lanza(conn, caplog):
    conn.close()

    with caplog.at_level(logging.ERROR):
        assert audit(conn, "usuario", "crear", None, {"a": 1}, "ok") is None

    assert any(
        record.levelno == logging.ERROR and "audit" in record.getMessage()
        for record in caplog.records
    )


def test_audit_fallo_de_insercion_no_lanza(conn, caplog):
    conn.execute("DROP TABLE audit_log")
    conn.commit()

    with caplog.at_level(logging.ERROR):
        assert audit(conn, "usuario", "crear", None, {"a": 1}, "ok") is None

    assert conn.in_transaction is False
    assert any(
        record.levelno == logging.ERROR and "audit" in record.getMessage()
        for record in caplog.records
    )
