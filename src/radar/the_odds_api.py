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
    """Cliente HTTP de The Odds API; únicamente `get_odds` consume cuota."""

    BASE_URL = "https://api.the-odds-api.com/v4"
    _SPORTS_CACHE_TTL_SECONDS = 24 * 60 * 60

    def __init__(self, settings: object, budget: RateLimitBudgetManager):
        self.settings = settings
        self.budget = budget
        self._sports_cache: list[dict[str, Any]] | None = None
        self._sports_cached_at = 0.0

    def _api_key(self) -> str:
        key = getattr(self.settings, "sharp_odds_api_key", None)
        if not key:
            raise ValueError("sharp_odds_api_key no está configurada")
        return str(key)

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
        """Devuelve eventos deportivos; endpoint gratuito según el plan descrito."""
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
        """Solicita cuotas y conserva únicamente casas sharp configuradas."""
        if not self.budget.consume("the_odds_api", "month", cost=1):
            logger.warning("monthly_quota_exhausted provider=the_odds_api")
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

