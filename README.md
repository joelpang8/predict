# Kalshi Sports Edge Finder

Read-only analysis tool for NFL, NBA and MLB contracts on Kalshi. It compares
Kalshi prices with independent fair-probability estimates and ranks the
best-looking opportunities.

> **Disclaimer:** this is an analysis tool, not financial advice. No edge is
> guaranteed. **It never places orders.**

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Kalshi client + NFL markets, terminal table | done; verified live 2026-10-20 |
| 2 | Odds feed, de-vigging, edge/EV | built; needs an Odds API key for a live run |
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

# 2. The NFL game-winner series is KXNFLGAME (already set in .env.example)

# 3. Show open markets: YES/NO bid and ask (¢), spread, depth at the ask,
#    24h volume, open interest
python -m edgefinder markets --sport nfl
python -m edgefinder markets --sport nfl --limit 20
python -m edgefinder markets --sport nfl --no-book   # skip orderbook calls
```

The implied probability of buying a side is that side's **ask**, not the last
trade. Kalshi's orderbook lists bids only; a side's ask is 1 − the best
opposite-side bid, and "ask qty" is the size there.

## Phase 2: edges

Put your key in `.env` as `ODDS_API_KEY`, then:

```bash
python -m edgefinder edges --sport nfl                 # costs 1 Odds API credit
python -m edgefinder edges --sport nfl --min-edge 3    # only net edge >= 3 points
```

Every Kalshi game market is matched to one sportsbook game, and both sides
(YES and NO) are priced:

- **fair %**: weighted average of de-vigged moneylines (Pinnacle and Betfair
  weighted higher), adjusted for Kalshi paying $0.50 on an NFL tie
- **edge pts**: fair − ask
- **fee ¢/ct**: Kalshi taker fee per contract at the suggested size
- **net pts**: edge after the fee, which is also the EV per contract in dollars
- **size**: quarter Kelly, capped at 2% of bankroll and at the quantity on
  offer at the ask

Games that can't be matched with certainty, have too few books, have
stale data or have already started are skipped. The reason is printed and
appended to `data/unmatched.jsonl`. Credits used and remaining are printed
after every run.

## Tests

```bash
python -m pytest
```
