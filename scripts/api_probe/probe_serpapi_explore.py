"""Probe: SerpAPI engine `google_travel_explore` with a fixed arrival_id.

Question: ADR 0001 rejected `google_flights_deals` because it ignored
arrival_id. Does Travel Explore honor arrival_id and pick cheap dates for one
route when given a flexible month + trip length instead of fixed dates?

Request shape (docs relied on):
  - Engine param list (SerpAPI's own MCP engine JSON):
    https://raw.githubusercontent.com/serpapi/serpapi-mcp/main/engines/google_travel_explore.json
    -> departure_id, arrival_id, type (1=round trip), month (1-12, 0=next 6
       months), travel_duration (1=weekend, 2=1 week, 3=2 weeks), stops
       (1=nonstop), currency, hl, gl. No 5-day option: 2 (1 week) is closest.
  - Response keys (destinations[].destination_airport.code, flight_price,
    start_date, end_date, airline, airline_code, number_of_stops; "Empty
    results" comes back as an `error` payload):
    https://serpapi.com/google-travel-explore-api and
    https://github.com/serpapi/serpapi-superapp (destination_explorer.py)
Budget: HARD max 2 searches (Free Plan, 250/cycle nearly allocated). Identical
params within 1h are served from SerpAPI cache for free.
"""
from __future__ import annotations

import re
import time
from datetime import date, timedelta

PIP_DEPS: list[str] = []
PROVIDER = "serpapi_explore"
URL = "https://serpapi.com/search"
ENV = "SERPAPI_KEY"
SIGNUP = "https://serpapi.com/users/sign_up"
CASES = [("KHH", "NGO"), ("TPE", "NRT")]   # exactly 2 calls, no retries
_sleep = time.sleep


def _new_session():
    import requests
    return requests.Session()


def _scrub(text, secret: str) -> str:
    text = str(text)
    if secret:
        text = text.replace(secret, "***")
    return re.sub(r"(api_key=)[^&\s'\"]+", r"\1***", text)


def _row(case, route, **kw) -> dict:
    row = {"provider": PROVIDER, "case": case, "route": route, "ok": False,
           "results": None, "dates": None, "min_price": None, "currency": None,
           "carriers": None, "calls": 0, "note": ""}
    row.update(kw)
    row["note"] = str(row["note"])[:300]
    return row


def _parse(payload: dict, arr: str) -> dict:
    meta = payload.get("search_metadata") or {}
    echo = (payload.get("search_parameters") or {}).get("arrival_id")
    dests = payload.get("destinations") or []
    if isinstance(dests, dict):
        dests = [dests]
    dests = [d for d in dests if isinstance(d, dict)]
    priced = [d for d in dests if isinstance(d.get("flight_price"), (int, float))]
    codes = sorted({(d.get("destination_airport") or {}).get("code") or "?" for d in priced})
    match = [d for d in priced if (d.get("destination_airport") or {}).get("code") == arr]
    match.sort(key=lambda d: d["flight_price"])
    carriers = list(dict.fromkeys(str(d.get("airline_code") or d.get("airline"))
                                  for d in match if d.get("airline_code") or d.get("airline")))
    extra = sorted(k for k in payload if k not in ("search_metadata", "search_parameters"))
    best = (f"best {match[0].get('start_date')}->{match[0].get('end_date')} "
            f"{match[0]['flight_price']} {match[0].get('airline')} "
            f"stops={match[0].get('number_of_stops')}") if match else "no priced row for arrival"
    note = (f"status={meta.get('status')}; echo arrival_id={echo}; arrival honored "
            f"{len(match)}/{len(priced)} priced of {len(dests)} rows "
            f"(codes {','.join(codes[:6]) or '-'}); {best}; keys={','.join(extra)}")
    return dict(ok=True, results=len(dests),
                dates=len({d.get("start_date") for d in match if d.get("start_date")}),
                min_price=float(match[0]["flight_price"]) if match else None,
                carriers=",".join(carriers[:8]) or None, note=note)


def probe(env: dict, today: date) -> list[dict]:
    key = (env or {}).get(ENV, "").strip()
    if not key:
        return [_row("google_travel_explore", "-",
                     note=f"SKIPPED: {ENV} not set (needs user signup at {SIGNUP})")]
    # Flexible dates: the month ~45 days out (centre of the 30-60 day window).
    month = (today + timedelta(days=45)).month
    rows = []
    try:
        session = _new_session()
    except Exception as exc:  # requests missing
        return [_row("google_travel_explore", "-", note=_scrub(f"import failed: {exc!r}", key))]
    for i, (dep, arr) in enumerate(CASES):
        route = f"{dep}-{arr}"
        case = f"{route} explore RT month={month} 1wk nonstop TWD"
        if i:
            _sleep(0.6)
        params = {"engine": "google_travel_explore", "departure_id": dep,
                  "arrival_id": arr, "type": 1, "month": month,
                  "travel_duration": 2, "stops": 1, "currency": "TWD",
                  "hl": "zh-tw", "gl": "tw", "api_key": key}
        try:
            resp = session.get(URL, params=params, timeout=60)
            try:
                payload = resp.json()
            except ValueError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            if resp.status_code != 200 or payload.get("error") or not payload:
                err = payload.get("error") or resp.text[:200]
                status = (payload.get("search_metadata") or {}).get("status")
                empty = resp.status_code == 200 and "results" in str(err).lower()  # "Empty results"
                rows.append(_row(case, route, calls=1, currency="TWD",
                                 results=0 if empty else None,
                                 note=_scrub(f"HTTP {resp.status_code} status={status}: {err}", key)))
                continue
            rows.append(_row(case, route, calls=1, currency="TWD", **_parse(payload, arr)))
        except Exception as exc:
            rows.append(_row(case, route, calls=1, note=_scrub(f"{type(exc).__name__}: {exc}", key)))
    return rows
