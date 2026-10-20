# Where we left off (2026-10-20)

## What exists

A read-only Kalshi sports edge finder for NFL, NBA and MLB, in this repo
(`joelpang8/predict`, branch `main`). It never places orders. All seven build
phases are done and checked against live data; 248 tests pass.

- Compares Kalshi prices with de-vigged sportsbook odds (The Odds API, free
  tier) and an Elo model, after Kalshi's real fees.
- Ranks Top Picks with guardrails, or shows "No good bets today".
- Streamlit dashboard: Top Picks, All Markets, Market Detail, Paper Trading,
  Settings, a Refresh button with cooldown, and a background poller.
- Stores everything in `data/edgefinder.sqlite` (local only, not in git).
- On the first live runs nothing cleared the 3-point minimum edge after
  fees. That's normal for liquid markets.

## Setup on your computer (once)

```bash
git clone https://github.com/joelpang8/predict && cd predict
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then put your key in: ODDS_API_KEY=...
python -m edgefinder history load --sport nfl
python -m edgefinder history load --sport mlb
python -m edgefinder history load --sport nba   # about 15 min the first time
```

## Every time you use it

```bash
source .venv/bin/activate
streamlit run ui/streamlit_app.py   # opens http://localhost:8501
```

- Pick a sport, press **Refresh** (1 odds credit per sport), and read Top Picks.
- Switch on **Background poller** in the sidebar on game days.
- To try a pick, use **Log paper bet**. Settle finished games in the
  **Paper Trading** tab.

Terminal equivalents: `python -m edgefinder refresh --sport nfl`,
`picks`, `paper list|settle|stats`, `backtest clv`.

## Next steps

1. **Regenerate your Odds API key** (it was pasted in chat) at
   the-odds-api.com and update `.env`.
2. **Paper-trade for 3–4 weeks.** Don't bet real money until
   `python -m edgefinder backtest clv` shows flagged sides beating the
   closing line on average. If they don't, the "edges" aren't real.
3. **Keep the poller running near game times** so closing lines get
   recorded. The app stops when your computer sleeps. For 24/7 running, use
   an always-on machine or a small cloud server.
4. Watch the **credit budget**: 15 a day shared by all sports (500 a month
   on the free tier).

## Known limits

- Elo is a cross-check, not a pick source: the NFL closing line beats it,
  the MLB model is weak, and early-season ratings lag roster changes.
- nba_api doesn't work from cloud servers (Basketball-Reference is used
  instead). It may work from home; set `NBA_SOURCE=nba_api` to try it.
- Kalshi's public rate limit is tight, so a refresh takes 20–30 s.

## Possible next work

- Reload history automatically when the app starts after being off.
- A setup guide for running on a small always-on server.
- A better MLB pitcher adjustment using real pitching stats.
