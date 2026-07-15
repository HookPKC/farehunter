"""SerpAPI Google Flights client — daily snapshots of full-service carrier fares.

The Aviasales cache is a cheapest-fare feed and structurally excludes
full-service carriers (CI/BR/JX...). Google Flights has them; SerpAPI exposes
Google Flights as an API with a free tier (~100 searches/month). We spend
that budget as SEARCHES_PER_DAY route snapshots per day, rotating through the
configured routes, and record the cheapest all-full-service itinerary into the
existing fare_class='full' track.

Endpoint: GET https://serpapi.com/search?engine=google_flights
Auth: api_key query param (env var SERPAPI_KEY).
"""
from __future__ import annotations

import os
import logging
from datetime import date, timedelta

import requests

from .models import Offer
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
               cooldown_hours: int = VERIFY_COOLDOWN_HOURS) -> bool:
    """True 若 (o,d,dep) 於 cooldown_hours 內已有 source='google' 觀測。
    julianday() 比較,對混合時間字串格式安全。"""
    return conn.execute(
        """SELECT 1 FROM observations
           WHERE origin=? AND destination=? AND depart_date=?
             AND source='google'
             AND julianday(observed_at) >= julianday('now') - ?/24.0
           LIMIT 1""", (o, d, dep_s, cooldown_hours)).fetchone() is not None


def _valid_horizon(dep_s: str, today: date) -> bool:
    """日期健全性:明天 ~ +VERIFY_MAX_AHEAD_DAYS 之間才可查。"""
    try:
        dep = date.fromisoformat(dep_s)
    except (ValueError, TypeError):
        return False
    return today + timedelta(days=1) <= dep <= today + timedelta(days=VERIFY_MAX_AHEAD_DAYS)


def alert_candidates(conn, thresholds: dict[tuple[str, str], float | None],
                     today: date | None = None,
                     window_hours: int = VERIFY_WINDOW_HOURS) -> list[dict]:
    """回傳最近 window_hours 的 alert 驗證候選,依偏好排序(強者先)。
    每筆含 origin/destination/depart_date/return_date/price/reason。
    不套用冷卻與 claimed(由 build_verification_plans 統一處理);配不到
    return_date 的 alert 直接剔除(不猜)。純 DB 讀取、零 API。

    排序:reason(new_low>absolute>big_drop)→ price/threshold 升冪 →
    出發日近 → sent_at 新。不寫死任何航線。
    """
    today = today or date.today()
    rows = conn.execute(
        """SELECT origin, destination, depart_date, price, reason, sent_at,
                  julianday(sent_at) AS sent_j
           FROM alerts
           WHERE julianday(sent_at) >= julianday('now') - ?/24.0
           ORDER BY sent_j DESC""", (window_hours,)).fetchall()
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
                                cooldown_hours: int = VERIFY_COOLDOWN_HOURS):
    """相容包裝(純 C′ 行為 / rollback 用):回傳最強且未冷卻的單一 alert
    候選或 None。新的多槽路徑走 build_verification_plans。"""
    for c in alert_candidates(conn, thresholds, today, window_hours):
        if not _is_cooled(conn, c["origin"], c["destination"],
                          c["depart_date"], cooldown_hours):
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


def hero_candidates(conn, routes: list[dict],
                    today: date | None = None) -> list[dict]:
    """各監控航線目前 Hero(首頁大字)背後的權威 route/date,取最久未經
    Google 實價背書者先。Hero 選擇完全複用 export_web 的權威 helper,保證
    與首頁顯示同一筆。純 DB 讀取、零 API。
    """
    from .export_web import authoritative_latest, hero_from_latest
    today = today or date.today()
    out = []
    for rt in routes:
        o, d = rt["origin"], rt["destination"]
        hero = hero_from_latest(authoritative_latest(conn, o, d))
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
                             today: date | None = None,
                             claimed_trips: set | None = None,
                             max_slots: int = 3,
                             cooldown_hours: int = VERIFY_COOLDOWN_HOURS) -> list[dict]:
    """統籌三個決策面(Alert→CTA→Hero)產生最多 max_slots 個驗證 plan。

    固定順序處理三個 pool;每 pool 先試「換 route」(route 未被 claimed),
    補位時才允許同 route 不同日期;某 pool 用罄由後續 pool 遞補。全程套用
    72h 冷卻與跨槽 claimed 去重。線性可讀、無遞迴、無 Priority Score。

    claimed_trips: (o,d,depart,return) 已被 rotation/前槽占用的行程,就地更新。
    候選不足時回傳少於 max_slots 個(不硬湊、不浪費額度)。
    """
    today = today or date.today()
    claimed_trips = claimed_trips if claimed_trips is not None else set()
    claimed_routes = {(o, d) for (o, d, _dep, _ret) in claimed_trips}
    pools = [alert_candidates(conn, thresholds, today),
             cta_candidates(ranked_path, today),
             hero_candidates(conn, routes, today)]
    plans: list[dict] = []

    def _try_pick(pool, allow_same_route):
        for c in pool:
            o, d = c["origin"], c["destination"]
            trip = (o, d, c["depart_date"], c["return_date"])
            if trip in claimed_trips:
                continue
            if not allow_same_route and (o, d) in claimed_routes:
                continue
            if _is_cooled(conn, o, d, c["depart_date"], cooldown_hours):
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
