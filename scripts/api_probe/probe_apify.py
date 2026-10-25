"""Probe: Apify Store Google Flights actor with a per-date price calendar.

Actor: lagenuina_agency/google-flights-scraper. Chosen over the suggested
lergassy/thirdwatch/kestrel actors because its vendor publishes a working
example with the exact run-sync call, input fields and real output fields:
  https://github.com/assa83/google-flights-api (README.md, python/example.py,
  n8n/workflow.json)
  input:  origin, destination, departureDate, returnDate, currency, sortBy,
          maxResults, calendarDays (cheapest fare per day for the next N days)
  output: rows typed "flight" (price, currency, airlines, flightNumbers,
          stops, departureDate) | "calendarDay" (departureDate, lowestPrice,
          currency) | "error" (error).
  price:  $0.16 / 1,000 rows on the Free plan, no start fee.
To try lergassy instead, change ACTOR_ID (same input names, plus
"calendarOnly": true; its rows are typed "calendar_day" -- parsed too).

API: POST https://api.apify.com/v2/acts/{user}~{actor}/run-sync-get-dataset-items
with "Authorization: Bearer <token>"; query `timeout` (run seconds), `maxItems`
(cap on charged rows) and `maxTotalChargeUsd` (cap on run cost):
  https://docs.apify.com/api/v2/act-run-sync-get-dataset-items-post
Budget: 3 runs, <= 150 rows total (~$0.024); each run hard-capped at $0.03.
"""
from __future__ import annotations

import time
from datetime import date, timedelta

PIP_DEPS: list[str] = []
PROVIDER = "apify"
ACTOR_ID = "lagenuina_agency~google-flights-scraper"
URL = f"https://api.apify.com/v2/acts/{ACTOR_ID}/run-sync-get-dataset-items"
ENV = "APIFY_TOKEN"
SIGNUP = "https://console.apify.com/sign-up"
RUN_TIMEOUT_S = 55          # the run aborts itself before the 60 s HTTP timeout
MAX_CHARGE_USD = "0.03"
PRICE_PER_ROW = 0.16 / 1000
_sleep = time.sleep


def _new_session():
    import requests
    return requests.Session()


def _scrub(text, secret: str) -> str:
    text = str(text)
    return text.replace(secret, "***") if secret else text


def _row(case, route, **kw) -> dict:
    row = {"provider": PROVIDER, "case": case, "route": route, "ok": False,
           "results": None, "dates": None, "min_price": None, "currency": None,
           "carriers": None, "calls": 0, "note": ""}
    row.update(kw)
    row["note"] = str(row["note"])[:300]
    return row


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _parse(items: list, calendar: bool) -> dict:
    items = [i for i in items if isinstance(i, dict)]
    types: dict = {}
    for i in items:
        types[str(i.get("type"))] = types.get(str(i.get("type")), 0) + 1
    flights = [i for i in items if i.get("type") == "flight" and _num(i.get("price")) is not None]
    flights.sort(key=lambda i: _num(i["price"]))
    days = [i for i in items if i.get("type") in ("calendarDay", "calendar_day")]
    priced = [(i.get("departureDate") or i.get("date"), _num(i.get("lowestPrice", i.get("price"))), i)
              for i in days]
    priced = sorted((p for p in priced if p[1] is not None), key=lambda p: p[1])
    codes = list(dict.fromkeys(str(fn).split(" ")[0] for f in flights
                               for fn in (f.get("flightNumbers") or []) if fn))
    errors = [str(i.get("error"))[:80] for i in items if i.get("type") == "error"]
    cost = f"~${len(items) * PRICE_PER_ROW:.4f}"
    nonstop = [f for f in flights if f.get("stops") == 0]
    fl = (f"cheapest flight {flights[0]['price']} {','.join(flights[0].get('airlines') or [])} "
          f"{flights[0].get('departureDate')} stops={flights[0].get('stops')}; "
          f"nonstop min {nonstop[0]['price'] if nonstop else None}") if flights else "no flight rows"
    if calendar:
        top = ", ".join(f"{p[0]}={p[1]:g}" for p in priced[:3])
        rt = any(p[2].get("returnDate") for p in priced)
        note = (f"{len(priced)}/{len(days)} days priced; cheapest {top or '-'}; "
                f"calendar has returnDate={rt}; {fl}; types={types}; {cost}")
        res = dict(results=len(days), dates=len(priced),
                   min_price=priced[0][1] if priced else None,
                   currency=(priced[0][2].get("currency") if priced else None))
    else:
        level = ((flights[0].get("priceInsights") or {}).get("priceLevel")) if flights else None
        note = f"{fl}; google priceLevel={level}; types={types}; {cost}"
        res = dict(results=len(flights),
                   dates=len({f.get("departureDate") for f in flights if f.get("departureDate")}),
                   min_price=_num(flights[0]["price"]) if flights else None,
                   currency=flights[0].get("currency") if flights else None)
    if errors:
        note = f"actor errors: {'; '.join(errors[:2])}; " + note
    return dict(ok=bool(flights or priced) or not errors, carriers=",".join(codes[:8]) or None,
                note=note, **res)


def probe(env: dict, today: date) -> list[dict]:
    token = (env or {}).get(ENV, "").strip()
    if not token:
        return [_row("google-flights actor", "-",
                     note=f"SKIPPED: {ENV} not set (needs user signup at {SIGNUP})")]

    def d(n: int) -> str:
        return (today + timedelta(days=n)).isoformat()

    def inp(o, t, dep, cal_days, max_results):
        return {"origin": o, "destination": t, "departureDate": d(dep),
                "returnDate": d(dep + 5), "currency": "TWD", "sortBy": "price",
                "maxResults": max_results, "calendarDays": cal_days}

    cases = [  # (case, route, input, maxItems, is_calendar)
        (f"KHH-NGO calendar 60d from {d(14)} RT+5d", "KHH-NGO", inp("KHH", "NGO", 14, 60, 1), 65, True),
        (f"TPE-NRT calendar 60d from {d(14)} RT+5d", "TPE-NRT", inp("TPE", "NRT", 14, 60, 1), 65, True),
        (f"KHH-NGO flights {d(35)} RT+5d", "KHH-NGO", inp("KHH", "NGO", 35, 0, 20), 20, False),
    ]
    try:
        session = _new_session()
    except Exception as exc:
        return [_row("google-flights actor", "-", note=_scrub(f"import failed: {exc!r}", token))]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    rows = []
    for i, (case, route, body, max_items, is_cal) in enumerate(cases):
        if i:
            _sleep(0.6)
        params = {"timeout": RUN_TIMEOUT_S, "maxItems": max_items,
                  "maxTotalChargeUsd": MAX_CHARGE_USD}
        try:
            resp = session.post(URL, params=params, json=body, headers=headers, timeout=60)
            try:
                data = resp.json()
            except ValueError:
                data = None
            if not resp.ok or not isinstance(data, list):
                err = data.get("error") if isinstance(data, dict) else None
                msg = (err.get("message") or err.get("type")) if isinstance(err, dict) else err
                rows.append(_row(case, route, calls=1, note=_scrub(
                    f"HTTP {resp.status_code}: {msg or resp.text[:200]}", token)))
                continue
            rows.append(_row(case, route, calls=1, **_parse(data, is_cal)))
        except Exception as exc:
            rows.append(_row(case, route, calls=1, note=_scrub(f"{type(exc).__name__}: {exc}", token)))
    return rows
