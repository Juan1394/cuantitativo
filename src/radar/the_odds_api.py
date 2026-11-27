"""Cliente de cuotas de The Odds API para las tres ligas admitidas."""

from __future__ import annotations

import logging
import time
from typing import Any

from src.radar.http_client import get_json, get_last_status_code
from src.radar.rate_limit import RateLimitBudgetManager

logger = logging.getLogger(__name__)

# Lista sharp: Pinnacle y Betfair Exchange (incluido su key europeo).
SHARP_BOOKMAKERS = frozenset({"pinnacle", "betfair_ex_eu"})

# Mapeo de competition code interno a sport_key de The Odds API.
SPORT_KEY_MAP = {
    "SP1": "soccer_spain_la_liga",
    "E0": "soccer_epl",
    "I1": "soccer_italy_serie_a",
}


class TheOddsAPIClient:
    """Cliente HTTP; solo `get_odds` consume cuota, una vez por llamada lógica.

    El coste normal en Fase 1 es un crédito por cada combinación market × region.
    Mercados especiales como `h2h_lay` pueden tener otro coste y no están
    contemplados en este cálculo.
    """

    BASE_URL = "https://api.the-odds-api.com/v4"
    _SPORTS_CACHE_TTL_SECONDS = 24 * 60 * 60

    def __init__(self, settings: object, budget: RateLimitBudgetManager):
        if not getattr(settings, "sharp_odds_api_key", None):
            raise ValueError("sharp_odds_api_key is required")
        self.settings = settings
        self._budget = budget
        self._sports_cache: list[dict[str, Any]] | None = None
        self._sports_cached_at = 0.0
        self._events_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    def _api_key(self) -> str:
        return str(self.settings.sharp_odds_api_key)

    @staticmethod
    def _compute_cost(regions: str, markets: str) -> int:
        """Cuenta las combinaciones de regions y markets estándar."""
        region_count = len([region for region in regions.split(",") if region.strip()])
        market_count = len([market for market in markets.split(",") if market.strip()])
        cost = region_count * market_count
        if cost <= 0:
            raise ValueError("invalid regions or markets")
        return cost

    def list_sports(self) -> list[dict[str, Any]] | None:
        """Devuelve deportes; endpoint gratuito y cacheado 24 horas en memoria."""
        now = time.monotonic()
        if self._sports_cache is not None and now - self._sports_cached_at < self._SPORTS_CACHE_TTL_SECONDS:
            return self._sports_cache
        payload = get_json(
            f"{self.BASE_URL}/sports",
            headers={},
            params={"apiKey": self._api_key()},
        )
        if isinstance(payload, dict) and payload.get("error") == "rate_limit":
            return None
        if (
            not isinstance(payload, list)
            or not all(isinstance(item, dict) and "key" in item for item in payload)
        ):
            logger.warning(
                "the_odds_api_missing_expected_key key=key endpoint=sports status_code=%s",
                get_last_status_code(),
            )
            return None
        self._sports_cache = payload
        self._sports_cached_at = now
        return payload

    def list_events(self, sport_key: str) -> list[dict[str, Any]] | None:
        """Lista eventos gratis y los cachea en memoria durante 24 horas."""
        now = time.monotonic()
        cached = self._events_cache.get(sport_key)
        if cached is not None and now - cached[0] < self._SPORTS_CACHE_TTL_SECONDS:
            return cached[1]
        payload = get_json(
            f"{self.BASE_URL}/sports/{sport_key}/events",
            headers={},
            params={"apiKey": self._api_key()},
        )
        if isinstance(payload, dict) and payload.get("error") == "rate_limit":
            return None
        if (
            isinstance(payload, list)
            and all(isinstance(item, dict) and "id" in item for item in payload)
        ):
            self._events_cache[sport_key] = (now, payload)
            return payload
        logger.warning(
            "the_odds_api_missing_expected_key sport_key=%s key=id status_code=%s",
            sport_key,
            get_last_status_code(),
        )
        return None

    def get_odds(
        self,
        sport_key: str,
        regions: str = "eu",
        markets: str = "h2h",
    ) -> list[dict[str, Any]] | None:
        """Obtiene cuotas; cobra una vez por llamada lógica según markets × regions.

        La estimación cubre mercados estándar. `h2h_lay` y otros mercados
        especiales pueden tener un coste distinto y quedan fuera de Fase 1.
        """
        cost = self._compute_cost(regions, markets)
        if not self._budget.consume("the_odds_api", "month", cost):
            remaining = self._budget.get_remaining("the_odds_api", "month")
            logger.warning(
                "monthly_quota_insufficient provider=the_odds_api cost=%s remaining=%s",
                cost,
                remaining,
            )
            return None
        payload = get_json(
            f"{self.BASE_URL}/sports/{sport_key}/odds",
            headers={},
            params={
                "apiKey": self._api_key(),
                "regions": regions,
                "markets": markets,
                "oddsFormat": "decimal",
            },
            before_attempt=None,
        )
        if isinstance(payload, dict) and payload.get("error") == "rate_limit":
            return None
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            logger.error(
                "the_odds_api_missing_expected_key sport_key=%s key=bookmakers status_code=%s",
                sport_key,
                get_last_status_code(),
            )
            return None

        filtered: list[dict[str, Any]] = []
        for event in payload:
            bookmakers = event.get("bookmakers")
            if not isinstance(bookmakers, list):
                logger.error(
                    "the_odds_api_missing_expected_key sport_key=%s key=bookmakers status_code=%s",
                    sport_key,
                    get_last_status_code(),
                )
                return None
            event_copy = dict(event)
            event_copy["bookmakers"] = [
                bookmaker
                for bookmaker in bookmakers
                if isinstance(bookmaker, dict)
                and bookmaker.get("key") in SHARP_BOOKMAKERS
            ]
            filtered.append(event_copy)
        return filtered
