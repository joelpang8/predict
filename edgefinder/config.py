"""Runtime configuration, loaded from environment variables / .env.

Every number here that is not documented by Kalshi or the odds provider is
labelled as a self-imposed default or an assumption.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal

from dotenv import load_dotenv

# Hosts from docs.kalshi.com/getting_started/api_environments ("recommended";
# the shared hosts are "also supported").
KALSHI_HOSTS = {
    "prod": "https://external-api.kalshi.com/trade-api/v2",
    "prod_shared": "https://api.elections.kalshi.com/trade-api/v2",
    "demo": "https://external-api.demo.kalshi.co/trade-api/v2",
    "demo_shared": "https://demo-api.kalshi.co/trade-api/v2",
}

SPORTS = ("nfl", "nba", "mlb")

# Ten books = 1 Odds API "region" = 1 credit per sport per pull. All are on the
# free tier per the bookmaker list (Caesars/Fanatics are paid-only).
DEFAULT_BOOKMAKERS = (
    "pinnacle", "betfair_ex_eu", "lowvig", "betonlineag", "draftkings",
    "fanduel", "betmgm", "betrivers", "bovada", "espnbet",
)
# Sharper books count more in the consensus. Self-imposed defaults.
DEFAULT_BOOK_WEIGHTS = {"pinnacle": 3.0, "betfair_ex_eu": 2.0, "circa": 3.0, "lowvig": 1.5, "betonlineag": 1.5}


def _series_list(raw: str | None) -> tuple[str, ...]:
    return tuple(s.strip().upper() for s in (raw or "").split(",") if s.strip())


def _csv(raw: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    vals = tuple(s.strip() for s in (raw or "").split(",") if s.strip())
    return vals or default


def _weights(raw: str | None) -> dict[str, float]:
    if not raw:
        return dict(DEFAULT_BOOK_WEIGHTS)
    out = {}
    for part in raw.split(","):
        k, _, v = part.partition(":")
        out[k.strip()] = float(v)
    return out


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
    odds_bookmakers: tuple[str, ...] = DEFAULT_BOOKMAKERS
    book_weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_BOOK_WEIGHTS))
    # Refuse an odds call that would leave fewer credits than this.
    odds_min_remaining: int = 20
    # Fewer books than this and the consensus is not trusted.
    min_books: int = 3

    # Fees: $0.01 = non-direct (retail) balance precision, $0.0001 = direct member.
    fee_precision: Decimal = Decimal("0.01")

    # Assumption, not data: chance an NFL game ends tied (Kalshi settles ties at
    # $0.50; sportsbook moneylines refund them). Override in .env.
    nfl_tie_prob: float = 0.003

    # Freshness: never compute an edge from data older than this, or from two
    # sources fetched further apart than max_skew_s.
    max_data_age_s: int = 300
    max_skew_s: int = 120

    # Sizing
    bankroll: Decimal = Decimal("1000")
    kelly_multiplier: float = 0.25
    max_bet_pct: float = 0.02


def load_settings(env_file: str | None = ".env") -> Settings:
    if env_file:
        load_dotenv(env_file, override=False)
    g = os.getenv
    env = g("KALSHI_ENV", "prod").strip().lower()
    if env not in KALSHI_HOSTS:
        raise ValueError(f"KALSHI_ENV must be prod or demo, got {env!r}")
    base = g("KALSHI_BASE_URL") or KALSHI_HOSTS[env]
    return Settings(
        kalshi_base_url=base.rstrip("/"),
        kalshi_max_rps=float(g("KALSHI_MAX_RPS", "2")),
        kalshi_series={s: _series_list(g(f"KALSHI_SERIES_{s.upper()}")) for s in SPORTS},
        odds_api_key=g("ODDS_API_KEY", ""),
        odds_bookmakers=_csv(g("ODDS_BOOKMAKERS"), DEFAULT_BOOKMAKERS),
        book_weights=_weights(g("BOOK_WEIGHTS")),
        odds_min_remaining=int(g("ODDS_MIN_REMAINING", "20")),
        min_books=int(g("MIN_BOOKS", "3")),
        fee_precision=Decimal(g("FEE_PRECISION", "0.01")),
        nfl_tie_prob=float(g("NFL_TIE_PROB", "0.003")),
        max_data_age_s=int(g("MAX_DATA_AGE_S", "300")),
        max_skew_s=int(g("MAX_SKEW_S", "120")),
        bankroll=Decimal(g("BANKROLL", "1000")),
        kelly_multiplier=float(g("KELLY_MULTIPLIER", "0.25")),
        max_bet_pct=float(g("MAX_BET_PCT", "0.02")),
    )
