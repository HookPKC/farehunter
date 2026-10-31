"""Bright Data 抓 Google Flights：每月 5,000 次免費的 Google 實價來源（ADR 0004）。

**為什麼有這支**：警報與看板的價格絕大多數是 Aviasales 快取估價；Google 實價只有
SerpAPI（約 6 次／天）與 Scrape.do（約 3 次／天）在補。Bright Data 的免費層每月
5,000 次、每月 1 日重置、用完即停不扣款（官方 brightdata-mcp README），而且走它
自己的代理網路，不受 Google 對 GitHub 主機限流影響。2026-10-31 實測 8 航線直飛
最低價與 fli 日曆 8/8 完全相同（ADR 0003）。

**做什麼**：送 Google Flights 搜尋頁的網址給 Bright Data（Web Unlocker zone），拿回
整頁 HTML，從每個航班的 aria-label 解析「來回總價、是否直飛、航空公司」，寫成
source='google'、provider='brightdata' 的觀測——之後的價格狀態、看板、冷卻全部沿用
既有機制，不另開語意。

**額度**：配速讓整個月平均用到 USE_FRACTION（見 run_allowance）；帳本記在 prices.db
的 api_usage 表。沒有 BRIGHTDATA_API_KEY 時整支不做任何事。
"""
from __future__ import annotations

import calendar
import html as html_lib
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

from .models import Offer

log = logging.getLogger(__name__)

API_URL = "https://api.brightdata.com/request"
#: 官方 brightdata-mcp 第一次啟動時建的 Web Unlocker zone，免費額度掛在這裡
DEFAULT_ZONE = "mcp_unlocker"
PROVIDER = "brightdata"
TIMEOUT_S = 60               # 實測每頁 2–15 秒
MONTHLY_FREE = 5000          # 每月 1 日（UTC）重置
USE_FRACTION = 0.95          # 留 5% 餘裕給月底的誤差
PER_RUN_MAX = 15             # 單輪上限：每次 2–15 秒，15 次最多約 4 分鐘
SWEEP_DEADLINE_S = 300       # 巡檢在單輪內最多花這麼久，到了就停
RECHECK_HOURS = 24           # 同一行程 24 小時內已有 Google 實價就不再查
STAY_NIGHTS = 5              # 快取沒資料的航線，巡檢固定查 5 晚來回
MIN_LEAD_DAYS = 2            # 太近的出發日不查（快取價多半已失效，也難以成行）

#: Google Flights 英文介面顯示的航空公司名稱 → IATA 代碼。只收我們航線上會出現的；
#: 對不上的不寫入（寧可少一筆，也不要寫進錯的代碼讓「同航程」比對誤判）。
#: 「Jetstar」在台灣—日本線上都是捷星日本（GK）。
AIRLINE_CODES = {
    "tigerair taiwan": "IT", "scoot": "TR", "peach": "MM", "peach aviation": "MM",
    "jetstar": "GK", "jetstar japan": "GK", "thai lion air": "SL", "thai airasia": "FD",
    "airasia": "AK", "airasia x": "D7", "thai vietjet air": "VZ", "thai vietjet": "VZ",
    "vietjet air": "VJ", "china airlines": "CI", "eva air": "BR", "starlux airlines": "JX",
    "starlux": "JX", "japan airlines": "JL", "ana": "NH", "all nippon airways": "NH",
    "cathay pacific": "CX", "hong kong express": "UO", "hk express": "UO",
    "greater bay airlines": "HB", "mandarin airlines": "AE", "zipair": "ZG",
    "zipair tokyo": "ZG", "spring japan": "IJ", "skymark": "BC", "jeju air": "7C",
    "t'way air": "TW", "jin air": "LJ", "air busan": "BX", "air seoul": "RS",
    "eastar jet": "ZE", "korean air": "KE", "asiana airlines": "OZ", "asiana": "OZ",
}

#: 每個航班一個 aria-label：「From 8,004 New Taiwan dollars round trip total. Nonstop flight
#: with Scoot. Leaves …」。只解析來回總價（curr=TWD、hl=en 才是這個格式）。
_LABEL = re.compile(r'aria-label="From ([\d,]+) (?:New )?Taiwan dollars? round trip total\.([^"]*)"')
_AIRLINE = re.compile(r"flight with ([^.]+)\.")


class BrightDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class Itinerary:
    price: float
    nonstop: bool
    airlines: tuple[str, ...]          # Google 顯示的名稱，原樣

    @property
    def carriers(self) -> str | None:
        """IATA 代碼（逗號連接）；任一家對不上就回 None。"""
        codes = [AIRLINE_CODES.get(a.strip().lower()) for a in self.airlines]
        if not codes or None in codes:
            return None
        return ",".join(dict.fromkeys(codes))


def configured() -> bool:
    return bool(os.environ.get("BRIGHTDATA_API_KEY"))


def search_url(origin: str, dest: str, depart: str, ret: str) -> str:
    """送給 Bright Data 抓的 Google Flights 搜尋頁（2026-10-31 實測的寫法）。

    跟通知、看板裡的 Google Flights 深連結是兩回事：那個 q= 參數凍結不動
    （HANDOFF §2），這裡只是抓取用。
    """
    q = f"Flights to {dest} from {origin} on {depart} through {ret}"
    return f"https://www.google.com/travel/flights?q={quote(q)}&curr=TWD&hl=en&gl=tw"


def fetch(origin: str, dest: str, depart: str, ret: str,
          session: requests.Session | None = None) -> str:
    key = os.environ.get("BRIGHTDATA_API_KEY", "")
    if not key:
        raise BrightDataError("missing BRIGHTDATA_API_KEY")
    zone = os.environ.get("BRIGHTDATA_ZONE") or DEFAULT_ZONE
    s = session or requests
    try:
        r = s.post(API_URL, timeout=TIMEOUT_S,
                   headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                   json={"zone": zone, "url": search_url(origin, dest, depart, ret),
                         "format": "raw"})
    except requests.RequestException as exc:
        raise BrightDataError(f"transport: {type(exc).__name__}: {exc}"
                              .replace(key, "***")[:200]) from exc
    if r.status_code != 200:
        # 錯誤內容只取開頭、遮掉金鑰；絕不印 zone 管理 API 的回應（含 zone 密碼，見 ADR 0004）
        raise BrightDataError(f"HTTP {r.status_code}: {r.text[:200]}".replace(key, "***"))
    if not r.text:
        raise BrightDataError("HTTP 200 but empty body")
    return r.text


def parse(page: str) -> list[Itinerary]:
    out = []
    for price, rest in _LABEL.findall(page):
        rest = html_lib.unescape(rest)
        m = _AIRLINE.search(rest)
        names = re.split(r",\s*|\s+and\s+", m.group(1)) if m else []
        out.append(Itinerary(price=float(price.replace(",", "")),
                             nonstop="Nonstop flight" in rest,
                             airlines=tuple(n for n in names if n)))
    return out


def select(origin: str, dest: str, depart: str, ret: str, itineraries: list[Itinerary],
           want_carriers: str | None = None) -> tuple[list[Offer], Offer | None]:
    """要寫入的觀測，以及「跟 want_carriers 同一組航空公司」的那一筆（沒有就 None）。

    寫入：直飛最低價（代碼對得上的）；若 want_carriers 的直飛最低價是另一筆，也寫入——
    同航程比對要求航空公司相同（price_state.same_itinerary），只寫全場最低會讓
    其他航空公司的快取價永遠驗證不到。
    """
    want = _sig(want_carriers)

    def offer(it: Itinerary) -> Offer:
        return Offer(origin=origin, destination=dest, depart_date=depart, return_date=ret,
                     price=it.price, currency="TWD", carriers=it.carriers, stops=0,
                     duration="", link=search_url(origin, dest, depart, ret),
                     fare_class="any", source="google", provider=PROVIDER)

    direct = sorted((it for it in itineraries if it.nonstop and it.carriers),
                    key=lambda it: it.price)
    if not direct:
        return [], None
    chosen = [direct[0]]
    match = next((it for it in direct if want and _sig(it.carriers) == want), None)
    if match is not None and match is not direct[0]:
        chosen.append(match)
    offers = [offer(it) for it in chosen]
    matched = next((o for o in offers if want and _sig(o.carriers) == want), None)
    return offers, matched


def _sig(carriers: str | None) -> str | None:
    if not carriers:
        return None
    parts = sorted(p.strip().upper() for p in carriers.split(",") if p.strip())
    return ",".join(parts) or None


def month_start(now: datetime) -> datetime:
    now = now.astimezone(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


def run_allowance(used_this_month: int, now: datetime, *,
                  monthly=MONTHLY_FREE, fraction=USE_FRACTION, per_run_max=PER_RUN_MAX) -> int:
    """這一輪可以用幾次：本月剩餘額度 ÷ 本月剩餘小時（monitor 每小時一輪）。

    前面的輪次失敗或被跳過，剩餘額度攤到後面，月底剛好用到 fraction；向上取整讓
    剩餘不足一輪平均時也還能用 1 次。剩餘小時最少算 1，所以結果不會超過剩餘額度。
    """
    remaining = int(monthly * fraction) - used_this_month
    if remaining <= 0:
        return 0
    start = month_start(now)
    days = calendar.monthrange(start.year, start.month)[1]
    hours_left = max(1.0, (start + timedelta(days=days) - now).total_seconds() / 3600)
    return min(per_run_max, math.ceil(remaining / hours_left))


class Verifier:
    """一輪 monitor 的 Bright Data 用量：警報驗證優先，剩下的給巡檢。"""

    def __init__(self, store, now: datetime, *, allowance: int | None = None,
                 fetcher=None, clock=time.monotonic):
        self.store, self.now = store, now
        used = store.api_usage_since(PROVIDER, month_start(now).date().isoformat())
        self.left = run_allowance(used, now) if allowance is None else allowance
        self.fetch = fetcher or fetch
        self.clock = clock
        self.stats = {"allowance": self.left, "requests": 0, "ok": 0, "recorded": 0,
                      "alerts_checked": 0, "swept": 0, "errors": 0}

    def check(self, origin: str, dest: str, depart: str, ret: str,
              want_carriers: str | None = None) -> Offer | None:
        """查一次、寫入觀測、記帳；回傳跟 want_carriers 同航空公司的那筆（沒有就 None）。

        任何失敗都只記 log：Bright Data 是加分項，壞了抓價與警報照常。
        """
        if self.left <= 0:
            return None
        self.left -= 1
        self.stats["requests"] += 1
        ok = False
        try:
            itineraries = parse(self.fetch(origin, dest, depart, ret))
            if not itineraries:
                # 頁面拿到了卻一筆都解析不到：多半是 Google 改版，要看得見
                raise BrightDataError("page fetched but no itineraries parsed")
            ok = True
            offers, matched = select(origin, dest, depart, ret, itineraries, want_carriers)
            for o in offers:
                self.store.record(o)
            self.stats["recorded"] += len(offers)
            return matched
        except BrightDataError as exc:
            self.stats["errors"] += 1
            log.warning("Bright Data %s→%s %s/%s: %s", origin, dest, depart, ret, exc)
            return None
        finally:
            self.stats["ok"] += ok
            self.store.record_api_usage(PROVIDER, self.now.date().isoformat(), ok=ok)

    def sweep(self, candidates: list[Offer], routes: list[tuple[str, str]]) -> None:
        """把剩下的額度花在「各航線最便宜、24 小時內沒有 Google 實價」的行程上。

        航線輪流取（每條先拿最便宜的），所以額度少時也是每條線都顧到；快取完全沒有
        資料的航線（KHH-NGO 這類）改查固定日期，讓它也有 Google 實價。
        """
        if self.left <= 0:
            return
        since = (self.now - timedelta(hours=RECHECK_HOURS)).isoformat(timespec="seconds")
        earliest = (self.now.date() + timedelta(days=MIN_LEAD_DAYS)).isoformat()
        queues: dict[tuple[str, str], list[tuple[str, str, str | None]]] = {r: [] for r in routes}
        seen = set()
        for c in sorted(candidates, key=lambda o: o.price):
            key = (c.origin, c.destination, c.depart_date, c.return_date)
            if (not c.return_date or c.stops != 0 or c.depart_date < earliest
                    or key in seen or (c.origin, c.destination) not in queues):
                continue
            seen.add(key)
            queues[(c.origin, c.destination)].append((c.depart_date, c.return_date, c.carriers))
        for route, q in queues.items():
            if not q:
                q.extend((d, r, None) for d, r in anchor_trips(self.now))
        deadline = self.clock() + SWEEP_DEADLINE_S
        while self.left > 0 and self.clock() < deadline and any(queues.values()):
            for (o, d), q in queues.items():
                while q:
                    dep, ret, carriers = q.pop(0)
                    if not self.store.has_google_since(o, d, dep, ret, since):
                        break
                else:
                    continue
                self.check(o, d, dep, ret, carriers)
                self.stats["swept"] += 1
                if self.left <= 0 or self.clock() >= deadline:
                    break


def anchor_trips(now: datetime, count: int = 8) -> list[tuple[str, str]]:
    """快取沒資料的航線要查哪些日子：未來 2–4 個月內每週一個出發日，住 STAY_NIGHTS 晚。

    起點隨日期輪替（一週七天），幾天下來各個星期幾都會被查到。
    """
    start = now.date() + timedelta(days=14 + now.timetuple().tm_yday % 7)
    out = []
    for k in range(count):
        dep = start + timedelta(days=7 * k)
        out.append((dep.isoformat(), (dep + timedelta(days=STAY_NIGHTS)).isoformat()))
    return out
