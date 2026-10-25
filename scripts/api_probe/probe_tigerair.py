"""FareHunter probe: Tigerair Taiwan (IT) own fare calendars, keyless.

Three public, unauthenticated JSON endpoints used by tigerairtw.com itself (found in
GitHub projects that run them from GitHub Actions in Oct 2026, no secrets):

  A. GET  https://api-book.tigerairtw.com/api/cms/station-daily-prices/{STATION}/TWD
     One call = lowest fare per day for EVERY route touching that station, both
     directions (KHH->NGO and NGO->KHH), ~1 year ahead. Rows:
     {origin, destination, pricingDate, pricingAmount, pricingCurrency, updatedAt}.
     Used by a362b852/tigerair-tracker (hourly) and YenZackary/tigerair_fare_tracker
     (every 15 min, KHH included).
  B. POST https://api-book.tigerairtw.com/graphql  query appDailyPrices(input) ->
     [{date, origin, destination, currency, amount}]. Documented by
     HayaoGai/Tigerair-Price-Monitor (Aug 2026): no login/cookie/session/reCAPTCHA;
     pricingCurrency must be set; amount 0 = no seat / no route.
  C. GET  https://api-cms.tigerairtw.com/api/app/book/daily-prices?origin&destination&
     userCurrency&pricingCurrency&since&until -> {"data": [{date, amount}]}
     (mywu-cloud/flights, mid 2026).

All amounts are the cheapest one-way fare of the day, BEFORE tax (no flight number).
The booking search itself (appCreateFlightSearchSession) needs reCAPTCHA and is NOT used.
ToS: internal site endpoints, CORS-locked to tigerairtw.com -> ToS-gray; read-only,
4 requests, 3 s apart.
"""
from __future__ import annotations

import datetime as dt
import time

PROVIDER = "tigerair"
PIP_DEPS: list[str] = []
STATION_URL = "https://api-book.tigerairtw.com/api/cms/station-daily-prices/{station}/TWD"
GQL_URL = "https://api-book.tigerairtw.com/graphql"
CMS_URL = "https://api-cms.tigerairtw.com/api/app/book/daily-prices"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/141.0.0.0 Safari/537.36")
TIMEOUT_S = 45
SLEEP_S = 3.0
CURRENCY = "TWD"
STAY_DAYS = 5
WINDOW = (30, 60)      # depart today+30 .. today+60
ANCHOR_DAYS = 43       # today+43 -> today+48 (on 2026-10-25: 2026-12-07 -> 12-12, the Google reference)
GQL_QUERY = ("query DailyPrices($input: QueryDailyPricesInput!) {\n"
             "  appDailyPrices(input: $input) { date origin destination currency amount }\n}")
BLOCK_WORDS = ("captcha", "access denied", "just a moment", "cf-chl", "attention required",
               "request blocked", "forbidden", "waiting room", "queue-it")

_sleep = time.sleep    # monkeypatched in the self-test


def _new_session():
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                      "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"})
    return s


def _row(case, route, ok=False, calls=0, note="", **kw):
    row = {"provider": PROVIDER, "case": case, "route": route, "ok": bool(ok),
           "results": None, "dates": None, "min_price": None, "currency": None,
           "carriers": None, "calls": int(calls), "note": str(note)[:300]}
    row.update(kw)
    row["note"] = str(row["note"])[:300]
    return row


def _err(exc):
    return f"{type(exc).__name__}: {str(exc)[:220]}"


def _blocked(resp):
    """Return a short reason when the response is not usable JSON, else None."""
    text = (getattr(resp, "text", "") or "")[:3000]
    low = text.lower()
    hit = [w for w in BLOCK_WORDS if w in low]
    if resp.status_code != 200:
        return f"HTTP {resp.status_code}" + (f" block-words={hit}" if hit else "") + f" body={text[:110]!r}"
    if low.lstrip().startswith("<"):
        return "HTTP 200 but HTML" + (f" block-words={hit}" if hit else "") + f" body={text[:110]!r}"
    return None


def _num(x):
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        try:
            return float(x.replace(",", ""))
        except ValueError:
            return None
    return None


def _day(x):
    if not isinstance(x, str) or len(x) < 10:
        return None
    try:
        return dt.date.fromisoformat(x[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------- A. station cache
def _parse_station(body):
    """-> (legs {(o,d): {date: price}}, info) ; keeps the newest updatedAt per (o,d,date)."""
    if not isinstance(body, list):
        raise ValueError(f"expected a JSON array, got {type(body).__name__}")
    legs, stamp, newest, maxday, bad = {}, {}, "", None, 0
    for r in body:
        if not isinstance(r, dict):
            bad += 1
            continue
        o, d = r.get("origin"), r.get("destination")
        day, amt = _day(r.get("pricingDate")), _num(r.get("pricingAmount"))
        if not (isinstance(o, str) and isinstance(d, str) and day) or r.get("pricingCurrency") != CURRENCY:
            bad += 1
            continue
        if amt is None or amt <= 0:
            continue
        up = str(r.get("updatedAt") or "")
        key = (o, d)
        if day in legs.get(key, {}) and up <= stamp.get((o, d, day), ""):
            continue
        legs.setdefault(key, {})[day] = amt
        stamp[(o, d, day)] = up
        if up[:1].isdigit():
            newest = max(newest, up)
        maxday = day if maxday is None or day > maxday else maxday
    return legs, {"rows": len(body), "bad": bad, "routes": len(legs), "newest": newest[:16],
                  "horizon": maxday.isoformat() if maxday else None}


def _route_row(route, station, legs, info, today, calls):
    o, d = route.split("-")
    out, back = legs.get((o, d), {}), legs.get((d, o), {})
    case = f"{route} station-daily-prices/{station} RT{STAY_DAYS}d dep+{WINDOW[0]}..+{WINDOW[1]}"
    lo, hi = today + dt.timedelta(days=WINDOW[0]), today + dt.timedelta(days=WINDOW[1])
    combos = []
    for dep, p in out.items():
        ret = dep + dt.timedelta(days=STAY_DAYS)
        if lo <= dep <= hi and ret in back:
            combos.append((p + back[ret], dep))
    best = min(combos) if combos else None
    flex = [(out[a] + back[b], a, b) for a in out if lo <= a <= hi
            for b in back if 4 <= (b - a).days <= 6]
    fbest = min(flex) if flex else None
    anc = today + dt.timedelta(days=ANCHOR_DAYS)
    anr = anc + dt.timedelta(days=STAY_DAYS)
    a_out, a_back = out.get(anc), back.get(anr)
    anchor = (f"{a_out:.0f}+{a_back:.0f}={a_out + a_back:.0f}" if a_out and a_back
              else f"out={a_out} back={a_back}")
    if not out and not back:
        others = sorted(f"{a}-{b}" for a, b in legs if station in (a, b))[:6]
        note = (f"HTTP 200 rows={info['rows']} routes={info['routes']}: no {route} rows "
                f"(seen e.g. {','.join(others)}); horizon {info['horizon']}")
        return _row(case, route, ok=True, calls=calls, results=0, dates=0, currency=CURRENCY, note=note)
    note = (f"HTTP 200 rows={info['rows']}; {o}>{d} {len(out)}d, {d}>{o} {len(back)}d, horizon {info['horizon']}; "
            f"{len(combos)} exact-{STAY_DAYS}d combos"
            + (f", best dep {best[1].isoformat()}" if best else "")
            + (f"; 4-6d best {fbest[0]:.0f} ({fbest[1].isoformat()}>{fbest[2].isoformat()})" if fbest else "")
            + f"; anchor {anc.isoformat()}>{anr.isoformat()} {anchor}; UNTAXED one-way sums, "
            f"no flight no.; newest updatedAt {info['newest']}")
    return _row(case, route, ok=True, calls=calls, results=len(out) + len(back), dates=len(out),
                min_price=float(best[0]) if best else None, currency=CURRENCY,
                carriers="IT", note=note)


def _station(session, station):
    resp = session.get(STATION_URL.format(station=station), timeout=TIMEOUT_S)
    why = _blocked(resp)
    if why:
        raise RuntimeError(why)
    return _parse_station(resp.json())


# ---------------------------------------------------------------- B/C. per-route calendars
def _calendar_row(case, route, items, ref, label, calls, status):
    """items: [(date, amount)] ; ref: {date: price} from the station cache for the same leg."""
    priced = {d: a for d, a in items if d and a and a > 0}
    same = sum(1 for d, a in priced.items() if d in ref and abs(ref[d] - a) < 0.5)
    diff = [f"{d.isoformat()[5:]}:{a:.0f}/{ref[d]:.0f}" for d, a in sorted(priced.items())
            if d in ref and abs(ref[d] - a) >= 0.5][:3]
    only = sum(1 for d in priced if d not in ref)
    days = sorted(priced)
    note = (f"HTTP {status}; {len(items)} items, {len(priced)} priced"
            + (f" {days[0].isoformat()}..{days[-1].isoformat()}" if days else "")
            + f"; vs station cache: same {same}, differ {len(diff)}" + (f" {' '.join(diff)}" if diff else "")
            + f", only-here {only}; "
            f"{label}; untaxed one-way, amount 0 = no seat")
    return _row(case, route, ok=True, calls=calls, results=len(items), dates=len(priced),
                min_price=min(priced.values()) if priced else None, currency=CURRENCY,
                carriers="IT" if priced else None, note=note)


def _gql(session, o, d, since, until):
    body = {"operationName": "DailyPrices", "query": GQL_QUERY,
            "variables": {"input": {"origin": o, "destination": d, "source": "resultPageCal",
                                    "pricingCurrency": CURRENCY, "userCurrency": CURRENCY,
                                    "since": since.isoformat(), "until": until.isoformat()}}}
    resp = session.post(GQL_URL, json=body, timeout=TIMEOUT_S,
                        headers={"Content-Type": "application/json", "x-language": "zh-TW"})
    why = _blocked(resp)
    if why:
        raise RuntimeError(why)
    js = resp.json()
    if not isinstance(js, dict):
        raise ValueError(f"expected JSON object, got {type(js).__name__}")
    rows = (js.get("data") or {}).get("appDailyPrices") if isinstance(js.get("data"), dict) else None
    if js.get("errors") and not isinstance(rows, list):
        msgs = "; ".join(str((e or {}).get("message", e))[:90] for e in js["errors"][:2]
                         if isinstance(e, (dict, str)))
        raise RuntimeError(f"GraphQL errors: {msgs}")
    if not isinstance(rows, list):
        raise ValueError(f"no data.appDailyPrices list (keys {sorted(js)[:5]})")
    return [(_day(r.get("date")), _num(r.get("amount"))) for r in rows if isinstance(r, dict)], resp.status_code


def _cms(session, o, d, since, until):
    params = {"origin": o, "destination": d, "userCurrency": CURRENCY, "pricingCurrency": CURRENCY,
              "since": since.isoformat(), "until": until.isoformat()}
    resp = session.get(CMS_URL, params=params, timeout=TIMEOUT_S)
    why = _blocked(resp)
    if why:
        raise RuntimeError(why)
    js = resp.json()
    rows = js.get("data") if isinstance(js, dict) else None
    if not isinstance(rows, list):
        raise ValueError(f"no data list (got {type(js).__name__}"
                         + (f" keys {sorted(js)[:5]}" if isinstance(js, dict) else "") + ")")
    return [(_day(r.get("date")), _num(r.get("amount"))) for r in rows if isinstance(r, dict)], resp.status_code


# ---------------------------------------------------------------- entry point
def probe(env, today):
    rows = []
    try:
        session = _new_session()
    except Exception as exc:  # noqa: BLE001 (e.g. requests missing)
        return [_row("setup", "-", note=f"setup failed: {_err(exc)}")]

    legs_khh = {}
    try:  # 1. KHH station: KHH-NGO and KHH-CTS from one response
        legs_khh, info = _station(session, "KHH")
        rows.append(_route_row("KHH-NGO", "KHH", legs_khh, info, today, calls=1))
        rows.append(_route_row("KHH-CTS", "KHH", legs_khh, info, today, calls=0))
    except Exception as exc:  # noqa: BLE001
        rows.append(_row("KHH-NGO station-daily-prices/KHH", "KHH-NGO", calls=1, note=_err(exc)))

    _sleep(SLEEP_S)
    try:  # 2. TPE station: control route
        legs_tpe, info = _station(session, "TPE")
        rows.append(_route_row("TPE-NRT", "TPE", legs_tpe, info, today, calls=1))
    except Exception as exc:  # noqa: BLE001
        rows.append(_row("TPE-NRT station-daily-prices/TPE", "TPE-NRT", calls=1, note=_err(exc)))

    since = today + dt.timedelta(days=WINDOW[0])
    until = today + dt.timedelta(days=WINDOW[1] + STAY_DAYS)
    span = f"{since.isoformat()}..{until.isoformat()}"

    _sleep(SLEEP_S)
    case = f"KHH-NGO outbound graphql appDailyPrices {span}"
    try:  # 3. live GraphQL calendar, outbound leg
        items, status = _gql(session, "KHH", "NGO", since, until)
        rows.append(_calendar_row(case, "KHH-NGO", items, legs_khh.get(("KHH", "NGO"), {}),
                                  "source=resultPageCal", 1, status))
    except Exception as exc:  # noqa: BLE001
        rows.append(_row(case, "KHH-NGO", calls=1, note=_err(exc)))

    _sleep(SLEEP_S)
    case = f"KHH-NGO return leg NGO>KHH api-cms daily-prices {span}"
    try:  # 4. REST calendar on api-cms, return leg
        items, status = _cms(session, "NGO", "KHH", since, until)
        rows.append(_calendar_row(case, "KHH-NGO", items, legs_khh.get(("NGO", "KHH"), {}),
                                  "api-cms REST variant", 1, status))
    except Exception as exc:  # noqa: BLE001
        rows.append(_row(case, "KHH-NGO", calls=1, note=_err(exc)))
    return rows
