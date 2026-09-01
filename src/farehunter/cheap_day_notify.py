"""把「特別便宜的出發日」推到 LINE／Telegram。

Usage: python -m farehunter.cheap_day_notify [prices.db] [docs/data.json]

**只推有 Google 實價背書的日子。**

這是這支程式最重要的一條規則，理由是使用者的原話：「我要的是 google 實價
正確，不然點進去的價格不是正確的也沒有什麼用」。推一個快取估價過去，使用者
點進 Google Flights 看到別的數字——實測快取對實價的絕對誤差中位數 7.9%、
90 百分位 27%，且 28% 的情況快取比實價便宜 >10%——那則推播就是負價值：
比不推還糟，因為它是主動推到手機上的錯誤資訊。

因此篩選條件是三個 AND：
  notable      落差 ≥ NOTIFY_PCT（30%），值得打斷使用者
  source=google  價格本身是實價，不是快取估價
  夠新鮮        實價的觀測時間在 MAX_AGE_HOURS 內

**驗證同時也是過濾器**，這是這個管線最漂亮的部分：verify_airlines 驗完之後
export_web 會重算看板，那個日期的最新觀測變成 google 實價。如果實價其實沒那麼
便宜，它的 discount_pct 就會掉下來、自動不再 notable，甚至整個從看板消失——
不需要額外寫「別推假便宜」的邏輯，資料自己會修正。

**一個已知且刻意接受的偏差**：鄰近日期的中位數大多仍是快取價，所以
「實價 vs 快取鄰居」的比較有輕微的蘋果比橘子問題（實測快取相對實價的
帶號誤差中位數 −2.2%，也就是快取略低，會讓實價看起來稍微沒那麼便宜）。
要消除它得把整組鄰居都驗過，那是 20 倍的額度，不值得。方向性正確就夠用。
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date as _date, datetime, timedelta, timezone

from .cheap_days import DEFAULT_DATA_PATH
from .notify import (WEEKDAYS, send_line, send_telegram, channels_configured,
                     _tw_stamp)
from .storage import Store

log = logging.getLogger(__name__)

#: 實價觀測多新才值得推。正常管線是 verify → export → notify 同一個 workflow
#: 跑完，資料齡只有幾分鐘，所以這條線很寬鬆也不會擋到正常流程；它擋的是
#: 「排程壞掉、拿昨天的價格來推」。實測價格漂移：只有 10% 的價格能維持
#: 24 小時不變，所以隔夜的實價已經不能叫「你點進去會看到的價格」。
MAX_AGE_HOURS = 6

#: 同一個（航線, 出發日）多久內不重複推播。便宜日會連續好幾天掛在看板上，
#: 沒有這條線會天天轟炸同一個日期。
SUPPRESS_DAYS = 14

#: 除非又便宜了這麼多——那是新消息，值得再說一次。
RENOTIFY_DROP_PCT = 10.0

#: 訊息裡「便宜 X%」與「實價 Y」必須自洽：X 應該等於 (1 - Y/鄰近中位數)。
#: 正常管線兩者由 export_web 同時算出所以必然一致，這條線擋的是它們**不**一致
#: 的情況——那代表某處只更新了一半，而推播會同時秀出兩個數字，使用者一眼
#: 就會看到矛盾。容許 1 個百分點的四捨五入誤差。
CONSISTENCY_TOLERANCE_PP = 1.0


def _self_consistent(it: dict, tolerance_pp: float = CONSISTENCY_TOLERANCE_PP) -> bool:
    """「便宜 X%」跟「實價 Y」對得起來嗎。缺中位數就無從檢查，放行。"""
    try:
        med = float(it.get("neighbour_median") or 0.0)
        if med <= 0:
            return True
        stated = float(it.get("discount_pct") or 0.0)
        computed = (1 - float(it["price"]) / med) * 100.0
        return abs(computed - stated) <= tolerance_pp
    except (TypeError, ValueError, KeyError):
        return False


def _fresh(observed_at: str | None, now: datetime, max_age_hours: float) -> bool:
    """觀測時間夠新嗎。無法解析一律回 False（不猜，寧可不推）。"""
    if not observed_at:
        return False
    try:
        ts = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts >= now - timedelta(hours=max_age_hours)


def notifiable(items: list[dict], *, now: datetime,
               max_age_hours: float = MAX_AGE_HOURS) -> list[dict]:
    """從看板項目挑出「值得推、而且推出去不會騙人」的。純函式，零 IO。

    落差大者先。缺欄位的項目直接跳過，不猜。
    """
    out = []
    for it in items or []:
        try:
            if not it.get("notable"):
                continue
            if str(it.get("source") or "").lower() != "google":
                continue          # 快取估價一律不推——見模組 docstring
            if not _fresh(it.get("observed_at"), now, max_age_hours):
                continue
            if not (it.get("origin") and it.get("destination")
                    and it.get("depart_date")):
                continue
            float(it["price"])    # 價格必須是數字，否則排版會炸
            if not _self_consistent(it):
                log.warning("看板項目自相矛盾，不推播 %s→%s %s："
                            "實價 %s vs 宣稱便宜 %s%%（中位數 %s）",
                            it.get("origin"), it.get("destination"),
                            it.get("depart_date"), it.get("price"),
                            it.get("discount_pct"), it.get("neighbour_median"))
                continue
            out.append(it)
        except (AttributeError, TypeError, ValueError, KeyError):
            continue
    out.sort(key=lambda c: -float(c.get("discount_pct") or 0.0))
    return out


def format_cheap_day(it: dict) -> str:
    """LINE／Telegram 文案。價格一律精確——能走到這裡的都是實價。

    比價連結沿用 notify.format_alert 完全相同的 q= 組法。**不要動那個參數**，
    專案實測過改了會讓 Google 的解析退化（已凍結，見 HANDOFF_AI §2）。
    """
    from urllib.parse import quote
    o, d = it["origin"], it["destination"]
    dep_s = it["depart_date"]
    ret_s = it.get("return_date")
    dep = _date.fromisoformat(dep_s)
    day = f"{dep_s} 週{WEEKDAYS[dep.weekday()]}"
    if ret_s:
        day += f" ↩ {ret_s}（{(_date.fromisoformat(ret_s) - dep).days} 天來回）"
    q = f"Flights from {o} to {d} on {dep_s}"
    if ret_s:
        q += f" through {ret_s}"
    booking = "https://www.google.com/travel/flights?q=" + quote(q)
    who = it.get("carriers") or "多家航空（點入查看）"
    disc = float(it.get("discount_pct") or 0.0)
    med = float(it.get("neighbour_median") or 0.0)
    n = int(it.get("neighbours") or 0)
    lines = [
        f"🟢 特別便宜的日子 {o}⇄{d}・直飛",
        f"日期: {day}",
        f"Google 實價: {float(it['price']):,.0f} TWD（{who}）",
        f"比前後十天便宜 {disc:.0f}%"
        + (f"（鄰近 {n} 天中位數 約 {round(med / 100) * 100:,.0f}）" if med and n else ""),
        f"驗證於 {_tw_stamp(it.get('observed_at'))} 台灣時間",
        f"立即比價: {booking}",
        "（此價格已由 Google Flights 實際查詢確認；票價隨時波動，"
        "點入後以 Google／航空公司顯示為準）",
    ]
    return "\n".join(lines)


def should_notify(store: Store, it: dict, *, now: datetime,
                  suppress_days: int = SUPPRESS_DAYS,
                  renotify_drop_pct: float = RENOTIFY_DROP_PCT) -> bool:
    """這個便宜日最近推過了嗎？除非又便宜了 renotify_drop_pct 才重推。

    便宜日會連續好幾天掛在看板上，沒有這條線會天天轟炸同一個日期。
    """
    row = store.conn.execute(
        """SELECT price, notified_at FROM cheap_day_notices
           WHERE origin=? AND destination=? AND depart_date=?""",
        (it["origin"], it["destination"], it["depart_date"])).fetchone()
    if row is None:
        return True
    try:
        last = datetime.fromisoformat(str(row["notified_at"]).replace("Z", "+00:00"))
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return True                       # 時間戳壞了 → 當作沒推過
    if last < now - timedelta(days=suppress_days):
        return True
    prev = float(row["price"])
    if prev > 0 and float(it["price"]) <= prev * (1 - renotify_drop_pct / 100.0):
        return True                       # 又便宜一大截 → 這是新消息
    return False


def run(db_path: str = "prices.db", data_path: str | None = None,
        *, now: datetime | None = None, dry_run: bool = False) -> dict:
    """讀看板 → 篩 → 去重 → 推送。

    嚴格 fail-soft 於讀取端（檔案缺失／損壞 → 什麼都不做），但推送失敗會計入
    summary 讓 workflow 看得到。
    """
    now = now or datetime.now(timezone.utc)
    # None → 單一來源常數（見 cheap_days.DEFAULT_DATA_PATH）
    data_path = data_path or DEFAULT_DATA_PATH
    summary = {"board": 0, "candidates": 0, "suppressed": 0,
               "sent": 0, "failed": 0, "dry_run": dry_run}
    try:
        with open(data_path, encoding="utf-8") as fh:
            items = json.load(fh).get("cheap_days") or []
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        log.warning("讀不到看板資料（%s），本輪不推播: %s", data_path, exc)
        return summary
    summary["board"] = len(items)
    cands = notifiable(items, now=now)
    summary["candidates"] = len(cands)
    if not cands:
        log.info("看板 %d 筆，沒有『已驗證且夠新鮮』的 notable 便宜日", len(items))
        return summary

    store = Store(db_path)
    try:
        for it in cands:
            if not should_notify(store, it, now=now):
                summary["suppressed"] += 1
                log.info("近期已推過，跳過: %s→%s %s",
                         it["origin"], it["destination"], it["depart_date"])
                continue
            text = format_cheap_day(it)
            if dry_run:
                print(text); print("-" * 40)
                summary["sent"] += 1
                continue
            sent = [c for c, ok in (("telegram", send_telegram(text)),
                                    ("line", send_line(text))) if ok]
            if not sent:
                # 沒設任何管道時 notify.notify 的行為是印出來，這裡沿用：
                # 本機執行看得到內容，而不是靜默什麼都沒發生。
                if not channels_configured():
                    log.info("未設定任何通知管道，只印出:\n%s", text)
                    print(text)
                else:
                    summary["failed"] += 1
                    log.error("推播失敗: %s→%s %s",
                              it["origin"], it["destination"], it["depart_date"])
                    continue
            summary["sent"] += 1
            store.record_cheap_day_notice(
                it["origin"], it["destination"], it["depart_date"],
                float(it["price"]), now.isoformat(timespec="seconds"))
            log.info("已推播 %s→%s %s（-%.0f%%）: %s",
                     it["origin"], it["destination"], it["depart_date"],
                     float(it.get("discount_pct") or 0), ", ".join(sent) or "stdout")
    finally:
        store.close()
    log.info("便宜日推播 summary: %s", summary)
    return summary


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    dry = "--dry-run" in argv
    args = [a for a in argv if not a.startswith("-")]
    s = run(args[0] if len(args) > 0 else "prices.db",
            args[1] if len(args) > 1 else "docs/data.json",
            dry_run=dry)
    print(f"便宜日推播: 看板 {s['board']} 筆, 合格 {s['candidates']}, "
          f"抑制 {s['suppressed']}, 送出 {s['sent']}, 失敗 {s['failed']}")
    # 推播失敗不讓 workflow 紅燈：這一步跑在抓價與 commit 之後，
    # 為了通知失敗而讓整輪變紅會遮掉真正的資料問題。失敗看 log。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
