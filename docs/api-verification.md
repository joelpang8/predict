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
| NBA / MLB game series | `KXNBAGAME` ("NBA Game"; `quadratic_with_maker_fees`, multiplier 1) and `KXMLBGAME` ("Professional Baseball Game"; multiplier **0.5**). `GET /series/fee_changes?series_ticker=KXMLBGAME&show_historical=true`: 1 from 2025-10-04, **0.5 from 2026-08-07**, after the fee-schedule PDF (July 7) that still lists 1. We use the live value. Team codes: all 30 NBA from open markets; all 30 MLB from 659 settled 2026 events (e.g. `AZ`, `ATH`, `CWS`, `WSH`, `KC`, `SD`, `SF`, `TB`). |
| MLB tickers | `KXMLBGAME-26OCT272000MILTB`: date + **US/Eastern start time** (HHMM) + teams; doubleheaders add `G1`/`G2` (e.g. `KXMLBGAME-26SEP041410DETCLEG1` and `…1915…G2`, matching MLB's 14:10 and 19:15 ET starts). `expected_expiration_time` = start + 3h. The matcher checks the odds kickoff within 20 min of the ticker time. |
| Postponements | NFL/NBA: a game starting within 48h of schedule resolves normally, otherwise at a fair price. MLB: the market stays open for a reschedule within two days, otherwise fair price. |
| Game time | **No kickoff field.** `occurrence_datetime` = `expected_expiration_time` (looks like kickoff + 3h; not documented). `rules_primary` gives the scheduled *date*; `close_time` is days later. Matching must not treat any of these as kickoff. |
| Rate limits (authenticated) | Token buckets: most requests cost 10 tokens; Basic tier Read budget 200 tokens/s (20 req/s), 3 s burst. 429 body `{"error":"too many requests"}`. **429 has no `Retry-After` header**; the docs say to use exponential backoff. |
| Candlesticks | `GET /markets/candlesticks`: `market_tickers` (≤100, comma-separated), `start_ts`, `end_ts`, `period_interval` 1/60/1440; ≤10,000 candles per call; no auth. Each candle has `end_period_ts`, `volume_fp`, `open_interest_fp`, `yes_ask`/`yes_bid`/`price` OHLC `*_dollars`. No NO-side fields, so NO ask = 1 − YES bid. Confirmed live. |
| Trades | `GET /markets/trades?ticker&min_ts&limit≤1000&cursor`: `count_fp`, `yes_price_dollars`, `taker_outcome_side`, `created_time`, `is_block_trade`. Confirmed live. |
| Market lifecycle | docs `market_lifecycle`: statuses `initialized, active, inactive, closed, determined, disputed, amended, finalized`; `result` is `yes`, `no` or `scalar`; only `finalized` is paid out. Paper bets settle on `finalized` only. NFL ties settle at $0.50 (`rules_secondary`). |

### Fees (verified 2026-10-20)

Source: Kalshi Fee Schedule PDF, "Last updated and effective: July 7, 2026"
(supplied by the user; kalshi.com blocks this environment's downloads).

- Taker fee = `round up(M x 0.07 x C x P x (1-P))`; maker fee uses `0.0175`
  (and M defaults to 0 for makers). "round up = rounds up such that the
  fee + positionCost is rounded to a centicent".
- Fee Rounding doc: trade fee rounded up to $0.000001, then a rounding fee to
  the member's balance precision ($0.0001 direct, $0.01 non-direct), with
  rebates across fills of one order.
- `edgefinder/fees.py` reproduces **every row** of the PDF's General Trading
  Fees Table (1 and 100 contracts, $0.01 to $0.99) with $0.01 precision
  (`tests/test_fees.py`).
- Non-Standard Fees table: `KXNFLGAME` and `KXMLBGAME` maker 1, taker 1.
  Live `GET /series/KXNFLGAME`: `quadratic_with_maker_fees`, multiplier 1;
  no event overrides on the 28 open games.
- Fee types outside the quadratic family (e.g. `flat`) are not described in
  the PDF; those markets are skipped.

### Not verified / open

| Item | Status |
|---|---|
| Unauthenticated rate limit | Undocumented. Live: 429s even at 2 req/s, and occasionally at 1 req/s. The client defaults to 1 req/s and backs off exponentially (2, 4, 8, 16 s). |
| NFL tie probability | `NFL_TIE_PROB=0.003` is an **assumption**, not sourced data. Kalshi settles ties at $0.50 (verified in `rules_secondary`); moneylines refund them. Effect on fair value is ≤0.15 pts at the extremes. |

## The Odds API (phase 2; read 2026-10-20 from the-odds-api.com/liveapi/guides/v4)

| Item | Finding |
|---|---|
| Free tier | 500 credits per month. |
| Cost | `/v4/sports/{sport}/odds`: 1 credit per market per region (`markets=h2h&regions=us` = 1). With `bookmakers=`, every group of 10 bookmakers counts as 1 region. No events returned = no charge. `/sports` is free. |
| Usage headers | `x-requests-remaining`, `x-requests-used`, `x-requests-last` (cost of this call). |
| Rate limit | 429 on bursts; "space out requests over several seconds". |
| Sport keys | `americanfootball_nfl`, `basketball_nba`, `baseball_mlb`. |
| Sharp books | Pinnacle = `pinnacle`, region `eu` ("odds are from public website which may incur a delay"). Circa is not listed. US region has `lowvig`, `betonlineag`, `draftkings`, `fanduel`. |
| Live check | Done 2026-10-20: 1 credit per NFL pull (`x-requests-last: 1`). All 14 odds events matched a Kalshi game; every team name in the feed was recognised. |
| Budget | `bookmakers=` with ≤10 books (Pinnacle plus US books) costs 1 credit per sport per refresh. 500/month is about 16 per day across all sports, so the refresh cooldown alone won't protect the quota; phase 2 needs a credit budget. |

## nflverse (phase 4; checked 2026-10-20)

| Item | Finding |
|---|---|
| Package | `nfl_data_py` 0.3.3 (last release 2024-09) requires pandas<2 and numpy<2, so it can't be used here. Using `nflreadpy` 0.1.5, nflverse's maintained Python reader (`load_schedules` downloads nflverse-data `schedules/games`). |
| Fields | From the nflreadr schedules data dictionary: `gametime` is 24h US/Eastern regardless of venue; `location` is Home/Neutral; `result` = home − away; `*_rest` = days of rest; `*_moneyline` = closing odds (coverage 2006+). |
| Team codes | nflverse `JAX, LA, STL, SD, OAK` map to Kalshi `JAC, LAR, LAR, LAC, LV`. Every 2026 team maps to one of Kalshi's 32 codes. |
| Ties | 16 ties in 7,110 games since 2000, which sets `NFL_TIE_PROB=0.0023`. |

## NBA and MLB history (phase 7; checked 2026-10-20)

| Item | Finding |
|---|---|
| nba_api | `stats.nba.com` accepts the connection from this cloud environment but never answers (ReadTimeout). This is common for data-centre IPs. The loader tries nba_api first (`NBA_SOURCE=auto`) and falls back to Basketball-Reference. On a home connection nba_api is expected to work, but that hasn't been verified here. |
| Basketball-Reference | `/leagues/NBA_<season>_games-<month>.html`; the schedule table has `data-stat` cells (`date_game`, `game_start_time` in ET per the column header "Start (ET)", visitor/home names and points) and includes upcoming games. Requests are spaced 3.5 s apart (Sports-Reference allows about 20 a minute). |
| pybaseball | `season_game_logs` reads Retrosheet files from `raw.githubusercontent.com/chadwickbureau/retrosheet/master/gamelog/GL<year>.TXT`, which now return 404, and it calls the GitHub API (not available here). Its Baseball-Reference schedule pages lack starting pitchers. Not used. |
| MLB Stats API | `statsapi.mlb.com/api/v1/schedule?sportId=1&startDate&endDate&gameType=R,F,D,L,W&hydrate=probablePitcher`: per game `gamePk`, `gameDate` (UTC), `officialDate`, `gameNumber`/`doubleHeader`, `gameType`, status, scores, probable starters (99.8% coverage on 2025–26 finals). Official, no key. |
