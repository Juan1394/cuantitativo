"""Sistema propio de migraciones basado en PRAGMA user_version."""

import re
import sqlite3
from pathlib import Path
from typing import Iterator

from src.logging_config import get_logger

logger = get_logger(__name__)

MIGRATION_PATTERN = re.compile(r"^(\d+)_.*\.sql$")


def get_current_version(conn: sqlite3.Connection) -> int:
    """Devuelve el valor actual de PRAGMA user_version."""

    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def list_migrations(migrations_dir: Path) -> list[tuple[int, Path]]:
    """Devuelve las migraciones disponibles ordenadas por número."""

    if not migrations_dir.is_dir():
        raise FileNotFoundError(
            f"directorio de migraciones no encontrado: {migrations_dir}"
        )

    migrations: list[tuple[int, Path]] = []
    for path in migrations_dir.iterdir():
        if not path.is_file():
            continue
        match = MIGRATION_PATTERN.match(path.name)
        if match is None:
            continue
        migrations.append((int(match.group(1)), path))

    versions = [version for version, _ in migrations]
    if len(versions) != len(set(versions)):
        raise ValueError("numeros de migracion duplicados")

    return sorted(migrations, key=lambda item: item[0])


def _strip_line_comments(sql: str) -> str:
    """Elimina comentarios -- fuera de comillas; no soporta comentarios /* */."""

    output: list[str] = []
    in_single_quote = False
    in_double_quote = False
    in_line_comment = False
    index = 0

    while index < len(sql):
        character = sql[index]
        next_character = sql[index + 1] if index + 1 < len(sql) else ""

        if in_line_comment:
            if character == "\n":
                output.append(character)
                in_line_comment = False
            index += 1
            continue

        if character == "'" and not in_double_quote:
            if in_single_quote and next_character == "'":
                output.extend((character, next_character))
                index += 2
                continue
            in_single_quote = not in_single_quote
        elif character == '"' and not in_single_quote:
            if in_double_quote and next_character == '"':
                output.extend((character, next_character))
                index += 2
                continue
            in_double_quote = not in_double_quote
        elif (
            character == "-"
            and next_character == "-"
            and not in_single_quote
            and not in_double_quote
        ):
            in_line_comment = True
            index += 2
            continue

        output.append(character)
        index += 1

    return "".join(output)


def _iter_sql_statements(path: Path) -> Iterator[str]:
    """Lee sentencias SQL; comentarios /* */ no se soportan todavía."""

    buffer = ""
    sql = _strip_line_comments(path.read_text(encoding="utf-8"))
    for line in sql.splitlines(keepends=True):
        if not line.strip():
            continue
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            buffer = ""
            if statement:
                yield statement

    if buffer.strip():
        raise ValueError(f"sentencia SQL incompleta en {path.name}")


def _execute_sql_file(conn: sqlite3.Connection, path: Path) -> None:
    """Ejecuta las sentencias de un fichero SQL dentro de la transacción activa."""

    for statement in _iter_sql_statements(path):
        conn.execute(statement)


def apply_migrations(
    conn: sqlite3.Connection, migrations_dir: Path
) -> list[str]:
    """Aplica las migraciones pendientes y devuelve sus nombres de fichero."""

    conn.execute("BEGIN IMMEDIATE")
    applied: list[str] = []
    migration_names: list[str] = []

    try:
        current_version = get_current_version(conn)
        for version, path in list_migrations(migrations_dir):
            if version <= current_version:
                continue

            try:
                _execute_sql_file(conn, path)
                conn.execute(f"PRAGMA user_version = {version}")
            except Exception as exc:
                raise RuntimeError(
                    f"error al aplicar la migracion {path.name}"
                ) from exc

            migration_names.append(path.name)
            applied.append(path.name)

        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

    for name in migration_names:
        logger.info("migracion aplicada: %s", name)

    return applied
