"""Probe: Ignav flight API (live Google-Flights-class fares, REST + X-Api-Key).

Request shape (docs relied on):
  - Official agent skill (endpoints, body fields, response, error format):
    https://raw.githubusercontent.com/gusgordon/ignav-skill/main/SKILL.md
    POST /api/fares/round-trip {origin, destination, departure_date,
    return_date, adults, cabin_class, market}; response itineraries[].price
    {amount, currency, status}, outbound/inbound.segments[].marketing_carrier_code.
    Errors: {"error": {type, code, message, field}}; failed requests not billed.
  - Cheapest-dates calendar (renamed from /fares/calendar ~2026-10-19; body
    {origin, destination, departure_date_range: {start, end} (1-31 days),
    stay_days, adults, cabin_class, market}; response dates[].price | null),
    recorded live by a third party:
    https://github.com/JonasW04/country-fare-finder/blob/master/docs/provider-ignav.md
    https://ignav.com/docs/api-reference ("Cheapest Dates": one request = up to 31 dates)
  - `market` sets the currency (default US). TW is not listed in docs: the
    control case runs first and, if it is rejected for `market`, the rest fall
    back to US (no extra calls).
Budget: 4 calls (first 1,000 successful requests free, then $2/1,000).
"""
from __future__ import annotations

import time
from datetime import date, timedelta

PIP_DEPS: list[str] = []
PROVIDER = "ignav"
BASE = "https://ignav.com/api"
ENV = "IGNAV_API_KEY"
SIGNUP = "https://ignav.com/signup"
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


def _segs(it: dict, leg: str) -> list:
    return [s for s in ((it.get(leg) or {}).get("segments") or []) if isinstance(s, dict)]


def _parse_fares(body: dict) -> dict:
    its = [i for i in body.get("itineraries") or [] if isinstance(i, dict)
           and isinstance((i.get("price") or {}).get("amount"), (int, float))]
    its.sort(key=lambda i: i["price"]["amount"])
    carriers = list(dict.fromkeys(s.get("marketing_carrier_code") for i in its
                                  for s in _segs(i, "outbound") + _segs(i, "inbound")
                                  if s.get("marketing_carrier_code")))
    direct = [i for i in its if len(_segs(i, "outbound")) == 1 and len(_segs(i, "inbound")) <= 1]

    def desc(i):
        codes = ",".join(dict.fromkeys(s.get("marketing_carrier_code") or "?" for s in _segs(i, "outbound")))
        return f"{i['price']['amount']} {codes} {i['price'].get('status')}"
    note = (f"{len(its)} itins; cheapest {desc(its[0]) if its else '-'}; "
            f"cheapest nonstop {desc(direct[0]) if direct else 'none'} ({len(direct)} nonstop); "
            f"cache_hit={body.get('cache_hit')}")
    return dict(ok=True, results=len(its), dates=1 if its else 0,
                min_price=float(its[0]["price"]["amount"]) if its else None,
                currency=its[0]["price"].get("currency") if its else None,
                carriers=",".join(carriers[:8]) or None, note=note)


def _parse_calendar(body: dict) -> dict:
    days = [d for d in body.get("dates") or [] if isinstance(d, dict)]
    priced = [d for d in days if isinstance((d.get("price") or {}).get("amount"), (int, float))]
    priced.sort(key=lambda d: d["price"]["amount"])
    top = ", ".join(f"{d.get('departure_date')}/{d.get('return_date')}={d['price']['amount']}"
                    for d in priced[:3])
    note = (f"{len(priced)}/{len(days)} dates priced; cheapest {top or '-'}; "
            f"status={priced[0]['price'].get('status') if priced else None}; no carriers in calendar")
    return dict(ok=True, results=len(days), dates=len(priced),
                min_price=float(priced[0]["price"]["amount"]) if priced else None,
                currency=priced[0]["price"].get("currency") if priced else None, note=note)


def probe(env: dict, today: date) -> list[dict]:
    key = (env or {}).get(ENV, "").strip()
    if not key:
        return [_row("fares", "-", note=f"SKIPPED: {ENV} not set (needs user signup at {SIGNUP})")]
    def d(n: int) -> str:
        return (today + timedelta(days=n)).isoformat()

    def rt(o: str, t: str, n: int) -> tuple:
        return (f"{o}-{t} round-trip {d(n)}+5d", f"{o}-{t}", "/fares/round-trip",
                {"origin": o, "destination": t, "departure_date": d(n), "return_date": d(n + 5)},
                _parse_fares)

    cases = [  # control first: it doubles as the market=TW canary
        rt("TPE", "NRT", 35),
        rt("KHH", "NGO", 35),
        rt("KHH", "NGO", 75),
        (f"KHH-NGO cheapest-dates {d(30)}..{d(60)} stay 5d", "KHH-NGO", "/fares/cheapest-dates",
         {"origin": "KHH", "destination": "NGO",
          "departure_date_range": {"start": d(30), "end": d(60)}, "stay_days": 5},
         _parse_calendar),
    ]
    try:
        session = _new_session()
    except Exception as exc:
        return [_row("fares", "-", note=_scrub(f"import failed: {exc!r}", key))]
    headers = {"X-Api-Key": key, "Content-Type": "application/json", "Accept": "application/json"}
    market, rows = "TW", []
    for i, (case, route, path, body, parse) in enumerate(cases):
        if i:
            _sleep(0.6)
        payload = {**body, "adults": 1, "cabin_class": "economy", "market": market}
        case = f"{case} market={market}"
        try:
            resp = session.post(BASE + path, json=payload, headers=headers, timeout=60)
            try:
                data = resp.json()
            except ValueError:
                data = {}
            if resp.status_code != 200 or not isinstance(data, dict) or "error" in data:
                err = (data.get("error") if isinstance(data, dict) else None) or {}
                if not isinstance(err, dict):
                    err = {"message": err}
                if err.get("field") == "market" or "market" in str(err.get("code", "")):
                    market = "US"
                note = (f"HTTP {resp.status_code} {err.get('code')} field={err.get('field')}: "
                        f"{err.get('message') or resp.text[:150]}")
                rows.append(_row(case, route, calls=1, note=_scrub(note, key)))
                continue
            rows.append(_row(case, route, calls=1, **parse(data)))
        except Exception as exc:
            rows.append(_row(case, route, calls=1, note=_scrub(f"{type(exc).__name__}: {exc}", key)))
    return rows
