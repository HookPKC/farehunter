"""google-flights2（RapidAPI）：警報的第二驗證來源，每月 150 次免費（ADR 0005）。

**為什麼有這支**：Bright Data（ADR 0004）驗證不到的警報——頁面上沒有那家航空、逾時、額度用完——
會以「未驗證」發出；2026-11-01～11-13 的 10 則未驗證警報有 3 則事後看是錯的。這裡在發出前
再問一次 Google Flights 的另一個入口。

**實測**（2026-11-14，跟 Bright Data 同一小時查過的兩趟）：KHH-NRT 11/30–12/07 捷星 11,114、
泰亞航 13,087、華航 14,468，KHH-OKA 01/15–01/17 捷星 7,478——4 個價格完全相同。

**回應的兩個坑**：`stops` 欄位連轉機航班都是 0，不能用——直飛看 `flights` 只有一段；
`price` 可能是字串 "unavailable"。航空公司代碼取班號前綴（"GK 18" → GK），不靠名稱對照。

**額度**：免費方案每月 150 次（回應 header `X-RateLimit-Requests-Limit`），約 30 天重置。只用在
警報驗證：每輪最多 PER_RUN_MAX、每天 DAILY_MAX、近 30 天 WINDOW_MAX；不巡檢。帳本記在
api_usage（provider='gflights2'）。沒有 RAPIDAPI_KEY 時整支不做任何事；任何失敗都只記 log。

金鑰放在 header，不在網址；錯誤訊息仍一律遮罩，連線錯誤只留例外類型。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, timedelta

import requests

from . import brightdata_flights as bd
from . import calendar_hints

log = logging.getLogger(__name__)

HOST = "google-flights2.p.rapidapi.com"
API_URL = f"https://{HOST}/api/v1/searchFlights"
PICKER_URL = f"https://{HOST}/api/v1/getCalendarPicker"
#: Picker 跟警報驗證共用同一份額度（帳本同一個 provider）。Picker 只在近 30 天用量低於這個數、
#: 今天用量低於 PICKER_DAILY_CEILING 時才抓，其餘留給警報驗證。每輪最多抓 1 段。
PICKER_WINDOW_CEILING = 65
PICKER_DAILY_CEILING = 3
PROVIDER = "gflights2"
WINDOW_DAYS = 30
WINDOW_MAX = 130             # 每月 150 次，留 20 次給手動探測與誤差
DAILY_MAX = 6
PER_RUN_MAX = 2
CONNECT_TIMEOUT_S = 10
READ_TIMEOUT_S = 60


class GFlights2Error(bd.BrightDataError):
    pass


class GFlights2Timeout(GFlights2Error, bd.BrightDataTimeout):
    pass


@dataclass(frozen=True)
class Itinerary:
    """跟 brightdata_flights.Itinerary 同介面（select 用 price／nonstop／carriers）。"""
    price: float
    nonstop: bool
    airlines: tuple[str, ...]
    codes: tuple[str, ...]

    @property
    def carriers(self) -> str | None:
        if not self.codes or any(not c for c in self.codes):
            return None
        return ",".join(dict.fromkeys(self.codes))


def configured() -> bool:
    return bool(os.environ.get("RAPIDAPI_KEY", "").strip())


def fetch(origin: str, dest: str, depart: str, ret: str,
          session: requests.Session | None = None) -> dict:
    return _get(API_URL, {"departure_id": origin, "arrival_id": dest, "outbound_date": depart,
                          "return_date": ret, "travel_class": "ECONOMY", "adults": "1",
                          "currency": "TWD", "country_code": "TW", "language_code": "en-US"},
                session)


def _get(url: str, params: dict, session=None) -> dict:
    """共用的 GET：金鑰在 header；錯誤訊息遮罩、連線錯誤只留例外類型。"""
    key = os.environ.get("RAPIDAPI_KEY", "").strip()
    if not key:
        raise GFlights2Error("missing RAPIDAPI_KEY")
    try:
        r = (session or requests).get(url, params=params,
                                      timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
                                      headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": HOST})
    except requests.RequestException as exc:
        kind = GFlights2Timeout if isinstance(exc, requests.Timeout) else GFlights2Error
        raise kind(f"transport: {type(exc).__name__}") from None
    if r.status_code != 200:
        raise GFlights2Error(f"HTTP {r.status_code}: {r.text}".replace(key, "***")[:200])
    try:
        body = r.json()
    except ValueError:
        raise GFlights2Error("HTTP 200 but not JSON") from None
    if not isinstance(body, dict) or body.get("status") is not True:
        msg = body.get("message") if isinstance(body, dict) else body
        raise GFlights2Error(f"status false: {msg}".replace(key, "***")[:200])
    return body


def fetch_picker(origin: str, dest: str, start: str, end: str, *, nights: int,
                 session=None) -> list[tuple[str, str, float]]:
    """getCalendarPicker：[start, end] 每個出發日「＋nights 晚」的最低價（含轉機、沒有航空公司）。

    只收回程日剛好是去程日＋nights、日期在範圍內、價格是正數的列——回應若變形，寧可少收。
    """
    body = _get(PICKER_URL, {"departure_id": origin, "arrival_id": dest, "start_date": start,
                             "end_date": end, "trip_type": "ROUND", "trip_days": str(nights),
                             "currency": "TWD", "country_code": "TW", "language_code": "en-US"},
                session)
    out = []
    for row in body.get("data") or []:
        try:
            dep, ret, price = str(row["departure"]), str(row["return"]), row["price"]
            ok = ((date.fromisoformat(ret) - date.fromisoformat(dep)).days == nights
                  and start <= dep <= end)
        except (KeyError, TypeError, ValueError):
            continue
        if ok and not isinstance(price, bool) and isinstance(price, (int, float)) and price > 0:
            out.append((dep, ret, float(price)))
    return out


def refresh_hints(store, now, *, fetcher=None) -> dict:
    """該重抓的 Picker 時間段抓一段（每輪最多 1 次請求），寫進 calendar_hints。

    哪一段該抓：那段日期裡、REFRESH_DAYS 天內抓到的線索不到一半。額度不夠（留給警報驗證）
    就不抓；失敗只記 log——線索過期後巡檢自然退回固定日期。
    """
    stats = {"requests": 0, "ok": 0, "hints": 0, "errors": 0, "skipped": ""}
    day = now.date()
    window = store.api_usage_since(PROVIDER, (day - timedelta(days=WINDOW_DAYS - 1)).isoformat())
    today = store.api_usage_since(PROVIDER, day.isoformat())
    if window >= PICKER_WINDOW_CEILING or today >= PICKER_DAILY_CEILING:
        stats["skipped"] = "quota"
        return stats
    since = (now - timedelta(days=calendar_hints.REFRESH_DAYS)).isoformat(timespec="seconds")
    due = None
    for start, end in calendar_hints.windows(day):
        for o, d in calendar_hints.ROUTES:
            have = store.calendar_hints_fresh(o, d, start.isoformat(), end.isoformat(), since)
            if have < calendar_hints.WINDOW_DAYS // 2:
                due = (o, d, start.isoformat(), end.isoformat())
                break
        if due:
            break
    if due is None:
        stats["skipped"] = "fresh"
        return stats
    o, d, start, end = due
    stats["requests"] = 1
    ok = False
    try:
        rows = (fetcher or fetch_picker)(o, d, start, end, nights=calendar_hints.STAY_NIGHTS)
        if not rows:
            raise GFlights2Error("picker returned no usable rows")
        store.record_calendar_hints(o, d, rows, now.isoformat(timespec="seconds"))
        ok = True
        stats["ok"], stats["hints"] = 1, len(rows)
    except GFlights2Error as exc:
        stats["errors"] = 1
        log.warning("google-flights2 Picker %s→%s %s~%s: %s", o, d, start, end, exc)
    except Exception as exc:
        stats["errors"] = 1
        log.warning("google-flights2 Picker %s→%s 意外失敗：%s", o, d, type(exc).__name__)
    finally:
        store.record_api_usage(PROVIDER, day.isoformat(), ok=ok)
    return stats


def _code(flight_number) -> str:
    """'GK 18' → 'GK'；看不懂就空字串（那個行程的 carriers 會是 None，不寫入）。"""
    head = str(flight_number or "").strip().split(" ")[0].upper()
    return head if len(head) == 2 and head.isalnum() else ""


def parse(body: dict) -> list[Itinerary]:
    its = ((body.get("data") or {}).get("itineraries") or {}) if isinstance(body, dict) else {}
    out = []
    for group in ("topFlights", "otherFlights"):
        for it in its.get(group) or []:
            price, legs = it.get("price"), it.get("flights") or []
            if isinstance(price, bool) or not isinstance(price, (int, float)) or not legs:
                continue                      # "unavailable" 之類
            out.append(Itinerary(price=float(price), nonstop=len(legs) == 1,
                                 airlines=tuple(str(f.get("airline") or "") for f in legs),
                                 codes=tuple(_code(f.get("flight_number")) for f in legs)))
    return out


class Verifier(bd.Verifier):
    """只做警報驗證（verify_alert／recent_match／checked_recently），不巡檢。"""
    provider = PROVIDER
    label = "google-flights2"
    parse = staticmethod(parse)

    def _allowance(self) -> int:
        day = self.now.date()
        window = self.store.api_usage_since(
            self.provider, (day - timedelta(days=WINDOW_DAYS - 1)).isoformat())
        today = self.store.api_usage_since(self.provider, day.isoformat())
        return max(0, min(PER_RUN_MAX, DAILY_MAX - today, WINDOW_MAX - window))

    def _default_fetch(self):
        return fetch

    def sweep(self, candidates, routes) -> None:      # 額度只給警報驗證
        return None
