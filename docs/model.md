# Baseline model: Elo (NFL, NBA, MLB)

**Data:** nflverse schedules/results via `nflreadpy` (1999–present, 7,548 games
on 2026-10-20), loaded with `python -m edgefinder history load --sport nfl`
and refreshed nightly at 05:07 ET by the poller. Fields used: teams, scores,
US/Eastern date and time, neutral site, rest days, closing moneylines
(2006+), listed starting QBs.

**Model** (`edgefinder/models/elo.py`): standard Elo with home field (none
at neutral sites), a bye bonus (13+ days rest, not in week 1), a
margin-of-victory multiplier with autocorrelation damping, regression to
the mean each off-season, and playoff scaling. The target is win = 1,
tie = 0.5, loss = 0, which is how Kalshi settles team markets, so the
output is a fair value for YES directly.

**Parameters** (`edgefinder/models/service.py`): K 20, home field 35, bye
25, season regression 0.4, playoff ×1.2. Grid-searched on 2015–2021 only.

**Out-of-sample results** (`python -m edgefinder model eval`):

| Test seasons | Games | Elo log loss | Closing line log loss | Home-rate baseline |
|---|---|---|---|---|
| 2022–2025 | 1,139 | 0.6329 | 0.6075 | 0.6877 |
| 2020–2025 (fit 2012–2019) | 1,693 | 0.6360 | 0.6076 | — |

Elo clearly beats the naive baseline, and the closing market beats Elo by
about 0.025 log loss. Elo should therefore be used as a cross-check
(agreement or disagreement), **not** as a source of picks. Big
disagreements with the market usually mean the market knows something
Elo doesn't (injuries, QB changes, form), so they are flagged, not bet.

Calibration on 2022–2025 is reasonable from 0.3 to 0.9. Below 0.3 the
model is a little too pessimistic about underdogs (0.26 predicted vs 0.35
actual in the 0.2–0.3 bin, n = 65).

**Known limits:** no injury or roster information beyond the listed
starting QB; ratings early in a season are mostly last year's (rows in
weeks 1–2 carry a note); home field has declined since 2020 and the
fitted value reflects that.

## NBA

**Data:** 29,509 games from 2004–05 to now (Basketball-Reference fallback;
nba_api is tried first but stats.nba.com doesn't answer from cloud IPs).
Postseason starts at the first play-in game (2020+) or the "Playoffs"
divider (earlier).

**Model:** NBA-style margin multiplier `(|MOV|+3)^0.8 / (7.5 + 0.006·Elo diff)`
and a back-to-back penalty (rest = 1 day). Fit on 2010–2019: K 20, home
court 70, season regression 0.25, back-to-back 50.

| Test seasons | Games | Elo log loss | Home-rate baseline |
|---|---|---|---|
| 2022–2026 | 6,605 | 0.6221 (Brier 0.216) | 0.6897 |

There's no closing line in this history source, so there's no market
benchmark. Calibration: home teams are over-predicted by about 4 pts in
the 0.4–0.9 range, consistent with home court shrinking since 2020. Early
in a season (either team under 10 games) the note "early season" is added
and the ranking penalises it. On opening night 2026 Elo disagreed with the
market by up to 25 pts, because ratings don't know about off-season roster
changes.

## MLB

**Data:** 40,480 games from 2010 to now, from MLB's Stats API (pybaseball's
sources are broken; see docs/api-verification.md), with starting pitchers
(99.8% of finals).

**Model:** no margin multiplier; starting-pitcher ratings (each starter's
rating moves by 4 × (result − expectation) after each start, and the
difference between the two starters is added to the game). Fit on
2012–2019: K 6, home field 24, regression 1/3.

| Test seasons | Games | Elo log loss | Without pitcher adj. | Home-rate baseline |
|---|---|---|---|---|
| 2021–2025 | 12,356 | 0.6782 | 0.6783 | 0.6912 |

Baseball is close to a coin flip game by game. Elo adds a little, and the
results-based pitcher adjustment adds almost nothing: a real pitcher
adjustment needs pitching stats (FanGraphs is blocked here; MLB Stats API
season lines per pitcher would work, at one call per pitcher). Treat MLB
model% as weak.

## Backtest and CLV

`python -m edgefinder backtest model --sport <s>` reproduces the tables
above (`--tune` refits on `--train` seasons). `python -m edgefinder backtest
clv` reports closing line value for every side the app flagged and every
paper bet, against our own last odds pull before kickoff. It's empty until
the app has run through some game days with flagged sides.
