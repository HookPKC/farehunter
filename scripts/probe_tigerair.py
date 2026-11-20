"""虎航官網日曆一次性探測：端點還能不能用、格式、跟 Google 直飛實價差多少（特賣偵測的第 1 步）。

只讀不寫：不改 prices.db、不 commit。不用金鑰。每次執行只打 2 個請求（TPE、KHH）。
端點（公開專案 YenZackary/tigerair_fare_tracker、jhunterxsaxon/tigerair-price 都這樣用）：
  GET https://api-book.tigerairtw.com/api/cms/station-daily-prices/{station}/TWD
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import sys
import time

import requests

API = "https://api-book.tigerairtw.com/api/cms/station-daily-prices/{station}/TWD"
HEADERS = {"Accept": "application/json", "Referer": "https://www.tigerairtw.com/",
           "User-Agent": "farehunter-probe (+github actions; one-off, 2 requests)"}
OURS = [("TPE", "NRT"), ("TPE", "KIX"), ("KHH", "NRT"), ("KHH", "KIX"), ("KHH", "CTS"),
        ("KHH", "FUK"), ("KHH", "OKA"), ("KHH", "NGO")]


def fetch(station: str):
    t = time.monotonic()
    try:
        r = requests.get(API.format(station=station), headers=HEADERS, timeout=(10, 60))
    except requests.RequestException as e:
        print(f"[{station}] 連線錯誤：{type(e).__name__}")
        return None
    print(f"[{station}] HTTP {r.status_code}，{len(r.content):,} bytes，{time.monotonic() - t:.1f} 秒，"
          f"content-type={r.headers.get('content-type')}")
    try:
        body = r.json()
    except ValueError:
        print("   非 JSON：", r.text[:300])
        return None
    rows = body if isinstance(body, list) else (body.get("data") if isinstance(body, dict) else None)
    if not isinstance(rows, list):
        print("   結構不是 list：", json.dumps(body, ensure_ascii=False)[:500])
        return None
    print(f"   {len(rows)} 列；第一列：{json.dumps(rows[0], ensure_ascii=False) if rows else '—'}")
    if rows:
        print(f"   欄位：{sorted(rows[0])}")
    return rows


def main() -> int:
    legs: dict = {}                     # (o, d, date) → (price, updatedAt)
    for i, st in enumerate(("TPE", "KHH")):
        if i:
            time.sleep(2)
        rows = fetch(st) or []
        for r in rows:
            try:
                o, d, dt = r["origin"], r["destination"], str(r["pricingDate"])[:10]
                p = float(r["pricingAmount"])
            except (KeyError, TypeError, ValueError):
                continue
            if r.get("pricingCurrency", "TWD") != "TWD" or p <= 0 or "XX" in o + d:
                continue
            k = (o, d, dt)
            if k not in legs or str(r.get("updatedAt")) > str(legs[k][1]):
                legs[k] = (p, r.get("updatedAt"))
    if not legs:
        print("沒有可用資料")
        return 1
    ups = sorted(str(v[1]) for v in legs.values() if v[1])
    if ups:
        print(f"\nupdatedAt 範圍：{ups[0]} ～ {ups[-1]}")
    print("\n== 我們監控的航線（去程／回程各自的單程日曆）")
    for o, d in OURS:
        for a, b in ((o, d), (d, o)):
            ps = sorted((dt, p) for (x, y, dt), (p, _) in legs.items() if (x, y) == (a, b))
            if not ps:
                print(f"  {a}-{b}: 沒有虎航資料")
                continue
            vals = [p for _, p in ps]
            cheap = sorted(ps, key=lambda t: t[1])[:5]
            print(f"  {a}-{b}: {len(ps)} 天（{ps[0][0]}～{ps[-1][0]}），最低 {min(vals):,.0f}、"
                  f"中位 {statistics.median(vals):,.0f}；最便宜 5 天 {[(t[0][5:], int(t[1])) for t in cheap]}")

    print("\n== 跟 Google 直飛實價比（prices.db，近 48 小時、虎航 IT、住 2–10 晚）")
    try:
        db = sqlite3.connect("prices.db")
        rows = db.execute(
            """SELECT origin, destination, depart_date, return_date, MIN(price), MAX(observed_at)
               FROM observations WHERE source='google' AND fare_class='any' AND stops=0
                 AND carriers='IT' AND observed_at >= datetime('now','-2 day')
                 AND julianday(return_date)-julianday(depart_date) BETWEEN 2 AND 10
               GROUP BY 1,2,3,4 ORDER BY 1,2,3 LIMIT 400""").fetchall()
    except sqlite3.Error as e:
        print("讀不到 prices.db：", e)
        return 0
    diffs, n = {}, 0
    for o, d, dep, ret, g, at in rows:
        out, back = legs.get((o, d, dep)), legs.get((d, o, ret))
        if not out or not back:
            continue
        s = out[0] + back[0]
        diffs.setdefault(f"{o}-{d}", []).append((g - s, g / s, dep, ret, int(g), int(s)))
        n += 1
    print(f"配得上的行程 {n} 趟")
    for r, xs in diffs.items():
        gaps = [x[0] for x in xs]
        print(f"  {r}: {len(xs)} 趟；Google 實價 − 官網去回加總：中位 {statistics.median(gaps):,.0f}，"
              f"範圍 {min(gaps):,.0f}～{max(gaps):,.0f}；例：{[(x[2][5:], x[3][5:], x[4], x[5]) for x in xs[:4]]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
