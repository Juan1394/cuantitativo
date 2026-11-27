"""Cliente de fixtures de API-Football (API-Sports v3)."""

from __future__ import annotations

import logging
from typing import Any

from src.radar.http_client import get_json
from src.radar.rate_limit import RateLimitBudgetManager

logger = logging.getLogger(__name__)

# Mapeo de códigos internos a los IDs de liga de API-Football.
LEAGUE_MAP = {"SP1": 140, "E0": 39, "I1": 135}


class APIFootballClient:
    """Cliente limitado al endpoint de fixtures de API-Football."""

    BASE_URL = "https://v3.football.api-sports.io"

    def __init__(self, settings: object, budget: RateLimitBudgetManager):
        self.settings = settings
        self.budget = budget

    def get_fixtures(
        self, league_id: int, season: int, date: str
    ) -> list[dict[str, Any]] | None:
        """Obtiene fixtures; el coste por llamada se resta del presupuesto diario."""
        if not self.budget.consume("api_football", "day", cost=1):
            logger.warning("rate_limit_budget_exhausted provider=api_football")
            return None
        key = getattr(self.settings, "api_football_key", None)
        if not key:
            raise ValueError("api_football_key no está configurada")
        payload = get_json(
            f"{self.BASE_URL}/fixtures",
            headers={"x-apisports-key": str(key)},
            params={"league": league_id, "season": season, "date": date},
        )
        if isinstance(payload, dict) and payload.get("error") == "rate_limit":
            return None
        fixtures = payload.get("response") if isinstance(payload, dict) else None
        if isinstance(fixtures, list) and all(isinstance(item, dict) for item in fixtures):
            return fixtures
        logger.warning("api_football_missing_expected_key key=response status_code=200")
        return None
