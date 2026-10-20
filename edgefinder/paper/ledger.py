"""Paper trading: log hypothetical bets from computed rows, settle them from
Kalshi results, and report P&L, hit rate and closing line value (CLV).

Nothing here talks to an order endpoint; settlement only reads public market data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from edgefinder.config import Settings
from edgefinder.fees import taker_fee_per_contract
from edgefinder.sources.kalshi import KalshiClient
from edgefinder.store.db import Store, iso, parse_ts


class PaperError(ValueError):
    pass


def log_bet(
    store: Store,
    settings: Settings,
    sport: str,
    market_ticker: str,
    side: str,
    now: datetime,
    contracts: Decimal | None = None,
    notes: str = "",
) -> int:
    """Record a paper bet at the latest computed ask. Refuses stale or started games."""
    side = side.upper()
    if side not in ("YES", "NO"):
        raise PaperError("side must be YES or NO")
    run = store.last_run(sport, edges_only=True)
    if not run:
        raise PaperError("no computed edges yet; refresh first")
    row = next((r for r in store.edge_rows(run["id"]) if r["market_ticker"] == market_ticker and r["side"] == side), None)
    if row is None:
        raise PaperError(f"{market_ticker} {side} is not in the latest results")
    oldest = min(parse_ts(row["kalshi_fetched"]), parse_ts(row["odds_fetched"]))
    if (now - oldest).total_seconds() > settings.max_data_age_s:
        raise PaperError("latest prices are stale; refresh before logging a bet")
    if parse_ts(row["kickoff"]) <= now:
        raise PaperError("game has started")

    n = Decimal(contracts) if contracts is not None else Decimal(row["contracts"])
    if n <= 0:
        raise PaperError("suggested size is 0 (no positive edge); pass a contract count to log it anyway")
    price = Decimal(row["ask"])
    fee_pc = taker_fee_per_contract(
        price, n, multiplier=Decimal(row["fee_multiplier"]), fee_type=row["fee_type"], precision=settings.fee_precision
    )
    return store.add_paper_bet(
        created_at=iso(now),
        sport=sport,
        event_ticker=row["event_ticker"],
        market_ticker=market_ticker,
        side=side,
        label=row["label"],
        kickoff=row["kickoff"],
        price=str(price),
        fee_per_contract=str(fee_pc),
        contracts=str(n),
        fair_at_entry=row["fair"],
        net_edge_at_entry=row["fair"] - float(price) - float(fee_pc),
        source_run_id=run["id"],
        notes=notes,
    )


def yes_settlement_value(market: dict) -> Decimal | None:
    """Dollar value of one YES contract at settlement, or None if not final.

    Per docs.kalshi.com Market Lifecycle: only `finalized` is paid out (a
    `determined` result can still be disputed). `result` is yes, no or scalar;
    NFL ties settle as $0.50 per side (market rules_secondary).
    """
    if market.get("status") != "finalized":
        return None
    result = market.get("result")
    if result == "yes":
        return Decimal(1)
    if result == "no":
        return Decimal(0)
    if result == "scalar" and market.get("settlement_value_dollars") is not None:
        return Decimal(str(market["settlement_value_dollars"]))
    return None


def settle(store: Store, kalshi: KalshiClient, now: datetime) -> list[dict]:
    settled = []
    for bet in store.paper_bets(status="open"):
        market = kalshi.get_market(bet["market_ticker"])
        yv = yes_settlement_value(market)
        if yv is None:
            continue
        payout = yv if bet["side"] == "YES" else 1 - yv
        n, price, fee = Decimal(bet["contracts"]), Decimal(bet["price"]), Decimal(bet["fee_per_contract"])
        pnl = n * (payout - price - fee)
        closing = store.closing_ask(bet["market_ticker"], bet["side"], parse_ts(bet["kickoff"]))
        store.update_paper_bet(
            bet["id"],
            status="settled",
            settled_at=iso(now),
            result=market.get("result"),
            payout_per_contract=str(payout),
            pnl=str(pnl),
            closing_ask=None if closing is None else str(closing),
        )
        settled.append({**bet, "payout": payout, "pnl": pnl, "closing_ask": closing})
    return settled


@dataclass
class Stats:
    bets: int
    open: int
    settled: int
    wins: int
    staked: Decimal
    pnl: Decimal
    clv_count: int
    avg_clv_cents: float | None

    @property
    def hit_rate(self) -> float | None:
        return self.wins / self.settled if self.settled else None

    @property
    def roi(self) -> float | None:
        return float(self.pnl / self.staked) if self.staked else None


def stats(bets: list[dict]) -> Stats:
    settled = [b for b in bets if b["status"] == "settled"]
    wins = sum(Decimal(b["payout_per_contract"]) > Decimal(b["price"]) + Decimal(b["fee_per_contract"]) for b in settled)
    staked = sum((Decimal(b["contracts"]) * (Decimal(b["price"]) + Decimal(b["fee_per_contract"])) for b in settled), Decimal(0))
    pnl = sum((Decimal(b["pnl"]) for b in settled), Decimal(0))
    # CLV: how much the market moved toward us before kickoff (closing ask - our price), in cents.
    clvs = [float(Decimal(b["closing_ask"]) - Decimal(b["price"])) * 100 for b in settled if b.get("closing_ask")]
    return Stats(
        bets=len(bets),
        open=len(bets) - len(settled),
        settled=len(settled),
        wins=wins,
        staked=staked,
        pnl=pnl,
        clv_count=len(clvs),
        avg_clv_cents=sum(clvs) / len(clvs) if clvs else None,
    )
