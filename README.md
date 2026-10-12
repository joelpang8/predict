# Kalshi Sports Edge Finder

Read-only analysis tool for NFL, NBA and MLB contracts on Kalshi. It compares
Kalshi prices with independent fair-probability estimates and ranks the
best-looking opportunities.

> **Disclaimer:** this is an analysis tool, not financial advice. No edge is
> guaranteed. **It never places orders.**

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Kalshi client + NFL markets, terminal table | built; not yet run live (see below) |
| 2 | Odds feed, de-vigging, edge/EV | blocked on the official fee schedule |
| 3 | SQLite snapshots, refresh job + cooldown, paper-trade log | — |
| 4 | Historical data + Elo (NFL first) | — |
| 5 | Trending signals | — |
| 6 | Streamlit dashboard, Refresh, Top Picks | — |
| 7 | NBA + MLB, backtesting, CLV | — |

What was checked against Kalshi's official sources is in
[`docs/api-verification.md`](docs/api-verification.md).

## Setup

Python 3.11+.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # edit as needed; never commit .env
```

No Kalshi API key is needed: every endpoint used is public market data.
Set `KALSHI_ENV=demo` to use Kalshi's demo environment.

## Phase 1: run it

```bash
# 1. Find the NFL series tickers (titles, fee type and fee multiplier shown)
python -m edgefinder series --sport nfl

# 2. Put the game-winner series in .env, e.g. KALSHI_SERIES_NFL=<TICKER>
#    (or pass --series on the command line)

# 3. Show open markets: YES/NO bid and ask (¢), spread, depth at the ask,
#    24h volume, open interest
python -m edgefinder markets --sport nfl
python -m edgefinder markets --sport nfl --series <TICKER> --limit 20
python -m edgefinder markets --sport nfl --no-book   # skip orderbook calls
```

The implied probability of buying a side is that side's **ask**, not the last
trade. Kalshi's orderbook lists bids only; a side's ask is 1 − the best
opposite-side bid, and "ask qty" is the size there.

## Tests

```bash
python -m pytest
```
