"""Crowd / trend signals from public market data. Context only, never pick generators.

Kalshi does not expose individual users' positions, so everything here is a
public proxy (docs.kalshi.com: candlesticks, trades, orderbook):

- line movement 1h / 6h / 24h: change in the ask of the side being bought,
  from our own stored snapshots when we have one near the target time,
  otherwise from Kalshi's hourly candlesticks. Positive = the market moved
  toward this side (its price rose).
- trend score: volume in the last `trend_recent_h` hours vs this market's own
  average for a window that long over the lookback (from candlesticks).
- order book imbalance: resting bids within `imbalance_window` of the best
  bid, YES vs NO, scaled to -1..+1 for this side.
- large trades: trades in the last 24 h with notional >= `large_trade_usd`,
  and which side the taker bought.
- flags: "check for news" when the market moved sharply against a side we
  rate as underpriced; "confirming" when it moved toward it with book support.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from edgefinder.sources.kalshi_models import Orderbook, dec


@dataclass(frozen=True)
class SignalConfig:
    lookback_h: int = 72
    trend_recent_h: int = 3
    min_baseline_h: int = 12  # need this much history before a trend score means anything
    imbalance_window: Decimal = Decimal("0.05")
    large_trade_usd: Decimal = Decimal("2500")
    news_move_pts: float = 5.0
    confirm_move_pts: float = 3.0
    trend_flag_x: float = 5.0  # flag volume only above this multiple (sports volume is naturally bursty)


@dataclass(frozen=True)
class Candle:
    end: datetime
    yes_ask: Decimal | None
    yes_bid: Decimal | None
    volume: Decimal

    @classmethod
    def from_api(cls, c: dict) -> "Candle":
        from datetime import timezone

        return cls(
            end=datetime.fromtimestamp(c["end_period_ts"], tz=timezone.utc),
            yes_ask=dec((c.get("yes_ask") or {}).get("close_dollars")),
            yes_bid=dec((c.get("yes_bid") or {}).get("close_dollars")),
            volume=dec(c.get("volume_fp")) or Decimal(0),
        )

    def side_ask(self, side: str) -> Decimal | None:
        if side == "YES":
            return self.yes_ask if self.yes_ask and 0 < self.yes_ask < 1 else None
        return 1 - self.yes_bid if self.yes_bid and 0 < self.yes_bid < 1 else None


@dataclass(frozen=True)
class Trade:
    time: datetime
    count: Decimal
    yes_price: Decimal
    taker_side: str  # "yes" | "no": the outcome the taker bought

    @classmethod
    def from_api(cls, t: dict) -> "Trade":
        return cls(
            time=datetime.fromisoformat(t["created_time"].replace("Z", "+00:00")),
            count=dec(t.get("count_fp")) or Decimal(0),
            yes_price=dec(t.get("yes_price_dollars")) or Decimal(0),
            taker_side=(t.get("taker_outcome_side") or t.get("taker_side") or "").lower(),
        )

    @property
    def notional(self) -> Decimal:
        price = self.yes_price if self.taker_side == "yes" else 1 - self.yes_price
        return self.count * price


@dataclass
class Signals:
    move_1h: float | None = None  # points (cents) toward this side
    move_6h: float | None = None
    move_24h: float | None = None
    trend_score: float | None = None  # recent volume / normal
    imbalance: float | None = None  # -1..+1, positive = book leans to this side
    large_trades: int | None = None  # None = not fetched
    large_trade_max_usd: Decimal | None = None
    large_trades_with: int = 0  # bought this side
    flags: list[str] = field(default_factory=list)


def price_at(
    candles: list[Candle], side: str, t: datetime, snapshot_lookup=None, tolerance: timedelta | None = None
) -> Decimal | None:
    """Ask for `side` as of time t: own snapshot near t if available, else last candle ending at or before t."""
    if snapshot_lookup:
        snap = snapshot_lookup(side, t, tolerance or timedelta(minutes=10))
        if snap is not None:
            return snap
    best = None
    for c in candles:
        if c.end <= t and c.side_ask(side) is not None and (best is None or c.end > best.end):
            best = c
    return best.side_ask(side) if best else None


def trend_score(candles: list[Candle], now: datetime, cfg: SignalConfig) -> float | None:
    recent_start = now - timedelta(hours=cfg.trend_recent_h)
    recent = sum((c.volume for c in candles if c.end > recent_start), Decimal(0))
    older = [c for c in candles if c.end <= recent_start]
    if len(older) < cfg.min_baseline_h:
        return None
    per_window = sum((c.volume for c in older), Decimal(0)) / len(older) * cfg.trend_recent_h
    if per_window <= 0:
        return None
    return float(recent / per_window)


def book_imbalance(book: Orderbook | None, side: str, window: Decimal) -> float | None:
    if book is None or not book.yes_bids or not book.no_bids:
        return None

    def depth(levels):
        best = levels[0][0]
        return sum((size for price, size in levels if price >= best - window), Decimal(0))

    y, n = depth(book.yes_bids), depth(book.no_bids)
    if y + n == 0:
        return None
    imb = float((y - n) / (y + n))
    return imb if side == "YES" else -imb


def compute(
    side: str,
    current_ask: Decimal,
    gross_edge: float,
    candles: list[Candle],
    book: Orderbook | None,
    trades: list[Trade] | None,
    now: datetime,
    cfg: SignalConfig = SignalConfig(),
    snapshot_lookup=None,
) -> Signals:
    s = Signals()
    for hours, attr in ((1, "move_1h"), (6, "move_6h"), (24, "move_24h")):
        tol = timedelta(minutes=max(10, hours * 6))
        past = price_at(candles, side, now - timedelta(hours=hours), snapshot_lookup, tol)
        if past is not None:
            setattr(s, attr, float(current_ask - past) * 100)
    s.trend_score = trend_score(candles, now, cfg)
    s.imbalance = book_imbalance(book, side, cfg.imbalance_window)

    if trades is not None:
        cutoff = now - timedelta(hours=24)
        big = [t for t in trades if t.time >= cutoff and t.notional >= cfg.large_trade_usd]
        s.large_trades = len(big)
        s.large_trade_max_usd = max((t.notional for t in big), default=None)
        s.large_trades_with = sum(t.taker_side == side.lower() for t in big)

    moves = [m for m in (s.move_1h, s.move_6h) if m is not None]
    if gross_edge > 0 and moves and min(moves) <= -cfg.news_move_pts:
        s.flags.append(f"check for news: price fell {-min(moves):.0f} pts against this side")
    if moves and max(moves) >= cfg.confirm_move_pts and (s.imbalance or 0) > 0:
        s.flags.append("confirming: price and book moving toward this side")
    if s.trend_score is not None and s.trend_score >= cfg.trend_flag_x:
        s.flags.append(f"volume {s.trend_score:.1f}x normal")
    if s.large_trades:
        s.flags.append(f"{s.large_trades} large trade(s), {s.large_trades_with} bought this side")
    return s
