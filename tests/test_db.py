import sqlite3

import pytest

from src.db import get_connection, transaction


def test_commit_fallido_deja_conexion_limpia(tmp_path):
    database_path = tmp_path / "deferred_fk.db"
    conn = get_connection(database_path)
    conn.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE child(" 
        "id INTEGER PRIMARY KEY, "
        "parent_id INTEGER NOT NULL, "
        "FOREIGN KEY(parent_id) REFERENCES parent(id) "
        "DEFERRABLE INITIALLY DEFERRED)"
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        with transaction(conn):
            conn.execute("INSERT INTO child (id, parent_id) VALUES (1, 999)")

    assert conn.in_transaction is False
    assert conn.execute("SELECT count(*) FROM child").fetchone()[0] == 0

    other_conn = get_connection(database_path)
    try:
        other_conn.execute("BEGIN IMMEDIATE")
        other_conn.rollback()
    finally:
        other_conn.close()
        conn.close()
