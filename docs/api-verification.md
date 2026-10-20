# API verification record

What was checked, against what source, and what is still unverified.
Update this file whenever an assumption is confirmed or changes.

## Kalshi

Sources:
- docs.kalshi.com, read 2026-10-20: `getting_started/api_environments`,
  `quick_start_market_data`, `pagination`, `orderbook_responses`,
  `rate_limits`, `fee_rounding`, `fixed_point_migration`, `llms-full.txt`.
- Kalshi's official SDK `kalshi_python_sync` 3.33.0 (generated from their
  OpenAPI spec), for per-endpoint parameters and auth.
- Live calls to production on 2026-10-20 (recorded in `tests/fixtures/`).

### Verified

| Item | Finding |
|---|---|
| Prod host | `https://external-api.kalshi.com/trade-api/v2` (recommended); `https://api.elections.kalshi.com/trade-api/v2` also supported. Used live. |
| Demo host | `https://external-api.demo.kalshi.co/trade-api/v2`; `https://demo-api.kalshi.co/trade-api/v2` also supported. |
| Auth for market data | None needed. Docs: "public endpoints that don't require API keys"; spec: empty auth settings for series, events, markets, orderbook(s), trades, candlesticks. Confirmed live. |
| Auth (not used) | `KALSHI-ACCESS-KEY`, `-SIGNATURE` (RSA-PSS/SHA-256 or Ed25519 over `timestamp + METHOD + path`, no query), `-TIMESTAMP` (ms). v1 never signs. |
| Pagination | Cursor-based: pass `cursor` back; stop when it is null/empty. `/markets` and `/markets/trades`: `limit` default 100, max 1000. `/events`: max 200. `/series` not paginated. |
| Prices / counts | Dollar strings (`*_dollars`, sub-penny possible); counts `*_fp` (fractional possible). NFL game markets: `price_level_structure=linear_cent`, 1¢ ticks. |
| Orderbook | `orderbook_fp.yes_dollars` / `.no_dollars`, bids only, `[price, count]`. YES ask = 1 − best NO bid, same size. **Live and docs order is ascending (best last); the SDK docstring says the opposite, so we sort ourselves.** Live check: implied asks equal the market's `yes_ask_dollars` / `no_ask_dollars`. |
| Batch orderbooks | `GET /markets/orderbooks`, 1–100 tickers as repeated `tickers=`. Used live. |
| NFL game series | `KXNFLGAME` ("NFL Game"), `fee_type=quadratic_with_maker_fees`, `fee_multiplier=1`. One event per game (`KXNFLGAME-26OCT22NECHI`, `mutually_exclusive=true`), one market per team (`…-CHI`, `yes_sub_title` = team). |
| Game time | **No kickoff field.** `occurrence_datetime` = `expected_expiration_time` (looks like kickoff + 3h; not documented). `rules_primary` gives the scheduled *date*; `close_time` is days later. Matching must not treat any of these as kickoff. |
| Rate limits (authenticated) | Token buckets: most requests cost 10 tokens; Basic tier Read budget 200 tokens/s (20 req/s), 3 s burst. 429 body `{"error":"too many requests"}`. **429 has no `Retry-After` header**; the docs say to use exponential backoff. |

### Not verified / open

| Item | Status |
|---|---|
| **Fee formula** | **Blocking.** The only official source is the Fee Schedule (`kalshi.com/fee-schedule`, `kalshi.com/docs/kalshi-fee-schedule.pdf`). Both sit behind a Vercel bot check that rejects this environment (HTTP 429, "Code 21"). The help center just links to it. Known from official sources: fee type and multiplier per series, event overrides, trade fee rounded **up to $0.000001** (Fee Rounding doc), standard maker multiplier `0.25` (changelog). The formula itself is not implemented. |
| Unauthenticated rate limit | Undocumented. Live: about 6 calls in ~2 s triggered a 429. The client defaults to 2 req/s and backs off exponentially (2, 4, 8, 16 s). |

## The Odds API (phase 2; read 2026-10-20 from the-odds-api.com/liveapi/guides/v4)

| Item | Finding |
|---|---|
| Free tier | 500 credits per month. |
| Cost | `/v4/sports/{sport}/odds`: 1 credit per market per region (`markets=h2h&regions=us` = 1). With `bookmakers=`, every group of 10 bookmakers counts as 1 region. No events returned = no charge. `/sports` is free. |
| Usage headers | `x-requests-remaining`, `x-requests-used`, `x-requests-last` (cost of this call). |
| Rate limit | 429 on bursts; "space out requests over several seconds". |
| Sport keys | `americanfootball_nfl`, `basketball_nba`, `baseball_mlb`. |
| Sharp books | Pinnacle = `pinnacle`, region `eu` ("odds are from public website which may incur a delay"). Circa is not listed. US region has `lowvig`, `betonlineag`, `draftkings`, `fanduel`. |
| Budget | `bookmakers=` with ≤10 books (Pinnacle plus US books) costs 1 credit per sport per refresh. 500/month is about 16 per day across all sports, so the refresh cooldown alone won't protect the quota; phase 2 needs a credit budget. |
