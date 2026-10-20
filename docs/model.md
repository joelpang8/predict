# Baseline model: Elo (NFL)

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
