import pytest
from pydantic import ValidationError


def _settings_class(monkeypatch):
    monkeypatch.setenv("API_FOOTBALL_KEY", "test-football")
    monkeypatch.setenv("FOOTBALL_DATA_ORG_KEY", "test-data-org")
    monkeypatch.setenv("SHARP_ODDS_API_KEY", "test-sharp")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    from config import PROJECT_ROOT, Settings

    return PROJECT_ROOT, Settings


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("snapshot_interval_near_h", -1),
        ("snapshot_interval_near_h", 6),
        ("snapshot_interval_far_h", 0),
        ("snapshot_near_window_h", 0),
        ("snapshot_daily_cap_pct", 0),
        ("snapshot_daily_cap_pct", 1.1),
        ("snapshot_min_valid_per_event", 0),
        ("closure_grace_period_min", 0),
        ("scheduler_interval_min", 0),
        ("retrain_every_n_matches", 0),
        ("manual_odds_staleness_min", 0),
        ("timezone", "Marte/Olimpo"),
    ],
)
def test_validadores_numericos_y_timezone_rechazan_valores_invalidos(
    monkeypatch, field, value
):
    _, Settings = _settings_class(monkeypatch)

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            api_football_key="test-football",
            football_data_org_key="test-data-org",
            sharp_odds_api_key="test-sharp",
            **{field: value},
        )


def test_rutas_de_base_de_datos_son_absolutas_desde_otro_cwd(
    monkeypatch, tmp_path
):
    PROJECT_ROOT, Settings = _settings_class(monkeypatch)
    monkeypatch.chdir(tmp_path)

    settings = Settings(
        _env_file=None,
        api_football_key="test-football",
        football_data_org_key="test-data-org",
        sharp_odds_api_key="test-sharp",
    )

    assert settings.db_paths.bets.is_absolute()
    assert settings.db_paths.bets == PROJECT_ROOT / "data" / "bets.db"
