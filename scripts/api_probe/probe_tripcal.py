"""FareHunter probe: Trip.com low-price calendar (GetLowPriceInCalender), keyless.

POST https://www.trip.com/restapi/soa2/14427/GetLowPriceInCalender — the call behind the
date strip on trip.com/flights. veriglow/open-agent-map lists it as callable without a
browser session ("可裸調", last_verified 2026-05-07, confidence 0.95) with body
  {dCity, aCity, dDate, flightWayType OW|RT, aDate?, cabinClass, transferType ANY|DIRECT,
   searchInfo{travelerNum{adult}}, offSet (+-N days), Head{Group,Source,Version,Currency,Locale}}
and headers `currency` == Head.Currency. Reply: lowestCurrencyPrice, currency,
hasDirectFlight, lowPriceInCalenderDtoInfoList[{dDate (unix s), currencyPrice, ...}].

Caveats from other users: prices are cached marketing "from" prices, not bookable
inventory (daghlny/flight-price-skill, 2026); some international routes return null
prices; without the Head object the soa2 APIs answer HTTP 200 with "SourceEnum cannot
be null" (imoonkey/openweb, 2026-04); www.trip.com WAF answers 403 to some headless
clients. ToS: internal site endpoint -> ToS-gray; 4 requests, 3 s apart.
"""
from __future__ import annotations

import datetime as dt
import time

PROVIDER = "tripcom-calendar"
PIP_DEPS: list[str] = []
URL = "https://www.trip.com/restapi/soa2/14427/GetLowPriceInCalender"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/141.0.0.0 Safari/537.36")
TIMEOUT_S = 40
SLEEP_S = 3.0
CURRENCY = "TWD"
LOCALE = "en-XX"
STAY_DAYS = 5
DEPART_DAYS = 43       # on 2026-10-25: 2026-12-07 -> 12-12 (the Google reference trip)
OFFSET = 30
TZ_LOCAL = dt.timezone(dt.timedelta(hours=8))   # unix dDate -> Taipei/Beijing calendar day
# (route, leg origin, leg destination, way, transfer, depart offset)
CASES = [
    ("KHH-NGO", "KHH", "NGO", "OW", "DIRECT", DEPART_DAYS),
    ("KHH-NGO", "NGO", "KHH", "OW", "DIRECT", DEPART_DAYS + STAY_DAYS),   # return leg
    ("KHH-NGO", "KHH", "NGO", "RT", "ANY", DEPART_DAYS),
    ("TPE-NRT", "TPE", "NRT", "OW", "DIRECT", DEPART_DAYS),               # control
]
BLOCK_WORDS = ("captcha", "access denied", "just a moment", "cf-chl", "verify you are human",
               "slider", "forbidden", "request blocked")

_sleep = time.sleep


def _new_session():
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json",
                      "Accept-Language": "en-US,en;q=0.9,zh-TW;q=0.8",
                      "Origin": "https://www.trip.com", "Referer": "https://www.trip.com/flights/"})
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
    """dDate as unix seconds/ms, '/Date(ms+0800)/', or 'YYYY-MM-DD...' -> date."""
    if isinstance(x, str):
        if x.startswith("/Date("):
            digits = "".join(ch for ch in x[6:].split("+")[0].split("-")[0].split(")")[0] if ch.isdigit())
            x = int(digits) if digits else None
        elif len(x) >= 10 and x[4] == "-":
            try:
                return dt.date.fromisoformat(x[:10])
            except ValueError:
                return None
        elif x.isdigit():
            x = int(x)
    if isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0:
        secs = x / 1000 if x > 1e11 else x
        try:
            return dt.datetime.fromtimestamp(secs, TZ_LOCAL).date()
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _body(o, d, dep, ret, way, transfer):
    body = {"dCity": o, "aCity": d, "dDate": dep.isoformat(), "flightWayType": way,
            "cabinClass": "Economy", "transferType": transfer,
            "searchInfo": {"travelerNum": {"adult": 1, "child": 0, "infant": 0}},
            "offSet": OFFSET,
            "Head": {"Group": "Trip", "Source": "ONLINE", "Version": "3",
                     "Currency": CURRENCY, "Locale": LOCALE}}
    if way == "RT":
        body["aDate"] = ret.isoformat()
    return body


def _parse(js, dep):
    if not isinstance(js, dict):
        raise ValueError(f"expected JSON object, got {type(js).__name__}")
    data = js.get("data") if isinstance(js.get("data"), dict) else js
    items = data.get("lowPriceInCalenderDtoInfoList")
    status = js.get("ResponseStatus") or js.get("responseStatus") or {}
    ack = status.get("Ack") if isinstance(status, dict) else None
    if not isinstance(items, list):
        errs = status.get("Errors") if isinstance(status, dict) else None
        msg = (js.get("message") or js.get("msg") or js.get("errorMessage")
               or (errs[0].get("Message") if isinstance(errs, list) and errs and isinstance(errs[0], dict) else None))
        raise ValueError(f"no lowPriceInCalenderDtoInfoList (Ack={ack} msg={str(msg)[:100]!r} "
                         f"keys={sorted(data)[:6]})")
    priced = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        day = _day(it.get("dDate"))
        p = _num(it.get("currencyPrice"))
        if p is None:
            p = _num(it.get("price"))
        if day and p and p > 0:
            priced[day] = min(p, priced.get(day, p))
    return items, priced, {"ack": ack, "currency": data.get("currency"),
                           "lowest": _num(data.get("lowestCurrencyPrice")),
                           "direct": data.get("hasDirectFlight"), "at_dep": priced.get(dep)}


def _blocked(resp):
    text = (resp.text or "")[:3000]
    low = text.lower()
    hit = [w for w in BLOCK_WORDS if w in low]
    if resp.status_code != 200 or low.lstrip().startswith("<"):
        return (f"HTTP {resp.status_code}" + (" HTML" if low.lstrip().startswith("<") else "")
                + (f" block-words={hit}" if hit else "") + f" body={text[:120]!r}")
    return None


def probe(env, today):
    try:
        session = _new_session()
    except Exception as exc:  # noqa: BLE001
        return [_row("setup", "-", note=f"setup failed: {_err(exc)}")]
    rows, legs = [], {}
    for i, (route, o, d, way, transfer, offset) in enumerate(CASES):
        if i:
            _sleep(SLEEP_S)
        dep = today + dt.timedelta(days=offset)
        ret = dep + dt.timedelta(days=STAY_DAYS)
        case = (f"{o}>{d} {way} {transfer} dDate {dep.isoformat()}"
                + (f" aDate {ret.isoformat()}" if way == "RT" else "") + f" offSet {OFFSET}")
        try:
            resp = session.post(URL, json=_body(o, d, dep, ret, way, transfer), timeout=TIMEOUT_S,
                                headers={"Content-Type": "application/json", "currency": CURRENCY,
                                         "locale": LOCALE})
            why = _blocked(resp)
            if why:
                rows.append(_row(case, route, calls=1, note=why))
                continue
            items, priced, meta = _parse(resp.json(), dep)
            legs[(o, d, way, transfer)] = (priced, dep)
            days = sorted(priced)
            note = (f"HTTP 200 Ack={meta['ack']}; {len(items)} cells, {len(priced)} priced"
                    + (f" {days[0].isoformat()}..{days[-1].isoformat()}" if days else "")
                    + f"; lowestCurrencyPrice={meta['lowest']} hasDirectFlight={meta['direct']}"
                    + f"; price on dDate={meta['at_dep']}")
            if (o, d, way) == ("NGO", "KHH", "OW"):
                out = legs.get(("KHH", "NGO", "OW", "DIRECT"))
                a = out[0].get(out[1]) if out else None
                b = meta["at_dep"]
                note += (f"; anchor OW+OW {a:.0f}+{b:.0f}={a + b:.0f}" if a and b
                         else f"; anchor OW+OW out={a} back={b}")
            note += "; cached 'from' prices, carrier not given"
            rows.append(_row(case, route, ok=True, calls=1, results=len(items), dates=len(priced),
                             min_price=min(priced.values()) if priced else None,
                             currency=str(meta["currency"] or CURRENCY)[:8], note=note))
        except Exception as exc:  # noqa: BLE001
            rows.append(_row(case, route, calls=1, note=_err(exc)))
    return rows
