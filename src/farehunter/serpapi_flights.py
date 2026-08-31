"""SerpAPI Google Flights client — daily snapshots of full-service carrier fares.

The Aviasales cache is a cheapest-fare feed and structurally excludes
full-service carriers (CI/BR/JX...). Google Flights has them; SerpAPI exposes
Google Flights as an API. We spend SEARCHES_PER_DAY route snapshots per day,
rotating through the configured routes, and record the cheapest
all-full-service itinerary into the existing fare_class='full' track.

**額度：不要相信註解，去看 docs/quota.json。** 這段原本寫「free tier
(~100 searches/month)」，而 SEARCHES_PER_DAY = 6（＝約 180 次/月）是圍繞那個
數字設計的。但 2026-08 實測 31 天每天跑滿、從未被擋——180 > 100，所以那句話
是錯的，真實額度更大。`farehunter.quota` 每天記錄真實方案與用量，調整
SEARCHES_PER_DAY 之前先看那份紀錄；憑註解或印象加量是在賭使用者的錢。

Endpoint: GET https://serpapi.com/search?engine=google_flights
Auth: api_key query param (env var SERPAPI_KEY).
"""
from __future__ import annotations

import os
import logging
from datetime import date, timedelta

import requests

from .models import Offer
from .normalize import CACHE_SOURCES
from .travelpayouts import FULL_SERVICE

log = logging.getLogger(__name__)

BASE_URL = "https://serpapi.com/search"
SEARCHES_PER_DAY = 6
ROTATION_PER_DAY = 3   # 每日固定輪替槽數;與 SEARCHES_PER_DAY(總上限)解耦,
                       # 保持輪替推進步長穩定,不因驗證槽擴充而改變覆蓋節奏
HORIZON_WEEKS = [6, 12, 18, 26, 34, 42]   # ≈1.5/3/4/6/8/10 個月，輪替涵蓋整個規劃期


class SerpApiError(RuntimeError):
    pass


def search_google_flights(origin: str, destination: str,
                          outbound: str, ret: str,
                          currency: str = "TWD",
                          api_key: str | None = None,
                          session: requests.Session | None = None) -> dict:
    key = api_key or os.environ.get("SERPAPI_KEY", "")
    if not key:
        raise RuntimeError("Missing SERPAPI_KEY environment variable.")
    s = session or requests
    resp = s.get(BASE_URL, params={
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": outbound,
        "return_date": ret,
        "currency": currency,
        "stops": 1,                     # SerpAPI: 1 = 僅直飛
        "hl": "zh-TW",
        "api_key": key,
    }, timeout=90)
    if resp.status_code != 200:
        raise SerpApiError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    payload = resp.json()
    if payload.get("error"):
        raise SerpApiError(payload["error"])
    return payload


def _airline_code(flight_number: str) -> str:
    """'BR 198' -> 'BR'; 'IT2 34' malformed -> best effort first token."""
    return (flight_number or "").split(" ")[0].strip()


def parse_full_service(payload: dict, origin: str, destination: str,
                       outbound: str, ret: str) -> Offer | None:
    """Cheapest itinerary whose EVERY segment is a full-service carrier."""
    candidates = (payload.get("best_flights") or []) + (payload.get("other_flights") or [])
    best = None
    for it in candidates:
        try:
            segs = it.get("flights") or []
            if not segs:
                continue
            if len(segs) > 1:           # 轉機行程不列入
                continue
            codes = [_airline_code(s.get("flight_number", "")) for s in segs]
            if not all(c in FULL_SERVICE for c in codes):
                continue
            price = float(it["price"])
            if best is None or price < best[0]:
                best = (price, sorted(set(codes)), it.get("total_duration"))
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("Skipping malformed itinerary: %s", exc)
    if best is None:
        return None
    price, codes, total_dur = best
    link = (f"https://www.google.com/travel/flights?q=Flights%20from%20{origin}"
            f"%20to%20{destination}%20on%20{outbound}%20through%20{ret}")
    return Offer(origin=origin, destination=destination,
                 depart_date=outbound, return_date=ret,
                 price=price, currency="TWD",
                 carriers=",".join(codes), stops=0,
                 duration=str(total_dur or ""), link=link,
                 fare_class="full", source="google", provider="serpapi")


def parse_cheapest_direct(payload: dict, origin: str, destination: str,
                          outbound: str, ret: str) -> Offer | None:
    """Cheapest single-segment (direct) itinerary of ANY carrier, with its
    real airline code — the real monthly low with a confirmed carrier."""
    candidates = (payload.get("best_flights") or []) + (payload.get("other_flights") or [])
    best = None
    for it in candidates:
        try:
            segs = it.get("flights") or []
            if len(segs) != 1:
                continue
            code = _airline_code(segs[0].get("flight_number", ""))
            if not code:
                continue
            price = float(it["price"])
            if best is None or price < best[0]:
                best = (price, code, it.get("total_duration"))
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("Skipping malformed itinerary: %s", exc)
    if best is None:
        return None
    price, code, total_dur = best
    link = (f"https://www.google.com/travel/flights?q=Flights%20from%20{origin}"
            f"%20to%20{destination}%20on%20{outbound}%20through%20{ret}")
    return Offer(origin=origin, destination=destination,
                 depart_date=outbound, return_date=ret,
                 price=price, currency="TWD", carriers=code, stops=0,
                 duration=str(total_dur or ""), link=link,
                 fare_class="any", source="google", provider="serpapi")


def pick_routes_for_today(routes: list[dict], today: date | None = None,
                          per_day: int = ROTATION_PER_DAY) -> list[dict]:
    """Deterministic daily rotation: consecutive slice of the route list."""
    if not routes:
        return []
    today = today or date.today()
    start = (today.toordinal() * per_day) % len(routes)
    return [routes[(start + i) % len(routes)] for i in range(min(per_day, len(routes)))]


def snapshot_dates(today: date | None = None,
                   horizon_weeks: int = 4) -> tuple[str, str]:
    """Departure ~N weeks out snapped to Friday; 5-day trip."""
    today = today or date.today()
    dep = today + timedelta(weeks=horizon_weeks)
    dep += timedelta(days=(4 - dep.weekday()) % 7)   # snap forward to Friday
    return dep.isoformat(), (dep + timedelta(days=5)).isoformat()


def horizon_for_slot(routes_count: int, today: date, slot: int,
                     per_day: int = ROTATION_PER_DAY) -> int:
    """Cycle horizons as the rotation completes passes over the route list:
    pass 1 -> 4 weeks, pass 2 -> 12 weeks, pass 3 -> 24 weeks, repeat."""
    cumulative = today.toordinal() * per_day + slot
    return HORIZON_WEEKS[(cumulative // max(routes_count, 1)) % len(HORIZON_WEEKS)]


# ---- C′ 低價候選驗證槽（2026-07 提案，反證調查後定稿）-----------------------
# 從最近 24h 的 alerts 挑「最值得花一次實價確認的候選」。純 DB 讀取、零 API。
# 時間戳格式警告：alerts.sent_at 是 SQLite datetime('now') 的
# 'YYYY-MM-DD HH:MM:SS'，observations.observed_at 是 Python isoformat 的
# 'YYYY-MM-DDTHH:MM:SS+00:00'——兩者皆 UTC 但格式不同，同日邊界上直接字串
# 比較會出錯（位置 11 的 'T' > ' '），因此所有時間比較一律走 julianday()。
_REASON_RANK = {"new_low": 0, "absolute": 1, "big_drop": 2}  # analyzer.py enum
VERIFY_WINDOW_HOURS = 24
VERIFY_COOLDOWN_HOURS = 72
VERIFY_MAX_AHEAD_DAYS = 270


def _is_cooled(conn, o: str, d: str, dep_s: str,
               cooldown_hours: int = VERIFY_COOLDOWN_HOURS,
               now_ref: str = "now") -> bool:
    """True 若 (o,d,dep) 於 cooldown_hours 內已有 source='google' 觀測。
    julianday() 比較,對混合時間字串格式安全。now_ref 為時間基準（見 _now_ref）。"""
    return conn.execute(
        """SELECT 1 FROM observations
           WHERE origin=? AND destination=? AND depart_date=?
             AND source='google'
             AND julianday(observed_at) >= julianday(?) - ?/24.0
           LIMIT 1""", (o, d, dep_s, now_ref, cooldown_hours)).fetchone() is not None


def _valid_horizon(dep_s: str, today: date) -> bool:
    """日期健全性:明天 ~ +VERIFY_MAX_AHEAD_DAYS 之間才可查。"""
    try:
        dep = date.fromisoformat(dep_s)
    except (ValueError, TypeError):
        return False
    return today + timedelta(days=1) <= dep <= today + timedelta(days=VERIFY_MAX_AHEAD_DAYS)


def alert_candidates(conn, thresholds: dict[tuple[str, str], float | None],
                     today: date | None = None,
                     window_hours: int = VERIFY_WINDOW_HOURS,
                     now_ref: str | None = None) -> list[dict]:
    """回傳最近 window_hours 的 alert 驗證候選,依偏好排序(強者先)。
    每筆含 origin/destination/depart_date/return_date/price/reason。
    不套用冷卻與 claimed(由 build_verification_plans 統一處理);配不到
    return_date 的 alert 直接剔除(不猜)。純 DB 讀取、零 API。

    today 為日期窗口基準;now_ref 為 SQL julianday 時間基準(單一時間來源:
    由呼叫端一併傳入,預設 'now' 即 production 真實時鐘)。

    排序:reason(new_low>absolute>big_drop)→ price/threshold 升冪 →
    出發日近 → sent_at 新。不寫死任何航線。
    """
    today = today or date.today()
    now_ref = now_ref or "now"
    rows = conn.execute(
        """SELECT origin, destination, depart_date, price, reason, sent_at,
                  julianday(sent_at) AS sent_j
           FROM alerts
           WHERE julianday(sent_at) >= julianday(?) - ?/24.0
           ORDER BY sent_j DESC""", (now_ref, window_hours)).fetchall()
    seen: set[tuple[str, str, str]] = set()
    ranked = []
    for r in rows:
        key = (r["origin"], r["destination"], r["depart_date"])
        if key in seen:               # 同一 route/date 只留最新一筆警報
            continue
        seen.add(key)
        if r["reason"] not in _REASON_RANK:
            continue
        if not _valid_horizon(r["depart_date"], today):
            continue
        thr = thresholds.get((r["origin"], r["destination"]))
        ratio = (r["price"] / thr) if thr else float("inf")
        ranked.append((_REASON_RANK[r["reason"]], ratio,
                       date.fromisoformat(r["depart_date"]).toordinal(),
                       -r["sent_j"], r))
    ranked.sort(key=lambda c: c[:4])
    out = []
    for *_, r in ranked:
        o, d, dep_s = r["origin"], r["destination"], r["depart_date"]
        match = conn.execute(
            """SELECT return_date FROM observations
               WHERE origin=? AND destination=? AND depart_date=?
                 AND fare_class='any' AND ABS(price - ?) < 0.5
                 AND return_date IS NOT NULL AND return_date != ''
               ORDER BY ABS(julianday(observed_at) - julianday(?)) ASC
               LIMIT 1""", (o, d, dep_s, r["price"], r["sent_at"])).fetchone()
        if match is None:
            continue                  # 配不到可靠回程日,不猜,剔除
        out.append({"origin": o, "destination": d, "depart_date": dep_s,
                    "return_date": match["return_date"], "price": r["price"],
                    "reason": r["reason"], "slot_kind": "alert"})
    return out


def pick_verification_candidate(conn, thresholds, today=None,
                                window_hours: int = VERIFY_WINDOW_HOURS,
                                cooldown_hours: int = VERIFY_COOLDOWN_HOURS,
                                now_ref: str | None = None):
    """相容包裝(純 C′ 行為 / rollback 用):回傳最強且未冷卻的單一 alert
    候選或 None。新的多槽路徑走 build_verification_plans。"""
    now_ref = now_ref or "now"
    for c in alert_candidates(conn, thresholds, today, window_hours, now_ref):
        if not _is_cooled(conn, c["origin"], c["destination"],
                          c["depart_date"], cooldown_hours, now_ref):
            return c
    return None


def cta_candidates(ranked_path: str = "docs/ranked.json",
                   today: date | None = None) -> list[dict]:
    """從 Recommendation Engine 既有輸出 ranked.json 的 best_option 取首頁 CTA
    驗證候選,依 observed_at 最舊者先(最久未更新的 CTA 最該重驗)。唯讀,
    嚴格 fail-soft:檔案缺失/JSON 損壞/欄位缺失一律回 []。不修改引擎。
    """
    today = today or date.today()
    try:
        import json
        with open(ranked_path, encoding="utf-8") as fh:
            data = json.load(fh)
        routes = data.get("routes") or []
    except (OSError, ValueError, TypeError):
        return []
    out = []
    for r in routes:
        try:
            bo = r.get("best_option")
            if not bo:
                continue
            o, d = r.get("origin"), r.get("destination")
            dep_s, ret_s = bo.get("depart_date"), bo.get("return_date")
            if not (o and d and dep_s and ret_s):
                continue
            if not _valid_horizon(dep_s, today):
                continue
            obs = bo.get("observed_at") or ""
            out.append({"origin": o, "destination": d, "depart_date": dep_s,
                        "return_date": ret_s, "price": bo.get("price"),
                        "observed_at": obs, "slot_kind": "cta"})
        except (AttributeError, TypeError):
            continue                  # 單筆欄位不符 → 跳過,不炸
    out.sort(key=lambda c: c["observed_at"])   # 最舊者先
    return out


def cheap_days_candidates(data_path: str = "docs/data.json",
                          today: date | None = None, *,
                          require_notable: bool = True,
                          min_discount_pct: float = 0.0) -> list[dict]:
    """從 docs/data.json 的 cheap_days 取驗證候選：正在被推薦、但價格是快取估價的日期。

    為什麼需要這個池子：cheap_days 看板是最新的推薦介面，卻是唯一沒有驗證優先權
    的。實測拿 aviasales 快取價對 Google 即時價比對（同航程、時間差 ≤6 小時、
    74 筆），絕對誤差中位數 7.9%、90 百分位 27%，而且 28% 的情況下快取比實價
    便宜 >10%。看板第一名若剛好落在那 28% 裡，使用者點進去會看到高得多的價格。

    來源必須是快取——已經是 google 實價的不必再花額度。

    **require_notable 為什麼要可調（兩個呼叫端的預算結構完全不同）**：

    - `True`（預設，給 fsc_snapshot）：只收 notable（落差 ≥ NOTIFY_PCT）。
      SerpAPI 那邊只有 3 個驗證槽、要跟 alert / cta / hero 共用，所以 cheap_day
      必須是「夠大的落差才值得佔一槽」，沒有 notable 的日子自動讓位。實測
      cheap∩hero = 0、cheap∩cta = 0，是真正不同的驗證目標。**不要把這個預設
      改成 False**，否則 cheap_day 會天天填滿、長期擠掉 cta / hero。
    - `False`（給 verify_airlines）：Scrape.do 的額度是這支程式專用的，沒人
      跟它搶，卡在 notable 只是讓額度閒置。實測 14 天看板歷史：notable 每日
      1.9 筆（576 credits/月 = 免費層 58%），放寬後 2.6 筆（819 = 82%），
      VERIFICATIONS_PER_DAY=3 的理論上限是 930 = 93%，都在 1,000 之內。
      放寬的效果是「看板上出現的每條航線都有實價」而不只是落差 ≥30% 的。

    min_discount_pct 是放寬時的下限（呼叫端傳 cheap_days.DROP_PCT）：看板本身
    已經只收 ≥ DROP_PCT 的項目，這個參數是明示意圖、並防止門檻日後被調鬆時
    這裡跟著失守。

    落差大者先（最該被證實或推翻的）。唯讀，嚴格 fail-soft：檔案缺失 / JSON
    損壞 / 欄位缺失一律回 []。
    """
    today = today or date.today()
    try:
        import json
        with open(data_path, encoding="utf-8") as fh:
            items = json.load(fh).get("cheap_days") or []
    except (OSError, ValueError, TypeError, AttributeError):
        return []
    out = []
    for it in items:
        try:
            if require_notable and not it.get("notable"):
                continue
            if float(it.get("discount_pct") or 0.0) < min_discount_pct:
                continue
            if str(it.get("source") or "").lower() not in CACHE_SOURCES:
                continue                  # 已是 google 實價 → 不必花額度
            o, d = it.get("origin"), it.get("destination")
            dep_s, ret_s = it.get("depart_date"), it.get("return_date")
            if not (o and d and dep_s and ret_s):
                continue                  # 沒有回程日就組不出比價查詢，不猜
            if not _valid_horizon(dep_s, today):
                continue
            out.append({"origin": o, "destination": d, "depart_date": dep_s,
                        "return_date": ret_s, "price": it.get("price"),
                        "observed_at": it.get("observed_at") or "",
                        "discount_pct": it.get("discount_pct") or 0.0,
                        "slot_kind": "cheap_day"})
        except (AttributeError, TypeError):
            continue                      # 單筆欄位不符 → 跳過，不炸
    out.sort(key=lambda c: -float(c["discount_pct"]))   # 落差大者先
    return out


def hero_candidates(conn, routes: list[dict],
                    today: date | None = None,
                    now_ref: str | None = None) -> list[dict]:
    """各監控航線目前 Hero(首頁大字)背後的權威 route/date,取最久未經
    Google 實價背書者先。Hero 選擇完全複用 export_web 的權威 helper,保證
    與首頁顯示同一筆。純 DB 讀取、零 API。
    """
    from .export_web import authoritative_latest, hero_from_latest
    today = today or date.today()
    out = []
    for rt in routes:
        o, d = rt["origin"], rt["destination"]
        hero = hero_from_latest(authoritative_latest(
            conn, o, d, as_of_date=today, as_of_ts=now_ref))
        if hero is None:
            continue
        dep_s, ret_s = hero["depart_date"], hero.get("return_date")
        if not ret_s or not _valid_horizon(dep_s, today):
            continue
        # 排序鍵:非 google 或舊 google 先驗(observed_at 早者優先)
        out.append({"origin": o, "destination": d, "depart_date": dep_s,
                    "return_date": ret_s, "price": hero["price"],
                    "observed_at": hero.get("observed_at") or "",
                    "source": hero.get("source"), "slot_kind": "hero"})
    out.sort(key=lambda c: c["observed_at"])   # 最久未更新者先
    return out


def build_verification_plans(conn, thresholds, routes,
                             ranked_path: str = "docs/ranked.json",
                             data_path: str = "docs/data.json",
                             today: date | None = None,
                             claimed_trips: set | None = None,
                             max_slots: int = 3,
                             cooldown_hours: int = VERIFY_COOLDOWN_HOURS,
                             now_ref: str | None = None) -> list[dict]:
    """統籌三個決策面(Alert→CTA→Hero)產生最多 max_slots 個驗證 plan。

    固定順序處理三個 pool;每 pool 先試「換 route」(route 未被 claimed),
    補位時才允許同 route 不同日期;某 pool 用罄由後續 pool 遞補。全程套用
    72h 冷卻與跨槽 claimed 去重。線性可讀、無遞迴、無 Priority Score。

    單一時間來源:today 為日期窗口基準,now_ref 為 SQL julianday 基準,兩者
    由此函式一次決定並貫穿所有 pool 與冷卻查詢,不讓下游各自取 date.today()。
    production 傳 now_ref=None → 'now'(真實時鐘),測試傳固定字串。

    claimed_trips: (o,d,depart,return) 已被 rotation/前槽占用的行程,就地更新。
    候選不足時回傳少於 max_slots 個(不硬湊、不浪費額度)。
    """
    today = today or date.today()
    now_ref = now_ref or "now"
    claimed_trips = claimed_trips if claimed_trips is not None else set()
    claimed_routes = {(o, d) for (o, d, _dep, _ret) in claimed_trips}
    # 池子順序＝驗證優先序，每個池子最多貢獻一個槽，用罄由後面遞補。
    #   alert      已經推播到使用者手機的，主張最強
    #   cheap_day  看板正在推薦、但價格是快取估價的（見 cheap_days_candidates）
    #   cta / hero 網站上的推薦與大字
    # cheap_day 只收 notable 且來源為快取的項目，池深通常 0–4，
    # 沒有 notable 的日子自動讓位，不會長期擠掉 cta / hero。
    pools = [alert_candidates(conn, thresholds, today, now_ref=now_ref),
             cheap_days_candidates(data_path, today),
             cta_candidates(ranked_path, today),
             hero_candidates(conn, routes, today, now_ref=now_ref)]
    plans: list[dict] = []

    def _try_pick(pool, allow_same_route):
        for c in pool:
            o, d = c["origin"], c["destination"]
            trip = (o, d, c["depart_date"], c["return_date"])
            if trip in claimed_trips:
                continue
            if not allow_same_route and (o, d) in claimed_routes:
                continue
            if _is_cooled(conn, o, d, c["depart_date"], cooldown_hours, now_ref):
                continue
            return c
        return None

    # 每個 pool 貢獻至多一個槽,先求 route 分散;pool 用罄則後面 pool 遞補
    for pool in pools:
        if len(plans) >= max_slots:
            break
        pick = _try_pick(pool, allow_same_route=False)
        if pick is None:              # 沒有新 route 候選 → 允許同 route 不同日期
            pick = _try_pick(pool, allow_same_route=True)
        if pick is not None:
            trip = (pick["origin"], pick["destination"],
                    pick["depart_date"], pick["return_date"])
            claimed_trips.add(trip)
            claimed_routes.add((pick["origin"], pick["destination"]))
            plans.append(pick)
    return plans
