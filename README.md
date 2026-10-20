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
| 2 | Odds feed, de-vigging, edge/EV | done; verified live 2026-10-20 |
| 3 | SQLite snapshots, refresh job + cooldown, paper-trade log | done; verified live 2026-10-20 |
| 4 | Historical data + Elo (NFL first) | done; see docs/model.md |
| 5 | Trending signals | done; verified live 2026-10-20 |
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
python -m edgefinder refresh --sport nfl               # costs 1 Odds API credit (alias: edges)
python -m edgefinder refresh --sport nfl --min-edge 3  # only net edge >= 3 points
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
stale data or have already started are skipped, and the reason is printed
and stored. Credits used today and remaining on the account are printed
after every run.

## Phase 3: refresh, history, paper trading

Everything is stored in `data/edgefinder.sqlite` (git-ignored).

```bash
python -m edgefinder refresh --sport nfl   # pull + recompute (20 s cooldown, daily credit budget)
python -m edgefinder show --sport nfl      # last results, no API calls
python -m edgefinder poll                  # background poller, Ctrl-C to stop

python -m edgefinder paper add KXNFLGAME-26OCT25SFATL-ATL YES             # suggested size
python -m edgefinder paper add KXNFLGAME-26OCT25SFATL-ATL YES --contracts 10
python -m edgefinder paper list
python -m edgefinder paper settle          # settles markets Kalshi has finalized
python -m edgefinder paper stats           # P&L, hit rate, CLV
```

- **Cooldown:** a refresh within 20 s of the last one makes no API calls and
  shows the cached results with the seconds remaining.
- **Credits:** odds pulls stop for the day (US/Eastern) at
  `ODDS_DAILY_CREDIT_BUDGET` (default 15, about 500 a month). Kalshi keeps
  refreshing.
- **Stale data:** if a source fails, or the newest odds are more than
  `MAX_SKEW_S` older than the Kalshi prices, edges are **not** recomputed. The
  last good results stay on screen with a warning, marked STALE once older
  than `MAX_DATA_AGE_S`. Every row shows when its Kalshi and odds data were
  pulled.
- **Poller:** refreshes Kalshi every 5 min (2 min within 3 h of a kickoff),
  storing a price snapshot each time. It pulls odds at most every 3 h and
  never uses the last 5 credits of the daily budget.
- **Paper bets** record the ask, fee, size and fair value at the time. They
  settle only when Kalshi marks the market `finalized` (a tie pays $0.50).
  CLV compares your price with the last stored Kalshi ask before kickoff.

## Phase 4: history and the Elo model

```bash
python -m edgefinder history load --sport nfl   # nflverse results, 1999 to now (also nightly via poll)
python -m edgefinder model ratings              # current Elo ratings
python -m edgefinder model eval                 # out-of-sample log loss/calibration vs closing line
python -m edgefinder model eval --tune --train 2015-2021 --test 2022-2025
```

`refresh` now adds a **model%** column: the Elo estimate for that side,
next to the sportsbook consensus (**fair%**). The closing betting line is
more accurate than Elo (log loss 0.608 vs 0.633 on 2022–2025), so model%
is a cross-check, not a pick generator. See [`docs/model.md`](docs/model.md).

## Phase 5: trend signals (context, never picks)

Kalshi doesn't publish anyone's positions, so these are public proxies. Each
`refresh` adds them to every row:

| Column / flag | Meaning | Source |
|---|---|---|
| **move 1h/6h/24h** | Change in this side's ask, in points. Positive means the market moved toward this side. | Our stored snapshots when one is close to the target time, otherwise Kalshi hourly candlesticks |
| **vol x** | Volume in the last 3 h vs this market's normal 3 h over the last 72 h | Candlesticks |
| **book** | Resting bids within 5¢ of the best, this side vs the other, from −1 to +1 | Orderbook |
| large trades | Trades of $2,500+ in the last 24 h, and how many bought this side | Trades, for the top 6 rows by net edge |
| **check for news** | A side we rate underpriced fell 5+ pts in 1 h or 6 h: the market may know something | |
| **confirming** | This side rose 3+ pts with the book leaning its way | |

Thresholds are in `.env` (`SIGNAL_*`, `TRADES_TOP_N`, `LARGE_TRADE_USD`,
`NEWS_MOVE_PTS`, `CONFIRM_MOVE_PTS`, `TREND_FLAG_X`). Kalshi's public rate
limit is tight, so the client sends at most 1 request a second and a refresh
takes about 20–30 s. If the signal calls fail, edges are still shown, with a
warning.

## Tests

```bash
python -m pytest
```
