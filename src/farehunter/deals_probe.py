"""一次性探測：SerpAPI 的 google_flights_deals 引擎。

Usage: python -m farehunter.deals_probe [TPE] [NRT]

**為什麼要探測而不是直接寫解析器**

`google_flights_deals` 據 SerpAPI 文件支援彈性日期區間
（`outbound_date=2026-11-01,2026-11-30`），一次查詢回傳整個區間裡最便宜的
組合。那正是已移除的 SearchApi 日曆所提供的能力——實測每次搜尋換到 4.9 筆
資料，而目前的單日 google_flights 查詢只有 1.9 筆。

但有三件事是查文件查不到、只能實際打一次才知道的：
  1. Free Plan 到底能不能用這個引擎
  2. 回應的實際結構（欄位名、巢狀層次、有沒有航空公司與回程日）
  3. **一次查詢扣幾次額度**——Scrape.do 就是一次扣 10 credits，
     不能假設「一次查詢 = 一次額度」

第 3 點特別重要，因為整個提案的價值就建立在「效率比單日查詢高」上。如果
一次 deals 扣 5 次額度，那它其實比現在還糟，而寫完解析器才發現就太遲了。

**這支程式恰好打一次 deals 查詢，沒有迴圈、沒有重試。** 前後各打一次
account.json（免費、不計額度）來量測真實扣款。

探測完、決定要不要接之後，這個檔案可以刪掉——它的產出是一份判斷依據，
不是長期執行的功能。
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, timedelta

import requests

from .quota import fetch_quota
from .serpapi_flights import BASE_URL

log = logging.getLogger(__name__)

ENGINE = "google_flights_deals"


def _summarise(node, depth: int = 0, max_depth: int = 3):
    """把回應壓成「結構長什麼樣」，不是整包倒出來。

    目的是看欄位名與巢狀層次以便寫解析器；整包 JSON 可能有幾百 KB，
    倒進 Actions log 只會淹沒重點。
    """
    pad = "  " * depth
    if isinstance(node, dict):
        if depth >= max_depth:
            return f"{pad}{{{', '.join(sorted(node)[:12])}}}"
        out = []
        for k in sorted(node):
            v = node[k]
            if isinstance(v, (dict, list)):
                out.append(f"{pad}{k}:")
                out.append(_summarise(v, depth + 1, max_depth))
            else:
                out.append(f"{pad}{k}: {str(v)[:80]}")
        return "\n".join(out)
    if isinstance(node, list):
        if not node:
            return f"{pad}[] (空)"
        return (f"{pad}[{len(node)} 筆] 第一筆:\n"
                + _summarise(node[0], depth + 1, max_depth))
    return f"{pad}{str(node)[:80]}"


def probe(origin: str = "TPE", destination: str = "NRT",
          *, days_ahead: int = 60, window_days: int = 29,
          api_key: str | None = None,
          session: requests.Session | None = None,
          today: date | None = None) -> dict:
    """恰好一次 deals 查詢，前後量測額度。回傳探測結果 dict。"""
    key = api_key or os.environ.get("SERPAPI_KEY", "")
    if not key:
        raise RuntimeError("Missing SERPAPI_KEY environment variable.")
    today = today or date.today()
    start = today + timedelta(days=days_ahead)
    end = start + timedelta(days=window_days)
    s = session or requests

    result: dict = {"engine": ENGINE, "origin": origin,
                    "destination": destination,
                    "outbound_window": f"{start}..{end}"}

    before = None
    try:
        before = fetch_quota(api_key=key, session=s)
        result["quota_before"] = before.total_searches_left
    except Exception as exc:                       # noqa: BLE001
        log.warning("查不到探測前額度（不影響探測）: %s", exc)

    params = {
        "engine": ENGINE,
        "departure_id": origin,
        "arrival_id": destination,
        # 彈性日期區間——這正是要驗證的功能
        "outbound_date": f"{start.isoformat()},{end.isoformat()}",
        "travel_duration": 1,          # 1 = 一週行程
        "currency": "TWD",
        "hl": "zh-TW",
        "api_key": key,
    }
    resp = s.get(BASE_URL, params=params, timeout=90)
    result["http_status"] = resp.status_code
    if resp.status_code != 200:
        result["error"] = resp.text[:500]
        log.error("deals 查詢失敗 HTTP %s: %s", resp.status_code, resp.text[:300])
    else:
        payload = resp.json()
        if payload.get("error"):
            result["error"] = str(payload["error"])[:500]
            log.error("deals 回傳 error: %s", payload["error"])
        else:
            result["top_level_keys"] = sorted(payload.keys())
            result["structure"] = _summarise(payload)
            # 找出「像是一批航班/交易」的陣列，用來估算每次查詢的資訊量
            arrays = {k: len(v) for k, v in payload.items()
                      if isinstance(v, list) and v}
            result["arrays"] = arrays
            result["rows"] = max(arrays.values()) if arrays else 0
            # 第一次探測發現：查 TPE→NRT 回來的第一筆是 TPE→ISG，而
            # search_parameters 只回聲 departure_id——這個引擎似乎忽略
            # arrival_id，是「從這裡出發哪裡便宜」而不是「這條航線便宜嗎」。
            # 命中率決定它對本專案（8 條固定航線）到底有沒有用，所以要量。
            deals = payload.get("deals") or []
            if deals:
                dests: dict[str, int] = {}
                for it in deals:
                    if isinstance(it, dict):
                        code = str(it.get("arrival_airport_code") or "?")
                        dests[code] = dests.get(code, 0) + 1
                result["destinations"] = dict(sorted(dests.items(),
                                                     key=lambda kv: -kv[1]))
                result["asked_for"] = destination
                result["asked_for_hits"] = dests.get(destination, 0)
                result["deal_sample"] = [
                    {k: it.get(k) for k in
                     ("arrival_airport_code", "outbound_date", "return_date",
                      "price", "airline_code", "stops", "discount_percentage")}
                    for it in deals[:8] if isinstance(it, dict)]

    if before is not None:
        try:
            after = fetch_quota(api_key=key, session=s)
            result["quota_after"] = after.total_searches_left
            if (before.total_searches_left is not None
                    and after.total_searches_left is not None):
                result["searches_charged"] = (before.total_searches_left
                                              - after.total_searches_left)
        except Exception as exc:                   # noqa: BLE001
            log.warning("查不到探測後額度: %s", exc)
    return result


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = [a for a in argv if not a.startswith("-")]
    try:
        r = probe(args[0] if len(args) > 0 else "TPE",
                  args[1] if len(args) > 1 else "NRT")
    except Exception as exc:                       # noqa: BLE001
        print(f"探測失敗: {exc}", file=sys.stderr)
        return 1

    print("=" * 60)
    print(f"引擎: {r['engine']}   航線: {r['origin']}→{r['destination']}")
    print(f"日期區間: {r['outbound_window']}")
    print(f"HTTP: {r.get('http_status')}")
    print(f"額度: 前 {r.get('quota_before')} → 後 {r.get('quota_after')}"
          f"   本次扣 {r.get('searches_charged', '未知')} 次")
    print("=" * 60)
    if r.get("error"):
        print(f"\n❌ 錯誤: {r['error']}")
        print("\n→ Free Plan 可能不支援這個引擎，或參數不合法。")
        return 0                      # 探測本身算成功：我們得到答案了
    print(f"\n頂層欄位: {r.get('top_level_keys')}")
    print(f"陣列長度: {r.get('arrays')}")
    print(f"→ 這次查詢換到約 {r.get('rows')} 筆（對照：單日 google_flights 約 1.9 筆）")
    if r.get("destinations") is not None:
        print(f"\n--- 目的地分布（關鍵：這個引擎理不理 arrival_id）---")
        print(f"要求的目的地 {r.get('asked_for')} 命中 {r.get('asked_for_hits')} 筆")
        print(f"實際回傳: {r.get('destinations')}")
        print("\n--- 前 8 筆 deal ---")
        for d in r.get("deal_sample", []):
            print(f"  {d}")
    print("\n--- 回應結構 ---")
    print(r.get("structure", "(無)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
