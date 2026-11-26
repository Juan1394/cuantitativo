"""Configuración central de la aplicación.

Los demás módulos deben importar ``settings`` desde aquí. No deben leer
variables de entorno directamente ni construir rutas de bases de datos a mano:
las únicas rutas permitidas son ``settings.db_paths.bets`` y
``settings.db_paths.audit``.
"""

import sys
from pathlib import Path
from typing import Literal, NamedTuple

from pydantic import ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DbPaths(NamedTuple):
    """Rutas derivadas de las bases de datos según el modo de ejecución."""

    bets: Path
    audit: Path


class Settings(BaseSettings):
    """Ajustes validados de la aplicación, cargados desde entorno y .env."""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).with_name(".env"),
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="forbid",
    )

    active_competitions: list[str] = ["SP1", "E0", "I1"]
    dry_run: bool = False
    ev_threshold: float = 0.02
    kelly_fraction: float = 0.25
    max_bankroll_pct_per_bet: float = 0.03
    devig_method: Literal["multiplicative", "shin"] = "multiplicative"
    sanity_check_max_edge: float = 0.25
    manual_odds_deviation_pct: float = 0.15
    manual_odds_staleness_min: int = 5
    technical_min_bets: int = 20
    interpretable_min_bets: int = 50
    calibration_min_per_decile: int = 10
    snapshot_interval_near_h: int = 1
    snapshot_interval_far_h: int = 2
    snapshot_near_window_h: int = 6
    snapshot_daily_cap_pct: float = 0.70
    snapshot_min_valid_per_event: int = 3
    closure_grace_period_min: int = 120
    scheduler_interval_min: int = 10
    retrain_every_n_matches: int = 10
    timezone: str = "Europe/Madrid"
    api_football_key: str
    football_data_org_key: str
    sharp_odds_provider: Literal["pinnodds", "the_odds_api"] = "pinnodds"
    sharp_odds_api_key: str
    sharp_odds_fallback_provider: Literal["pinnodds", "the_odds_api"] | None = None
    sharp_odds_fallback_api_key: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    backup_dir: Path = Path("backups")
    log_dir: Path = Path("logs")
    models_dir: Path = Path("models")

    @property
    def db_paths(self) -> DbPaths:
        """Devuelve las únicas rutas de base de datos permitidas."""

        base_dir = Path("data/dryrun") if self.dry_run else Path("data")
        return DbPaths(bets=base_dir / "bets.db", audit=base_dir / "audit.db")

    @staticmethod
    def _require(condition: bool, field: str, reason: str) -> None:
        if not condition:
            raise ValueError(f"{field}: {reason}")

    @model_validator(mode="after")
    def validate_settings(self) -> "Settings":
        allowed_competitions = {"SP1", "E0", "I1"}
        configured_competitions = set(self.active_competitions)

        self._require(
            configured_competitions.issubset(allowed_competitions),
            "active_competitions",
            "solo se admiten subconjuntos de ['SP1', 'E0', 'I1']; "
            f"valores no permitidos: {sorted(configured_competitions - allowed_competitions)}",
        )
        self._require(
            len(configured_competitions) == len(self.active_competitions),
            "active_competitions",
            "no se admiten duplicados; cada competición debe aparecer una sola vez",
        )
        self._require(self.ev_threshold > 0, "ev_threshold", "debe ser mayor que 0")
        self._require(
            0 < self.kelly_fraction <= 1,
            "kelly_fraction",
            "debe cumplir 0 < valor <= 1",
        )
        self._require(
            0 < self.max_bankroll_pct_per_bet <= 0.05,
            "max_bankroll_pct_per_bet",
            "debe cumplir 0 < valor <= 0.05",
        )
        self._require(
            self.manual_odds_deviation_pct > 0,
            "manual_odds_deviation_pct",
            "debe ser mayor que 0",
        )
        self._require(
            self.technical_min_bets < self.interpretable_min_bets,
            "technical_min_bets",
            "debe ser menor que interpretable_min_bets",
        )

        if self.sharp_odds_fallback_provider is None:
            self._require(
                self.sharp_odds_fallback_api_key is None,
                "sharp_odds_fallback_api_key",
                "debe ser None cuando sharp_odds_fallback_provider es None",
            )
        else:
            self._require(
                self.sharp_odds_fallback_api_key is not None,
                "sharp_odds_fallback_api_key",
                "no puede ser None cuando sharp_odds_fallback_provider no es None",
            )

        return self


def _format_validation_errors(exc: ValidationError) -> list[str]:
    """Extrae mensajes de error sin exponer valores de entrada ni claves."""

    messages = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ())) or "settings"
        message = error.get("msg", "").removeprefix("Value error, ")
        messages.append(f"{location}: {message}")
    return messages


try:
    settings = Settings()
except ValidationError as exc:
    validation_messages = _format_validation_errors(exc)
    if __name__ == "__main__":
        for line in validation_messages:
            print(line, file=sys.stderr)
        raise SystemExit(1)
    raise ValueError("; ".join(validation_messages)) from None


if __name__ == "__main__":
    print(f"db_paths.bets: {settings.db_paths.bets}")
    print(f"db_paths.audit: {settings.db_paths.audit}")
    print(f"ev_threshold: {settings.ev_threshold}")
    print(f"kelly_fraction: {settings.kelly_fraction}")
    print(f"technical_min_bets: {settings.technical_min_bets}")
    print(f"interpretable_min_bets: {settings.interpretable_min_bets}")
    raise SystemExit(0)
