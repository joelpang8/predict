"""Terminal entry point.

    python -m edgefinder series --sport nfl
    python -m edgefinder markets --sport nfl [--series KX...] [--no-book] [--limit 50]
    python -m edgefinder refresh --sport nfl [--min-edge 3] [--limit 20]   (alias: edges)
    python -m edgefinder show --sport nfl          # cached results, no API calls
    python -m edgefinder poll                      # background poller (Ctrl-C to stop)
    python -m edgefinder paper add TICKER YES|NO [--contracts N]
    python -m edgefinder paper list | settle | stats
    python -m edgefinder history load --sport nfl  # nflverse schedules/results into SQLite
    python -m edgefinder model ratings | eval [--tune] --sport nfl
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd

from edgefinder.config import SPORTS, load_settings
from edgefinder.jobs import poller
from edgefinder.jobs.refresh import RefreshService, View
from edgefinder.matching.matcher import ET
from edgefinder.paper import ledger
from edgefinder.sources.kalshi import KalshiClient, KalshiError, discover_series
from edgefinder.sources.kalshi_models import KalshiMarket, Orderbook
from edgefinder.store.db import Store, parse_ts

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


def _t(ts: str | None) -> str:
    return parse_ts(ts).strftime("%H:%M:%S") if ts else "-"


def edge_table(rows: list[dict]) -> pd.DataFrame:
    out = []
    for r in rows:
        n, stake = Decimal(r["contracts"]), Decimal(r["stake"])
        out.append(
            {
                "game": r["event_ticker"].split("-", 1)[1],
                "bet": f"{r['side']} {r['market_ticker'].rsplit('-', 1)[1]}",
                "means": r["label"],
                "kickoff (ET)": parse_ts(r["kickoff"]).astimezone(ET).strftime("%a %m-%d %H:%M"),
                "ask¢": cents(Decimal(r["ask"])),
                "fair%": f"{r['fair'] * 100:.1f}",
                "edge pts": f"{r['gross_edge'] * 100:+.1f}",
                "fee¢/ct": cents(Decimal(r["fee_per_contract"])),
                "model%": f"{r['model_fair'] * 100:.1f}" if r.get("model_fair") is not None else "-",
                "net pts": f"{r['net_edge'] * 100:+.1f}",
                "EV/$": f"{r['ev_per_dollar'] * 100:+.1f}%",
                "ask qty": num(Decimal(r["ask_depth"])) if r["ask_depth"] else "-",
                "size": f"{n:.0f} (${stake:.2f})" if n else "-",
                "books": len(r["books"].split(",")),
                "updated K / odds (UTC)": f"{_t(r['kalshi_fetched'])} / {_t(r['odds_fetched'])}",
            }
        )
    return pd.DataFrame(out)


def print_view(v: View, min_edge: float | None = None, limit: int = 0) -> None:
    print(DISCLAIMER)
    k = f"{v.kalshi_fetched:%Y-%m-%d %H:%M:%S}" if v.kalshi_fetched else "never"
    o = f"{v.odds_fetched:%Y-%m-%d %H:%M:%S}" if v.odds_fetched else "never"
    print(f"Kalshi data: {k} UTC | odds data: {o} UTC")
    credits = f"Odds credits today: {v.credits_spent_today}/{v.credits_budget}"
    if v.credits_remaining is not None:
        credits += f" | account remaining: {v.credits_remaining}"
    print(credits + (f" | Refresh cooldown: {v.cooldown_remaining_s}s" if v.cooldown_remaining_s else ""))
    for w in v.warnings:
        print(f"WARNING: {w}")
    print()
    rows = [r for r in v.rows if min_edge is None or r["net_edge"] * 100 >= min_edge]
    if rows:
        print(edge_table(rows[:limit] if limit else rows).to_markdown(index=False))
    else:
        print("No rows to show.")
    if v.unmatched:
        print(f"\nSkipped {len(v.unmatched)}:")
        for u in v.unmatched:
            print(f"  - {u['key']}: {u['reason']}")


def service(settings) -> RefreshService:
    return RefreshService(settings, Store(settings.db_path))


def cmd_refresh(args, settings) -> int:
    out = service(settings).refresh(args.sport, trigger="cli")
    if out.status == "cooldown":
        print(f"Cooldown: {out.view.cooldown_remaining_s}s left. Showing cached results.\n")
    elif out.credits_cost:
        print(f"This refresh used {out.credits_cost} odds credit(s).\n")
    print_view(out.view, args.min_edge, args.limit)
    return 0 if out.status in ("ok", "cooldown") else 1


def cmd_show(args, settings) -> int:
    print_view(service(settings).view(args.sport), args.min_edge, args.limit)
    return 0


def cmd_poll(args, settings) -> int:
    print(DISCLAIMER)
    print("Polling (Ctrl-C to stop). Kalshi every "
          f"{settings.poll_interval_s}s ({settings.poll_interval_near_kickoff_s}s within {settings.near_kickoff_h:g}h of kickoff); "
          f"odds at most every {settings.poller_odds_interval_s // 60} min within the daily budget.", flush=True)
    logging.getLogger("edgefinder").setLevel(logging.INFO)
    try:
        poller.run_blocking(service(settings))
    except (KeyboardInterrupt, SystemExit):
        pass
    return 0


def cmd_paper(args, settings) -> int:
    store = Store(settings.db_path)
    now = datetime.now(timezone.utc)
    if args.paper_cmd == "add":
        try:
            n = Decimal(args.contracts) if args.contracts is not None else None
            bet_id = ledger.log_bet(store, settings, args.sport, args.ticker, args.side, now, n, args.notes or "")
        except ledger.PaperError as e:
            print(f"Not logged: {e}", file=sys.stderr)
            return 1
        b = next(b for b in store.paper_bets() if b["id"] == bet_id)
        print(f"Paper bet #{bet_id}: {b['contracts']} x {b['side']} {b['market_ticker']} at {cents(Decimal(b['price']))}c "
              f"+ {cents(Decimal(b['fee_per_contract']))}c fee ({b['label']}), logged {b['created_at']}")
        return 0
    if args.paper_cmd == "settle":
        with KalshiClient(settings) as k:
            done = ledger.settle(store, k, now)
        print(f"Settled {len(done)} bet(s).")
        for b in done:
            print(f"  #{b['id']} {b['label']}: payout {b['payout']} per contract, P&L ${b['pnl']:.2f}")
        return 0
    bets = store.paper_bets()
    if args.paper_cmd == "list":
        if not bets:
            print("No paper bets yet.")
            return 0
        cols = ["id", "created_at", "label", "side", "market_ticker", "contracts", "price", "fee_per_contract",
                "net_edge_at_entry", "status", "result", "pnl", "closing_ask"]
        print(pd.DataFrame(bets)[cols].to_markdown(index=False))
        return 0
    st = ledger.stats(bets)
    print(f"Bets: {st.bets} ({st.open} open, {st.settled} settled)")
    if st.settled:
        print(f"Hit rate: {st.hit_rate:.1%} | staked ${st.staked:.2f} | P&L ${st.pnl:.2f} | ROI {st.roi:+.1%}")
    if st.avg_clv_cents is not None:
        print(f"Avg CLV vs Kalshi closing ask: {st.avg_clv_cents:+.1f}c over {st.clv_count} bet(s)")
    return 0


def cmd_history(args, settings) -> int:
    from edgefinder.history import loader

    n = loader.load(Store(settings.db_path), args.sport)
    print(f"Loaded {n} {args.sport.upper()} games into {settings.db_path}")
    return 0


def _span(s: str) -> tuple[int, int]:
    a, _, b = s.partition("-")
    return int(a), int(b or a)


def cmd_model(args, settings) -> int:
    from edgefinder.models import evaluate
    from edgefinder.models.elo import run
    from edgefinder.models.service import PARAMS

    store = Store(settings.db_path)
    games = store.history(args.sport)
    if not games:
        print("No history loaded. Run: python -m edgefinder history load --sport nfl", file=sys.stderr)
        return 1
    params = PARAMS[args.sport]
    if args.model_cmd == "ratings":
        st = run(games, params)
        rows = sorted(st.ratings.items(), key=lambda kv: -kv[1])
        print(f"Elo after {len(st.history)} games (season {st.season}); {params}")
        print(pd.DataFrame([{"rank": i + 1, "team": t, "elo": round(r)} for i, (t, r) in enumerate(rows)]).to_markdown(index=False))
        return 0
    train, test = _span(args.train), _span(args.test)
    if args.tune:
        params, ll = evaluate.tune(games, *train, base=params)
        print(f"Tuned on {train[0]}-{train[1]}: {params} (train log loss {ll:.4f})")
    r = evaluate.evaluate(games, params, *test)
    print(f"Out-of-sample {test[0]}-{test[1]}, {r.n} games")
    print(f"  Elo log loss {r.model_log_loss:.4f} | Brier {r.model_brier:.4f} | home-rate baseline {r.base_rate_log_loss:.4f}")
    if r.market_n:
        print(f"  Closing moneyline (de-vigged) on {r.market_n} games: {r.market_log_loss:.4f} vs Elo {r.model_log_loss_on_market_games:.4f}"
              " (lower is better)")
    print(pd.DataFrame(r.calibration).round(3).to_markdown(index=False))
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

    for name, hlp in (
        ("refresh", "Pull Kalshi + odds (cooldown, credit budget), recompute and store edges"),
        ("edges", "Alias for refresh"),
        ("show", "Show the last computed results without calling any API"),
    ):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("--sport", choices=["nfl"], default="nfl")
        p.add_argument("--min-edge", type=float, default=None, help="Only rows with net edge >= this many points")
        p.add_argument("--limit", type=int, default=0)

    sub.add_parser("poll", help="Run the background poller in the foreground")

    p = sub.add_parser("paper", help="Paper trading log")
    psub = p.add_subparsers(dest="paper_cmd", required=True)
    pa = psub.add_parser("add", help="Log a paper bet at the latest computed ask")
    pa.add_argument("ticker")
    pa.add_argument("side", choices=["YES", "NO", "yes", "no"])
    pa.add_argument("--contracts", type=str, default=None, help="Default: the suggested size")
    pa.add_argument("--sport", choices=["nfl"], default="nfl")
    pa.add_argument("--notes", default="")
    psub.add_parser("list")
    psub.add_parser("settle", help="Settle finalized markets from Kalshi results")
    psub.add_parser("stats", help="P&L, hit rate, CLV")

    p = sub.add_parser("history", help="Historical data")
    hsub = p.add_subparsers(dest="history_cmd", required=True)
    hl = hsub.add_parser("load", help="Download nflverse schedules/results into SQLite")
    hl.add_argument("--sport", choices=["nfl"], default="nfl")

    p = sub.add_parser("model", help="Elo model")
    msub = p.add_subparsers(dest="model_cmd", required=True)
    mr = msub.add_parser("ratings")
    mr.add_argument("--sport", choices=["nfl"], default="nfl")
    me = msub.add_parser("eval", help="Out-of-sample log loss, Brier, calibration vs closing line")
    me.add_argument("--sport", choices=["nfl"], default="nfl")
    me.add_argument("--train", default="2015-2021")
    me.add_argument("--test", default="2022-2025")
    me.add_argument("--tune", action="store_true", help="Grid-search parameters on the training seasons first")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    try:
        if args.cmd in ("series", "markets"):
            with KalshiClient(settings) as client:
                return {"series": cmd_series, "markets": cmd_markets}[args.cmd](args, client)
        handler = {
            "refresh": cmd_refresh, "edges": cmd_refresh, "show": cmd_show, "poll": cmd_poll, "paper": cmd_paper,
            "history": cmd_history, "model": cmd_model,
        }
        return handler[args.cmd](args, settings)
    except KalshiError as e:
        print(f"Kalshi request failed: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
