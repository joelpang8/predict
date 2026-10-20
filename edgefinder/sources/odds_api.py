"""The Odds API v4 client (moneylines only).

Verified 2026-10-20 against https://the-odds-api.com/liveapi/guides/v4/ :
  GET https://api.the-odds-api.com/v4/sports/{sport}/odds
      ?apiKey&regions|bookmakers&markets=h2h&oddsFormat=american&dateFormat=iso
  Cost = markets x regions; with `bookmakers`, each group of 10 books = 1 region.
  A call that returns no events costs nothing.
  Usage headers: x-requests-remaining, x-requests-used, x-requests-last.
  429 on bursts: space requests over several seconds.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.the-odds-api.com/v4"
SPORT_KEYS = {"nfl": "americanfootball_nfl", "nba": "basketball_nba", "mlb": "baseball_mlb"}


class OddsApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class BookOdds:
    book: str
    last_update: datetime | None
    prices: dict[str, float]  # outcome name -> American odds (h2h)


@dataclass(frozen=True)
class OddsEvent:
    id: str
    sport_key: str
    commence_time: datetime
    home_team: str
    away_team: str
    books: tuple[BookOdds, ...]

    @classmethod
    def from_api(cls, e: dict) -> "OddsEvent":
        books = []
        for b in e.get("bookmakers") or []:
            for m in b.get("markets") or []:
                if m.get("key") != "h2h":
                    continue
                prices = {o["name"]: float(o["price"]) for o in m.get("outcomes") or []}
                lu = m.get("last_update") or b.get("last_update")
                books.append(BookOdds(b["key"], _ts(lu), prices))
        return cls(
            id=e["id"],
            sport_key=e["sport_key"],
            commence_time=_ts(e["commence_time"]),
            home_team=e["home_team"],
            away_team=e["away_team"],
            books=tuple(books),
        )


@dataclass
class Usage:
    """Credit accounting from response headers, logged per call."""

    last_cost: int | None = None
    used: int | None = None
    remaining: int | None = None
    calls: list[dict] = field(default_factory=list)

    @property
    def spent_this_session(self) -> int:
        return sum(c["cost"] or 0 for c in self.calls)


def _ts(v: str | None) -> datetime | None:
    if not v:
        return None
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


def _int(v: str | None) -> int | None:
    try:
        return int(float(v)) if v is not None else None
    except ValueError:
        return None


def expected_cost(bookmakers: tuple[str, ...], regions: tuple[str, ...], markets: int = 1) -> int:
    """Credits one odds call will cost, per the documented formula."""
    region_units = math.ceil(len(bookmakers) / 10) if bookmakers else len(regions)
    return markets * region_units


class OddsApiClient:
    def __init__(
        self,
        api_key: str,
        bookmakers: tuple[str, ...] = (),
        regions: tuple[str, ...] = ("us",),
        min_remaining: int = 0,
        transport: httpx.BaseTransport | None = None,
    ):
        if not api_key:
            raise OddsApiError("ODDS_API_KEY is not set (see .env.example)")
        self._key = api_key
        self.bookmakers, self.regions, self.min_remaining = bookmakers, regions, min_remaining
        self.usage = Usage()
        self._http = httpx.Client(base_url=BASE_URL, timeout=20, transport=transport)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "OddsApiClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def get_moneylines(self, sport: str) -> tuple[list[OddsEvent], datetime]:
        cost = expected_cost(self.bookmakers, self.regions)
        if self.usage.remaining is not None and self.usage.remaining - cost < self.min_remaining:
            raise OddsApiError(
                f"credit reserve reached: {self.usage.remaining} left, call costs {cost}, reserve {self.min_remaining}"
            )
        params = {"apiKey": self._key, "markets": "h2h", "oddsFormat": "american", "dateFormat": "iso"}
        if self.bookmakers:
            params["bookmakers"] = ",".join(self.bookmakers)
        else:
            params["regions"] = ",".join(self.regions)
        resp = self._http.get(f"/sports/{SPORT_KEYS[sport]}/odds", params=params)
        fetched = datetime.now(timezone.utc)
        self._record(sport, resp)
        if resp.status_code != 200:
            raise OddsApiError(f"odds HTTP {resp.status_code}: {resp.text[:300]}")
        return [OddsEvent.from_api(e) for e in resp.json()], fetched

    def _record(self, sport: str, resp: httpx.Response) -> None:
        h = resp.headers
        self.usage.last_cost = _int(h.get("x-requests-last"))
        self.usage.used = _int(h.get("x-requests-used"))
        self.usage.remaining = _int(h.get("x-requests-remaining"))
        entry = {
            "sport": sport,
            "status": resp.status_code,
            "cost": self.usage.last_cost,
            "remaining": self.usage.remaining,
            "used": self.usage.used,
        }
        self.usage.calls.append(entry)
        log.info("odds api call %s", entry)
