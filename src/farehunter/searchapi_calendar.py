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
    # API 限制: 去程天數 × 回程天數 ≤ 200 組合。14×14=196 是安全上限，
    # 回程窗與去程窗同步位移 +5 天（主力 5 晚，邊緣 4/6 晚部分涵蓋）。
    if (out_end - out_start).days > 13:
        out_end = out_start + timedelta(days=13)
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
        "return_date_start": (out_start + timedelta(days=5)).isoformat(),
        "return_date_end": (out_end + timedelta(days=5)).isoformat(),
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
                                  source="google", provider="searchapi")
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("Skipping malformed calendar row: %s", exc)
    return [best[k] for k in sorted(best)]


def fetch_oneway_calendar(origin: str, destination: str,
                          start: date, end: date,
                          currency: str = "TWD",
                          api_key: str | None = None,
                          session: requests.Session | None = None) -> dict:
    """One-way calendar: no date matrix, so a single request covers up to
    ~200 departure days — the long-range engine."""
    key = api_key or os.environ.get("SEARCHAPI_KEY", "")
    if not key:
        raise RuntimeError("Missing SEARCHAPI_KEY environment variable.")
    if (end - start).days > 199:
        end = start + timedelta(days=199)
    s_ = session or requests
    resp = s_.get(BASE_URL, params={
        "engine": "google_flights_calendar",
        "flight_type": "one_way",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": (start + timedelta(days=7)).isoformat(),
        "outbound_date_start": start.isoformat(),
        "outbound_date_end": end.isoformat(),
        "stops": "nonstop",
        "currency": currency,
        "api_key": key,
    }, timeout=120)
    payload = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if resp.status_code != 200 or payload.get("error"):
        raise SearchApiError(f"HTTP {resp.status_code}: {payload.get('error') or resp.text[:300]}")
    return payload


def parse_oneway_prices(payload: dict, today: date | None = None) -> dict[str, float]:
    """calendar rows -> {departure_date: price}."""
    today_iso = (today or date.today()).isoformat()
    out: dict[str, float] = {}
    for row in payload.get("calendar", []):
        try:
            if row.get("has_no_flights") or "price" not in row:
                continue
            dep = str(row["departure"])
            if dep < today_iso:
                continue
            price = float(row["price"])
            if dep not in out or price < out[dep]:
                out[dep] = price
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("Skipping malformed one-way row: %s", exc)
    return out


def combine_roundtrips(out_prices: dict[str, float],
                       back_prices: dict[str, float],
                       nights: tuple[int, ...] = TRIP_NIGHTS) -> list[dict]:
    """Cheapest out+back sum per departure date（去回可不同航空，皆為可訂單程價）."""
    combos = []
    for dep, op in sorted(out_prices.items()):
        d0 = date.fromisoformat(dep)
        cands = []
        for n in nights:
            r = (d0 + timedelta(days=n)).isoformat()
            if r in back_prices:
                cands.append((op + back_prices[r], r, back_prices[r]))
        if cands:
            total, ret, rp = min(cands)
            combos.append({"depart_date": dep, "return_date": ret,
                           "total": total, "out_price": op, "ret_price": rp})
    return combos
