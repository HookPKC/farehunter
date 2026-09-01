"""SerpAPI 額度自我檢查。Usage: python -m farehunter.quota

**為什麼需要這支程式**

兩個真實教訓：

1. SearchApi 的一次性額度於 2026-08 用盡後，gcal_sweep 連續五週回 HTTP 429、
   寫 0 筆資料，而 workflow 全綠——潛伏了 10 天沒人發現。付費 API 的額度是
   會沉默死亡的，不主動去看就不會知道。
2. `serpapi_flights` 的註解長期寫著「free tier (~100 searches/month)」，整個
   系統的 `SEARCHES_PER_DAY = 6` 是圍繞這個前提設計的。但 2026-08 實測 31 天
   每天跑滿、約 180 次/月，從未被擋——也就是說那個前提是錯的，而且這個錯誤的
   假設一直在限制系統的解析度。沒人知道真實額度有多大。

`https://serpapi.com/account.json` **免費且不計入額度**，回傳方案名稱、本月
用量與剩餘次數。每天記一次，上面兩個問題就都不必再猜。

嚴格 fail-soft：這是維運觀測工具，不是資料來源。任何失敗都只記 log，絕不
讓抓價流程失敗——為了看儀表板而弄掛引擎是本末倒置。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

ACCOUNT_URL = "https://serpapi.com/account.json"

#: 續航低於這麼多天就記 warning。這是「該處理了」的線，不是「用完了」——
#: 留幾天餘裕才有時間反應。
LOW_RUNWAY_DAYS = 7

#: 續航超過這麼多天視為餘裕大（機會提示，非警報）。一個計費週期約 30 天，
#: 撐得過一整個週期就代表額度沒有在限制系統。
HEADROOM_RUNWAY_DAYS = 30

#: 沒有上一份快照可比時的每日燒用量估計。fsc_snapshot 每天 SEARCHES_PER_DAY
#: 次是唯一常態消耗來源；刻意不 import 以免形成循環依賴，改用測試釘住一致性。
EXPECTED_DAILY = 6.0


def _days_between(then: str | None, now: datetime) -> float | None:
    """兩個時間戳之間的天數。無法解析回 None（不猜）。"""
    if not then:
        return None
    try:
        ts = datetime.fromisoformat(str(then).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds() / 86400.0


@dataclass(frozen=True)
class Quota:
    """account.json 的關鍵欄位。全部 Optional：SerpAPI 改欄位名時要能降級
    成「拿到什麼記什麼」，而不是整支爆掉。"""
    plan_name: str = ""
    searches_per_month: int | None = None
    plan_searches_left: int | None = None
    total_searches_left: int | None = None
    this_month_usage: int | None = None


def _as_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_account(payload: dict) -> Quota:
    """account.json → Quota。缺欄位一律 None，不猜、不填 0（0 會被誤讀成
    「額度用完」，那是完全相反的意思）。"""
    if not isinstance(payload, dict):
        return Quota()
    return Quota(
        plan_name=str(payload.get("plan_name") or ""),
        searches_per_month=_as_int(payload.get("searches_per_month")),
        plan_searches_left=_as_int(payload.get("plan_searches_left")),
        total_searches_left=_as_int(payload.get("total_searches_left")),
        this_month_usage=_as_int(payload.get("this_month_usage")),
    )


def fetch_quota(api_key: str | None = None,
                session: requests.Session | None = None,
                timeout: int = 30) -> Quota:
    """打 account.json。免費、不計入額度（見模組 docstring）。"""
    key = api_key or os.environ.get("SERPAPI_KEY", "")
    if not key:
        raise RuntimeError("Missing SERPAPI_KEY environment variable.")
    s = session or requests
    resp = s.get(ACCOUNT_URL, params={"api_key": key}, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    return parse_account(resp.json())


def assess(q: Quota, *, now: datetime | None = None,
           prev: dict | None = None,
           expected_daily: float = EXPECTED_DAILY) -> dict:
    """算「還能撐幾天」，而不是「撐不撐得到月底」。

    **為什麼不看月底**（2026-09-01 的生產事故）：原本用
    `this_month_usage / day_of_month` 當日均再外推到月底。但 SerpAPI 的免費層
    不照日曆月重置——實測 8/31 剩 80、9/1 仍是 80，`this_month_usage` 是
    **計費週期**的累計。9/1 那天 176 ÷ 1 = 每天 176 次、外推 5,280，於是報出
    假的 low 警報和「還需 5104 次」這種荒謬數字。ADR 0001 記了「不照日曆月
    重置」這個事實，但這裡的程式還在用錯的假設。

    現在改成：週期起點未知也無所謂，只看「剩餘 ÷ 每日燒用量 = 還能撐幾天」。
    每日燒用量優先用「與上一份快照的實測差值」（真實執行才算數，符合專案
    鐵律：排程表 ≠ 實際執行），沒有上一份時退回 expected_daily。

    status:
      exhausted  剩餘為 0——已經在沉默失敗，或下一次查詢就會失敗
      low        續航 < LOW_RUNWAY_DAYS 天，該處理了
      headroom   續航 > HEADROOM_RUNWAY_DAYS 天（機會提示，非警報）
      ok         夠用
      unknown    欄位不足以判斷（例如 SerpAPI 改了回應格式）

    prev: 上一份 quota.json 的內容。除了算燒用量，也用來偵測週期重置
    （用量突然變小）——那是唯一能知道重置日的方法。
    """
    now = now or datetime.now(timezone.utc)
    left = q.total_searches_left
    used = q.this_month_usage
    out = {
        "plan_name": q.plan_name,
        "searches_per_month": q.searches_per_month,
        "total_searches_left": left,
        "this_month_usage": used,
        "burn_per_day": None,
        "burn_source": None,
        "runway_days": None,
        "period_reset": False,
        "status": "unknown",
        "note": "",
    }
    if left is not None and left <= 0:
        out["status"] = "exhausted"
        out["note"] = "額度已用盡——查詢會開始靜默失敗，請確認方案"
        return out

    # 與上一份快照比較：實測燒用量，並偵測計費週期重置
    burn, source, reset_note = None, None, ""
    if prev and used is not None:
        prev_used = prev.get("this_month_usage")
        elapsed = _days_between(prev.get("checked_at"), now)
        if isinstance(prev_used, (int, float)) and elapsed and elapsed > 0:
            if used < prev_used:
                # 用量倒退 = 計費週期剛重置。這是唯一能觀測到重置日的方式，
                # 記下來，並且不要拿倒退的差值去算燒用量。
                out["period_reset"] = True
                # 用獨立變數收集，最後才併進 note。曾經直接寫 out["note"]，
                # 結果被下面的狀態訊息整段蓋掉——而「重置日」是這支程式能
                # 觀測到的最有價值的一件事，不能被順手覆寫。
                reset_note = (f"計費週期已重置（用量 {prev_used:.0f} → "
                              f"{used:.0f}）——記下這個日期")
            else:
                burn, source = (used - prev_used) / elapsed, "measured"

    if burn is None or burn <= 0:
        burn, source = expected_daily, "expected"
    out["burn_per_day"] = round(burn, 2)
    out["burn_source"] = source

    def _note(text: str) -> str:
        return f"{reset_note}；{text}" if reset_note else text

    if left is None:
        out["note"] = _note("回應缺少 total_searches_left，無法估續航")
        return out

    runway = left / burn
    out["runway_days"] = round(runway, 1)
    tail = (f"剩餘 {left} 次、每日約 {burn:.1f} 次"
            f"（{'實測' if source == 'measured' else '預估'}）"
            f"→ 續航約 {runway:.0f} 天")
    if runway < LOW_RUNWAY_DAYS:
        out["status"] = "low"
        out["note"] = _note(f"額度快見底：{tail}")
        return out
    if runway > HEADROOM_RUNWAY_DAYS:
        out["status"] = "headroom"
        out["note"] = _note(f"{tail}；額度餘裕大，若要提高解析度這個數字就是依據")
        return out
    out["status"] = "ok"
    out["note"] = _note(tail)
    return out


def snapshot(path: str = "docs/quota.json", *,
             api_key: str | None = None,
             session: requests.Session | None = None,
             now: datetime | None = None) -> dict:
    """取額度、判讀、寫檔。**永不拋例外**——見模組 docstring 的 fail-soft。

    回傳判讀結果（失敗時 status='error'）。寫檔失敗也只記 log：檔案是給人看的
    歷史紀錄（每天隨 commit 進 repo 就有時間序列），不是流程的一部分。
    """
    now = now or datetime.now(timezone.utc)
    # 沒有金鑰就什麼都不做——不連網、不寫檔。這是安全預設而非邊角處理：
    # fsc_snapshot 的測試會 mock 掉 search_google_flights 但不會設金鑰，若這裡
    # 照跑就會（a）嘗試連外、（b）覆寫 repo 裡真實的 docs/quota.json。同樣的
    # 隔離漏洞已經在 docs/data.json 上咬過兩次，不靠「每個測試都記得傳參數」。
    if not (api_key or os.environ.get("SERPAPI_KEY", "")):
        log.info("未設 SERPAPI_KEY，跳過額度檢查")
        return {"status": "skipped", "note": "no SERPAPI_KEY",
                "checked_at": now.isoformat(timespec="seconds")}
    # 讀上一份快照當基準：檔案就是自己的歷史，用來實測每日燒用量並偵測
    # 計費週期重置。讀不到就退回預估值，不影響主要產出。
    prev = None
    try:
        prev = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        pass
    try:
        q = fetch_quota(api_key=api_key, session=session)
        result = assess(q, now=now, prev=prev)
    except Exception as exc:                     # noqa: BLE001 — 見 docstring
        log.warning("額度檢查失敗（不影響本輪抓價）: %s", exc)
        result = {"status": "error", "note": str(exc)[:300],
                  "plan_name": "", "searches_per_month": None,
                  "total_searches_left": None, "this_month_usage": None}
    result["checked_at"] = now.isoformat(timespec="seconds")
    _log_result(result)
    try:
        Path(path).write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as exc:
        log.warning("額度報告寫檔失敗: %s", exc)
    return result


def _log_result(r: dict) -> None:
    """方案與用量要顯眼——這支程式存在的首要理由就是讓人看到真實額度。"""
    if r["status"] in ("error", "skipped"):
        return
    msg = ("SerpAPI 額度｜方案 %s｜月額度 %s｜本月已用 %s｜剩餘 %s｜狀態 %s")
    args = (r.get("plan_name") or "(未回報)", r.get("searches_per_month"),
            r.get("this_month_usage"), r.get("total_searches_left"),
            r["status"])
    if r["status"] in ("exhausted", "low"):
        log.warning(msg + "｜%s", *args, r.get("note", ""))
    else:
        log.info(msg, *args)
        if r.get("note"):
            log.info("額度備註: %s", r["note"])


def main(argv=None) -> int:
    import sys
    argv = argv if argv is not None else sys.argv[1:]
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    r = snapshot(argv[0] if argv else "docs/quota.json")
    print(f"SerpAPI 額度: 方案 {r.get('plan_name') or '(未回報)'}, "
          f"月額度 {r.get('searches_per_month')}, "
          f"本月已用 {r.get('this_month_usage')}, "
          f"剩餘 {r.get('total_searches_left')}, 狀態 {r['status']}")
    if r.get("note"):
        print(f"備註: {r['note']}")
    # 永遠回 0：額度檢查失敗不該讓抓價 workflow 紅燈（fail-soft）。
    # 額度真的用盡時，是靠 log warning 與 docs/quota.json 的歷史被看見。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
