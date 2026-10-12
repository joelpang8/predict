"""Parsed Kalshi market-data objects.

Field names follow Kalshi's official OpenAPI spec (kalshi_python_sync 3.33.0):
prices are fixed-point dollar strings (`*_dollars`, up to 6 decimals, sub-penny
possible) and counts are fixed-point strings (`*_fp`, 2 decimals, fractional
contracts possible). We parse them to Decimal and never to float.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

ONE = Decimal("1")
ZERO = Decimal("0")


def dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def ts(value: Any) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def quote(price: Decimal | None) -> Decimal | None:
    """A usable price is strictly between 0 and 1 dollar; anything else means no quote."""
    if price is None or price <= ZERO or price >= ONE:
        return None
    return price


@dataclass(frozen=True)
class KalshiMarket:
    ticker: str
    event_ticker: str
    market_type: str
    status: str
    title: str
    yes_sub_title: str
    no_sub_title: str
    open_time: datetime | None
    close_time: datetime | None
    expected_expiration_time: datetime | None
    occurrence_datetime: datetime | None
    yes_bid: Decimal | None
    yes_ask: Decimal | None
    no_bid: Decimal | None
    no_ask: Decimal | None
    yes_ask_size: Decimal | None
    last_price: Decimal | None
    volume: Decimal | None
    volume_24h: Decimal | None
    open_interest: Decimal | None
    result: str
    rules_primary: str
    raw: dict

    @classmethod
    def from_api(cls, m: dict) -> "KalshiMarket":
        return cls(
            ticker=m["ticker"],
            event_ticker=m["event_ticker"],
            market_type=m.get("market_type", ""),
            status=m.get("status", ""),
            title=m.get("title") or "",
            yes_sub_title=m.get("yes_sub_title") or "",
            no_sub_title=m.get("no_sub_title") or "",
            open_time=ts(m.get("open_time")),
            close_time=ts(m.get("close_time")),
            expected_expiration_time=ts(m.get("expected_expiration_time")),
            occurrence_datetime=ts(m.get("occurrence_datetime")),
            yes_bid=quote(dec(m.get("yes_bid_dollars"))),
            yes_ask=quote(dec(m.get("yes_ask_dollars"))),
            no_bid=quote(dec(m.get("no_bid_dollars"))),
            no_ask=quote(dec(m.get("no_ask_dollars"))),
            yes_ask_size=dec(m.get("yes_ask_size_fp")),
            last_price=dec(m.get("last_price_dollars")),
            volume=dec(m.get("volume_fp")),
            volume_24h=dec(m.get("volume_24h_fp")),
            open_interest=dec(m.get("open_interest_fp")),
            result=m.get("result") or "",
            rules_primary=m.get("rules_primary") or "",
            raw=m,
        )

    @property
    def yes_spread(self) -> Decimal | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.yes_bid


@dataclass(frozen=True)
class Orderbook:
    """Bids only, as Kalshi returns them. Asks are implied:
    a YES bid at p is a NO ask at 1 - p, with the same size (per the official docs)."""

    ticker: str
    yes_bids: tuple[tuple[Decimal, Decimal], ...]  # (price, size)
    no_bids: tuple[tuple[Decimal, Decimal], ...]

    @classmethod
    def from_api(cls, ticker: str, book: dict | None) -> "Orderbook":
        book = book or {}

        def levels(key: str) -> tuple[tuple[Decimal, Decimal], ...]:
            out = []
            for level in book.get(key) or []:
                price, size = dec(level[0]), dec(level[1])
                if price is not None and size is not None and size > ZERO:
                    out.append((price, size))
            # Sort ourselves (best = highest bid) rather than trusting response order.
            return tuple(sorted(out, key=lambda lv: lv[0], reverse=True))

        return cls(ticker=ticker, yes_bids=levels("yes_dollars"), no_bids=levels("no_dollars"))

    def best_yes_bid(self) -> tuple[Decimal, Decimal] | None:
        return self.yes_bids[0] if self.yes_bids else None

    def best_no_bid(self) -> tuple[Decimal, Decimal] | None:
        return self.no_bids[0] if self.no_bids else None

    def yes_ask(self) -> tuple[Decimal, Decimal] | None:
        """(price, size) to buy YES: implied by the best NO bid."""
        b = self.best_no_bid()
        return (ONE - b[0], b[1]) if b else None

    def no_ask(self) -> tuple[Decimal, Decimal] | None:
        """(price, size) to buy NO: implied by the best YES bid."""
        b = self.best_yes_bid()
        return (ONE - b[0], b[1]) if b else None
