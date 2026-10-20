# Kalshi Sports Edge Finder: project rules

## What this is
A read-only tool that finds NFL/NBA/MLB Kalshi contracts priced differently
from an independent fair probability (de-vigged sportsbook consensus plus an
Elo model) and ranks them. Python 3.11+, httpx, pandas, SQLite, Streamlit,
APScheduler, pytest. Secrets live in `.env` (template: `.env.example`), never
committed.

## Hard rules
- **No orders, ever, in v1.** Only HTTP GET to Kalshi, never signed. No
  portfolio/order/account endpoints. `tests/test_kalshi.py` enforces this.
- **Verify, don't recall.** Endpoints, pagination, rate limits and fees come
  from Kalshi's official docs/spec. Record what was checked, and what wasn't,
  in `docs/api-verification.md`. Never estimate the fee formula: until it is
  verified, net edge is not computed.
- **Implied probability = the ask** for the side being bought (never last
  trade). Consider both YES and NO. Kalshi's orderbook is bids only:
  YES ask = 1 − best NO bid.
- **Prices are Decimal dollars** (`*_dollars` strings, sub-penny possible);
  counts are fixed-point (`*_fp`, fractional possible). No floats for money.
- **Matching is conservative.** A wrong game match makes a fake edge. Log
  every unmatched or ambiguous market and skip it; never guess. Every
  matching rule has unit tests.
- **No edges from stale or mismatched data.** Both sources carry
  timestamps, and there's a configurable maximum age.
- Fee formula, guardrail thresholds, Kelly fraction and ranking weights live
  in config / one clearly labelled function each.
- The UI and CLI show the disclaimer: analysis tool, not financial advice, no
  edge guaranteed.

## Layout
- `edgefinder/config.py`: settings from `.env`; official Kalshi hosts
- `edgefinder/sources/kalshi.py`: read-only client (throttle, retry on
  429/5xx with Retry-After, cursor pagination, batch orderbooks), series
  discovery
- `edgefinder/sources/kalshi_models.py`: `KalshiMarket`, `Orderbook` parsing
- `edgefinder/fees.py`: Kalshi fee formula (single source of truth, cites the PDF)
- `edgefinder/devig.py`, `edgefinder/sizing.py`, `edgefinder/edge.py`: de-vig
  consensus, Kelly sizing, edge/EV per side
- `edgefinder/sources/odds_api.py`: The Odds API client (h2h only; logs credits)
- `edgefinder/matching/`: team table (exact aliases only) and the matcher
- `edgefinder/pipeline.py`: fetch_kalshi / fetch_odds / compute steps
- `edgefinder/store/`: SQLite schema + `Store` (runs, Kalshi/odds snapshots,
  edge rows, unmatched log, paper bets). Money as TEXT decimals.
- `edgefinder/jobs/refresh.py`: Refresh job (cooldown, odds cache, daily credit
  budget, never computes from stale or skewed data, keeps last good results)
- `edgefinder/jobs/poller.py`: APScheduler poller (faster near kickoff)
- `edgefinder/paper/ledger.py`: paper bets, settlement (finalized only), stats/CLV
- `edgefinder/history/`: NFL nflverse (nflreadpy), NBA nba_api with a
  Basketball-Reference fallback, MLB Stats API (pybaseball's sources are
  broken: see docs/api-verification.md); `loader.load` (CLI + nightly job)
- `edgefinder/backtest.py`: model backtest; CLV vs our recorded closing lines
- `edgefinder/models/`: Elo (`elo.py`; NFL bye, NBA MOV/back-to-back, MLB
  starter ratings), evaluation/tuning (`evaluate.py`), `ModelService`
  (Kalshi game -> one schedule row, or start time for doubleheaders; NBA may
  use the matched odds event's home/away; else no estimate)
- `edgefinder/signals/trending.py`: line movement (own snapshots first, then
  candlesticks), volume trend, book imbalance, large trades, news/confirming
  flags. Context only: signals never create a pick.
- `edgefinder/scoring.py`: guardrails (eligibility) + transparent weighted
  score + penalties, dedupe (YES A = NO B), exposure caps, reason line,
  "No good bets today" summary
- `edgefinder/dashboard.py` (data helpers) and `ui/streamlit_app.py` (UI).
  Chart colours are the validated dataviz slots 1–3, light and dark.
- Settings tab overrides live in SQLite `app_settings` (`config.apply_overrides`).
- Model results and limits: `docs/model.md`. Elo is a cross-check, not a
  pick source: the closing market beats it out of sample.
- `edgefinder/cli.py`: `python -m edgefinder series|markets|refresh|show|poll|paper|history|model`
- `tests/`: pytest; `tests/fixtures/` holds recorded live Kalshi responses
- `docs/api-verification.md`: source-of-truth log for external APIs

Build order and status are in `README.md`. Stop after each phase for review.
