"""Cliente del API v4 de football-data.org para fixtures y partidos."""

from __future__ import annotations

import logging
from typing import Any

from src.radar.http_client import get_json
from src.radar.rate_limit import RateLimitBudgetManager

logger = logging.getLogger(__name__)

# Mapeo de códigos internos de liga a códigos de competición del proveedor.
COMPETITION_MAP = {"SP1": "PD", "E0": "PL", "I1": "SA"}


class FootballDataOrgClient:
    """Cliente de football-data.org; cada intento consume una petición/minuto."""

    BASE_URL = "https://api.football-data.org/v4"

    def __init__(self, settings: object, budget: RateLimitBudgetManager):
        if not getattr(settings, "football_data_org_key", None):
            raise ValueError("football_data_org_key is required")
        self.settings = settings
        self._budget = budget

    def _headers(self) -> dict[str, str]:
        return {"X-Auth-Token": str(self.settings.football_data_org_key)}

    def _consume_attempt(self) -> bool:
        return self._budget.consume("football_data_org", "minute", 1)

    def get_matches(
        self, competition_code: str, date_from: str, date_to: str
    ) -> list[dict[str, Any]] | None:
        """Lista partidos por competición (no consulta el endpoint global)."""
        provider_code = COMPETITION_MAP.get(competition_code)
        if provider_code is None:
            raise ValueError(f"competition_code no admitido: {competition_code}")
        payload = get_json(
            f"{self.BASE_URL}/competitions/{provider_code}/matches",
            headers=self._headers(),
            params={"dateFrom": date_from, "dateTo": date_to},
            before_attempt=self._consume_attempt,
        )
        if isinstance(payload, dict) and payload.get("error") == "rate_limit":
            return None
        matches = payload.get("matches") if isinstance(payload, dict) else None
        if isinstance(matches, list) and all(isinstance(item, dict) for item in matches):
            return matches
        logger.warning(
            "football_data_org_missing_expected_key competition_code=%s key=matches status_code=200",
            competition_code,
        )
        return None
