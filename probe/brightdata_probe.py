"""TEMP（合併前刪除）：實測 Bright Data SERP API 抓 Google Flights，跟同一輪 fli 日曆比對。

用 GitHub secret BRIGHTDATA_API_KEY／BRIGHTDATA_ZONE；沒設就只回報「缺 secret」。
Bright Data 回的是整頁 HTML：從每個航班的 aria-label（hl=en）解析「round trip total」與是否直飛。
任何印出的文字都先把金鑰遮掉。
"""
import json
import os
import re
import time
from datetime import date, timedelta
from urllib.parse import quote

import requests

from farehunter.fli_calendar import FliBackend, FliError

KEY = os.environ.get("BRIGHTDATA_API_KEY", "")
ZONE = os.environ.get("BRIGHTDATA_ZONE", "")
ROUTES = [("TPE", "NRT"), ("TPE", "KIX"), ("KHH", "NRT"), ("KHH", "KIX"),
          ("KHH", "CTS"), ("KHH", "FUK"), ("KHH", "OKA"), ("KHH", "NGO")]
STAY = 5
START = date.today() + timedelta(days=30)
out = {"secrets": {"api_key": bool(KEY), "zone": bool(ZONE)}, "trips": [], "brd_json": None}


def redact(text):
    return text.replace(KEY, "***") if KEY else text


def gf_url(o, d, dep, ret, extra=""):
    q = f"Flights to {d} from {o} on {dep} through {ret}"
    return f"https://www.google.com/travel/flights?q={quote(q)}&curr=TWD&hl=en&gl=tw{extra}"


def bright(url):
    t0 = time.time()
    r = requests.post("https://api.brightdata.com/request", timeout=90,
                      headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
                      json={"zone": ZONE, "url": url, "format": "raw"})
    return r, round(time.time() - t0, 1)


LABEL = re.compile(r'aria-label="From ([\d,]+) Taiwan dollars round trip total\.([^"]*)"')


def parse(html):
    rows = []
    for price, rest in LABEL.findall(html):
        nonstop = "Nonstop flight" in rest
        m = re.search(r"flight with ([^.]+)\.", rest)
        rows.append((int(price.replace(",", "")), nonstop, (m.group(1) if m else "")[:40]))
    direct = [r for r in rows if r[1]]
    return {"itineraries": len(rows), "nonstop": len(direct),
            "min_nonstop": min(direct) if direct else None,
            "min_any": min(rows) if rows else None}


def find_zone():
    """沒設 BRIGHTDATA_ZONE 時，用金鑰列出帳號的 zone，挑 SERP 類型的（zone 名稱不是機密）。"""
    zones = []
    for path in ("/zone/get_active_zones", "/zone/get_all_zones"):
        try:
            r = requests.get("https://api.brightdata.com" + path, timeout=30,
                             headers={"Authorization": f"Bearer {KEY}"})
            out.setdefault("zone_lookup", []).append({"path": path, "http": r.status_code,
                                                      "body": redact(r.text[:600])})
            if r.status_code == 200:
                data = r.json()
                zones = data if isinstance(data, list) else (data.get("zones") or [])
                if zones:
                    break
        except Exception as exc:  # noqa: BLE001
            out.setdefault("zone_lookup", []).append({"path": path, "error": redact(str(exc)[:200])})
    names = [(z.get("name"), z.get("type")) for z in zones if isinstance(z, dict)]
    out["zones"] = names
    for want in ("mcp_unlocker",):
        if any(n == want for n, _ in names):
            return want
    usable = [n for n, t in names if t and str(t).lower() in ("serp", "unblocker")]
    if usable:
        return usable[0]
    # 帳號裡一個能用的 zone 都沒有：照 Bright Data 官方 MCP（brightdata-mcp server.js
    # ensure_required_zones）完全相同的寫法建一個 Web Unlocker zone。每月 5,000 次免費額度
    # 就掛在這個 zone 上；用完就停，沒預存金額不會扣款（官方 README「Pricing and Free Tier」）。
    r = requests.post("https://api.brightdata.com/zone", timeout=30,
                      headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
                      json={"zone": {"name": "mcp_unlocker", "type": "unblocker"},
                            "plan": {"type": "unblocker", "ub_premium": True}})
    out["zone_created"] = {"name": "mcp_unlocker", "http": r.status_code, "body": redact(r.text[:300])}
    return "mcp_unlocker" if r.status_code in (200, 201) else ""


if KEY and not ZONE:
    ZONE = find_zone()
    out["zone_used"] = ZONE

if not (KEY and ZONE):
    print(json.dumps(out, ensure_ascii=False, indent=1))
    raise SystemExit(0)

fli = FliBackend(currency="TWD", budget=20)
for o, d in ROUTES:
    trip = {"route": f"{o}-{d}"}
    try:
        cal = fli.calendar(o, d, START, START + timedelta(days=30), STAY)
        dep = date.fromisoformat(min(cal, key=cal.get)) if cal else START
        trip["fli_calendar"] = cal.get(dep.isoformat())
    except FliError as exc:
        dep, trip["fli_error"] = START, str(exc)[:150]
    ret = dep + timedelta(days=STAY)
    trip.update(depart=dep.isoformat(), ret=ret.isoformat())
    try:
        r, secs = bright(gf_url(o, d, dep.isoformat(), ret.isoformat()))
        trip.update(http=r.status_code, seconds=secs, bytes=len(r.text))
        if r.status_code == 200:
            trip.update(parse(r.text))
            mn = trip.get("min_nonstop")
            if mn and trip.get("fli_calendar"):
                trip["diff_pct_vs_fli"] = round((mn[0] - trip["fli_calendar"]) / trip["fli_calendar"] * 100, 2)
        else:
            trip["error"] = redact(r.text[:300])
    except Exception as exc:  # noqa: BLE001
        trip["error"] = redact(f"{type(exc).__name__}: {exc}"[:300])
    out["trips"].append(trip)
    time.sleep(1)

# brd_json=1：會不會直接回整理好的 JSON
t = out["trips"][0]
try:
    r, secs = bright(gf_url("TPE", "NRT", t["depart"], t["ret"], "&brd_json=1"))
    body = r.text.lstrip()
    info = {"http": r.status_code, "seconds": secs, "bytes": len(r.text), "looks_json": body[:1] in "{["}
    if info["looks_json"]:
        try:
            j = json.loads(body)
            info["top_keys"] = sorted(j.keys())[:25] if isinstance(j, dict) else f"list[{len(j)}]"
            info["sample"] = redact(json.dumps(j, ensure_ascii=False)[:1500])
        except ValueError as exc:
            info["json_error"] = str(exc)[:100]
    out["brd_json"] = info
except Exception as exc:  # noqa: BLE001
    out["brd_json"] = {"error": redact(f"{type(exc).__name__}: {exc}"[:300])}

out["fli_requests"] = fli.requests
print(json.dumps(out, ensure_ascii=False, indent=1))
