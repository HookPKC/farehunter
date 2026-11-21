"""虎航官網日曆：每輪抓一次虎航全網的每日單程最低價（ADR 0005）。

用途兩個：
1. **收集**：每個（方向, 日期）只在價格跟上次記的不同時寫一筆進 tigerair_fares。先收 1–2 週、
   不發任何通知，看虎航特賣多常出現、跌多少、撐多久，再決定特賣提醒怎麼定。只記未來
   STORE_DAYS_AHEAD 天內出發的日子，變化紀錄只留 HISTORY_DAYS 天（之前的只留每格最新一筆）。
2. **巡檢線索**：「去程＋2–10 晚後的回程」加總最便宜的組合，交給 Bright Data／ChocoData 的巡檢
   先查。跟 Picker 線索（calendar_hints）一樣只決定先查哪幾天；查到直飛實價才寫入、才可能
   發警報。線索只用這一輪抓到的資料：抓不到就沒有線索，巡檢照舊。

端點（不用金鑰；公開專案 YenZackary/tigerair_fare_tracker、jhunterxsaxon/tigerair-price 也這樣用）：
  GET https://api-book.tigerairtw.com/api/cms/station-daily-prices/{station}/TWD
回應是 list，每列 origin、destination、pricingCurrency、pricingDate、pricingAmount（字串
"999.000"）、updatedAt（台灣時間）。一個機場一次回所有進出航線、約一年的日子（2026-11-20 探測：
桃園 16,637 列、高雄 9,768 列，各約 1 秒）。

**價格是單程、未稅，不是直飛來回實價**。同一次探測跟資料庫裡 91 趟 Google 直飛實價比，
「官網去程＋回程」通常少 1,100–2,900（大致是來回的稅和雜費），部分日子的更新時間是幾天前。
所以官網價只存在 tigerair_fares：不進 observations，不顯示、不發警報、不進統計。
從日曆上消失的日子（賣完、停飛）不另外記。
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

import requests

from .models import MAX_STAY_NIGHTS, MIN_STAY_NIGHTS

log = logging.getLogger(__name__)

API_URL = "https://api-book.tigerairtw.com/api/cms/station-daily-prices/{station}/TWD"
#: 我們的航線都從這兩個機場出發；一個機場的回應含進出兩個方向。
STATIONS = ("TPE", "KHH")
HEADERS = {"Accept": "application/json", "Referer": "https://www.tigerairtw.com/",
           "User-Agent": "farehunter (+github actions; 2 requests/hour)"}
CONNECT_TIMEOUT_S = 10
READ_TIMEOUT_S = 30          # 實測約 1 秒；虎航卡住時不拖長整輪
PAUSE_S = 1.0                # 兩個機場之間停一下
#: 官網去回加總＋這個數仍超過巡檢線索上限的組合不挑。探測時各線「Google 實價－官網加總」
#: 的中位數最低是 1,113（KHH-NRT），取整當稅費下限：加上去還貴，直飛實價只會更貴。
FEE_FLOOR = 1000
PER_ROUTE = 4                # 每條線每輪最多排幾個線索（ChocoData 取兩倍，同 Picker）
MIN_LEAD_DAYS = 2            # 太近的出發日不查（同 brightdata_flights.MIN_LEAD_DAYS）
KEEP_PAST_DAYS = 30          # 出發日過了這麼多天的紀錄刪掉
#: 只記未來這麼多天內出發的日子。2026-11-21 上線後每小時有 240–270 筆變化（約三成是在兩個價格
#: 間來回跳），prices.db 每天多長約 0.7 MB；封存（ADR 0002）管不到這張表。120 天內約佔四成，
#: 特賣通常也是賣這幾個月內的票。巡檢線索不受影響：線索用的是當輪抓到的全年資料。
STORE_DAYS_AHEAD = 120
#: 變化紀錄只留這麼多天，更早的只留每格最新一筆（收 1–2 週看特賣怎麼出現就夠，表不會一直長）。
HISTORY_DAYS = 14


class TigerairError(RuntimeError):
    pass


def fetch(station: str, session=None) -> list:
    """一個機場的日曆（原始列）。錯誤訊息只留狀態碼或例外類型。"""
    try:
        r = (session or requests).get(API_URL.format(station=station), headers=HEADERS,
                                      timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S))
    except requests.RequestException as exc:
        raise TigerairError(f"transport: {type(exc).__name__}") from None
    if r.status_code != 200:
        raise TigerairError(f"HTTP {r.status_code}")
    try:
        body = r.json()
    except ValueError:
        raise TigerairError("HTTP 200 but not JSON") from None
    rows = body if isinstance(body, list) else (body.get("data") if isinstance(body, dict) else None)
    if not isinstance(rows, list):
        raise TigerairError("unexpected body shape")
    return rows


def parse(rows: list, wanted: set) -> dict:
    """原始列 → {(出發地, 目的地, 日期): (價格, updatedAt)}，只留 wanted 裡的方向。

    同一格出現多次時留 updatedAt 最新的。看不懂、非台幣、價格不是正數的列跳過——回應若變形，
    寧可少收。
    """
    out: dict = {}
    for r in rows:
        try:
            o, d = str(r["origin"]).upper(), str(r["destination"]).upper()
            dep = date.fromisoformat(str(r["pricingDate"])[:10]).isoformat()
            price = float(r["pricingAmount"])
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if (o, d) not in wanted or price <= 0 or r.get("pricingCurrency", "TWD") != "TWD":
            continue
        up = str(r.get("updatedAt") or "")
        k = (o, d, dep)
        if k not in out or up > out[k][1]:
            out[k] = (price, up)
    return out


def collect(store, now: datetime, routes, *, fetcher=None, pause=None) -> tuple[dict, dict]:
    """抓兩個機場、記下有變化的價格。回傳 (本輪的單程價 {(o, d, 日期): 價格}, 統計)。

    任一個機場失敗只少那一半；全部失敗回傳空的單程價（巡檢就沒有虎航線索）。
    """
    wanted = {(o, d) for o, d in routes} | {(d, o) for o, d in routes}
    stats = {"requests": 0, "ok": 0, "legs": 0, "written": 0, "errors": 0}
    legs: dict = {}
    pause = PAUSE_S if pause is None else pause
    for i, station in enumerate(STATIONS):
        if i and pause:
            time.sleep(pause)
        stats["requests"] += 1
        try:
            got = parse((fetcher or fetch)(station), wanted)
        except TigerairError as exc:
            stats["errors"] += 1
            log.warning("虎航官網日曆 %s：%s", station, exc)
            continue
        except Exception as exc:
            stats["errors"] += 1
            log.warning("虎航官網日曆 %s 意外失敗：%s", station, type(exc).__name__)
            continue
        stats["ok"] += 1
        for k, v in got.items():
            if k not in legs or v[1] > legs[k][1]:
                legs[k] = v
    stats["legs"] = len(legs)
    if legs:
        today = now.date()
        keep_to = (today + timedelta(days=STORE_DAYS_AHEAD)).isoformat()
        stats["written"] = store.record_tigerair_fares(
            {k: v for k, v in legs.items() if k[2] <= keep_to},
            now.isoformat(timespec="seconds"),
            keep_from=(today - timedelta(days=KEEP_PAST_DAYS)).isoformat(), keep_to=keep_to,
            history_since=(now - timedelta(days=HISTORY_DAYS)).isoformat(timespec="seconds"))
    return {k: v[0] for k, v in legs.items()}, stats


def pairs(legs: dict, origin: str, dest: str, *, earliest: str, cap: float | None = None,
          limit: int = PER_ROUTE) -> list[tuple[str, str]]:
    """這條線要優先查的 (去程日, 回程日)：官網去回加總由低到高，每個去程日只取一個。

    回程住 MIN_STAY_NIGHTS～MAX_STAY_NIGHTS 晚；同一個去程日取加總最低的回程（同價取晚數
    接近 5 晚的）。cap：直飛來回實價的上限（門檻 × CAP_RATIO）；加總＋FEE_FLOOR 超過就不挑。
    """
    out_legs = {dep: p for (o, d, dep), p in legs.items() if (o, d) == (origin, dest)}
    back = {dep: p for (o, d, dep), p in legs.items() if (o, d) == (dest, origin)}
    best = []
    for dep, p_out in out_legs.items():
        if dep < earliest:
            continue
        d0 = date.fromisoformat(dep)
        choice = None
        for n in range(MIN_STAY_NIGHTS, MAX_STAY_NIGHTS + 1):
            ret = (d0 + timedelta(days=n)).isoformat()
            if ret in back:
                key = (p_out + back[ret], abs(n - 5))
                if choice is None or key < choice[0]:
                    choice = (key, ret)
        if choice is None:
            continue
        total = choice[0][0]
        if cap is not None and total + FEE_FLOOR > cap:
            continue
        best.append((total, dep, choice[1]))
    best.sort()
    return [(dep, ret) for _, dep, ret in best[:limit]]


def put_first(queue: list, trips) -> int:
    """把線索行程 (去程日, 回程日) 排到巡檢佇列最前面（就地修改）；回傳排了幾個。

    已經在佇列裡的行程搬到前面、保留原本的航空公司欄位（快取候選有）；不重複。
    """
    trips = list(dict.fromkeys(trips))
    if not trips:
        return 0
    have = {(dep, ret): carriers for dep, ret, carriers in queue}
    picked = set(trips)
    queue[:] = ([(dep, ret, have.get((dep, ret))) for dep, ret in trips]
                + [t for t in queue if (t[0], t[1]) not in picked])
    return len(trips)


def hints(legs: dict, routes, now: datetime, caps: dict, *, limit: int) -> dict:
    """每條線的線索 {(o, d): [(去程日, 回程日), ...]}；沒有線索的線不列。"""
    earliest = (now.date() + timedelta(days=MIN_LEAD_DAYS)).isoformat()
    out = {}
    for o, d in routes:
        got = pairs(legs, o, d, earliest=earliest, cap=caps.get((o, d)), limit=limit)
        if got:
            out[(o, d)] = got
    return out
