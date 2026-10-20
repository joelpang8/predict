"""Best Picks: guardrails, one transparent score, de-duplication, exposure caps.

Every number in the score is shown in the UI. Components are each scaled to
0..1 and weighted (weights in config):

  edge       net edge after fees / `edge_full_pts`
  model      share of our edge that the Elo model also sees (0.5 if no model)
  liquidity  $ available at the ask / `liquidity_full_usd`
  movement   0.5 + (6h move toward this side, pts) / 10   (0.5 if unknown)
  freshness  1 - data age / max_data_age_s

Penalties are subtracted for: "check for news", few books, books disagreeing,
no model, model disagreeing, early season, starters not listed.

Guardrails decide eligibility (all must pass) and are separate from the score:
the score only orders rows that already passed.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from zoneinfo import ZoneInfo

from edgefinder.config import Settings
from edgefinder.edge import FeeParams, size_and_fee
from edgefinder.store.db import parse_ts

ET = ZoneInfo("America/New_York")


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


@dataclass
class Pick:
    row: dict
    contracts: Decimal
    stake: Decimal
    fee_per_contract: Decimal
    net_edge: float  # at the sized quantity
    ev_dollars: Decimal
    score: float = 0.0
    components: dict[str, float] = field(default_factory=dict)
    penalties: list[tuple[str, float]] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)  # failed guardrails; empty = eligible
    reason: str = ""

    @property
    def eligible(self) -> bool:
        return not self.blocked

    @property
    def liquidity_usd(self) -> Decimal | None:
        d = self.row.get("ask_depth")
        return Decimal(d) * Decimal(self.row["ask"]) if d is not None else None


@dataclass
class Ranking:
    picks: list[Pick]  # eligible, ranked, capped, top N
    rows: list[Pick]  # every row, sized/scored, with any guardrail failures
    summary: str  # why the list is short or empty


def size_row(row: dict, s: Settings) -> tuple[Decimal, Decimal, Decimal, float]:
    """(contracts, stake, fee per contract, net edge) at the current bankroll/Kelly settings."""
    fees = FeeParams(row["fee_type"], Decimal(row["fee_multiplier"]))
    depth = Decimal(row["ask_depth"]) if row.get("ask_depth") is not None else None
    ask = Decimal(row["ask"])
    fee_pc, n, stake = size_and_fee(row["fair"], ask, depth, fees, s)
    return n, stake, fee_pc, row["fair"] - float(ask) - float(fee_pc)


def _components(p: Pick, now: datetime, s: Settings) -> None:
    r = p.row
    net_pts = p.net_edge * 100
    c = {"edge": _clamp(net_pts / s.edge_full_pts)}
    ask, fee = float(r["ask"]), float(p.fee_per_contract)
    if r.get("model_fair") is None:
        c["model"] = 0.5
        p.penalties.append(("no model estimate", s.p_no_model))
    else:
        model_edge = r["model_fair"] - ask - fee
        c["model"] = _clamp(model_edge / p.net_edge) if p.net_edge > 0 else 0.0
        if model_edge < 0:
            p.penalties.append((f"Elo disagrees ({r['model_fair'] * 100:.0f}%)", s.p_model_against))
    liq = p.liquidity_usd
    c["liquidity"] = _clamp(float(liq / s.liquidity_full_usd)) if liq is not None else 0.0
    move = r.get("move_6h") if r.get("move_6h") is not None else r.get("move_1h")
    c["movement"] = 0.5 if move is None else _clamp(0.5 + move / 10)
    age = (now - min(parse_ts(r["kalshi_fetched"]), parse_ts(r["odds_fetched"]))).total_seconds()
    c["freshness"] = _clamp(1 - age / s.max_data_age_s)
    p.components = c

    flags = r.get("flags") or []
    if any(f.startswith("check for news") for f in flags):
        p.penalties.append(("check for news", s.p_news))
    nbooks = len(r["books"].split(",")) if r.get("books") else 0
    if nbooks < s.min_books + 2:
        p.penalties.append((f"only {nbooks} books", s.p_few_books))
    if r.get("book_spread") is not None and r["book_spread"] > 0.04:
        p.penalties.append((f"books disagree by {r['book_spread'] * 100:.1f} pts", s.p_book_disagree))
    note = r.get("model_note") or ""
    if "early season" in note:
        p.penalties.append(("early season", s.p_early_season))
    if "not listed" in note:
        p.penalties.append(("starters not listed", s.p_no_starters))

    weights = {"edge": s.w_edge, "model": s.w_model, "liquidity": s.w_liquidity, "movement": s.w_movement, "freshness": s.w_freshness}
    p.score = sum(weights[k] * v for k, v in c.items()) - sum(v for _, v in p.penalties)


def _guardrails(p: Pick, now: datetime, stale: bool, s: Settings) -> None:
    r = p.row
    if stale:
        p.blocked.append("data is stale")
    if p.net_edge * 100 < s.min_net_edge_pts:
        p.blocked.append(f"net edge {p.net_edge * 100:+.1f} < {s.min_net_edge_pts:g} pts")
    liq = p.liquidity_usd
    if liq is None or liq < s.min_liquidity_usd:
        p.blocked.append(f"liquidity ${liq or 0:,.0f} < ${s.min_liquidity_usd:,.0f}")
    spread = Decimal(r["spread"]) * 100 if r.get("spread") is not None else None
    if spread is None or spread > Decimal(str(s.max_spread_cents)):
        p.blocked.append("spread unknown" if spread is None else f"spread {spread:.0f}c > {s.max_spread_cents:g}c")
    kickoff = parse_ts(r["kickoff"])
    if kickoff <= now:
        p.blocked.append("game started")
    elif kickoff - now < timedelta(minutes=s.near_start_min):
        age = (now - min(parse_ts(r["kalshi_fetched"]), parse_ts(r["odds_fetched"]))).total_seconds()
        if age > s.near_start_max_age_s:
            p.blocked.append(f"{(kickoff - now).total_seconds() / 60:.0f} min to kickoff and data {age:.0f}s old")
    if p.contracts <= 0:
        p.blocked.append("suggested size rounds to 0")


def reason_line(p: Pick) -> str:
    r = p.row
    parts = [
        f"Sportsbook consensus {r['fair'] * 100:.0f}% ({len(r['books'].split(','))} books)",
        f"Kalshi ask {Decimal(r['ask']) * 100:.0f}c",
        f"net edge {p.net_edge * 100:+.1f} after fees",
    ]
    if r.get("model_fair") is not None:
        model_edge = r["model_fair"] - float(r["ask"]) - float(p.fee_per_contract)
        if model_edge <= 0:
            verdict = "Elo sees no edge"
        elif p.net_edge <= 0:
            verdict = "Elo sees an edge the books don't"
        else:
            verdict = "Elo agrees" if p.components.get("model", 0) >= 0.5 else "Elo partly agrees"
        parts.append(f"Elo {r['model_fair'] * 100:.0f}% ({verdict})")
    move = r.get("move_6h")
    if move is not None and abs(move) >= 1:
        parts.append(f"{move:+.0f} pts in 6h {'toward' if move > 0 else 'against'} this side")
    if r.get("trend_score") is not None and r["trend_score"] >= 2:
        parts.append(f"volume {r['trend_score']:.1f}x normal")
    if p.liquidity_usd is not None:
        parts.append(f"${p.liquidity_usd:,.0f} at the ask")
    note = r.get("model_note") or ""
    if "QBs:" in note:
        parts.append("starters listed")
    if p.penalties:
        parts.append("caution: " + ", ".join(n for n, _ in p.penalties))
    return "; ".join(parts)


def _category(blocked: str, s: Settings) -> str:
    """Group guardrail failures for the 'No good bets' summary."""
    for prefix, label in (
        ("net edge", f"net edge under {s.min_net_edge_pts:g} pts"),
        ("liquidity", f"under ${s.min_liquidity_usd:,.0f} at the ask"),
        ("spread unknown", "spread unknown"),
        ("spread", f"spread over {s.max_spread_cents:g}c"),
        ("suggested size", "size rounds to 0"),
        ("daily/sport exposure", "exposure cap"),
    ):
        if blocked.startswith(prefix):
            return label
    return "too close to kickoff" if "to kickoff" in blocked else blocked


def _outcome_key(r: dict) -> tuple[str, str]:
    """YES on A and NO on B are the same bet in a two-team game (both pay $0.50 on a tie)."""
    return r["event_ticker"], r["team"] if r["side"] == "YES" else r["opponent"]


def open_exposure_today(bets: list[dict], now: datetime) -> tuple[Decimal, dict[str, Decimal]]:
    start = now.astimezone(ET).replace(hour=0, minute=0, second=0, microsecond=0)
    total, by_sport = Decimal(0), defaultdict(Decimal)
    for b in bets:
        if b["status"] == "open" and parse_ts(b["created_at"]) >= start:
            stake = Decimal(b["contracts"]) * (Decimal(b["price"]) + Decimal(b["fee_per_contract"]))
            total += stake
            by_sport[b["sport"]] += stake
    return total, by_sport


def rank(rows: list[dict], s: Settings, now: datetime, stale: bool = False, open_bets: list[dict] | None = None) -> Ranking:
    scored = []
    for r in rows:
        n, stake, fee_pc, net = size_row(r, s)
        p = Pick(r, n, stake, fee_pc, net, n * Decimal(str(net)))
        _components(p, now, s)
        _guardrails(p, now, stale, s)
        p.reason = reason_line(p)
        scored.append(p)

    eligible = sorted((p for p in scored if p.eligible), key=lambda p: -p.score)
    # one pick per outcome, then one per game
    seen_outcome, seen_game, deduped = set(), set(), []
    for p in eligible:
        ko, kg = _outcome_key(p.row), p.row["event_ticker"]
        if ko in seen_outcome or kg in seen_game:
            continue
        seen_outcome.add(ko)
        seen_game.add(kg)
        deduped.append(p)

    used_total, used_sport = open_exposure_today(open_bets or [], now)
    cap_total = s.bankroll * Decimal(str(s.max_daily_exposure_pct))
    cap_sport = s.bankroll * Decimal(str(s.max_sport_exposure_pct))
    picks = []
    for p in deduped:
        if len(picks) >= s.top_n:
            break
        room = min(cap_total - used_total, cap_sport - used_sport[p.row["sport"]])
        cost = Decimal(p.row["ask"]) + p.fee_per_contract
        if p.stake > room:
            n = (room / cost).to_integral_value(rounding=ROUND_FLOOR) if room > 0 else Decimal(0)
            if n <= 0:
                p.blocked.append("daily/sport exposure cap reached")
                continue
            p.contracts, p.stake = n, n * cost
            p.ev_dollars = n * Decimal(str(p.net_edge))
            p.reason += f"; size cut to {n:.0f} by exposure cap"
        used_total += p.stake
        used_sport[p.row["sport"]] += p.stake
        picks.append(p)

    if picks:
        summary = f"{len(picks)} pick(s) from {len(rows)} sides."
    elif not rows:
        summary = "No computed markets yet. Press Refresh."
    else:
        counts = defaultdict(int)
        for p in scored:
            for b in p.blocked:
                counts[_category(b, s)] += 1
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:3]
        summary = f"No good bets today: 0 of {len(rows)} sides cleared every guardrail (" + "; ".join(f"{k}: {v}" for k, v in top) + ")."
    return Ranking(picks, scored, summary)
