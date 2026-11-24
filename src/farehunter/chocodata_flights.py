"""ChocoData 補冷門線的 Google 實價：免費約 1,000 頁，點數不足時自動停、每天試一次（ADR 0005）。

**為什麼有這支**：Bright Data（ADR 0004）每天約 160 頁，但多半落在熱門線；2026-11-06～11-13
的 Google 實價 KHH-NGO 只有 36 筆、KHH-CTS 38、KHH-FUK 42，KHH-NRT 卻有 522。NGO／CTS 的
門檻要重新校準、傳統航空門檻也要資料，冷門線需要更多 Google 實價。

**做什麼**：ChocoData 沒有專用的機票端點，但通用擷取 `universal.get`（給網址、回渲染後的
HTML）抓 Bright Data 同一個 Google Flights 搜尋頁，2026-11-13 實測 4/4 跟 Bright Data 一致。
所以網址（`search_url`）、解析（`parse`）、要寫哪幾筆（`select`）都直接沿用 Bright Data 的，
只把 provider 換成 'chocodata'——source 仍是 'google'，價格語意完全相同。

**額度**：免費方案 5,000 credits，每頁扣 5（＝約 1,000 頁）。**是一次性還是每月重置不確定**：
後台方案卡寫「1,000 requests (one-time)」，外部比較表寫「每月 5,000 credits」，API 回應看不出來
（只回每頁扣幾點，沒有剩餘點數與重置日）。所以不寫死，讓 ChocoData 自己回答（2026-11-24）：
- 平常：每輪最多 PER_RUN_MAX、每天最多 DAILY_MAX、近 WINDOW_DAYS 天最多 WINDOW_MAX 頁
  （每月重置的話剛好用得完，一次性的話約 12 月中用完）。
- 回「點數不足」→ 本輪立刻停，記進 provider_exhaustion。之後每 PROBE_EVERY_HOURS 小時只試 1 頁：
  拿到頁面＝點數恢復（每月重置），清掉紀錄、回到平常配速；還是不足就繼續等。第一次不足後
  GIVE_UP_DAYS 天都沒恢復＝確定是一次性，不再試（要重新啟用就刪掉 provider_exhaustion 那一列）。
帳本記在 api_usage（provider='chocodata'）；看板用量面板顯示目前狀態。

**金鑰在網址查詢參數裡**（官方 SDK：`?api_key=`，header 認證回 401）。repo 公開、Actions
紀錄公開：錯誤訊息絕不含網址或連線錯誤原文，回應內容一律先遮掉金鑰。

沒有 CHOCODATA_API_KEY 時整支不做任何事。任何失敗都只記 log，不影響抓價、警報、看板。
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import replace
from datetime import datetime, timedelta
from urllib.parse import quote, quote_plus

import requests

from .brightdata_flights import (MIN_LEAD_DAYS, RECHECK_HOURS, HIT_MEMORY_DAYS,
                                 MISS_MEMORY_DAYS, anchor_trips, parse, search_url, select)
from .brightdata_flights import PROVIDER as BRIGHTDATA
from . import calendar_hints, tigerair_calendar

log = logging.getLogger(__name__)

API_URL = "https://api.chocodata.com/api/v1/universal/get"
PROVIDER = "chocodata"
#: 只給冷門線（Google 實價最少的四條）。熱門線 Bright Data 已經夠多。
COLD_ROUTES = (("KHH", "CTS"), ("KHH", "NGO"), ("KHH", "FUK"), ("KHH", "OKA"))
WINDOW_DAYS = 30
WINDOW_MAX = 950             # 約 1,000 頁留緩衝（每頁 5 credits）
DAILY_MAX = 30               # 一個月約 900–930 頁
PROBE_EVERY_HOURS = 24       # 點數不足之後多久試一次
GIVE_UP_DAYS = 40            # 第一次不足後這麼多天都沒恢復＝一次性額度，不再試（超過一個月的週期）
PER_RUN_MAX = 2              # 每頁 2–8 秒；一輪最多 2 頁，不拖長 monitor
TRIPS_PER_ROUTE = 12         # 每條線每輪的候選行程數（快取日期＋固定日期）
#: 固定日期跟 Bright Data 用同一套 anchor_trips；不錯開的話算出來的日子幾乎相同、都被「24 小時內
#: 查過」擋掉。往後錯開 4 天（間隔 8 天的一半），兩家的日子交錯，冷門線一天能看到更多不同日期。
ANCHOR_OFFSET_DAYS = 4
#: ScrapingAnt（scrapingant_flights）也用這個巡檢類別；寫在這裡避免互相 import
SCRAPINGANT = "scrapingant"
CONNECT_TIMEOUT_S = 10
READ_TIMEOUT_S = 90


class ChocoDataError(RuntimeError):
    pass


class ChocoDataExhausted(ChocoDataError):
    """點數不足：本輪不再送。"""


def configured() -> bool:
    return bool(os.environ.get("CHOCODATA_API_KEY", "").strip())


def _redact(text: str, key: str) -> str:
    for v in {key, quote(key), quote(key, safe=""), quote_plus(key)}:
        if v:
            text = text.replace(v, "***")
    return text


def fetch(origin: str, dest: str, depart: str, ret: str,
          session: requests.Session | None = None) -> str:
    """回傳 Google Flights 搜尋頁的 HTML。錯誤訊息不含網址（網址裡有金鑰）。"""
    key = os.environ.get("CHOCODATA_API_KEY", "").strip()
    if not key:
        raise ChocoDataError("missing CHOCODATA_API_KEY")
    s = session or requests
    try:
        r = s.get(API_URL, params={"api_key": key, "url": search_url(origin, dest, depart, ret)},
                  timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
                  headers={"Accept": "application/json"})
    except requests.RequestException as exc:
        # 例外原文會帶完整網址（含金鑰）：只留類型
        raise ChocoDataError(f"transport: {type(exc).__name__}") from None
    try:
        body = r.json()
    except ValueError:
        body = None
    if r.status_code != 200:
        err = body.get("error") if isinstance(body, dict) else None
        code = err if isinstance(err, str) else (err or {}).get("code", "") if isinstance(err, dict) else ""
        msg = body.get("message", "") if isinstance(body, dict) else ""
        text = _redact(f"HTTP {r.status_code} {code}: {msg}", key)[:200]
        low = f"{code} {msg}".lower()
        if r.status_code == 402 or any(w in low for w in ("credit", "insufficient", "quota", "balance")):
            raise ChocoDataExhausted(text)
        raise ChocoDataError(text)
    page = body.get("html") if isinstance(body, dict) else None
    if not isinstance(page, str) or not page:
        raise ChocoDataError("HTTP 200 but no html field")
    return page


#: 解析不到航班時，頁面裡有沒有這些字（判斷是 Google 同意頁、驗證頁還是改版）
_PAGE_MARKERS = (("consent", "consent"), ("before you continue", "同意頁"),
                 ("unusual traffic", "異常流量"), ("captcha", "captcha"), ("/sorry/", "sorry 頁"),
                 ("aria-label", "有 aria-label"))


def page_hint(page) -> str:
    """解析失敗的頁面摘要（大小、<title>、特徵字），給 log 判斷原因。頁面是 Google 的公開 HTML，
    不含金鑰；只取標題前 60 字。"""
    if not isinstance(page, str):
        return f"非文字（{type(page).__name__}）"
    low = page.lower()
    title = ""
    i = low.find("<title")
    if i >= 0:
        j, k = low.find(">", i), low.find("</title>", i)
        if 0 <= j < k:
            title = " ".join(page[j + 1:k].split())[:60]
    marks = [name for key, name in _PAGE_MARKERS if key in low]
    return f"{len(page):,} 字，標題「{title}」，特徵：{'、'.join(marks) or '無'}"


def _hours_since(now: datetime, stamp: str) -> float:
    return (now - datetime.fromisoformat(stamp)).total_seconds() / 3600


def allowance(store, now: datetime) -> int:
    """這一輪可以送幾頁。點數不足過：每 PROBE_EVERY_HOURS 小時試 1 頁，GIVE_UP_DAYS 天後不再試；
    否則每輪、每天、近 WINDOW_DAYS 天三個上限取最小。"""
    state = store.exhaustion(PROVIDER)
    if state:
        first, last = state
        if _hours_since(now, first) >= GIVE_UP_DAYS * 24:
            return 0
        return 1 if _hours_since(now, last) >= PROBE_EVERY_HOURS else 0
    today = store.api_usage_since(PROVIDER, now.date().isoformat())
    window = store.api_usage_since(
        PROVIDER, (now.date() - timedelta(days=WINDOW_DAYS - 1)).isoformat())
    return max(0, min(PER_RUN_MAX, DAILY_MAX - today, WINDOW_MAX - window))


class ColdSweeper:
    """一輪 monitor 的 ChocoData 用量：只查冷門線、24 小時內沒有 Google 實價的行程。

    ScrapingAnt（scrapingant_flights.Sweeper）沿用整套流程，只換 provider／抓取／額度／固定日期的錯開。
    """
    provider = PROVIDER
    label = "ChocoData"
    anchor_offset_days = ANCHOR_OFFSET_DAYS
    #: 24 小時內這幾家誰拿過這趟的頁面（即使沒有直飛價）就不再查
    checkers = (BRIGHTDATA, PROVIDER, SCRAPINGANT)

    def _allowance(self) -> int:
        return allowance(self.store, self.now)

    def _default_fetch(self):
        return fetch

    def __init__(self, store, now: datetime, *, left: int | None = None, fetcher=None,
                 clock=time.monotonic):
        self.store, self.now = store, now
        self.left = self._allowance() if left is None else left
        self.fetch = fetcher or self._default_fetch()
        self.clock = clock
        self.hint_caps: dict = {}     # 同 brightdata_flights.Verifier.hint_caps
        self.tiger_hints: dict = {}   # 同 brightdata_flights.Verifier.tiger_hints（這裡取兩倍）
        self.stats = {"allowance": self.left, "requests": 0, "ok": 0, "recorded": 0,
                      "no_direct": 0, "errors": 0, "exhausted": False, "hinted": 0,
                      "tiger_hinted": 0}

    def _credits_back(self) -> None:
        """拿到頁面＝有點數。之前記過點數不足的話，代表額度重置了：清掉紀錄、記一筆 log。"""
        state = self.store.exhaustion(self.provider)
        if state:
            self.store.clear_exhausted(self.provider)
            log.warning("%s 點數恢復了（%s 起點數不足）：看起來額度會重置，回到平常配速",
                        self.label, state[0])

    def _ago(self, **delta) -> str:
        return (self.now - timedelta(**delta)).isoformat(timespec="seconds")

    def _fresh(self, o: str, d: str, dep: str, ret: str, since: str) -> bool:
        """24 小時內已有 Google 實價，或 Bright Data／ChocoData／ScrapingAnt 拿過這趟的頁面。

        別家只算**解析得到航班**的頁面：2026-11-22 ScrapingAnt 31 頁有 14 頁解析不到（Google 給了別的
        版面），原本那幾趟會連帶讓 ChocoData 24 小時內不查。自己解析不到的照樣算查過（同一家重試多半
        還是一樣，留給別家）。但**兩家都解析不到**（而且沒有一家解析到）的行程 7 天內誰都不再查：
        2026-11-24 遠期日子（2027 年 9–10 月）三家輪流各查一次、全部解析不到，一天半浪費約 16 頁。"""
        return (self.store.has_google_since(o, d, dep, ret, since)
                or any(self.store.api_checked_since(p, o, d, dep, ret, since,
                                                    parsed_only=p != self.provider)
                       for p in self.checkers)
                or self.store.dead_trip(o, d, dep, ret, self._ago(days=MISS_MEMORY_DAYS)))

    def _weekdays(self, o: str, d: str) -> tuple[frozenset, frozenset]:
        """(有直飛的星期幾, 最近落空的星期幾)，以最近一次為準；落空看兩家供應商。"""
        hits = self.store.google_nonstop_by_date(o, d, self._ago(days=HIT_MEMORY_DAYS))
        since = self._ago(days=MISS_MEMORY_DAYS)
        misses = [m for p in self.checkers for m in self.store.api_misses_by_date(p, o, d, since)]
        latest: dict[int, tuple[float, bool]] = {}
        for rows, hit in ((hits, True), (misses, False)):
            for dep, jd in rows:
                wd = datetime.fromisoformat(dep).weekday()
                if wd not in latest or jd > latest[wd][0]:
                    latest[wd] = (jd, hit)
        return (frozenset(wd for wd, (_, h) in latest.items() if h),
                frozenset(wd for wd, (_, h) in latest.items() if not h))

    def queues(self, candidates) -> dict[tuple[str, str], list[tuple[str, str, str | None]]]:
        """各冷門線的候選：先放本輪快取裡最便宜的直飛來回，再用固定日期補到 TRIPS_PER_ROUTE。"""
        earliest = (self.now.date() + timedelta(days=MIN_LEAD_DAYS)).isoformat()
        out: dict[tuple[str, str], list[tuple[str, str, str | None]]] = {r: [] for r in COLD_ROUTES}
        seen = set()
        for c in sorted(candidates, key=lambda o: o.price):
            r = (c.origin, c.destination)
            key = (*r, c.depart_date, c.return_date)
            if (r not in out or not c.return_date or c.stops != 0
                    or c.depart_date < earliest or key in seen):
                continue
            seen.add(key)
            out[r].append((c.depart_date, c.return_date, c.carriers))
        for (o, d), q in out.items():
            prefer, avoid = self._weekdays(o, d)
            if (o, d) in calendar_hints.ROUTES:
                # Picker 線索排最前面（見 calendar_hints）。取得比 Bright Data 多：前幾名它查過後，
                # 這裡會因為「24 小時內查過」跳過，往下查其餘的。
                have = {(dep, ret) for dep, ret, _ in q}
                hinted = [(dep, ret, None) for dep, ret in calendar_hints.pick(
                    self.store, self.now, o, d, avoid=avoid, cap=self.hint_caps.get((o, d)),
                    limit=2 * calendar_hints.PER_ROUTE) if (dep, ret) not in have]
                q[:0] = hinted
                self.stats["hinted"] += len(hinted)
            # 虎航官網日曆線索排最最前面（見 tigerair_calendar；取兩倍，理由同 Picker）
            self.stats["tiger_hinted"] += tigerair_calendar.put_first(
                q, self.tiger_hints.get((o, d), ())[:2 * tigerair_calendar.PER_ROUTE])
            need = TRIPS_PER_ROUTE - len(q)
            if need > 0:
                have = {(dep, ret) for dep, ret, _ in q}
                q.extend((dep, ret, None) for dep, ret in
                         anchor_trips(self.now + timedelta(days=self.anchor_offset_days), need,
                                      prefer=prefer, avoid=avoid)
                         if (dep, ret) not in have)
        return out

    def sweep(self, candidates) -> None:
        if self.left <= 0:
            return
        self.store.prune_api_checks(self.provider, self._ago(days=MISS_MEMORY_DAYS))
        since = self._ago(hours=RECHECK_HOURS)
        queues = self.queues(candidates)
        order = list(queues)
        k = int(self.now.timestamp() // 3600) % len(order)
        order = order[k:] + order[:k]          # 起點每小時換一條，四條線輪流
        while self.left > 0 and not self.stats["exhausted"] and any(queues.values()):
            for o, d in order:
                q = queues[(o, d)]
                while q:
                    dep, ret, carriers = q.pop(0)
                    if not self._fresh(o, d, dep, ret, since):
                        break
                else:
                    continue
                self.check(o, d, dep, ret, carriers)
                if self.left <= 0 or self.stats["exhausted"]:
                    break

    def check(self, o: str, d: str, dep: str, ret: str, carriers: str | None = None) -> None:
        self.left -= 1
        self.stats["requests"] += 1
        stamp = self.now.isoformat(timespec="seconds")
        ok = False
        started = self.clock()
        try:
            page = self.fetch(o, d, dep, ret)
            self._credits_back()
            itineraries = parse(page)
            self.store.record_api_check(self.provider, o, d, dep, ret, stamp,
                                        parsed=bool(itineraries))
            if not itineraries:
                raise ChocoDataError(f"page fetched but no itineraries parsed（{page_hint(page)}）")
            ok = True
            offers, _ = select(o, d, dep, ret, itineraries, carriers)
            for off in offers:
                self.store.record(replace(off, provider=self.provider), observed_at=stamp)
            self.stats["recorded"] += len(offers)
            self.stats["no_direct"] += not offers
        except ChocoDataExhausted as exc:
            self.stats["errors"] += 1
            self.stats["exhausted"] = True
            self.store.record_exhausted(self.provider, stamp)
            # ChocoData 之後每天試 1 頁（allowance）；ScrapingAnt 照 /v2/usage 的剩餘點數配速
            then = (f"之後每 {PROBE_EVERY_HOURS} 小時試 1 頁，看會不會重置"
                    if self.provider == PROVIDER else "等額度重置")
            log.warning("%s 點數不足，本輪停止（%s）：%s", self.label, then, exc)
        except ChocoDataError as exc:
            self.stats["errors"] += 1
            log.warning("%s %s→%s %s/%s: %s", self.label, o, d, dep, ret, exc)
        except Exception as exc:
            self.stats["errors"] += 1
            # 不用 log.exception：traceback 裡的區域變數不會印，但保守起見只記類型
            log.warning("%s %s→%s %s/%s 意外失敗：%s", self.label, o, d, dep, ret, type(exc).__name__)
        finally:
            self.stats["ok"] += ok
            self.store.record_api_usage(self.provider, self.now.date().isoformat(), ok=ok,
                                        seconds=self.clock() - started)
