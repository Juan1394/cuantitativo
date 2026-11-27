"""Conexiones SQLite y transacciones explícitas."""

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)


def ensure_dir(db_path: Path) -> None:
    """Crea el directorio padre de la base de datos si no existe."""

    db_path.parent.mkdir(parents=True, exist_ok=True)


def get_connection(db_path: Path) -> sqlite3.Connection:
    """Abre una conexión SQLite con los pragmas y el row_factory requeridos."""

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Ejecuta BEGIN IMMEDIATE y confirma o revierte según el resultado."""

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    else:
        try:
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                logger.error("rollback after failed commit failed", exc_info=True)
            raise
