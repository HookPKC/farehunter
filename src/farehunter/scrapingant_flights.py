"""ScrapingAnt 補冷門線的 Google 實價：每月重置的免費額度，接替一次性的 ChocoData（ADR 0005）。

**為什麼有這支**：ChocoData 的免費點數是一次性的，約 2026-12 中用完；冷門線的 Google 實價會掉回
只剩 Bright Data。ScrapingAnt 免費方案每月 10,000 點、**每月重置**（週期從註冊日起算）。

**做什麼**：通用擷取 `/v2/general`（`browser=true`、機房 proxy、出口固定 PROXY_COUNTRY）抓 Bright Data 同一個 Google Flights
搜尋頁，2026-11-22 實測 3/3 跟同一小時的 Bright Data／ChocoData 完全一致。網址、解析、要寫哪幾筆
都沿用 Bright Data 的，巡檢流程沿用 chocodata_flights.ColdSweeper（同樣只查冷門線、24 小時內誰查過
就不查、虎航與 Picker 線索優先），只把 provider 換成 'scrapingant'——source 仍是 'google'，價格語意
完全相同。

**額度**：每頁 10 點（實測）。每輪先查 `/v2/usage` 的剩餘點數（不扣點），留 RESERVE_CREDITS 緩衝後，
把剩下的頁數平均分到重置日前：今天最多「(剩餘頁數＋今天已用) ÷ 含今天的剩餘天數」，每輪最多
PER_RUN_MAX（每頁 6–20 秒，免費方案同時只能 1 個請求）。查不到剩餘點數時退回每天 FALLBACK_DAILY_MAX、
近 30 天 WINDOW_MAX。帳本記在 api_usage（provider='scrapingant'）。

金鑰只放 header `x-api-key`，不在網址；錯誤訊息一律遮掉金鑰，連線錯誤只留例外類型。沒有
SCRAPINGANT_API_KEY 時整支不做任何事。任何失敗都只記 log，不影響抓價、警報、看板。
"""
from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timedelta, timezone

import requests

from . import chocodata_flights as cd
from .brightdata_flights import search_url

log = logging.getLogger(__name__)

API_URL = "https://api.scrapingant.com/v2/general"
USAGE_URL = "https://api.scrapingant.com/v2/usage"
PROVIDER = cd.SCRAPINGANT
CREDITS_PER_PAGE = 10        # browser=true＋機房 proxy（2026-11-22 實測 10,000 → 9,970／3 頁）
RESERVE_CREDITS = 500        # 每月 10,000 點留 5% 給手動探測與誤差
PER_RUN_MAX = 2
FALLBACK_DAILY_MAX = 30      # 查不到剩餘點數時
WINDOW_DAYS = 30
WINDOW_MAX = 950
#: 固定日期錯開：Bright Data 0、ChocoData 4（間隔 8 天的一半），這裡取 2，三家的日子交錯
ANCHOR_OFFSET_DAYS = 2
#: 機房 proxy 的出口國家。2026-11-23 起固定：預設隨機出口時約 6 成頁面解析不到，log 看到其中一種是
#: 標題「Before you continue」的 Google 同意頁（歐洲 IP 才會跳）。固定美國出口避開同意牆；幣別與語言
#: 由網址的 curr=TWD、hl=en、gl=tw 決定，跟出口國家無關。參數名 proxy_country（官方 proxy 設定文件）。
PROXY_COUNTRY = "US"
CONNECT_TIMEOUT_S = 10
READ_TIMEOUT_S = 90


class ScrapingAntError(cd.ChocoDataError):
    pass


class ScrapingAntExhausted(ScrapingAntError, cd.ChocoDataExhausted):
    """點數用完：本輪不再送。"""


def configured() -> bool:
    return bool(os.environ.get("SCRAPINGANT_API_KEY", "").strip())


def _key() -> str:
    key = os.environ.get("SCRAPINGANT_API_KEY", "").strip()
    if not key:
        raise ScrapingAntError("missing SCRAPINGANT_API_KEY")
    return key


def fetch(origin: str, dest: str, depart: str, ret: str,
          session: requests.Session | None = None) -> str:
    """回傳 Google Flights 搜尋頁的 HTML。"""
    key = _key()
    try:
        r = (session or requests).get(
            API_URL, params={"url": search_url(origin, dest, depart, ret), "browser": "true",
                    "proxy_country": PROXY_COUNTRY},
            headers={"x-api-key": key}, timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S))
    except requests.RequestException as exc:
        raise ScrapingAntError(f"transport: {type(exc).__name__}") from None
    if r.status_code != 200:
        text = cd._redact(f"HTTP {r.status_code}: {r.text[:300]}", key)[:200]
        low = text.lower()
        if r.status_code == 402 or any(w in low for w in ("credit", "quota", "exceed", "limit reached")):
            raise ScrapingAntExhausted(text)
        raise ScrapingAntError(text)
    if not r.text:
        raise ScrapingAntError("HTTP 200 but empty body")
    return r.text


def remaining(session=None) -> tuple[int, datetime] | None:
    """(剩餘點數, 這期結束時間 UTC)；查不到回 None。查詢不扣點。"""
    try:
        key = _key()
        r = (session or requests).get(USAGE_URL, headers={"x-api-key": key},
                                      timeout=(CONNECT_TIMEOUT_S, 30))
        if r.status_code != 200:
            log.warning("ScrapingAnt usage：HTTP %s", r.status_code)
            return None
        d = r.json()
        end = datetime.fromisoformat(str(d["end_date"]))
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return int(d["remained_credits"]), end
    except (requests.RequestException, ValueError, KeyError, TypeError, ScrapingAntError) as exc:
        log.warning("ScrapingAnt usage 讀不到：%s", type(exc).__name__)
        return None


def allowance(store, now: datetime, usage=None) -> int:
    """這一輪可以送幾頁。usage＝remaining() 的結果（None 表示查不到，退回固定上限）。"""
    today_used = store.api_usage_since(PROVIDER, now.date().isoformat())
    if usage is None:
        window = store.api_usage_since(
            PROVIDER, (now.date() - timedelta(days=WINDOW_DAYS - 1)).isoformat())
        return max(0, min(PER_RUN_MAX, FALLBACK_DAILY_MAX - today_used, WINDOW_MAX - window))
    credits, end = usage
    pages_left = max(0, (credits - RESERVE_CREDITS) // CREDITS_PER_PAGE)
    days_left = max(1, math.ceil((end - now).total_seconds() / 86400))
    daily = (pages_left + today_used) // days_left       # 今天開始時的剩餘頁數平均分
    return max(0, min(PER_RUN_MAX, daily - today_used, pages_left))


class Sweeper(cd.ColdSweeper):
    """跟 ChocoData 同一套冷門線巡檢；provider、抓取、額度、固定日期的錯開不同。"""
    provider = PROVIDER
    label = "ScrapingAnt"
    anchor_offset_days = ANCHOR_OFFSET_DAYS
    usage_fetcher = staticmethod(remaining)

    def _allowance(self) -> int:
        return allowance(self.store, self.now, self.usage_fetcher())

    def _default_fetch(self):
        return fetch
