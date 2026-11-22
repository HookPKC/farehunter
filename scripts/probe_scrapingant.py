"""ScrapingAnt 一次性探測：抓 Google Flights 頁面能不能拿到跟 Bright Data 一樣的直飛實價（ADR 0005）。

只讀不寫：不改 prices.db、不 commit。每次最多 MAX_REQUESTS 個請求（免費方案每月 10,000 點）。
- 從 prices.db 挑幾趟 Bright Data／ChocoData 最近幾小時查過、有直飛實價的行程（不同航線），
  用同一個 search_url() 請 ScrapingAnt 打開，用同一個 parse()／select() 解析，比對價格。
- 前後各查一次 /v2/usage 的剩餘點數，算每頁實際扣幾點。
- 先用預設的機房 proxy；全部解析不到時，再用住宅 proxy 試一趟。

repo 是公開的，Actions 紀錄任何人都看得到：金鑰只放 header，不印網址、不印回應原文，
連線錯誤只印例外類型。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from farehunter import brightdata_flights as bd     # noqa: E402

API = "https://api.scrapingant.com/v2/general"
USAGE = "https://api.scrapingant.com/v2/usage"
MAX_REQUESTS = 5
TRIPS = 3
ROUTES = ("KHH-OKA", "KHH-NGO", "TPE-NRT", "KHH-NRT", "KHH-FUK", "TPE-KIX")


def _key() -> str:
    key = os.environ.get("SCRAPINGANT_API_KEY", "").strip()
    if not key:
        print("❌ 還沒設 SCRAPINGANT_API_KEY secret")
        sys.exit(1)
    return key


def usage(key: str):
    """剩餘點數；查不到回 None。先用 header 傳金鑰，被拒（422）才改用網址參數。"""
    for how in ("header", "query"):
        try:
            if how == "header":
                r = requests.get(USAGE, headers={"x-api-key": key}, timeout=30)
            else:
                r = requests.get(USAGE, params={"x-api-key": key}, timeout=30)
        except requests.RequestException as e:
            print(f"   usage 連線錯誤：{type(e).__name__}")
            return None
        if r.status_code == 200:
            try:
                d = r.json()
            except ValueError:
                print("   usage 回應不是 JSON")
                return None
            print(f"   usage（金鑰放 {how}）：方案 {d.get('plan_name')}，總點數 {d.get('plan_total_credits')}，"
                  f"剩 {d.get('remained_credits')}，期間 {d.get('start_date')} ～ {d.get('end_date')}")
            return d.get("remained_credits")
        print(f"   usage（金鑰放 {how}）：HTTP {r.status_code}")
        if r.status_code != 422:
            return None
    return None


def pick_trips(db_path: str) -> list[tuple]:
    """最近 6 小時 Bright Data／ChocoData 寫過直飛實價的行程，每條線取一趟（最近的）。"""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = con.execute(
        """SELECT origin, destination, depart_date, return_date, MIN(price), carriers,
                  MAX(observed_at), provider
           FROM observations
           WHERE source='google' AND fare_class='any' AND stops=0
             AND provider IN ('brightdata', 'chocodata')
             AND julianday(observed_at) >= julianday('now', '-6 hours')
             AND depart_date >= date('now', '+2 day')
           GROUP BY origin, destination, depart_date, return_date
           ORDER BY MAX(observed_at) DESC""").fetchall()
    out, seen = [], set()
    for r in rows:
        route = f"{r[0]}-{r[1]}"
        if route in ROUTES and route not in seen:
            seen.add(route)
            out.append(r)
        if len(out) >= TRIPS:
            break
    return out


def scrape(key: str, url: str, proxy_type: str | None):
    params = {"url": url, "browser": "true"}
    if proxy_type:
        params["proxy_type"] = proxy_type
    t = time.monotonic()
    try:
        r = requests.get(API, params=params, headers={"x-api-key": key}, timeout=(10, 150))
    except requests.RequestException as e:
        return None, f"連線錯誤 {type(e).__name__}", time.monotonic() - t
    secs = time.monotonic() - t
    info = (f"HTTP {r.status_code}，目標站 {r.headers.get('Ant-Page-Status-Code', '?')}，"
            f"{len(r.content):,} bytes，{secs:.1f} 秒")
    return (r.text if r.status_code == 200 else None), info, secs


def main() -> int:
    key = _key()
    print("== 探測前點數")
    before = usage(key)
    trips = pick_trips("prices.db")
    if not trips:
        print("❌ prices.db 裡最近 6 小時沒有可比對的 Google 直飛實價")
        return 1
    sent, hits, results = 0, 0, []
    for proxy in (None, "residential"):
        if proxy == "residential" and hits:
            break                                  # 機房 proxy 就成功了，不必花住宅點數
        todo = trips if proxy is None else trips[:1]
        print(f"\n== proxy：{proxy or '預設（機房）'}")
        for o, d, dep, ret, ref, carriers, at, prov in todo:
            if sent >= MAX_REQUESTS:
                break
            sent += 1
            page, info, _ = scrape(key, bd.search_url(o, d, dep, ret), proxy)
            its = bd.parse(page) if page else []
            offers, _ = bd.select(o, d, dep, ret, its, None) if its else ([], None)
            got = min((x.price for x in offers if x.fare_class == "any"), default=None)
            hits += got is not None
            diff = f"{(got / ref - 1) * 100:+.1f}%" if got else "—"
            print(f"  {o}-{d} {dep}/{ret}：{info}；解析 {len(its)} 個行程、直飛 "
                  f"{sum(i.nonstop for i in its)}；最低直飛 {got if got else '無'}"
                  f"（{prov} {at[11:16]}Z：{ref:,.0f}，差 {diff}）")
            results.append((proxy, got is not None))
    print("\n== 探測後點數")
    after = usage(key)
    if isinstance(before, int) and isinstance(after, int) and sent:
        print(f"共 {sent} 個請求，扣 {before - after} 點，平均每頁 {(before - after) / sent:.0f} 點")
    print(f"\n結論：{hits}/{sent} 個請求拿到直飛實價")
    return 0


if __name__ == "__main__":
    sys.exit(main())
