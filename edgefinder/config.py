"""Runtime configuration, loaded from environment variables / .env.

Every number here that is not documented by Kalshi or the odds provider is
labelled as a self-imposed default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

# Hosts as listed in Kalshi's official OpenAPI spec (kalshi_python_sync 3.33.0,
# Configuration.get_host_settings). The "shared" hosts are documented as
# "also supported".
KALSHI_HOSTS = {
    "prod": "https://external-api.kalshi.com/trade-api/v2",
    "prod_shared": "https://api.elections.kalshi.com/trade-api/v2",
    "demo": "https://external-api.demo.kalshi.co/trade-api/v2",
    "demo_shared": "https://demo-api.kalshi.co/trade-api/v2",
}

SPORTS = ("nfl", "nba", "mlb")


def _series_list(raw: str | None) -> tuple[str, ...]:
    return tuple(s.strip().upper() for s in (raw or "").split(",") if s.strip())


@dataclass(frozen=True)
class Settings:
    kalshi_base_url: str
    # Self-imposed: unauthenticated limits are undocumented (see docs/api-verification.md).
    kalshi_max_rps: float = 2.0
    kalshi_timeout_s: float = 15.0
    kalshi_max_retries: int = 4
    # Explicit series tickers per sport; empty means auto-discover.
    kalshi_series: dict[str, tuple[str, ...]] = field(default_factory=dict)
    odds_api_key: str = ""


def load_settings(env_file: str | None = ".env") -> Settings:
    if env_file:
        load_dotenv(env_file, override=False)
    env = os.getenv("KALSHI_ENV", "prod").strip().lower()
    if env not in ("prod", "demo", "prod_shared", "demo_shared"):
        raise ValueError(f"KALSHI_ENV must be prod or demo, got {env!r}")
    base = os.getenv("KALSHI_BASE_URL") or KALSHI_HOSTS[env]
    return Settings(
        kalshi_base_url=base.rstrip("/"),
        kalshi_max_rps=float(os.getenv("KALSHI_MAX_RPS", "2")),
        kalshi_series={s: _series_list(os.getenv(f"KALSHI_SERIES_{s.upper()}")) for s in SPORTS},
        odds_api_key=os.getenv("ODDS_API_KEY", ""),
    )
