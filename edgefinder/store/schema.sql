-- Money and prices are stored as TEXT decimal strings (exact), times as ISO-8601 UTC.

CREATE TABLE IF NOT EXISTS refresh_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    sport             TEXT NOT NULL,
    trigger           TEXT NOT NULL,          -- manual | poller | cli
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    status            TEXT NOT NULL,          -- running | ok | partial | failed
    kalshi_fetched_at TEXT,
    odds_fetched_at   TEXT,                   -- fresh pull this run, else NULL
    odds_source_at    TEXT,                   -- odds timestamp used for edges (fresh or cached)
    edges_computed    INTEGER NOT NULL DEFAULT 0,
    credits_cost      INTEGER NOT NULL DEFAULT 0,
    credits_remaining INTEGER,
    warnings          TEXT NOT NULL DEFAULT '[]'  -- JSON list
);
CREATE INDEX IF NOT EXISTS ix_runs_sport ON refresh_runs (sport, started_at);

-- One row per market per Kalshi pull: our own price history.
CREATE TABLE IF NOT EXISTS kalshi_snapshots (
    run_id         INTEGER NOT NULL REFERENCES refresh_runs(id),
    fetched_at     TEXT NOT NULL,
    sport          TEXT NOT NULL,
    event_ticker   TEXT NOT NULL,
    ticker         TEXT NOT NULL,
    status         TEXT,
    yes_bid        TEXT, yes_ask TEXT, no_bid TEXT, no_ask TEXT,
    yes_ask_qty    TEXT, no_ask_qty TEXT,       -- depth at the ask, from the orderbook
    last_price     TEXT,
    volume         TEXT, volume_24h TEXT, open_interest TEXT,
    expected_expiration TEXT
);
CREATE INDEX IF NOT EXISTS ix_ksnap_ticker ON kalshi_snapshots (ticker, fetched_at);

-- One row per book per outcome per odds pull.
CREATE TABLE IF NOT EXISTS odds_snapshots (
    run_id         INTEGER NOT NULL REFERENCES refresh_runs(id),
    fetched_at     TEXT NOT NULL,
    sport          TEXT NOT NULL,
    event_id       TEXT NOT NULL,
    sport_key      TEXT NOT NULL,
    commence_time  TEXT NOT NULL,
    home_team      TEXT NOT NULL,
    away_team      TEXT NOT NULL,
    book           TEXT NOT NULL,
    book_update    TEXT,
    outcome        TEXT NOT NULL,
    price          REAL NOT NULL               -- American odds
);
CREATE INDEX IF NOT EXISTS ix_osnap_pull ON odds_snapshots (sport, fetched_at);

CREATE TABLE IF NOT EXISTS edge_rows (
    run_id           INTEGER NOT NULL REFERENCES refresh_runs(id),
    sport            TEXT NOT NULL,
    event_ticker     TEXT NOT NULL,
    market_ticker    TEXT NOT NULL,
    team             TEXT NOT NULL,
    opponent         TEXT NOT NULL,
    side             TEXT NOT NULL,
    label            TEXT NOT NULL,
    kickoff          TEXT NOT NULL,
    ask              TEXT NOT NULL,
    ask_depth        TEXT,
    fair             REAL NOT NULL,
    gross_edge       REAL NOT NULL,
    fee_per_contract TEXT NOT NULL,
    fee_type         TEXT NOT NULL,
    fee_multiplier   TEXT NOT NULL,
    net_edge         REAL NOT NULL,
    ev_per_dollar    REAL NOT NULL,
    contracts        TEXT NOT NULL,
    stake            TEXT NOT NULL,
    books            TEXT NOT NULL,            -- comma-separated
    book_spread      REAL NOT NULL,
    kalshi_fetched   TEXT NOT NULL,
    odds_fetched     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_edges_run ON edge_rows (run_id);

CREATE TABLE IF NOT EXISTS unmatched (
    run_id  INTEGER NOT NULL REFERENCES refresh_runs(id),
    source  TEXT NOT NULL,
    key     TEXT NOT NULL,
    reason  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_bets (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at       TEXT NOT NULL,
    sport            TEXT NOT NULL,
    event_ticker     TEXT NOT NULL,
    market_ticker    TEXT NOT NULL,
    side             TEXT NOT NULL,           -- YES | NO
    label            TEXT NOT NULL,
    kickoff          TEXT NOT NULL,
    price            TEXT NOT NULL,           -- ask paid
    fee_per_contract TEXT NOT NULL,
    contracts        TEXT NOT NULL,
    fair_at_entry    REAL NOT NULL,
    net_edge_at_entry REAL NOT NULL,
    source_run_id    INTEGER REFERENCES refresh_runs(id),
    status           TEXT NOT NULL DEFAULT 'open',   -- open | settled
    settled_at       TEXT,
    result           TEXT,                    -- yes | no | scalar
    payout_per_contract TEXT,
    pnl              TEXT,
    closing_ask      TEXT,                    -- last Kalshi ask for our side before kickoff
    notes            TEXT
);
