from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from edgefinder.history.nfl import kalshi_code, normalise
from edgefinder.matching.matcher import KalshiGame
from edgefinder.models import evaluate
from edgefinder.models.elo import EloParams, EloState, elo_diff, predict, run, update, win_prob
from edgefinder.models.service import ModelProb, ModelService
from edgefinder.store.db import Store

P = EloParams(k=20, hfa=50, bye_bonus=25, season_regress=0.5, playoff_mult=1.2)


def game(gid, season, home, away, hs=None, as_=None, day="2025-09-07", neutral=0, week=2, gtype="REG", hr=7, ar=7, **kw):
    return {
        "sport": "nfl", "game_id": gid, "season": season, "game_type": gtype, "week": week, "gameday": day,
        "kickoff_utc": f"{day}T17:00:00+00:00", "home": home, "away": away, "home_score": hs, "away_score": as_,
        "neutral": neutral, "home_rest": hr, "away_rest": ar, **kw,
    }


# -- Elo mechanics --------------------------------------------------------------------


def test_win_prob():
    assert win_prob(0) == 0.5
    assert win_prob(400) == pytest.approx(10 / 11)
    assert win_prob(-400) == pytest.approx(1 / 11)


def test_home_field_only_when_not_neutral():
    st = EloState(P)
    assert elo_diff(st, game("g", 2025, "A", "B")) == 50
    assert elo_diff(st, game("g", 2025, "A", "B", neutral=1)) == 0


def test_bye_bonus_needs_13_days_and_not_week_1():
    st = EloState(P)
    assert elo_diff(st, game("g", 2025, "A", "B", neutral=1, hr=14)) == 25
    assert elo_diff(st, game("g", 2025, "A", "B", neutral=1, ar=13)) == -25
    assert elo_diff(st, game("g", 2025, "A", "B", neutral=1, hr=14, week=1)) == 0


def test_playoffs_scale_difference():
    st = EloState(P, {"A": 1600, "B": 1500})
    assert elo_diff(st, game("g", 2025, "A", "B", neutral=1, gtype="DIV")) == pytest.approx(120)


def test_update_is_zero_sum_and_winner_gains():
    st = EloState(P)
    update(st, game("g", 2025, "A", "B", 24, 10))
    assert st.ratings["A"] > 1500 > st.ratings["B"]
    assert st.ratings["A"] + st.ratings["B"] == pytest.approx(3000)
    assert st.history[0][3] == 1.0


def test_bigger_margin_bigger_shift():
    a, b = EloState(P), EloState(P)
    update(a, game("g", 2025, "A", "B", 21, 20, neutral=1))
    update(b, game("g", 2025, "A", "B", 41, 20, neutral=1))
    assert b.ratings["A"] - 1500 > a.ratings["A"] - 1500


def test_favourite_blowout_counts_less_than_underdog_blowout():
    fav, dog = EloState(P, {"A": 1700, "B": 1500}), EloState(P, {"A": 1500, "B": 1700})
    update(fav, game("g", 2025, "A", "B", 38, 3, neutral=1))
    update(dog, game("g", 2025, "A", "B", 38, 3, neutral=1))
    # Same margin, but the expected winner's MOV multiplier is damped.
    assert fav.ratings["A"] - 1700 < dog.ratings["A"] - 1500


def test_tie_between_equal_teams_is_no_change_and_scores_half():
    st = EloState(P)
    update(st, game("g", 2025, "A", "B", 20, 20, neutral=1))
    assert st.ratings["A"] == pytest.approx(1500) and st.history[0][3] == 0.5


def test_new_season_regresses_to_mean():
    st = EloState(P, {"A": 1600, "B": 1400}, season=2024)
    update(st, game("g", 2025, "A", "B", 20, 20, neutral=1))
    # Regressed by half to 1550/1450 before the game; the tie then nudges toward each other.
    assert 1500 < st.ratings["A"] < 1550


def test_predict_future_season_does_not_mutate():
    st = EloState(P, {"A": 1600, "B": 1400}, season=2024)
    p = predict(st, game("g", 2025, "A", "B", neutral=1))
    assert p == pytest.approx(win_prob(100))
    assert st.ratings == {"A": 1600, "B": 1400} and st.season == 2024


def test_run_ignores_unplayed_and_sorts_by_kickoff():
    gs = [
        game("g2", 2025, "A", "B", 10, 30, day="2025-09-14"),
        game("g1", 2025, "A", "B", 30, 10, day="2025-09-07"),
        game("g3", 2025, "A", "B", day="2025-09-21"),
    ]
    st = run(gs, P)
    assert [h[0] for h in st.history] == ["g1", "g2"]


# -- evaluation -------------------------------------------------------------------------


def test_log_loss_and_brier():
    assert evaluate.log_loss([(0.5, 1), (0.5, 0)]) == pytest.approx(0.693147, abs=1e-6)
    assert evaluate.brier([(1.0, 1), (0.0, 1)]) == pytest.approx(0.5)
    assert evaluate.log_loss([(0.5, 0.5)]) == pytest.approx(0.693147, abs=1e-6)


def test_calibration_bins():
    cal = evaluate.calibration([(0.15, 0), (0.18, 1), (0.95, 1)])
    assert cal[0] == {"bin": "0.1-0.2", "n": 2, "predicted": pytest.approx(0.165), "actual": 0.5}
    assert cal[-1]["bin"] == "0.9-1.0"


def test_market_prob_devigs_closing_line():
    assert evaluate.market_prob({"home_ml": -110, "away_ml": -110}) == pytest.approx(0.5)
    assert evaluate.market_prob({"home_ml": None, "away_ml": 120}) is None


# -- history normalisation ---------------------------------------------------------------


@pytest.mark.parametrize("nflverse,kalshi", [("JAX", "JAC"), ("LA", "LAR"), ("STL", "LAR"), ("SD", "LAC"), ("OAK", "LV"), ("KC", "KC")])
def test_franchise_codes(nflverse, kalshi):
    assert kalshi_code(nflverse) == kalshi


def test_normalise_converts_eastern_kickoff_to_utc():
    row = {"game_id": "x", "season": 2026, "game_type": "REG", "week": 7, "gameday": "2026-10-22", "gametime": "20:15",
           "home_team": "CHI", "away_team": "NE", "location": "Home"}
    g = normalise(row)
    assert g["kickoff_utc"] == "2026-10-23T00:15:00+00:00"
    assert g["neutral"] == 0 and g["home"] == "CHI"
    assert normalise({**row, "location": "Neutral", "home_team": "JAX"})["neutral"] == 1


# -- model service (Kalshi game -> schedule row) --------------------------------------------


def kgame(day=date(2026, 10, 22), teams=("CHI", "NE")):
    return KalshiGame("nfl", f"KXNFLGAME-X{day}", day, {t: None for t in teams}, None)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "m.sqlite")
    hist = [game(f"h{i}", 2025, "CHI", "NE", 24 if i % 2 else 10, 17, day=f"2025-10-{10 + i}") for i in range(4)]
    hist.append(game("next", 2026, "CHI", "NE", day="2026-10-22", week=7, home_starter="QB1", away_starter="QB2"))
    s.upsert_history(hist, datetime.now(timezone.utc))
    return s


def test_model_probs_for_matching_schedule_row(store):
    out = ModelService(store).probs([kgame()])
    mp = next(iter(out.values()))
    assert isinstance(mp, ModelProb)
    assert mp.probs["CHI"] + mp.probs["NE"] == pytest.approx(1)
    assert mp.game_id == "next" and "QB2 @ QB1" in mp.note


def test_model_refuses_wrong_date_or_teams(store):
    out = ModelService(store).probs([kgame(day=date(2026, 10, 23)), kgame(teams=("CHI", "NYJ"))])
    assert all(isinstance(v, str) and "0 schedule rows" in v for v in out.values())


def test_model_refuses_ambiguous_rows(store):
    store.upsert_history([game("dup", 2026, "NE", "CHI", day="2026-10-22")], datetime.now(timezone.utc))
    (v,) = ModelService(store).probs([kgame()]).values()
    assert "2 schedule rows" in v


def test_model_refuses_already_played(store):
    store.upsert_history([game("next", 2026, "CHI", "NE", 20, 3, day="2026-10-22")], datetime.now(timezone.utc))
    (v,) = ModelService(store).probs([kgame()]).values()
    assert "already played" in v


def test_no_history_gives_reason(tmp_path):
    (v,) = ModelService(Store(tmp_path / "e.sqlite")).probs([kgame()]).values()
    assert "history load" in v


def test_model_cache_invalidates_on_reload(store):
    svc = ModelService(store)
    first = svc.probs([kgame()])
    store.upsert_history([game("h9", 2026, "CHI", "NE", 0, 50, day="2026-10-01")], datetime.now(timezone.utc))
    second = svc.probs([kgame()])
    assert next(iter(second.values())).probs["CHI"] < next(iter(first.values())).probs["CHI"]
