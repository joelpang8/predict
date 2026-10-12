"""Terminal entry point (phase 1).

    python -m edgefinder series --sport nfl
    python -m edgefinder markets --sport nfl [--series KX...] [--no-book] [--limit 50]
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd

from edgefinder.config import SPORTS, load_settings
from edgefinder.sources.kalshi import KalshiClient, KalshiError, discover_series
from edgefinder.sources.kalshi_models import KalshiMarket, Orderbook

DISCLAIMER = "Analysis tool only - not financial advice. No edge is guaranteed. This app places no orders."


def cents(p: Decimal | None) -> str:
    if p is None:
        return "-"
    c = p * 100
    return f"{c:.0f}" if c == c.to_integral_value() else f"{c:.1f}"


def num(x: Decimal | None) -> str:
    return "-" if x is None else f"{x:,.0f}"


def market_rows(markets: list[KalshiMarket], books: dict[str, Orderbook]) -> pd.DataFrame:
    rows = []
    for m in markets:
        book = books.get(m.ticker)
        yes_ask_depth = book.yes_ask()[1] if book and book.yes_ask() else None
        no_ask_depth = book.no_ask()[1] if book and book.no_ask() else None
        start = m.occurrence_datetime or m.expected_expiration_time
        rows.append(
            {
                "event": m.event_ticker,
                "market": m.ticker,
                "yes =": m.yes_sub_title or m.title,
                "close (UTC)": m.close_time.strftime("%m-%d %H:%M") if m.close_time else "-",
                "exp. expiry": start.strftime("%m-%d %H:%M") if start else "-",
                # Implied probability of a side = its ask (what you would pay), never the last trade.
                "YES ask¢": cents(m.yes_ask),
                "YES bid¢": cents(m.yes_bid),
                "NO ask¢": cents(m.no_ask),
                "NO bid¢": cents(m.no_bid),
                "spread¢": cents(m.yes_spread),
                "YES ask qty": num(yes_ask_depth),
                "NO ask qty": num(no_ask_depth),
                "vol 24h": num(m.volume_24h),
                "OI": num(m.open_interest),
            }
        )
    return pd.DataFrame(rows)


def cmd_series(args, client: KalshiClient) -> int:
    hits = discover_series(client, args.sport)
    if not hits:
        print(f"No series matched {args.sport.upper()}. Find the series ticker on kalshi.com and pass it with `markets --series`.")
        return 1
    df = pd.DataFrame(
        [
            {
                "ticker": s.get("ticker"),
                "title": s.get("title"),
                "frequency": s.get("frequency"),
                "fee_type": s.get("fee_type"),
                "fee_multiplier": s.get("fee_multiplier"),
            }
            for s in hits
        ]
    )
    print(df.to_markdown(index=False))
    print(f"\nPut the game-winner series in .env as KALSHI_SERIES_{args.sport.upper()}=TICKER[,TICKER].")
    return 0


def cmd_markets(args, client: KalshiClient) -> int:
    series = tuple(args.series) if args.series else client.settings.kalshi_series.get(args.sport, ())
    if not series:
        found = discover_series(client, args.sport)
        series = tuple(s["ticker"] for s in found)
        print(f"No KALSHI_SERIES_{args.sport.upper()} configured; using discovered series: {', '.join(series) or 'none'}")
    if not series:
        print("No series to scan.")
        return 1

    markets: list[KalshiMarket] = []
    for s in series:
        markets.extend(client.iter_markets(series_ticker=s, status="open"))
    markets.sort(key=lambda m: (m.close_time or datetime.max.replace(tzinfo=timezone.utc), m.ticker))
    if args.limit:
        markets = markets[: args.limit]

    books = {} if args.no_book else client.get_orderbooks([m.ticker for m in markets])
    fetched = datetime.now(timezone.utc)

    print(DISCLAIMER)
    print(f"Source: {client.settings.kalshi_base_url}  |  fetched {fetched:%Y-%m-%d %H:%M:%S} UTC  |  {len(markets)} open markets\n")
    if not markets:
        print("No open markets in these series right now.")
        return 0
    print(market_rows(markets, books).to_markdown(index=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="edgefinder", description=DISCLAIMER)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("series", help="List Kalshi series that look like this sport")
    p.add_argument("--sport", choices=SPORTS, default="nfl")

    p = sub.add_parser("markets", help="Show open markets with bid/ask and depth")
    p.add_argument("--sport", choices=SPORTS, default="nfl")
    p.add_argument("--series", nargs="*", help="Override series tickers")
    p.add_argument("--no-book", action="store_true", help="Skip orderbook depth calls")
    p.add_argument("--limit", type=int, default=0, help="Show only the first N markets")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    try:
        with KalshiClient(settings) as client:
            return {"series": cmd_series, "markets": cmd_markets}[args.cmd](args, client)
    except KalshiError as e:
        print(f"Kalshi request failed: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
