"""Scrape.do Google Flights — daily airline verification for calendar prices.

The SearchApi calendar gives real per-date prices but no airline. Each day we
take the cheapest few unverified dates and run one real Google Flights search
each, recording the cheapest DIRECT itinerary with its actual carrier. The
dashboard chip then shows a confirmed airline instead of the ≈ estimate.

Endpoint: GET https://api.scrape.do/plugin/google/flights
Cost: 10 credits per successful request (free tier: 1,000 credits/month).
"""
from __future__ import annotations

import os
import logging

import requests

from .models import Offer

log = logging.getLogger(__name__)

BASE_URL = "https://api.scrape.do/plugin/google/flights"
VERIFICATIONS_PER_DAY = 3


class ScrapeDoError(RuntimeError):
    pass


def search_flights(origin: str, destination: str, outbound: str, ret: str,
                   currency: str = "TWD",
                   api_key: str | None = None,
                   session: requests.Session | None = None) -> dict:
    key = api_key or os.environ.get("SCRAPEDO_KEY", "")
    if not key:
        raise RuntimeError("Missing SCRAPEDO_KEY environment variable.")
    s = session or requests
    resp = s.get(BASE_URL, params={
        "token": key,
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": outbound,
        "return_date": ret,
        "currency": currency,
        "sort_by": 2,               # price ascending
        "hl": "zh-TW",
    }, timeout=90)
    try:
        payload = resp.json()
    except ValueError:
        raise ScrapeDoError(f"HTTP {resp.status_code}: non-JSON {resp.text[:200]}")
    if resp.status_code != 200 or payload.get("error"):
        raise ScrapeDoError(
            f"HTTP {resp.status_code}: {payload.get('message') or payload.get('error') or resp.text[:200]}")
    return payload


def _itineraries(payload: dict) -> list[dict]:
    """Tolerate both response shapes: best/other split, or one flights list."""
    tl = (payload.get("best_flights") or []) + (payload.get("other_flights") or [])
    if not tl:
        f = payload.get("flights")
        if isinstance(f, list) and f and isinstance(f[0], dict) and "price" in f[0]:
            tl = f
    return tl


def _airline_code(flight_number: str) -> str:
    return (flight_number or "").split(" ")[0].strip()


def parse_cheapest_direct(payload: dict, origin: str, destination: str,
                          outbound: str, ret: str,
                          currency: str = "TWD") -> Offer | None:
    """Cheapest single-segment (direct) itinerary with its actual carrier."""
    best = None
    for it in _itineraries(payload):
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
    link = ("https://www.google.com/travel/flights?q=" +
            f"Flights%20from%20{origin}%20to%20{destination}"
            f"%20on%20{outbound}%20through%20{ret}")
    return Offer(origin=origin, destination=destination,
                 depart_date=outbound, return_date=ret,
                 price=price, currency=currency, carriers=code,
                 stops=0, duration=str(total_dur or ""), link=link,
                 fare_class="any", source="google")
