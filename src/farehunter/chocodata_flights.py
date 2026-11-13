"""ChocoData 補冷門線的 Google 實價：一次性的 1,000 次免費額度，用完自動停（ADR 0005）。

**為什麼有這支**：Bright Data（ADR 0004）每天約 160 頁，但多半落在熱門線；2026-11-06～11-13
的 Google 實價 KHH-NGO 只有 36 筆、KHH-CTS 38、KHH-FUK 42，KHH-NRT 卻有 522。NGO／CTS 的
門檻要重新校準、傳統航空門檻也要資料，冷門線需要更多 Google 實價。

**做什麼**：ChocoData 沒有專用的機票端點，但通用擷取 `universal.get`（給網址、回渲染後的
HTML）抓 Bright Data 同一個 Google Flights 搜尋頁，2026-11-13 實測 4/4 跟 Bright Data 一致。
所以網址（`search_url`）、解析（`parse`）、要寫哪幾筆（`select`）都直接沿用 Bright Data 的，
只把 provider 換成 'chocodata'——source 仍是 'google'，價格語意完全相同。

**額度**：免費方案是**註冊時一次性** 5,000 credits，每頁扣 5（＝約 1,000 頁），不是每月
重置（後台方案卡寫「1,000 requests (one-time)」）。累計用到 TOTAL_MAX 就停；每天最多
DAILY_MAX、每輪最多 PER_RUN_MAX，約一個月用完。帳本記在 api_usage（provider='chocodata'）。
ChocoData 回「點數不足」時本輪立刻停。萬一點數其實每月重置，提高 TOTAL_MAX 就能接著用。

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

log = logging.getLogger(__name__)

API_URL = "https://api.chocodata.com/api/v1/universal/get"
PROVIDER = "chocodata"
#: 只給冷門線（Google 實價最少的四條）。熱門線 Bright Data 已經夠多。
COLD_ROUTES = (("KHH", "CTS"), ("KHH", "NGO"), ("KHH", "FUK"), ("KHH", "OKA"))
TOTAL_MAX = 950              # 一次性 1,000 頁（實測已花 4 頁）留緩衝；每頁 5 credits
DAILY_MAX = 30               # 約 32 天用完
PER_RUN_MAX = 2              # 每頁 2–8 秒；一輪最多 2 頁，不拖長 monitor
TRIPS_PER_ROUTE = 12         # 每條線每輪的候選行程數（快取日期＋固定日期）
#: 固定日期跟 Bright Data 用同一套 anchor_trips；不錯開的話算出來的日子幾乎相同、都被「24 小時內
#: 查過」擋掉。往後錯開 4 天（間隔 8 天的一半），兩家的日子交錯，冷門線一天能看到更多不同日期。
ANCHOR_OFFSET_DAYS = 4
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


def allowance(store, now: datetime) -> int:
    """這一輪可以送幾頁：每輪、每天、累計三個上限取最小。"""
    total = store.api_usage_since(PROVIDER, "2000-01-01")
    today = store.api_usage_since(PROVIDER, now.date().isoformat())
    return max(0, min(PER_RUN_MAX, DAILY_MAX - today, TOTAL_MAX - total))


class ColdSweeper:
    """一輪 monitor 的 ChocoData 用量：只查冷門線、24 小時內沒有 Google 實價的行程。"""

    def __init__(self, store, now: datetime, *, left: int | None = None, fetcher=None,
                 clock=time.monotonic):
        self.store, self.now = store, now
        self.left = allowance(store, now) if left is None else left
        self.fetch = fetcher or fetch
        self.clock = clock
        self.stats = {"allowance": self.left, "requests": 0, "ok": 0, "recorded": 0,
                      "no_direct": 0, "errors": 0, "exhausted": False}

    def _ago(self, **delta) -> str:
        return (self.now - timedelta(**delta)).isoformat(timespec="seconds")

    def _fresh(self, o: str, d: str, dep: str, ret: str, since: str) -> bool:
        """24 小時內已有 Google 實價，或 Bright Data／ChocoData 拿過這趟的頁面。"""
        return (self.store.has_google_since(o, d, dep, ret, since)
                or self.store.api_checked_since(BRIGHTDATA, o, d, dep, ret, since)
                or self.store.api_checked_since(PROVIDER, o, d, dep, ret, since))

    def _weekdays(self, o: str, d: str) -> tuple[frozenset, frozenset]:
        """(有直飛的星期幾, 最近落空的星期幾)，以最近一次為準；落空看兩家供應商。"""
        hits = self.store.google_nonstop_by_date(o, d, self._ago(days=HIT_MEMORY_DAYS))
        since = self._ago(days=MISS_MEMORY_DAYS)
        misses = (self.store.api_misses_by_date(BRIGHTDATA, o, d, since)
                  + self.store.api_misses_by_date(PROVIDER, o, d, since))
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
            need = TRIPS_PER_ROUTE - len(q)
            if need > 0:
                prefer, avoid = self._weekdays(o, d)
                have = {(dep, ret) for dep, ret, _ in q}
                q.extend((dep, ret, None) for dep, ret in
                         anchor_trips(self.now + timedelta(days=ANCHOR_OFFSET_DAYS), need,
                                      prefer=prefer, avoid=avoid)
                         if (dep, ret) not in have)
        return out

    def sweep(self, candidates) -> None:
        if self.left <= 0:
            return
        self.store.prune_api_checks(PROVIDER, self._ago(days=MISS_MEMORY_DAYS))
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
            itineraries = parse(page)
            self.store.record_api_check(PROVIDER, o, d, dep, ret, stamp,
                                        parsed=bool(itineraries))
            if not itineraries:
                raise ChocoDataError("page fetched but no itineraries parsed")
            ok = True
            offers, _ = select(o, d, dep, ret, itineraries, carriers)
            for off in offers:
                self.store.record(replace(off, provider=PROVIDER), observed_at=stamp)
            self.stats["recorded"] += len(offers)
            self.stats["no_direct"] += not offers
        except ChocoDataExhausted as exc:
            self.stats["errors"] += 1
            self.stats["exhausted"] = True
            log.warning("ChocoData 點數不足，本輪停止：%s", exc)
        except ChocoDataError as exc:
            self.stats["errors"] += 1
            log.warning("ChocoData %s→%s %s/%s: %s", o, d, dep, ret, exc)
        except Exception as exc:
            self.stats["errors"] += 1
            # 不用 log.exception：traceback 裡的區域變數不會印，但保守起見只記類型
            log.warning("ChocoData %s→%s %s/%s 意外失敗：%s", o, d, dep, ret, type(exc).__name__)
        finally:
            self.stats["ok"] += ok
            self.store.record_api_usage(PROVIDER, self.now.date().isoformat(), ok=ok,
                                        seconds=self.clock() - started)
