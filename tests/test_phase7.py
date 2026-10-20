"""NBA/MLB: tickers with start times and doubleheaders, history parsers (recorded
Basketball-Reference and MLB Stats API responses), Elo extensions, model
service, backtest CLV."""

from __future__ import annotations

import json
import pathlib
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from edgefinder.config import Settings
from edgefinder.history import mlb, nba
from edgefinder.history.common import add_rest_days
from edgefinder.matching.matcher import MatchReport, build_games, match_games
from edgefinder.matching.teams import team_code
from edgefinder.models.elo import EloParams, EloState, elo_diff, update
from edgefinder.models.service import ModelProb, ModelService
from edgefinder.sources.kalshi_models import KalshiMarket
from edgefinder.sources.odds_api import BookOdds, OddsEvent
from edgefinder.store.db import Store, iso
from tests.test_kalshi import market as raw_market

FIX = pathlib.Path(__file__).parent / "fixtures"
UTC = timezone.utc


# -- teams ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sport,name,code",
    [
        ("nba", "Los Angeles L", "LAL"), ("nba", "LA Clippers", "LAC"), ("nba", "New Jersey Nets", "BKN"),
        ("nba", "Seattle SuperSonics", "OKC"), ("nba", "New York", "NYK"), ("mlb", "Chicago WS", "CWS"),
        ("mlb", "A's", "ATH"), ("mlb", "Oakland Athletics", "ATH"), ("mlb", "Arizona Diamondbacks", "AZ"),
        ("mlb", "New York Y", "NYY"), ("mlb", "Cleveland Indians", "CLE"),
    ],
)
def test_team_codes(sport, name, code):
    assert team_code(sport, name) == code


@pytest.mark.parametrize("sport,name", [("mlb", "New York"), ("mlb", "Chicago"), ("mlb", "Los Angeles"), ("nba", "Los Angeles")])
def test_ambiguous_city_names_unknown(sport, name):
    assert team_code(sport, name) is None


# -- MLB tickers: start time and doubleheaders --------------------------------------------------


def mlb_market(event, code, title, date_txt="Sep 4, 2026", exp="2026-09-04T21:10:00Z"):
    return KalshiMarket.from_api(raw_market(
        f"{event}-{code}", event_ticker=event, yes_sub_title=title, expected_expiration_time=exp,
        rules_primary=f"If {title} wins the Detroit vs Cleveland professional baseball game originally scheduled for {date_txt}, then the market resolves to Yes.",
    ))


def dh_markets():
    g1, g2 = "KXMLBGAME-26SEP041410DETCLEG1", "KXMLBGAME-26SEP041915DETCLEG2"
    return [mlb_market(g1, "DET", "Detroit"), mlb_market(g1, "CLE", "Cleveland"),
            mlb_market(g2, "DET", "Detroit", exp="2026-09-05T02:15:00Z"), mlb_market(g2, "CLE", "Cleveland", exp="2026-09-05T02:15:00Z")]


def odds(eid, kick):
    return OddsEvent(eid, "baseball_mlb", kick, "Cleveland Guardians", "Detroit Tigers",
                     (BookOdds("pinnacle", kick, {"Cleveland Guardians": -120, "Detroit Tigers": 110}),))


def test_mlb_ticker_parses_start_time_and_game_number():
    rep = MatchReport()
    games = build_games("mlb", dh_markets(), rep)
    assert not rep.unmatched and len(games) == 2
    g1 = next(g for g in games if g.game_number == 1)
    assert g1.start == datetime(2026, 9, 4, 18, 10, tzinfo=UTC)  # 14:10 ET (EDT)


def test_doubleheader_matched_by_start_time():
    rep = MatchReport()
    games = build_games("mlb", dh_markets(), rep)
    match_games("mlb", games, [odds("late", datetime(2026, 9, 4, 23, 15, tzinfo=UTC)), odds("early", datetime(2026, 9, 4, 18, 10, tzinfo=UTC))], rep)
    pairs = {m.game.event_ticker: m.event.id for m in rep.matches}
    assert pairs == {"KXMLBGAME-26SEP041410DETCLEG1": "early", "KXMLBGAME-26SEP041915DETCLEG2": "late"}


def test_mlb_start_time_mismatch_refused():
    rep = MatchReport()
    games = build_games("mlb", dh_markets()[:2], rep)
    match_games("mlb", games, [odds("x", datetime(2026, 9, 4, 19, 10, tzinfo=UTC))], rep)  # an hour off
    assert not rep.matches and "Kalshi start" in rep.unmatched[-1].reason


def test_bad_ticker_time_refused():
    ms = [mlb_market("KXMLBGAME-26SEP042575DETCLE", c, t) for c, t in (("DET", "Detroit"), ("CLE", "Cleveland"))]
    rep = MatchReport()
    assert not build_games("mlb", ms, rep) and "bad start time" in rep.unmatched[0].reason


# -- history parsers (recorded responses) -----------------------------------------------------


def test_bbref_month_parser():
    rows = nba.parse_bbref_month((FIX / "bbref_nba_2026_october.html").read_text(), 2026)
    first = rows[0]
    assert first["away"] == "HOU" and first["home"] == "OKC" and (first["away_score"], first["home_score"]) == (124, 125)
    assert first["gameday"] == "2025-10-21" and first["kickoff_utc"] == "2025-10-21T23:30:00+00:00"  # 7:30p ET
    assert all(r["game_type"] == "REG" for r in rows)


def test_bbref_playoff_rows_flagged():
    page = (FIX / "bbref_nba_2026_october.html").read_text()
    rows_html = page.split("</tr>")
    marked = "</tr>".join(rows_html[:3]) + '</tr><tr class="thead"><th colspan="10">Playoffs</th></tr>' + "</tr>".join(rows_html[3:])
    rows = nba.parse_bbref_month(marked, 2026)
    assert rows[0]["game_type"] == "REG" and rows[-1]["game_type"] == "POST"


def test_nba_api_rows_paired_into_games():
    rows = [
        {"GAME_ID": "1", "MATCHUP": "BOS vs. NYK", "TEAM_NAME": "Boston Celtics", "GAME_DATE": "2025-10-22", "PTS": 110},
        {"GAME_ID": "1", "MATCHUP": "NYK @ BOS", "TEAM_NAME": "New York Knicks", "GAME_DATE": "2025-10-22", "PTS": 100},
        {"GAME_ID": "2", "MATCHUP": "LAL vs. GSW", "TEAM_NAME": "Los Angeles Lakers", "GAME_DATE": "2025-10-22", "PTS": 99},
    ]
    games = nba.pair_nba_api_rows(rows, 2026, "REG")
    assert len(games) == 1 and games[0]["home"] == "BOS" and games[0]["away"] == "NYK" and games[0]["home_score"] == 110


def test_statsapi_parser_doubleheader_and_starters():
    games = mlb.parse_schedule(json.loads((FIX / "statsapi_mlb_2026-09-04.json").read_text()), 2026)
    dh = [g for g in games if {g["home"], g["away"]} == {"CLE", "DET"}]
    assert len(dh) == 2 and {g["week"] for g in dh} == {1, 2}
    g1 = next(g for g in dh if g["week"] == 1)
    assert g1["kickoff_utc"] == "2026-09-04T18:10:00+00:00" and g1["home"] == "CLE" and (g1["away_score"], g1["home_score"]) == (6, 7)
    assert g1["home_starter"] and g1["away_starter"]


def test_statsapi_skips_postponed_and_marks_upcoming():
    data = {"dates": [{"games": [
        {"gamePk": 1, "gameType": "R", "gameDate": "2026-09-05T23:00:00Z", "officialDate": "2026-09-05", "gameNumber": 1,
         "status": {"detailedState": "Postponed", "abstractGameState": "Final"},
         "teams": {"home": {"team": {"name": "Boston Red Sox"}}, "away": {"team": {"name": "New York Yankees"}}}},
        {"gamePk": 2, "gameType": "R", "gameDate": "2026-09-06T23:00:00Z", "officialDate": "2026-09-06", "gameNumber": 1,
         "status": {"detailedState": "Scheduled", "abstractGameState": "Preview"},
         "teams": {"home": {"team": {"name": "Boston Red Sox"}, "probablePitcher": {"fullName": "A"}}, "away": {"team": {"name": "New York Yankees"}}}},
        {"gamePk": 3, "gameType": "S", "gameDate": "2026-03-01T18:00:00Z", "officialDate": "2026-03-01", "gameNumber": 1,
         "status": {"detailedState": "Final", "abstractGameState": "Final"},
         "teams": {"home": {"team": {"name": "Boston Red Sox"}, "score": 1}, "away": {"team": {"name": "New York Yankees"}, "score": 2}}},
    ]}]}
    games = mlb.parse_schedule(data, 2026)
    assert [g["game_id"] for g in games] == ["2"]  # postponed and spring training dropped
    assert games[0]["home_score"] is None and games[0]["home_starter"] == "A" and games[0]["away_starter"] is None


def test_rest_days():
    gs = add_rest_days([
        {"game_id": "a", "gameday": "2025-10-21", "home": "BOS", "away": "NYK"},
        {"game_id": "b", "gameday": "2025-10-22", "home": "NYK", "away": "MIA"},
        {"game_id": "c", "gameday": "2025-10-25", "home": "BOS", "away": "MIA"},
    ])
    assert gs[0]["home_rest"] is None and gs[1]["home_rest"] == 1 and gs[2]["home_rest"] == 4 and gs[2]["away_rest"] == 3


# -- Elo extensions --------------------------------------------------------------------------


def g(home="A", away="B", hs=None, as_=None, **kw):
    return {"game_id": "x", "season": 2026, "game_type": "REG", "week": None, "home": home, "away": away,
            "home_score": hs, "away_score": as_, "neutral": 1, **kw}


def test_back_to_back_penalty():
    st = EloState(EloParams(b2b_penalty=25, bye_bonus=0))
    assert elo_diff(st, g(home_rest=1, away_rest=2)) == -25
    assert elo_diff(st, g(home_rest=2, away_rest=1)) == 25


def test_nba_mov_multiplier_grows_with_margin():
    p = EloParams(mov="nba", k=20)
    a, b = EloState(p), EloState(p)
    update(a, g(hs=101, as_=100))
    update(b, g(hs=130, as_=100))
    assert b.ratings["A"] - 1500 > a.ratings["A"] - 1500 > 0


def test_mov_none_ignores_margin():
    p = EloParams(mov="none", k=4)
    a, b = EloState(p), EloState(p)
    update(a, g(hs=2, as_=1))
    update(b, g(hs=12, as_=1))
    assert a.ratings["A"] == pytest.approx(b.ratings["A"])


def test_pitcher_ratings_learn_and_shift_games():
    p = EloParams(mov="none", k=4, pitcher_k=4, pitcher_weight=1.0)
    st = EloState(p)
    for _ in range(5):
        update(st, g(hs=5, as_=1, home_starter="Ace", away_starter="Joe"))
    assert st.pitchers["Ace"] > 0 > st.pitchers["Joe"]
    with_ace = elo_diff(st, g(home="C", away="D", home_starter="Ace", away_starter="Joe"))
    without = elo_diff(st, g(home="C", away="D"))
    assert with_ace > without == 0


# -- model service: MLB doubleheader, NBA synthetic row -------------------------------------------


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "p7.sqlite")


def test_model_picks_doubleheader_game_by_start(store):
    sched = mlb.parse_schedule(json.loads((FIX / "statsapi_mlb_2026-09-04.json").read_text()), 2026)
    for x in sched:
        x["home_score"] = x["away_score"] = None  # pretend not yet played
    store.upsert_history(add_rest_days(sched), datetime.now(UTC))
    games = build_games("mlb", dh_markets(), MatchReport())
    out = ModelService(store).probs(games)
    g1 = out["KXMLBGAME-26SEP041410DETCLEG1"]
    assert isinstance(g1, ModelProb) and g1.game_id == "824424" and g1.note.startswith("SPs:")
    assert out["KXMLBGAME-26SEP041915DETCLEG2"].game_id == "824387"


def test_nba_game_without_schedule_row_uses_odds_event(store):
    store.upsert_history([{**g("BOS", "NYK", 110, 100), "sport": "nba", "game_id": "h1", "gameday": "2026-10-18",
                           "kickoff_utc": "2026-10-18T23:00:00+00:00", "neutral": 0}], datetime.now(UTC))
    ev = "KXNBAGAME-26OCT20NYKBOS"
    ms = [KalshiMarket.from_api(raw_market(f"{ev}-{c}", event_ticker=ev, yes_sub_title=t, expected_expiration_time="2026-10-21T02:30:00Z",
          rules_primary=f"If {t} wins the New York vs Boston Pro Basketball game originally scheduled for Oct 20, 2026, then the market resolves to Yes."))
          for c, t in (("NYK", "New York"), ("BOS", "Boston"))]
    games = build_games("nba", ms, MatchReport())
    event = OddsEvent("o1", "basketball_nba", datetime(2026, 10, 20, 23, 30, tzinfo=UTC), "Boston Celtics", "New York Knicks", ())
    mp = ModelService(store).probs(games, {ev: event})[ev]
    assert isinstance(mp, ModelProb) and mp.probs["BOS"] > 0.5 and "from the odds feed" in mp.note
    assert ModelService(store).probs(games)[ev].startswith("0 schedule rows")  # without the odds event: no guess


# -- backtest: CLV ----------------------------------------------------------------------------


def test_clv_from_last_odds_pull_before_kickoff(store):
    from edgefinder import backtest
    from edgefinder.jobs.refresh import RefreshService  # noqa: F401  (ensures schema)

    s = Settings(kalshi_base_url="x", min_books=2, nfl_tie_prob=0.0)
    kick = datetime(2026, 10, 25, 17, 0, tzinfo=UTC)
    run_id = store.start_run("nfl", "cli", kick - timedelta(hours=10))

    def pull(t, chi, ne):
        with store.conn() as c:
            for b in ("pinnacle", "draftkings"):
                for name, price in (("Chicago Bears", chi), ("New England Patriots", ne)):
                    c.execute("INSERT INTO odds_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                              (run_id, iso(t), "nfl", "ev", "americanfootball_nfl", iso(kick), "Chicago Bears", "New England Patriots", b, iso(t), name, price))

    pull(kick - timedelta(hours=10), -110, -110)   # entry time: 50/50
    pull(kick - timedelta(minutes=5), -150, 130)  # close: CHI ~58%
    pull(kick + timedelta(hours=1), 500, -900)    # in-game, must be ignored
    with store.conn() as c:
        c.execute("INSERT INTO edge_rows (run_id, sport, event_ticker, market_ticker, team, opponent, side, label, kickoff, ask, fair, "
                  "gross_edge, fee_per_contract, fee_type, fee_multiplier, net_edge, ev_per_dollar, contracts, stake, books, book_spread, "
                  "kalshi_fetched, odds_fetched) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (run_id, "nfl", "E", "E-CHI", "CHI", "NE", "YES", "CHI to win", iso(kick), "0.45", 0.50, 0.05, "0.02", "quadratic", "1",
                   0.04, 0.08, "10", "4.70", "pinnacle,draftkings", 0.0, iso(kick - timedelta(hours=10)), iso(kick - timedelta(hours=10))))
    rep = backtest.clv_report(store, s, kick + timedelta(hours=4))
    st = rep.stats("signal")
    assert st["n"] == 1
    row = rep.rows[0]
    assert row.closing_time == kick - timedelta(minutes=5)
    assert row.clv_pts == pytest.approx((row.closing_fair - 0.45) * 100) and row.closing_fair == pytest.approx(0.5799, abs=0.002)
    assert row.line_move_pts > 7
    assert backtest.clv_report(store, s, kick - timedelta(hours=1)).pending == 1  # before kickoff: no close yet


def test_bbref_playoffs_carry_into_later_months():
    page = (FIX / "bbref_nba_2026_october.html").read_text()
    assert all(r["game_type"] == "POST" for r in nba.parse_bbref_month(page, 2026, playoffs=True))


def test_postseason_starts_at_first_play_in():
    rows = [
        {"season": 2025, "gameday": "2025-04-13", "game_type": "REG", "_play_in": False},
        {"season": 2025, "gameday": "2025-04-15", "game_type": "REG", "_play_in": True},
        {"season": 2025, "gameday": "2025-05-01", "game_type": "REG", "_play_in": False},
        {"season": 2019, "gameday": "2019-05-01", "game_type": "REG", "_play_in": False},
    ]
    out = nba.mark_postseason(rows)
    assert [r["game_type"] for r in out] == ["REG", "POST", "POST", "REG"]
    assert all("_play_in" not in r for r in out)
