"""Market matching: a wrong match is a fake edge, so most tests are about refusing."""

from datetime import datetime, timedelta, timezone

import pytest

from edgefinder.matching.matcher import MatchReport, build_games, match_games
from edgefinder.matching.teams import NFL, team_code
from edgefinder.sources.kalshi_models import KalshiMarket
from edgefinder.sources.odds_api import BookOdds, OddsEvent
from tests.test_kalshi import market as raw_market

UTC = timezone.utc
EXP = "2026-10-23T03:15:00Z"  # Thu 8:15pm ET kickoff + 3h
KICK = datetime(2026, 10, 23, 0, 15, tzinfo=UTC)  # = Oct 22, 20:15 ET


def kmarket(event="KXNFLGAME-26OCT22NECHI", code="CHI", title="Chicago", date="Oct 22, 2026", exp=EXP):
    rules = f"If {title} wins the New England vs Chicago Pro Football game originally scheduled for {date}, then the market resolves to Yes."
    return KalshiMarket.from_api(
        raw_market(
            f"{event}-{code}",
            event_ticker=event,
            yes_sub_title=title,
            rules_primary=rules,
            expected_expiration_time=exp,
        )
    )


def game_markets(**kw):
    return [kmarket(code="CHI", title="Chicago", **kw), kmarket(code="NE", title="New England", **kw)]


def oevent(eid="ev1", home="Chicago Bears", away="New England Patriots", kick=KICK):
    return OddsEvent(eid, "americanfootball_nfl", kick, home, away, (BookOdds("pinnacle", kick, {home: -150, away: 130}),))


def run(markets, events):
    rep = MatchReport()
    games = build_games("nfl", markets, rep)
    match_games("nfl", games, events, rep)
    return rep


# -- team table ---------------------------------------------------------------------


def test_team_table_has_32_unique_codes():
    assert len(NFL) == 32


@pytest.mark.parametrize(
    "name,code",
    [
        ("Los Angeles C", "LAC"), ("Los Angeles Rams", "LAR"), ("New York J", "NYJ"),
        ("new york giants", "NYG"), ("San Francisco 49ers", "SF"), ("Jacksonville Jaguars", "JAC"),
    ],
)
def test_team_codes(name, code):
    assert team_code("nfl", name) == code


@pytest.mark.parametrize("name", ["Los Angeles", "New York", "Giants", "Washington Redskins", "Chicago Cubs"])
def test_ambiguous_or_foreign_names_are_unknown(name):
    assert team_code("nfl", name) is None


# -- happy path -------------------------------------------------------------------------


def test_matches_game_by_teams_and_eastern_date():
    rep = run(game_markets(), [oevent()])
    assert len(rep.matches) == 1 and not rep.unmatched
    assert set(rep.matches[0].game.markets) == {"CHI", "NE"}


def test_home_away_order_does_not_matter():
    rep = run(game_markets(), [oevent(home="New England Patriots", away="Chicago Bears")])
    assert len(rep.matches) == 1


def test_utc_date_differs_from_eastern_date_still_matches():
    # Kickoff is Oct 23 in UTC but Oct 22 in New York; Kalshi's date is Eastern.
    assert KICK.date().day == 23
    assert len(run(game_markets(), [oevent()]).matches) == 1


# -- refusals -------------------------------------------------------------------------


def test_wrong_date_is_unmatched():
    rep = run(game_markets(), [oevent(kick=KICK + timedelta(days=7))])
    assert not rep.matches
    assert "no odds event" in rep.unmatched[-1].reason


def test_unknown_odds_team_is_logged():
    rep = run(game_markets(), [oevent(home="Chicago Bearz")])
    assert not rep.matches
    assert any(u.source == "odds" and "unknown team" in u.reason for u in rep.unmatched)


def test_different_opponent_is_not_matched():
    rep = run(game_markets(), [oevent(away="New York Jets")])
    assert not rep.matches


def test_kickoff_inconsistent_with_kalshi_expiration_is_refused():
    # Same teams, same Eastern date, but kickoff 10 minutes before Kalshi's expected expiration.
    rep = run(game_markets(), [oevent(kick=datetime(2026, 10, 23, 3, 5, tzinfo=UTC))])
    assert not rep.matches
    assert "not consistent" in rep.unmatched[-1].reason


def test_doubleheader_both_fit_is_ambiguous():
    rep = run(game_markets(), [oevent("g1", kick=KICK), oevent("g2", kick=KICK + timedelta(minutes=30))])
    assert not rep.matches
    assert "ambiguous" in rep.unmatched[-1].reason


def test_doubleheader_only_one_fits_picks_it():
    early = datetime(2026, 10, 22, 17, 0, tzinfo=UTC)  # 1pm ET, 10h before expiration
    rep = run(game_markets(), [oevent("early", kick=early), oevent("late", kick=KICK)])
    assert [m.event.id for m in rep.matches] == ["late"]


def test_one_odds_event_claimed_by_two_kalshi_games_matches_neither():
    other = [
        kmarket(event="KXNFLGAME-26OCT22CHINE", code="CHI", title="Chicago"),
        kmarket(event="KXNFLGAME-26OCT22CHINE", code="NE", title="New England"),
    ]
    rep = run(game_markets() + other, [oevent()])
    assert not rep.matches
    assert sum("fits 2 Kalshi games" in u.reason for u in rep.unmatched) == 2


def test_ticker_and_rules_dates_must_agree():
    rep = run(game_markets(date="Oct 29, 2026"), [oevent()])
    assert not rep.matches and "date unclear" in rep.unmatched[0].reason


def test_missing_rules_date_refused():
    ms = game_markets()
    ms = [KalshiMarket.from_api({**m.raw, "rules_primary": "If Chicago wins, Yes."}) for m in ms]
    assert not run(ms, [oevent()]).matches


def test_yes_subtitle_must_match_ticker_code():
    ms = [kmarket(code="CHI", title="New England"), kmarket(code="NE", title="New England")]
    rep = run(ms, [oevent()])
    assert not rep.matches and "says" in rep.unmatched[0].reason


def test_unknown_kalshi_code_refused():
    ms = [kmarket(code="CHX", title="Chicago"), kmarket(code="NE", title="New England")]
    assert "unknown team code" in run(ms, [oevent()]).unmatched[0].reason


def test_single_market_event_refused():
    rep = run([kmarket()], [oevent()])
    assert "expected 2 markets" in rep.unmatched[0].reason


def test_ticker_team_block_must_equal_market_codes():
    ms = [kmarket(event="KXNFLGAME-26OCT22NYGCHI", code=c, title=t) for c, t in (("CHI", "Chicago"), ("NE", "New England"))]
    assert "ticker teams" in run(ms, [oevent()]).unmatched[0].reason


def test_unparseable_ticker_refused():
    ms = [kmarket(event="KXNFLGAME-26OCT22NECHI-G2", code=c, title=t) for c, t in (("CHI", "Chicago"), ("NE", "New England"))]
    assert "format" in run(ms, [oevent()]).unmatched[0].reason


def test_markets_disagreeing_on_expiration_refused():
    ms = [kmarket(code="CHI", title="Chicago"), kmarket(code="NE", title="New England", exp="2026-10-23T05:00:00Z")]
    assert "disagree" in run(ms, [oevent()]).unmatched[0].reason


def test_recorded_live_markets_all_parse_into_games():
    import json
    import pathlib

    data = json.loads((pathlib.Path(__file__).parent / "fixtures" / "kalshi_markets_kxnflgame.json").read_text())
    ms = [KalshiMarket.from_api(m) for m in data["markets"]]
    rep = MatchReport()
    games = build_games("nfl", ms, rep)
    # The fixture is a 4-market page; every complete game must parse, partial ones are refused.
    complete = {e for e in {m.event_ticker for m in ms} if sum(m.event_ticker == e for m in ms) == 2}
    assert {g.event_ticker for g in games} == complete
