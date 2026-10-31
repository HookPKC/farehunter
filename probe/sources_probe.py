"""TEMP（合併前刪除）：實測四個不用金鑰的價格來源，跟同一輪 fli 日曆比對。

來源：Kiwi 網站 GraphQL 日曆、Skyscanner 月曆、捷星 farecache、星宇月曆。
前三個是單程價：把「去程 d ＋ 回程 d+5」加起來，跟 fli 的 5 晚來回價比。
只印價格與統計，不印任何金鑰（這支也用不到金鑰）。
"""
import json
import statistics
import sys
import time
from datetime import date, datetime, timedelta

from curl_cffi import requests as cr

from farehunter.fli_calendar import FliBackend, FliError

ROUTES = [("TPE", "NRT"), ("TPE", "KIX"), ("KHH", "NRT"), ("KHH", "KIX"),
          ("KHH", "CTS"), ("KHH", "FUK"), ("KHH", "OKA"), ("KHH", "NGO")]
MONTH = date(2026, 12, 1)
DAYS = [MONTH + timedelta(days=i) for i in range(31)]
STAY = 5
JPY_TWD = 0.21          # 只給捷星回程（日幣計價）粗估用，結果會標成「約」
S = cr.Session(impersonate="chrome")
out = {"month": MONTH.isoformat()[:7], "fli": {}, "sources": {}}


def note(src, route, **kw):
    out["sources"].setdefault(src, {})[route] = kw


def pause():
    time.sleep(1.5)


# ---- 基準：fli 日曆（5 晚來回、直飛） ----
fli = FliBackend(currency="TWD", budget=20)
for o, d in ROUTES:
    try:
        out["fli"][f"{o}-{d}"] = fli.calendar(o, d, MONTH, MONTH + timedelta(days=60), STAY)
    except FliError as exc:
        out["fli"][f"{o}-{d}"] = {"error": str(exc)[:200]}


# ---- Kiwi 網站 GraphQL 日曆 ----
KIWI = "https://api.skypicker.com/umbrella/v2/graphql?featureName=PriceCalendar"
Q_CAL = """query($s: SearchPricesCalendarInput, $f: ItinerariesFilterInput, $o: ItinerariesOptionsInput) {
 itineraryPricesCalendar(search: $s, filter: $f, options: $o) { __typename ... on AppError { code message }
  ... on ItineraryPricesCalendar { calendar { date ratedPrice { price { amount } } } } } }"""


def kiwi(o, d, start, end):
    v = {"s": {"source": {"ids": [f"Station:airport:{o}"]}, "destination": {"ids": [f"Station:airport:{d}"]},
               "dates": {"start": f"{start}T00:00:00", "end": f"{end}T23:59:59"},
               "passengers": {"adults": 1},
               "cabinClass": {"cabinClass": "ECONOMY", "applyMixedClasses": False}},
         "f": {"limit": 100, "maxStopsCount": 0, "transportTypes": ["FLIGHT"],
               "enableSelfTransfer": False, "contentProviders": ["KIWI", "FRESH", "KAYAK"]},
         "o": {"partner": "skypicker", "currency": "twd", "locale": "en", "sortBy": "PRICE",
               "storeSearch": False}}
    r = S.post(KIWI, json={"query": Q_CAL, "variables": v}, timeout=45,
               headers={"Origin": "https://www.kiwi.com", "Referer": "https://www.kiwi.com/",
                        "Accept": "application/json"})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} {r.text[:150]}")
    body = r.json()
    res = next(iter((body.get("data") or {}).values()), None) or {}
    if body.get("errors") and not res:
        raise RuntimeError(str(body["errors"][0].get("message"))[:200])
    if res.get("__typename") == "AppError":
        raise RuntimeError(f"{res.get('code')} {res.get('message')}"[:200])
    days = {}
    for c in res.get("calendar") or []:
        p = ((c.get("ratedPrice") or {}).get("price") or {}).get("amount")
        if p and float(p) > 0:
            days[c["date"][:10]] = float(p)
    return days


# ---- Skyscanner 月曆 ----
def skyscanner(o, d, month):
    url = (f"https://www.skyscanner.net/g/monthviewservice/TW/TWD/en-GB/calendar/{o}/{d}/"
           f"{month:%Y-%m}/")
    r = S.get(url, params={"profile": "minimalmonthviewgridv2"}, timeout=25,
              headers={"Accept": "application/json", "Referer": "https://www.skyscanner.net/"})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} {r.text[:150]}")
    data = r.json()
    traces = data.get("Traces") or {}
    grid = ((data.get("PriceGrids") or {}).get("Grid") or [[]])[0]
    days, ages = {}, []
    for i, cell in enumerate(grid):
        c = (cell or {}).get("Direct") or {}
        if not c.get("Price"):
            continue
        day = (month + timedelta(days=i)).isoformat()
        days[day] = float(c["Price"])
        t = traces.get((c.get("TraceRefs") or [""])[0], "")
        try:
            fetched = datetime.strptime(t.split(":", 1)[1].split("*")[0], "%Y%m%d%H%M")
            ages.append((datetime.utcnow() - fetched).total_seconds() / 86400)
        except (IndexError, ValueError):
            pass
    return days, (round(statistics.median(ages), 1) if ages else None)


# ---- 捷星 farecache ----
def jetstar(o, d, start, end):
    r = S.get("https://digitalapi.jetstar.com/v1/farecache/flights/batch/availability-with-fareclasses",
              params={"flightCount": "1", "includeSoldOut": "true", "requestType": "Original",
                      "from": start, "end": end, "departures": o, "arrivals": d,
                      "direction": "outbound", "paxCount": "1", "includeFees": "true"},
              timeout=30, headers={"Accept": "application/json", "culture": "en-AU",
                                   "Origin": "https://www.jetstar.com",
                                   "Referer": "https://www.jetstar.com/au/en/home"})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} {r.text[:150]}")
    days, cur = {}, None
    for batch in r.json() or []:
        cur = batch.get("currencyCode") or cur
        route = (batch.get("routes") or {}).get(f"{o}{d}".lower()) or {}
        for ymd, rows in (route.get("flights") or {}).items():
            prices = [float(x["price"]) for x in rows or [] if x.get("price") and not x.get("soldOut")
                      and not x.get("stopCount")]
            if prices:
                days[f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"] = min(prices)
    return days, cur


# ---- 星宇月曆（來回） ----
def starlux(o, d, dep):
    body = {"cabin": "eco",
            "itineraries": [{"departure": o, "arrival": d, "departureDate": dep},
                            {"departure": d, "arrival": o,
                             "departureDate": (date.fromisoformat(dep) + timedelta(days=STAY)).isoformat()}],
            "travelers": {"adt": 1, "chd": 0, "inf": 0}, "goFareFamilyCode": None, "corporateCode": None}
    r = S.post("https://ecapi.starlux-airlines.com/searchFlight/v2/flights/calendars/monthly",
               json=body, timeout=30,
               headers={"Content-Type": "application/json", "jx-lang": "zh-TW",
                        "Origin": "https://www.starlux-airlines.com",
                        "Referer": "https://www.starlux-airlines.com/"})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} {r.text[:150]}")
    data = r.json()
    return data


def compare(route, outb, inb, fx=1.0):
    """去程 d ＋ 回程 d+5（單程相加）對 fli 5 晚來回。"""
    ref = out["fli"].get(route)
    if not isinstance(ref, dict) or "error" in ref:
        return {"fli": "no data"}
    diffs, rows = [], []
    for day in DAYS:
        k, back = day.isoformat(), (day + timedelta(days=STAY)).isoformat()
        if k in outb and back in inb and k in ref:
            rt = outb[k] + inb[back] * fx
            diffs.append(round((rt - ref[k]) / ref[k] * 100, 1))
            if len(rows) < 3:
                rows.append((k, round(rt), ref[k]))
    if not diffs:
        return {"pairs": 0}
    return {"pairs": len(diffs), "median_diff_pct": statistics.median(diffs),
            "within_2pct": sum(abs(x) <= 2 for x in diffs), "within_5pct": sum(abs(x) <= 5 for x in diffs),
            "examples(day, src_rt, fli_rt)": rows}


end_rt = (MONTH + timedelta(days=36)).isoformat()
for o, d in ROUTES:
    route = f"{o}-{d}"
    for name, fn in (("kiwi", lambda a, b: kiwi(a, b, MONTH.isoformat(), end_rt)),
                     ("skyscanner", None)):
        try:
            if name == "kiwi":
                outb, inb = fn(o, d), (pause(), fn(d, o))[1]
                note(name, route, out_days=len(outb), in_days=len(inb), **compare(route, outb, inb))
            else:
                ob, age_o = skyscanner(o, d, MONTH)
                pause()
                ib, age_i = skyscanner(d, o, MONTH)
                pause()
                ib2, _ = skyscanner(d, o, date(2027, 1, 1))
                ib.update(ib2)
                note(name, route, out_days=len(ob), in_days=len(ib), cache_age_days=[age_o, age_i],
                     **compare(route, ob, ib))
        except Exception as exc:  # noqa: BLE001
            note(name, route, error=f"{type(exc).__name__}: {exc}"[:250])
        pause()

for o, d in (("TPE", "NRT"), ("TPE", "KIX"), ("KHH", "NRT")):
    route = f"{o}-{d}"
    try:
        ob, cur_o = jetstar(o, d, MONTH.isoformat(), (MONTH + timedelta(days=30)).isoformat())
        pause()
        ib, cur_i = jetstar(d, o, MONTH.isoformat(), (MONTH + timedelta(days=36)).isoformat())
        fx = JPY_TWD if cur_i == "JPY" else 1.0
        note("jetstar", route, out_days=len(ob), in_days=len(ib), currency=[cur_o, cur_i],
             **compare(route, ob, ib, fx))
    except Exception as exc:  # noqa: BLE001
        note("jetstar", route, error=f"{type(exc).__name__}: {exc}"[:250])
    pause()

for o, d in (("TPE", "NRT"), ("TPE", "KIX")):
    route = f"{o}-{d}"
    try:
        data = starlux(o, d, (MONTH + timedelta(days=9)).isoformat())
        cal = ((data.get("data") or {}).get("calendars") or [])
        sample = cal[:3]
        note("starlux", route, top_keys=sorted((data.get("data") or {}).keys())[:12], n=len(cal),
             sample=json.dumps(sample, ensure_ascii=False)[:600])
    except Exception as exc:  # noqa: BLE001
        note("starlux", route, error=f"{type(exc).__name__}: {exc}"[:250])
    pause()

out["fli"] = {k: (len(v) if isinstance(v, dict) and "error" not in v else v) for k, v in out["fli"].items()}
out["fli_requests"] = fli.requests
print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
