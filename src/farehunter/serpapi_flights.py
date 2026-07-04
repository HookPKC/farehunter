"""SerpAPI Google Flights client — daily snapshots of full-service carrier fares.

The Aviasales cache is a cheapest-fare feed and structurally excludes
full-service carriers (CI/BR/JX...). Google Flights has them; SerpAPI exposes
Google Flights as an API with a free tier (~100 searches/month). We spend
that budget as SEARCHES_PER_DAY route snapshots per day, rotating through the
configured routes, and record the cheapest all-full-service itinerary into the
existing fare_class='full' track.

Endpoint: GET https://serpapi.com/search?engine=google_flights
Auth: api_key query param (env var SERPAPI_KEY).
"""
from __future__ import annotations

import os
import logging
from datetime import date, timedelta

import requests

from .models import Offer
from .travelpayouts import FULL_SERVICE

log = logging.getLogger(__name__)

BASE_URL = "https://serpapi.com/search"
SEARCHES_PER_DAY = 3
HORIZON_WEEKS = [6, 12, 18, 26, 34, 42]   # ≈1.5/3/4/6/8/10 個月，輪替涵蓋整個規劃期


class SerpApiError(RuntimeError):
    pass


def search_google_flights(origin: str, destination: str,
                          outbound: str, ret: str,
                          currency: str = "TWD",
                          api_key: str | None = None,
                          session: requests.Session | None = None) -> dict:
    key = api_key or os.environ.get("SERPAPI_KEY", "")
    if not key:
        raise RuntimeError("Missing SERPAPI_KEY environment variable.")
    s = session or requests
    resp = s.get(BASE_URL, params={
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": outbound,
        "return_date": ret,
        "currency": currency,
        "stops": 1,                     # SerpAPI: 1 = 僅直飛
        "hl": "zh-TW",
        "api_key": key,
    }, timeout=90)
    if resp.status_code != 200:
        raise SerpApiError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    payload = resp.json()
    if payload.get("error"):
        raise SerpApiError(payload["error"])
    return payload


def _airline_code(flight_number: str) -> str:
    """'BR 198' -> 'BR'; 'IT2 34' malformed -> best effort first token."""
    return (flight_number or "").split(" ")[0].strip()


def parse_full_service(payload: dict, origin: str, destination: str,
                       outbound: str, ret: str) -> Offer | None:
    """Cheapest itinerary whose EVERY segment is a full-service carrier."""
    candidates = (payload.get("best_flights") or []) + (payload.get("other_flights") or [])
    best = None
    for it in candidates:
        try:
            segs = it.get("flights") or []
            if not segs:
                continue
            if len(segs) > 1:           # 轉機行程不列入
                continue
            codes = [_airline_code(s.get("flight_number", "")) for s in segs]
            if not all(c in FULL_SERVICE for c in codes):
                continue
            price = float(it["price"])
            if best is None or price < best[0]:
                best = (price, sorted(set(codes)), it.get("total_duration"))
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("Skipping malformed itinerary: %s", exc)
    if best is None:
        return None
    price, codes, total_dur = best
    link = (f"https://www.google.com/travel/flights?q=Flights%20from%20{origin}"
            f"%20to%20{destination}%20on%20{outbound}%20through%20{ret}")
    return Offer(origin=origin, destination=destination,
                 depart_date=outbound, return_date=ret,
                 price=price, currency="TWD",
                 carriers=",".join(codes), stops=0,
                 duration=str(total_dur or ""), link=link,
                 fare_class="full", source="google")


def parse_cheapest_direct(payload: dict, origin: str, destination: str,
                          outbound: str, ret: str) -> Offer | None:
    """Cheapest single-segment (direct) itinerary of ANY carrier, with its
    real airline code — the real monthly low with a confirmed carrier."""
    candidates = (payload.get("best_flights") or []) + (payload.get("other_flights") or [])
    best = None
    for it in candidates:
        try:
            segs = it.get("flights") or []
            if len(segs) != 1:
                continue
            code = _airline_code(segs[0].get("flight_number", ""))
            if not code:
                continue
            price = float(it["price"])
            if best is None or price < best[0]:
                best = (price, code, it.get("total_duration"))
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("Skipping malformed itinerary: %s", exc)
    if best is None:
        return None
    price, code, total_dur = best
    link = (f"https://www.google.com/travel/flights?q=Flights%20from%20{origin}"
            f"%20to%20{destination}%20on%20{outbound}%20through%20{ret}")
    return Offer(origin=origin, destination=destination,
                 depart_date=outbound, return_date=ret,
                 price=price, currency="TWD", carriers=code, stops=0,
                 duration=str(total_dur or ""), link=link,
                 fare_class="any", source="google")


def pick_routes_for_today(routes: list[dict], today: date | None = None,
                          per_day: int = SEARCHES_PER_DAY) -> list[dict]:
    """Deterministic daily rotation: consecutive slice of the route list."""
    if not routes:
        return []
    today = today or date.today()
    start = (today.toordinal() * per_day) % len(routes)
    return [routes[(start + i) % len(routes)] for i in range(min(per_day, len(routes)))]


def snapshot_dates(today: date | None = None,
                   horizon_weeks: int = 4) -> tuple[str, str]:
    """Departure ~N weeks out snapped to Friday; 5-day trip."""
    today = today or date.today()
    dep = today + timedelta(weeks=horizon_weeks)
    dep += timedelta(days=(4 - dep.weekday()) % 7)   # snap forward to Friday
    return dep.isoformat(), (dep + timedelta(days=5)).isoformat()


def horizon_for_slot(routes_count: int, today: date, slot: int,
                     per_day: int = SEARCHES_PER_DAY) -> int:
    """Cycle horizons as the rotation completes passes over the route list:
    pass 1 -> 4 weeks, pass 2 -> 12 weeks, pass 3 -> 24 weeks, repeat."""
    cumulative = today.toordinal() * per_day + slot
    return HORIZON_WEEKS[(cumulative // max(routes_count, 1)) % len(HORIZON_WEEKS)]
