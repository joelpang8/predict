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
- `edgefinder/pipeline.py`: one pull-and-compute pass (reused by the refresh job)
- `edgefinder/cli.py`: `python -m edgefinder series|markets|edges`
- `tests/`: pytest; `tests/fixtures/` holds recorded live Kalshi responses
- `docs/api-verification.md`: source-of-truth log for external APIs

Build order and status are in `README.md`. Stop after each phase for review.
