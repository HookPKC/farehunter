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

import calendar
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

ACCOUNT_URL = "https://serpapi.com/account.json"

#: 預估月底用量低於總額度的這個比例時，標記為「額度遠未用盡」。
#: 這不是警報，是機會提示：若真有大量餘裕，SEARCHES_PER_DAY 就有調整空間
#: （但調整前必須先看到真實數字，不能猜——猜著加是在賭使用者的錢）。
HEADROOM_RATIO = 0.5


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


def assess(q: Quota, *, now: datetime | None = None) -> dict:
    """依本月至今的用量推估月底是否夠用。

    status:
      exhausted  剩餘為 0——已經在沉默失敗，或下一次查詢就會失敗
      low        照目前速度撐不到月底
      headroom   預估月底用量 < 總額度的 HEADROOM_RATIO（機會提示，非警報）
      ok         夠用
      unknown    欄位不足以判斷（例如 SerpAPI 改了回應格式）

    用「本月至今平均日用量」外推，不用設定檔裡的 SEARCHES_PER_DAY——實際跑
    幾次才算數。專案的鐵律：排程表 ≠ 實際執行。
    """
    now = now or datetime.now(timezone.utc)
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    day = now.day
    left = q.total_searches_left
    used = q.this_month_usage
    out = {
        "plan_name": q.plan_name,
        "searches_per_month": q.searches_per_month,
        "total_searches_left": left,
        "this_month_usage": used,
        "day_of_month": day,
        "days_in_month": days_in_month,
        "daily_rate": None,
        "projected_month_usage": None,
        "status": "unknown",
        "note": "",
    }
    if left is not None and left <= 0:
        out["status"] = "exhausted"
        out["note"] = "額度已用盡——查詢會開始靜默失敗，請確認方案"
        return out
    if used is None or day <= 0:
        out["note"] = "回應缺少 this_month_usage，無法推估"
        return out

    rate = used / day
    projected = rate * days_in_month
    out["daily_rate"] = round(rate, 2)
    out["projected_month_usage"] = round(projected, 1)

    if left is not None and left < rate * (days_in_month - day):
        out["status"] = "low"
        out["note"] = (f"照每日 {rate:.1f} 次的速度，剩餘 {left} 次撐不到月底"
                       f"（還需 {rate * (days_in_month - day):.0f} 次）")
        return out
    cap = q.searches_per_month
    # used > 0 才談餘裕：用量 0 時 projected 也是 0，說「餘裕大」沒有資訊量，
    # 反而可能是 SerpAPI 的計數還沒更新，不該拿來當調整額度的依據。
    if cap and used > 0 and projected < cap * HEADROOM_RATIO:
        out["status"] = "headroom"
        out["note"] = (f"預估月底用量 {projected:.0f}/{cap} "
                       f"（{projected / cap * 100:.0f}%），額度餘裕大。"
                       f"若要提高解析度，這個數字就是依據")
        return out
    out["status"] = "ok"
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
    try:
        q = fetch_quota(api_key=api_key, session=session)
        result = assess(q, now=now)
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
