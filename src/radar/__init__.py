"""Clientes de ingestión de datos y gestión de cuotas de proveedor."""

from src.radar.api_football import APIFootballClient
from src.radar.football_data_org import FootballDataOrgClient
from src.radar.rate_limit import RateLimitBudgetManager
from src.radar.snapshot_recorder import SnapshotPlan, SnapshotRecorder, SnapshotResult
from src.radar.the_odds_api import TheOddsAPIClient
from src.radar.normalization import (
    get_alias_status,
    normalize_market,
    normalize_team_name,
    normalize_text,
    resolve_fixture,
    resolve_odds_event_to_local,
)

__all__ = [
    "RateLimitBudgetManager",
    "TheOddsAPIClient",
    "FootballDataOrgClient",
    "APIFootballClient",
    "SnapshotRecorder",
    "SnapshotPlan",
    "SnapshotResult",
    "normalize_team_name",
    "get_alias_status",
    "normalize_market",
    "normalize_text",
    "resolve_fixture",
    "resolve_odds_event_to_local",
]
