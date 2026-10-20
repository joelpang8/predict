from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from edgefinder.config import Settings, apply_overrides
from edgefinder.scoring import open_exposure_today, rank, size_row
from edgefinder.store.db import iso

UTC = timezone.utc
NOW = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
S = Settings(kalshi_base_url="x", bankroll=D(1000), kelly_multiplier=0.25, max_bet_pct=0.02)


def row(event="KXNFLGAME-26OCT25AAABBB", team="AAA", opp="BBB", side="YES", ask="0.50", fair=0.58, **kw):
    r = {
        "sport": "nfl", "event_ticker": event, "market_ticker": f"{event}-{team}", "team": team, "opponent": opp,
        "side": side, "label": f"{team} to win", "kickoff": iso(NOW + timedelta(days=2)), "ask": ask, "ask_depth": "20000",
        "fair": fair, "gross_edge": fair - float(ask), "fee_per_contract": "0.02", "fee_type": "quadratic_with_maker_fees",
        "fee_multiplier": "1", "net_edge": 0.0, "books": "a,b,c,d,e,f,g,h,i,j", "book_spread": 0.02,
        "kalshi_fetched": iso(NOW - timedelta(seconds=30)), "odds_fetched": iso(NOW - timedelta(seconds=30)),
        "model_fair": fair, "model_note": "QBs: X @ Y", "spread": "0.01", "move_1h": 0.0, "move_6h": 0.0,
        "move_24h": 0.0, "trend_score": 1.0, "imbalance": 0.0, "flags": [],
    }
    r.update(kw)
    return r


def test_size_row_uses_current_settings():
    n, stake, fee, net = size_row(row(), S)
    assert n == 38 and stake == D("19.67") and net == pytest.approx(0.58 - 0.50 - float(D("0.67") / 38))
    n2, *_ = size_row(row(), replace(S, bankroll=D(2000)))
    assert n2 > n


def test_eligible_pick_with_reason_line():
    r = rank([row()], S, NOW)
    assert len(r.picks) == 1
    p = r.picks[0]
    assert p.eligible and p.score > 0
    assert p.reason.startswith("Sportsbook consensus 58% (10 books); Kalshi ask 50c; net edge +6.2 after fees")
    assert "Elo 58% (Elo agrees)" in p.reason and "starters listed" in p.reason


@pytest.mark.parametrize(
    "kw,blocked",
    [
        ({"fair": 0.52, "model_fair": 0.52}, "net edge"),
        ({"ask_depth": "100"}, "liquidity"),
        ({"ask_depth": None}, "liquidity"),
        ({"spread": "0.05"}, "spread 5c"),
        ({"spread": None}, "spread unknown"),
        ({"kickoff": iso(NOW - timedelta(minutes=1))}, "game started"),
        (
            {"kickoff": iso(NOW + timedelta(minutes=10)), "kalshi_fetched": iso(NOW - timedelta(minutes=3))},
            "min to kickoff",
        ),
    ],
)
def test_guardrails_block(kw, blocked):
    r = rank([row(**kw)], S, NOW)
    assert not r.picks
    assert any(b.startswith(blocked) or blocked in b for b in r.rows[0].blocked)


def test_near_kickoff_allowed_when_fresh():
    assert rank([row(kickoff=iso(NOW + timedelta(minutes=10)))], S, NOW).picks


def test_stale_view_blocks_everything():
    r = rank([row()], S, NOW, stale=True)
    assert not r.picks and "data is stale" in r.rows[0].blocked


def test_no_good_bets_summary():
    r = rank([row(fair=0.50, model_fair=0.5), row(team="CCC", event="E2", fair=0.49, model_fair=0.5)], S, NOW)
    assert r.summary.startswith("No good bets today: 0 of 2 sides")
    assert "net edge under 3 pts: 2" in r.summary


def test_yes_a_and_no_b_are_the_same_bet_keep_best():
    yes_a = row(side="YES", team="AAA", opp="BBB", ask="0.50")
    no_b = row(side="NO", team="BBB", opp="AAA", ask="0.49", market_ticker="KXNFLGAME-26OCT25AAABBB-BBB")
    r = rank([yes_a, no_b], S, NOW)
    assert len(r.picks) == 1 and r.picks[0].row["side"] == "NO"  # cheaper, so more edge


def test_one_pick_per_game():
    a = row(side="YES", team="AAA", opp="BBB")
    b = row(side="YES", team="BBB", opp="AAA", market_ticker="KXNFLGAME-26OCT25AAABBB-BBB", fair=0.60, model_fair=0.6)
    assert len(rank([a, b], S, NOW).picks) == 1


def test_penalties_lower_score_and_show_in_reason():
    clean = rank([row()], S, NOW).picks[0]
    risky = rank([row(flags=["check for news: price fell 7 pts against this side"], books="a,b,c", model_note="starting QBs not listed")], S, NOW).rows[0]
    assert risky.score < clean.score - 0.4
    assert {n for n, _ in risky.penalties} >= {"check for news", "only 3 books", "starters not listed"}
    assert "caution: check for news" in risky.reason


def test_model_disagreement_penalised():
    p = rank([row(model_fair=0.40)], S, NOW).rows[0]
    assert p.components["model"] == 0.0
    assert any(n.startswith("Elo disagrees") for n, _ in p.penalties)
    assert "Elo sees no edge" in p.reason


def test_missing_model_is_neutral_with_penalty():
    p = rank([row(model_fair=None, model_note="no history loaded")], S, NOW).rows[0]
    assert p.components["model"] == 0.5 and ("no model estimate", S.p_no_model) in p.penalties


def test_components_formulae():
    p = rank([row(move_6h=2.0, ask_depth="2000")], S, NOW).rows[0]
    assert p.components["edge"] == pytest.approx(p.net_edge * 100 / 10)
    assert p.components["liquidity"] == pytest.approx(2000 * 0.5 / 5000)
    assert p.components["movement"] == pytest.approx(0.7)
    assert p.components["freshness"] == pytest.approx(1 - 30 / 300)


def test_exposure_cap_trims_then_blocks():
    s = replace(S, max_daily_exposure_pct=0.03, max_sport_exposure_pct=0.03)  # $30
    rows = [row(event=f"E{i}", market_ticker=f"E{i}-AAA") for i in range(3)]
    r = rank(rows, s, NOW)
    assert [float(p.stake) for p in r.picks][0] == pytest.approx(19.67)
    assert r.picks[1].stake <= D("10.33") and "exposure cap" in r.picks[1].reason
    assert len(r.picks) == 2
    assert any("exposure cap reached" in b for p in r.rows for b in p.blocked)


def test_open_paper_bets_today_count_toward_exposure():
    bets = [
        {"status": "open", "created_at": iso(NOW - timedelta(hours=1)), "contracts": "100", "price": "0.50", "fee_per_contract": "0.02", "sport": "nfl"},
        {"status": "open", "created_at": iso(NOW - timedelta(days=2)), "contracts": "999", "price": "0.50", "fee_per_contract": "0.02", "sport": "nfl"},
        {"status": "settled", "created_at": iso(NOW), "contracts": "999", "price": "0.5", "fee_per_contract": "0", "sport": "nfl"},
    ]
    total, by = open_exposure_today(bets, NOW)
    assert total == D("52") and by["nfl"] == D("52")
    s = replace(S, max_daily_exposure_pct=0.06, max_sport_exposure_pct=0.06)  # $60 - $52 = $8 left
    p = rank([row()], s, NOW, open_bets=bets).picks[0]
    assert p.stake <= D(8)


def test_top_n():
    rows = [row(event=f"E{i}", market_ticker=f"E{i}-AAA") for i in range(12)]
    s = replace(S, top_n=5, max_daily_exposure_pct=1, max_sport_exposure_pct=1)
    assert len(rank(rows, s, NOW).picks) == 5


def test_settings_overrides_are_typed():
    s = apply_overrides(S, {"bankroll": "2500", "top_n": "7", "min_net_edge_pts": "2.5", "w_edge": "0.5", "nope": "1"})
    assert s.bankroll == D("2500") and s.top_n == 7 and s.min_net_edge_pts == 2.5 and s.w_edge == 0.5
