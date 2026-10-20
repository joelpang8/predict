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
    # Self-imposed: unauthenticated limits are undocumented and 429s were seen
    # at 2 req/s (see docs/api-verification.md).
    kalshi_max_rps: float = 1.0
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

    # Chance an NFL game ends tied (Kalshi settles ties at $0.50; sportsbook
    # moneylines refund them). nflverse results 2000-2026: 16 ties in 7,110 games.
    nfl_tie_prob: float = 0.0023

    # Freshness: never compute an edge from data older than this, or from two
    # sources fetched further apart than max_skew_s.
    max_data_age_s: int = 300
    max_skew_s: int = 120

    # Sizing
    bankroll: Decimal = Decimal("1000")
    kelly_multiplier: float = 0.25
    max_bet_pct: float = 0.02

    # Refresh / caching (self-imposed defaults)
    db_path: str = "data/edgefinder.sqlite"
    refresh_cooldown_s: int = 20
    # Free tier = 500 credits/month, about 16/day. Odds pulls stop for the day
    # (US/Eastern) at this total; Kalshi keeps refreshing.
    odds_daily_credit_budget: int = 15

    # Background poller
    poll_interval_s: int = 300
    poll_interval_near_kickoff_s: int = 120
    near_kickoff_h: float = 3.0
    # The poller pulls odds at most this often, and never uses the last
    # `poller_manual_reserve` credits of the daily budget (kept for Refresh).
    poller_odds_interval_s: int = 10800
    poller_manual_reserve: int = 5

    # Trend signals (context only)
    signal_lookback_h: int = 72
    trades_top_n: int = 6  # trades are one call per market, so only for the top candidates
    large_trade_usd: Decimal = Decimal("2500")
    news_move_pts: float = 5.0
    confirm_move_pts: float = 3.0
    trend_flag_x: float = 5.0

    # Guardrails: a row must pass all of these to be a pick (self-imposed defaults)
    min_net_edge_pts: float = 3.0
    min_liquidity_usd: Decimal = Decimal("500")  # $ available at the quoted ask
    max_spread_cents: float = 3.0
    near_start_min: int = 30  # within this many minutes of kickoff...
    near_start_max_age_s: int = 90  # ...only if the data is this fresh
    max_daily_exposure_pct: float = 0.10  # of bankroll, picks + today's open paper bets
    max_sport_exposure_pct: float = 0.06
    top_n: int = 10

    # Ranking weights (score = sum of weight x component in 0..1, minus penalties)
    w_edge: float = 0.40
    w_model: float = 0.20
    w_liquidity: float = 0.15
    w_movement: float = 0.10
    w_freshness: float = 0.15
    edge_full_pts: float = 10.0  # net edge that earns the full edge component
    liquidity_full_usd: Decimal = Decimal("5000")
    p_news: float = 0.30
    p_few_books: float = 0.10
    p_book_disagree: float = 0.10
    p_no_model: float = 0.10
    p_model_against: float = 0.15
    p_early_season: float = 0.05
    p_no_starters: float = 0.05


RANKING_DEFAULTS = {
    k: v
    for k, v in Settings.__dataclass_fields__.items()
    if k.startswith(("min_net", "min_liq", "max_spread", "near_start", "max_daily", "max_sport", "top_n", "w_", "p_", "edge_full", "liquidity_full"))
}


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
        kalshi_max_rps=float(g("KALSHI_MAX_RPS", "1")),
        kalshi_series={s: _series_list(g(f"KALSHI_SERIES_{s.upper()}")) for s in SPORTS},
        odds_api_key=g("ODDS_API_KEY", ""),
        odds_bookmakers=_csv(g("ODDS_BOOKMAKERS"), DEFAULT_BOOKMAKERS),
        book_weights=_weights(g("BOOK_WEIGHTS")),
        odds_min_remaining=int(g("ODDS_MIN_REMAINING", "20")),
        min_books=int(g("MIN_BOOKS", "3")),
        fee_precision=Decimal(g("FEE_PRECISION", "0.01")),
        nfl_tie_prob=float(g("NFL_TIE_PROB", "0.0023")),
        max_data_age_s=int(g("MAX_DATA_AGE_S", "300")),
        max_skew_s=int(g("MAX_SKEW_S", "120")),
        bankroll=Decimal(g("BANKROLL", "1000")),
        kelly_multiplier=float(g("KELLY_MULTIPLIER", "0.25")),
        max_bet_pct=float(g("MAX_BET_PCT", "0.02")),
        db_path=g("DB_PATH", "data/edgefinder.sqlite"),
        refresh_cooldown_s=int(g("REFRESH_COOLDOWN_S", "20")),
        odds_daily_credit_budget=int(g("ODDS_DAILY_CREDIT_BUDGET", "15")),
        poll_interval_s=int(g("POLL_INTERVAL_S", "300")),
        poll_interval_near_kickoff_s=int(g("POLL_INTERVAL_NEAR_KICKOFF_S", "120")),
        near_kickoff_h=float(g("NEAR_KICKOFF_H", "3")),
        poller_odds_interval_s=int(g("POLLER_ODDS_INTERVAL_S", "10800")),
        poller_manual_reserve=int(g("POLLER_MANUAL_RESERVE", "5")),
        signal_lookback_h=int(g("SIGNAL_LOOKBACK_H", "72")),
        trades_top_n=int(g("TRADES_TOP_N", "6")),
        large_trade_usd=Decimal(g("LARGE_TRADE_USD", "2500")),
        news_move_pts=float(g("NEWS_MOVE_PTS", "5")),
        confirm_move_pts=float(g("CONFIRM_MOVE_PTS", "3")),
        trend_flag_x=float(g("TREND_FLAG_X", "5")),
        **{k: _env_value(k, f.default) for k, f in RANKING_DEFAULTS.items() if g(k.upper()) is not None},
    )


def _env_value(name: str, default):
    raw = os.getenv(name.upper())
    return type(default)(raw) if not isinstance(default, Decimal) else Decimal(raw)


# Settings the dashboard's Settings tab may change (persisted in SQLite, see apply_overrides).
EDITABLE = (
    "bankroll", "kelly_multiplier", "max_bet_pct", "min_net_edge_pts", "min_liquidity_usd", "max_spread_cents",
    "near_start_min", "near_start_max_age_s", "max_daily_exposure_pct", "max_sport_exposure_pct", "top_n",
    "max_data_age_s", "w_edge", "w_model", "w_liquidity", "w_movement", "w_freshness",
)


def apply_overrides(settings: Settings, overrides: dict[str, str]) -> Settings:
    """Settings with dashboard overrides applied (typed like the defaults)."""
    from dataclasses import replace

    typed = {}
    for k, v in overrides.items():
        if k in EDITABLE:
            cur = getattr(settings, k)
            typed[k] = Decimal(v) if isinstance(cur, Decimal) else type(cur)(float(v)) if isinstance(cur, int) else type(cur)(v)
    return replace(settings, **typed)
