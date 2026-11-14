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
from dataclasses import dataclass, replace
from typing import NamedTuple
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

from .models import Offer
from .price_state import carrier_signature
from .travelpayouts import FULL_SERVICE

log = logging.getLogger(__name__)

API_URL = "https://api.brightdata.com/request"
#: 官方 brightdata-mcp 第一次啟動時建的 Web Unlocker zone，免費額度掛在這裡
DEFAULT_ZONE = "mcp_unlocker"
PROVIDER = "brightdata"
#: 連上 api.brightdata.com 通常不到 1 秒；連不上就是對方或網路出事，等久也沒用，早點放棄
#: 把時間留給後面的查詢（原本單一 timeout=60，連線、讀取各等 60 秒）。
CONNECT_TIMEOUT_S = 10
#: 實測每頁多半 2–15 秒，但 2026-11 有約 7% 等了 60 秒還沒回來（ReadTimeout），那次的額度
#: 照樣記帳、警報驗證也因此落空。放寬到 120 秒；統計的 ok_over_60s 記下「舊上限會丟掉、
#: 新上限救回來」的成功次數，用來確認放寬值不值得。
READ_TIMEOUT_S = 120
OLD_READ_TIMEOUT_S = 60
#: 一輪 monitor 花在 Bright Data 查詢上的總秒數上限（警報驗證＋巡檢，含失敗的；請求、解析、
#: 寫入都算，抓 Aviasales 的時間不算）。等待放寬到 120 秒後，連續逾時的最壞情況（15 次 × 130 秒
#: ≈ 33 分鐘）會超過 job 的 30 分鐘上限：整輪被砍、這小時的資料不 commit。到了就不再送新的請求。
RUN_BUDGET_S = 600
MONTHLY_FREE = 5000          # 每月 1 日（UTC）重置
USE_FRACTION = 0.95          # 留 5% 餘裕給月底的誤差
PER_RUN_MAX = 15             # 單輪上限：每次多半 2–15 秒、最慢等 READ_TIMEOUT_S；整輪另有 RUN_BUDGET_S
SWEEP_DEADLINE_S = 300       # 巡檢在單輪內最多花這麼久，到了就停
RECHECK_HOURS = 24           # 同一行程 24 小時內已有 Google 實價就不再查
STAY_NIGHTS = 5              # 快取沒資料的航線，巡檢固定查 5 晚來回
ANCHOR_STEP_DAYS = 8         # 固定日期的間隔；不是 7 的倍數，同一張清單裡星期幾各不相同
HIT_MEMORY_DAYS = 30         # 冷門線：最近 30 天有 Google 直飛實價的星期幾優先查
MISS_MEMORY_DAYS = 7         # 冷門線：7 天內查了卻沒有直飛的星期幾先跳過（api_checks 也留這麼久）
#: 每條航線每天至少排這麼多趟進巡檢；快取日期不夠的用固定日期補。2026-11-11：近 24 小時
#: 164 次查詢裡 KHH-NRT 佔 68 次，五條高雄冷門線各只有 2–8 次、未來 180 天有新價格的日期
#: 只有 2–7 天——巡檢只查快取裡有的日期，而冷門線的快取本來就只有幾天。同一趟 24 小時
#: 內不重查，所以這也是冷門線每天最多用掉的次數（5 條 × 12 ≈ 60），其餘仍歸大航線。
ROUTE_MIN_TRIPS = 12
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


class BrightDataTimeout(BrightDataError):
    """等不到回應（連線或讀取逾時）。分開算，才看得出放寬等待上限有沒有用。"""


@dataclass(frozen=True)
class Itinerary:
    price: float
    nonstop: bool
    airlines: tuple[str, ...]          # Google 顯示的名稱，原樣

    @property
    def carriers(self) -> str | None:
        """IATA 代碼（逗號連接）；任一家對不上就回 None。"""
        codes = [airline_code(a) for a in self.airlines]
        if not codes or None in codes:
            return None
        return ",".join(dict.fromkeys(codes))


def airline_code(name: str) -> str | None:
    """Google 顯示的航空公司名稱 → IATA 代碼；對照表沒有就 None。"""
    return AIRLINE_CODES.get(name.strip().lower())


class WeekdayOutcomes(NamedTuple):
    """冷門線各星期幾（0＝週一）的最近結果。"""
    prefer: frozenset          # 有直飛
    avoid: frozenset           # 最近一次落空


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
        r = s.post(API_URL, timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
                   headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                   json={"zone": zone, "url": search_url(origin, dest, depart, ret),
                         "format": "raw"})
    except requests.RequestException as exc:
        kind = BrightDataTimeout if isinstance(exc, requests.Timeout) else BrightDataError
        raise kind(f"transport: {type(exc).__name__}: {exc}"
                   .replace(key, "***")[:200]) from exc
    if r.status_code != 200:
        # 錯誤內容只取開頭、遮掉金鑰；絕不印 zone 管理 API 的回應（含 zone 密碼，見 ADR 0004）
        # 先遮再截：金鑰若剛好跨在截斷點上，先截會留下前半段
        raise BrightDataError(f"HTTP {r.status_code}: {r.text}".replace(key, "***")[:200])
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

    另外寫一筆**傳統航空**（非廉航，travelpayouts.FULL_SERVICE）的直飛最低價，
    fare_class='full'（跟 Aviasales、SerpAPI 的 full 列同一個語意）。頁面本來就列出所有
    航空公司，不多花額度；2026-11-12 使用者要另一組傳統航空的低價通知，而快取裡的
    傳統航空價格 30 天只有 2–12 趟，定不出門檻——先靠這裡累積。full 列跟 'any' 列
    完全分開（統計、看板主價、便宜日都只讀 'any'）。
    """
    want = carrier_signature(want_carriers)

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
    match = next((it for it in direct if want and carrier_signature(it.carriers) == want), None)
    if match is not None and match is not direct[0]:
        chosen.append(match)
    offers = [offer(it) for it in chosen]
    matched = next((o for o in offers if want and carrier_signature(o.carriers) == want), None)
    full = next((it for it in direct if _full_service(it.carriers)), None)
    if full is not None:
        offers.append(replace(offer(full), fare_class="full"))
    return offers, matched


def _full_service(carriers: str | None) -> bool:
    """每一家都是傳統航空（聯營、共掛班號時要全部都是，跟 SerpAPI 的 parse_full_service 一致）。"""
    codes = [c for c in (carriers or "").split(",") if c]
    return bool(codes) and all(c in FULL_SERVICE for c in codes)


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
        # no_direct：頁面正常但沒有可寫入的直飛價（那天沒直飛、或航空公司名稱對不上）
        # timeouts：等不到回應的次數（含在 errors 裡）；ok_over_60s：成功、但花了超過 60 秒
        # （舊的等待上限可能會丟掉的那種）；slowest_s：拿到頁面的請求裡最慢的一次。
        # 逾時、慢的成功與總耗時也逐日累計在 prices.db 的 api_usage（timeouts、slow_ok、seconds）
        self.stats = {"allowance": self.left, "requests": 0, "ok": 0, "recorded": 0,
                      "no_direct": 0, "alerts_checked": 0, "swept": 0, "errors": 0,
                      "timeouts": 0, "ok_over_60s": 0, "slowest_s": 0}
        self.spent_s = 0.0        # 這輪 check() 花掉的秒數（含失敗、解析與寫入），對照 RUN_BUDGET_S

    def _can_send(self) -> bool:
        """還有額度，而且這輪花在請求上的時間還沒到 RUN_BUDGET_S。"""
        return self.left > 0 and self.spent_s < RUN_BUDGET_S

    def check(self, origin: str, dest: str, depart: str, ret: str,
              want_carriers: str | None = None) -> Offer | None:
        """查一次、寫入觀測、記帳；回傳跟 want_carriers 同航空公司的那筆（沒有就 None）。

        任何失敗都只記 log：Bright Data 是加分項，壞了抓價與警報照常。
        """
        if not self._can_send():
            return None
        self.left -= 1
        self.stats["requests"] += 1
        ok = slow = timed_out = False
        # 同一個時間戳寫進 api_checks 與 observations：落空判定（api_misses_by_date）靠
        # 「observed_at ≥ checked_at 的同行程觀測」認出這一趟有結果
        stamp = self.now.isoformat(timespec="seconds")
        started = self.clock()
        try:
            page = self.fetch(origin, dest, depart, ret)
            took = self.clock() - started
            self.stats["slowest_s"] = max(self.stats["slowest_s"], round(took))
            itineraries = parse(page)
            # 拿到頁面就記「查過」：就算沒有可寫入的直飛價，24 小時內巡檢也不再查這趟
            self.store.record_api_check(PROVIDER, origin, dest, depart, ret, stamp,
                                        parsed=bool(itineraries),
                                        want=carrier_signature(want_carriers))
            if not itineraries:
                # 頁面拿到了卻一筆都解析不到：多半是 Google 改版，要看得見
                raise BrightDataError("page fetched but no itineraries parsed")
            ok = True
            slow = took > OLD_READ_TIMEOUT_S
            self.stats["ok_over_60s"] += slow
            offers, matched = select(origin, dest, depart, ret, itineraries, want_carriers)
            if not offers:
                _log_no_direct(origin, dest, depart, ret, itineraries)
            for o in offers:
                self.store.record(o, observed_at=stamp)
            self.stats["recorded"] += len(offers)
            self.stats["no_direct"] += not offers
            return matched
        except BrightDataError as exc:
            timed_out = isinstance(exc, BrightDataTimeout)
            self.stats["errors"] += 1
            self.stats["timeouts"] += timed_out
            log.warning("Bright Data %s→%s %s/%s: %s", origin, dest, depart, ret, exc)
            return None
        except Exception:
            # 預期外的錯（解析程式的 bug 之類）也只記下來：這一則警報照原狀態發
            self.stats["errors"] += 1
            log.exception("Bright Data %s→%s %s/%s 意外失敗", origin, dest, depart, ret)
            return None
        finally:
            elapsed = self.clock() - started
            self.spent_s += elapsed
            self.stats["ok"] += ok
            self.store.record_api_usage(PROVIDER, self.now.date().isoformat(), ok=ok,
                                        timeout=timed_out, slow_ok=slow, seconds=elapsed)

    def _ago(self, **delta) -> str:
        """now 往前推 delta（timedelta 的參數）的時間字串，給 storage 比較用。"""
        return (self.now - timedelta(**delta)).isoformat(timespec="seconds")

    def _since(self) -> str:
        """「24 小時內查過」的起點。"""
        return self._ago(hours=RECHECK_HOURS)

    def checked_recently(self, origin: str, dest: str, depart: str, ret: str,
                         carriers: str | None = None) -> bool:
        """24 小時內 Bright Data **替這一組航空公司**拿過這趟的頁面（不論有沒有寫入直飛價）。

        頁面只留下全場最低與指定航空的直飛價（select），所以替別家航空查過的不算：
        那次沒留下這家的價格，不代表這家沒有直飛。carriers 判不出簽章時退回「任何查詢都算」。
        """
        want = carrier_signature(carriers)
        if want is None:
            return self.store.api_checked_since(PROVIDER, origin, dest, depart, ret, self._since())
        return self.store.api_checked_for(PROVIDER, origin, dest, depart, ret, want, self._since())

    def recent_match(self, origin: str, dest: str, depart: str, ret: str,
                     carriers: str | None) -> Offer | None:
        """24 小時內 Bright Data 查到、跟 carriers 同一組航空公司的最新直飛價（沒有就 None）。"""
        want = carrier_signature(carriers)
        if want is None:
            return None
        for row in self.store.google_rows_since(origin, dest, depart, ret, self._since(),
                                                provider=PROVIDER):
            if carrier_signature(row["carriers"]) == want:
                return Offer(origin=origin, destination=dest, depart_date=depart,
                             return_date=ret, price=row["price"], currency=row["currency"],
                             carriers=row["carriers"], stops=row["stops"], duration="",
                             link=search_url(origin, dest, depart, ret),
                             fare_class=row["fare_class"], source="google", provider=PROVIDER)
        return None

    def verify_alert(self, origin: str, dest: str, depart: str, ret: str,
                     want_carriers: str | None) -> Offer | None:
        """警報發出前的驗證（ADR 0004 的第一優先）；跟 check 相同，只是另外計數
        （額度或時間用完、沒有真的送出請求時不算）。"""
        if not self._can_send():
            return None
        self.stats["alerts_checked"] += 1
        return self.check(origin, dest, depart, ret, want_carriers)

    def weekday_outcomes(self, origin: str, dest: str) -> WeekdayOutcomes:
        """冷門線各星期幾（0＝週一）的最近結果。

        看出發日的星期幾，以最近一次為準：30 天內有 Google 直飛實價（任何供應商）→ 優先；
        7 天內 Bright Data 查了卻沒寫入、而且之後沒再查到 → 先跳過。班表會換季（KHH-NGO
        夏季是週一、週六，2026-10 冬季班表改成週一、週三），所以只看最近的 Google 實價，
        不看幾個月前的快取。
        """
        hits = self.store.google_nonstop_by_date(origin, dest, self._ago(days=HIT_MEMORY_DAYS))
        misses = self.store.api_misses_by_date(PROVIDER, origin, dest,
                                               self._ago(days=MISS_MEMORY_DAYS))
        latest: dict[int, tuple[float, bool]] = {}      # 星期幾 → (最近時間, 有沒有直飛)
        for rows, hit in ((hits, True), (misses, False)):
            for dep, jd in rows:
                wd = datetime.fromisoformat(dep).weekday()
                if wd not in latest or jd > latest[wd][0]:
                    latest[wd] = (jd, hit)
        return WeekdayOutcomes(
            prefer=frozenset(wd for wd, (_, hit) in latest.items() if hit),
            avoid=frozenset(wd for wd, (_, hit) in latest.items() if not hit))

    def sweep(self, candidates: list[Offer], routes: list[tuple[str, str]]) -> None:
        """把剩下的額度花在「各航線最便宜、24 小時內沒查過」的行程上。

        「查過」＝任何供應商寫過 Google 實價，或 Bright Data 拿過這趟的頁面（沒有直飛價時
        不會留下觀測，見 api_checks）。航線輪流取（每條先拿最便宜的），起點每小時換一條：
        額度比航線數少時（11 月每輪約 7 次、8 條線），排在後面的航線才不會整個月輪不到。
        快取日期不到 ROUTE_MIN_TRIPS 趟的航線（高雄的冷門線）用固定日期補滿，排在快取
        日期之後，讓它每天也有一批 Google 實價。
        """
        if not self._can_send():
            return
        since = self._since()
        self.store.prune_api_checks(PROVIDER, self._ago(days=MISS_MEMORY_DAYS))
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
        for (o, d), q in queues.items():
            need = ROUTE_MIN_TRIPS - len(q)
            if need > 0:
                known = self.weekday_outcomes(o, d)
                have = {(dep, ret) for dep, ret, _ in q}
                q.extend((dep, ret, None) for dep, ret in
                         anchor_trips(self.now, need, prefer=known.prefer, avoid=known.avoid)
                         if (dep, ret) not in have)
        order = list(queues)
        if order:
            k = int(self.now.timestamp() // 3600) % len(order)
            order = order[k:] + order[:k]

        def fresh(o, d, dep, ret):
            return (self.store.has_google_since(o, d, dep, ret, since)
                    or self.store.api_checked_since(PROVIDER, o, d, dep, ret, since))

        deadline = self.clock() + SWEEP_DEADLINE_S
        while self._can_send() and self.clock() < deadline and any(queues.values()):
            for o, d in order:
                q = queues[(o, d)]
                while q:
                    dep, ret, carriers = q.pop(0)
                    if not fresh(o, d, dep, ret):
                        break
                else:
                    continue
                self.check(o, d, dep, ret, carriers)
                self.stats["swept"] += 1
                if not self._can_send() or self.clock() >= deadline:
                    break


def anchor_trips(now: datetime, count: int = 8, *, prefer: frozenset = frozenset(),
                 avoid: frozenset = frozenset()) -> list[tuple[str, str]]:
    """快取沒資料的航線要查哪些日子：2 週後起、最遠約 11 週的出發日，住 STAY_NIGHTS 晚。

    冷門線不是天天飛（KHH-NGO 冬季只有週一、週三有直飛），沒飛的那天查了也是白查
    （2026-10-31～11-01：KHH-NGO 查 11 次只有 1 次有直飛）。所以：
    - 還沒試過的星期幾（不在 prefer、也不在 avoid）先各試最早的一天；
    - 再從有直飛的星期幾（prefer）的所有日子裡，在整段期間平均挑、補滿 count 個
      （頭尾都含，旺季也查得到；起點每天輪替，挑到的日子每天不同）；
    - 最近落空的星期幾（avoid）不查，等 MISS_MEMORY_DAYS 天後紀錄過期再試。
    兩者都沒有資料時，每 ANCHOR_STEP_DAYS 天一個（不是 7 的倍數，星期幾各不相同）。
    起點隨日期輪替，每天查到的日子不同。
    """
    start = now.date() + timedelta(days=14 + now.timetuple().tm_yday % 7)
    if not prefer and not avoid:
        days = [start + timedelta(days=ANCHOR_STEP_DAYS * k) for k in range(count)]
    else:
        pool = [start + timedelta(days=i) for i in range(ANCHOR_STEP_DAYS * count)]
        days, tried = [], set()
        for day in pool:
            wd = day.weekday()
            if wd not in prefer and wd not in avoid and wd not in tried:
                tried.add(wd)
                days.append(day)
        good = [day for day in pool if day.weekday() in prefer]
        days += _spread(good, max(0, count - len(days)))
    return [(d.isoformat(), (d + timedelta(days=STAY_NIGHTS)).isoformat()) for d in days]


def _spread(days: list, n: int) -> list:
    """從 days 裡平均挑 n 個（頭尾都含；只挑 1 個時取中間）。

    用小數間隔取位置：days 是「週一、週三、週一、週三…」交錯時，固定每隔一個取會
    全落在同一個星期幾；小數間隔取到的位置奇偶交錯，兩個星期幾都會有。
    """
    if n <= 0:
        return []
    if n >= len(days):
        return list(days)
    if n == 1:
        return [days[len(days) // 2]]
    return [days[round(k * (len(days) - 1) / (n - 1))] for k in range(n)]


def _log_no_direct(origin: str, dest: str, depart: str, ret: str,
                   itineraries: list[Itinerary]) -> None:
    """頁面正常卻沒有可寫入的直飛價：記下是那天沒有直飛，還是航空公司名稱對不上。"""
    nonstop = [it for it in itineraries if it.nonstop]
    unknown = sorted({a for it in nonstop for a in it.airlines if airline_code(a) is None})
    log.info("Bright Data %s→%s %s/%s 沒有可寫入的直飛價：%d 個航班、直飛 %d 個%s",
             origin, dest, depart, ret, len(itineraries), len(nonstop),
             f"，對照表沒有的航空公司：{', '.join(unknown)}" if unknown else "")
