# API verification record

What was checked, against what source, and what is still unverified.
Update this file whenever an assumption is confirmed or changes.

## Source used (2026-10-12)

`docs.kalshi.com` and `kalshi.com` were not reachable from the build
environment. The official source available was Kalshi's own Python SDK,
**`kalshi_python_sync` 3.33.0** on PyPI (author: Kalshi, support@kalshi.com;
homepage github.com/Kalshi/exchange-infra), which is generated from Kalshi's
OpenAPI spec (API version 3.33.0). Everything marked "verified" below was
read from that spec's generated code and docstrings.

## Verified

| Item | Finding |
|---|---|
| Prod host | `https://external-api.kalshi.com/trade-api/v2` (default). `https://api.elections.kalshi.com/trade-api/v2` is listed as "Production shared API server, also supported". |
| Demo host | `https://external-api.demo.kalshi.co/trade-api/v2`; `https://demo-api.kalshi.co/trade-api/v2` "also supported". |
| Auth for market data | None. `GET /series`, `/series/{t}`, `/events`, `/events/{t}`, `/markets`, `/markets/{t}`, `/markets/{t}/orderbook`, `/markets/orderbooks`, `/markets/trades`, candlesticks: empty auth settings. |
| Auth (not used) | Signed requests use `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE` (RSA-PSS/SHA-256 or Ed25519 over `timestamp + METHOD + path`), `KALSHI-ACCESS-TIMESTAMP` (ms). v1 never signs anything. |
| Pagination | Cursor-based. Pass the response `cursor` back as `cursor`; an empty cursor means no more pages. `/markets` and `/markets/trades`: `limit` default 100, max 1000. `/events`: default 200, max 200. `/series` is not paginated. |
| `/markets` filters | `series_ticker`, `event_ticker`, `tickers` (comma-separated), `status` ∈ `unopened, open, closed, settled` (one at a time), timestamp filters with documented incompatibilities. |
| Prices | Fixed-point **dollar strings** (`yes_ask_dollars`, etc.), up to 6 decimals, sub-penny possible; tick sizes per market in `price_ranges`. Integer-cent fields are gone. |
| Counts | Fixed-point strings `*_fp`, 2 decimals; fractional contracts possible. |
| Orderbook | Returns **bids only** for YES and NO (`orderbook_fp.yes_dollars`, `.no_dollars`, each `[price, count]`). A YES bid at p = a NO ask at 1−p, same size. `depth` 0 = all levels. |
| Batch orderbooks | `GET /markets/orderbooks`, 1–100 tickers, sent as repeated `tickers=` params. |
| Trades | `taker_outcome_side` (`taker_side` deprecated), `count_fp`, `yes_price_dollars`, `created_time`, `is_block_trade`. |
| Fee parameters | Each series has `fee_type` ∈ `quadratic, quadratic_with_maker_fees, quadratic_with_combo_maker_fees, flat` and a `fee_multiplier`; events can override both (`fee_type_override`, `fee_multiplier_override`). `GET /series/fee_changes` lists scheduled changes. Markets may carry `fee_waiver_expiration_time`. |

## NOT verified — blocking or handled conservatively

| Item | Status |
|---|---|
| **Fee formula** | **Not found in a reachable official source.** The spec says the formulas are in `https://kalshi.com/docs/kalshi-fee-schedule.pdf` ("General Trading Fees Table"), which was blocked. No fee formula is implemented; phase 2 (net edge) is blocked until the PDF is read. |
| Rate limits | The spec links to `docs.kalshi.com/getting_started/rate_limits` (blocked) and says limits vary by tier. The client uses a self-imposed `KALSHI_MAX_RPS` (default 5) and honours 429 + `Retry-After`. `GET /account/limits` exists but needs auth, so we don't call it. |
| Sports series tickers | Not hard-coded. `python -m edgefinder series --sport nfl` discovers candidates; the user confirms them in `.env`. The category label "Sports" is unconfirmed, so discovery falls back to all series. |
| Live response shapes | Tests use hand-built payloads in the spec's shape, not recorded responses. Record real ones on first live run. |
