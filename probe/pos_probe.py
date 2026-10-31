"""TEMP（合併前刪除）：Google Flights 的價格會不會因為市場（gl）不同而不一樣？

目前 fli 沒帶 gl（Google 依 GitHub 主機 IP 判斷，多半是美國），SerpAPI 也沒帶
（SerpAPI 預設 gl=us）。這裡同一組行程各查一次「預設」與「gl=TW」，比較價格。
只印價格，不印任何金鑰。
"""
import json
import os
import statistics
import sys
from datetime import date, timedelta

import requests
import fli.search.dates as D
import fli.search.flights as F
from fli.search import _urls

from farehunter.fli_calendar import FliBackend, FliError
from farehunter.serpapi_flights import BASE_URL as SERP_URL, parse_cheapest_direct

STATE = {"country": None, "urls": {}}


def _patched(url, currency, language, country):
    out = _urls.with_locale_params(url, currency, language, STATE["country"])
    STATE["urls"].setdefault(STATE["country"] or "default", out.split("?", 1)[-1])
    return out


D.with_locale_params = _patched
F.with_locale_params = _patched

ROUTES = [("TPE", "NRT"), ("TPE", "KIX"), ("KHH", "NRT"), ("KHH", "KIX"),
          ("KHH", "CTS"), ("KHH", "FUK"), ("KHH", "OKA"), ("KHH", "NGO")]
COUNTRIES = (None, "TW")
today = date.today()
start, end = today + timedelta(days=7), today + timedelta(days=67)
b = FliBackend(currency="TWD", budget=60)
out = {"window": [start.isoformat(), end.isoformat()], "calendar": {}, "live": [], "serpapi": None}


def call(fn, *a):
    try:
        return fn(*a)
    except FliError as exc:
        return {"error": str(exc)[:400]}


cal = {}
for o, d in ROUTES:
    r = {}
    for c in COUNTRIES:
        STATE["country"] = c
        r[c or "default"] = call(b.calendar, o, d, start, end, 5)
    cal[(o, d)] = r
    a, t = r["default"], r["TW"]
    if isinstance(a, dict) and isinstance(t, dict) and "error" not in a and "error" not in t:
        common = sorted(set(a) & set(t))
        diffs = [round((t[k] - a[k]) / a[k] * 100, 2) for k in common]
        out["calendar"][f"{o}-{d}"] = {
            "days_default": len(a), "days_tw": len(t), "common": len(common),
            "equal": sum(x == 0 for x in diffs),
            "median_diff_pct_tw_vs_default": statistics.median(diffs) if diffs else None,
            "min_default": min(a.values()) if a else None, "min_tw": min(t.values()) if t else None,
            "examples": [(k, a[k], t[k]) for k in common if a[k] != t[k]][:5]}
    else:
        out["calendar"][f"{o}-{d}"] = {"default": a if not isinstance(a, dict) or "error" in a else len(a),
                                       "TW": t if not isinstance(t, dict) or "error" in t else len(t)}

for (o, d), r in cal.items():
    a = r["default"]
    if not isinstance(a, dict) or "error" in a or not a:
        continue
    dep = date.fromisoformat(min(a, key=a.get))
    ret = dep + timedelta(days=5)
    row = {"route": f"{o}-{d}", "depart": dep.isoformat(), "return": ret.isoformat(),
           "calendar_default": a[dep.isoformat()],
           "calendar_tw": (r["TW"] or {}).get(dep.isoformat()) if isinstance(r["TW"], dict) else None}
    for c in COUNTRIES:
        STATE["country"] = c
        row[f"live_{c or 'default'}"] = call(b.cheapest, o, d, dep, ret)
    out["live"].append(row)

key = os.environ.get("SERPAPI_KEY", "")
if key and out["live"]:
    t = out["live"][0]
    o, d = t["route"].split("-")
    res = {"trip": t["route"] + " " + t["depart"] + "~" + t["return"]}
    for gl in (None, "tw"):
        params = {"engine": "google_flights", "departure_id": o, "arrival_id": d,
                  "outbound_date": t["depart"], "return_date": t["return"], "currency": "TWD",
                  "stops": 1, "hl": "zh-TW", "api_key": key}
        if gl:
            params["gl"] = gl
        try:
            p = requests.get(SERP_URL, params=params, timeout=90).json()
            best = parse_cheapest_direct(p, o, d, t["depart"], t["return"])
            sp = p.get("search_parameters") or {}
            res[gl or "default"] = {"price": best.price if best else None,
                                    "carriers": best.carriers if best else None,
                                    "gl_echo": sp.get("gl"), "error": p.get("error")}
        except Exception as exc:  # noqa: BLE001
            res[gl or "default"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
    out["serpapi"] = res

out["requests"] = b.requests
out["url_params"] = STATE["urls"]
print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
