"""SearchApi.io Google Flights Calendar — weekly real-price sweeps.

One calendar request returns date→price for a whole outbound window
(real Google Flights prices, not cache). We sweep every route weekly and
record the results as fare_class='any' observations with source='google';
the dashboard prefers a fresh google price over the Aviasales cache price
for the same departure date.

Note: calendar rows carry NO airline information — carriers is left empty
and the UI labels these prices as Google 實價.

Docs: https://www.searchapi.io/docs/google-flights-calendar-api
"""
from __future__ import annotations

import os
import logging
from datetime import date, timedelta

import requests

from .models import Offer

log = logging.getLogger(__name__)

BASE_URL = "https://www.searchapi.io/api/v1/search"
TRIP_NIGHTS = (4, 5, 6)          # accept 4–6 night pairs, min per departure


class SearchApiError(RuntimeError):
    pass


def fetch_calendar(origin: str, destination: str,
                   out_start: date, out_end: date,
                   currency: str = "TWD",
                   api_key: str | None = None,
                   session: requests.Session | None = None) -> dict:
    key = api_key or os.environ.get("SEARCHAPI_KEY", "")
    if not key:
        raise RuntimeError("Missing SEARCHAPI_KEY environment variable.")
    base_out = out_start + timedelta(days=7)
    params = {
        "engine": "google_flights_calendar",
        "flight_type": "round_trip",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": base_out.isoformat(),
        "return_date": (base_out + timedelta(days=5)).isoformat(),
        "outbound_date_start": out_start.isoformat(),
        "outbound_date_end": out_end.isoformat(),
        "return_date_start": (out_start + timedelta(days=min(TRIP_NIGHTS))).isoformat(),
        "return_date_end": (out_end + timedelta(days=max(TRIP_NIGHTS))).isoformat(),
        "stops": "nonstop",
        "currency": currency,
        "api_key": key,
    }
    s = session or requests
    resp = s.get(BASE_URL, params=params, timeout=90)
    payload = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if resp.status_code != 200 or payload.get("error"):
        msg = str(payload.get("error") or resp.text[:300])
        if "stops" in msg.lower():
            raise SearchApiError(
                f"stops 參數不被接受（{msg}）— 中止以避免混入轉機價，需修正參數值")
        raise SearchApiError(f"HTTP {resp.status_code}: {msg}")
    return payload


def parse_calendar(payload: dict, origin: str, destination: str,
                   currency: str = "TWD",
                   today: date | None = None) -> list[Offer]:
    """calendar: [{departure, return, price, ...}] -> cheapest 4–6 night
    roundtrip per departure date."""
    today_iso = (today or date.today()).isoformat()
    best: dict[str, Offer] = {}
    for row in payload.get("calendar", []):
        try:
            if row.get("has_no_flights") or "price" not in row:
                continue
            dep, ret = str(row["departure"]), str(row.get("return") or "")
            if dep < today_iso or not ret:
                continue
            nights = (date.fromisoformat(ret) - date.fromisoformat(dep)).days
            if nights not in TRIP_NIGHTS:
                continue
            price = float(row["price"])
            prev = best.get(dep)
            if prev is None or price < prev.price:
                link = ("https://www.google.com/travel/flights?q=" +
                        f"Flights%20from%20{origin}%20to%20{destination}"
                        f"%20on%20{dep}%20through%20{ret}")
                best[dep] = Offer(origin=origin, destination=destination,
                                  depart_date=dep, return_date=ret,
                                  price=price, currency=currency,
                                  carriers="", stops=0, duration="",
                                  link=link, fare_class="any",
                                  source="google")
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("Skipping malformed calendar row: %s", exc)
    return [best[k] for k in sorted(best)]
