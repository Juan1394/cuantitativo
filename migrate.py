"""CLI para aplicar las migraciones SQLite."""

import argparse
import sys
from pathlib import Path

from src.db import ensure_dir, get_connection
from src.migrations import apply_migrations, get_current_version

MIGRATIONS_DIR = Path(__file__).with_name("migrations")


def _migrate_one(db_path: Path, migrations_dir: Path) -> None:
    ensure_dir(db_path)
    conn = get_connection(db_path)
    try:
        before = get_current_version(conn)
        print(f"{db_path}: version inicial={before}")
        apply_migrations(conn, migrations_dir)
        after = get_current_version(conn)
        print(f"{db_path}: version final={after}")
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Aplica las migraciones pendientes de la aplicacion."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Ruta de la base de datos; por defecto settings.db_paths.bets.",
    )
    parser.add_argument(
        "--both",
        action="store_true",
        help="Aplica tambien las migraciones a settings.db_paths.audit.",
    )
    args = parser.parse_args(argv)
    if args.db is not None and args.both:
        parser.error("--both no es compatible con --db")

    try:
        if args.db is not None:
            targets = [args.db]
        else:
            from config import settings

            targets = [settings.db_paths.bets]
            if args.both and settings.db_paths.audit not in targets:
                targets.append(settings.db_paths.audit)

        for db_path in targets:
            _migrate_one(db_path, MIGRATIONS_DIR)
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
