import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _clean_environment():
    return {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONIOENCODING": "utf-8",
    }


def test_migrate_db_no_exige_api_keys(tmp_path):
    database = tmp_path / "test_foo.db"
    result = subprocess.run(
        [sys.executable, str(ROOT / "migrate.py"), "--db", str(database)],
        cwd=tmp_path,
        env=_clean_environment(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert database.exists()


def test_migrate_sin_db_sin_api_keys_muestra_validacion(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "migrate.py")],
        cwd=tmp_path,
        env=_clean_environment(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "api_football_key" in result.stderr
    assert "football_data_org_key" in result.stderr
    assert "sharp_odds_api_key" in result.stderr
