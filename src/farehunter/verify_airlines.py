"""Daily airline verification. Usage: python -m farehunter.verify_airlines

驗證目標的優先順序（2026-08 改）：
1. **看板的「特別便宜」日**（`docs/data.json` 的 cheap_days，notable 且來源是
   快取）。這是首頁最顯眼的推薦，卻是唯一沒有實價背書的：實測快取價對 Google
   即時價（同航程、時間差 ≤6 小時、74 筆）絕對誤差中位數 7.9%、90 百分位 27%，
   且 28% 的情況快取比實價便宜 >10%。看板第一名若落在那 28% 裡，使用者點進去
   會看到高得多的價格——每月 90 次 Scrape.do 額度花在這裡回報最高。
2. **carriers='' 的 google 觀測**（fallback）。有實價但沒有航空公司的列，
   補上直飛航班資訊。這個池子原本由 SearchApi 日曆供料，該來源已於 2026-08
   移除，因此現在通常為空；保留是因為 serpapi 理論上也可能產生無航班號的列。

兩池都會過 `_is_cooled`（72 小時內已有 source='google' 觀測就跳過）。這同時
擋掉與 fsc_snapshot 的 cheap_day 槽重複——scrape.do 與 serpapi 都寫
source='google'，誰先跑誰認領，不會兩邊都花額度查同一個行程。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path

from .scrapedo_flights import (search_flights, parse_cheapest_direct,
                               VERIFICATIONS_PER_DAY, ScrapeDoError)
from .serpapi_flights import cheap_days_candidates, _is_cooled
from .cheap_days import DROP_PCT
from .storage import Store
from . import health

log = logging.getLogger(__name__)

#: Scrape.do 免費層預算（每次成功請求 10 credits，免費 1,000 credits/月）：
#:
#:   VERIFICATIONS_PER_DAY(3) × 31 天 × 10 credits = 930 credits = 93%
#:
#: 也就是說每日上限 3 本來就是照免費層算出來的，之前只是因為候選池卡在
#: notable 而填不滿（實測 14 天看板歷史：每日 1.9 筆 = 576 credits = 58%）。
#: 放寬後每日 2.6 筆 = 819 credits = 82%，仍在免費層內。
#:
#: **要調高 VERIFICATIONS_PER_DAY 之前先重算這行。** 4/天就是 1,240 credits，
#: 超過免費層，月底會開始靜默失敗——那正是 SearchApi 那次的失敗型態。

#: 候選觀測最多可以多舊。驗證一個 49 天前的日曆價，對今天的決策沒有意義——
#: 那個價格早就不存在了（實測快取／舊觀測與現價的絕對誤差中位數 7.9%、
#: 90 百分位 27%，而 49 天的跨度遠大於此）。
#:
#: 這個界線是必要的：SearchApi 的一次性額度於 2026-08 用盡後，日曆來源停止供料，
#: 而 carriers='' 的候選池被凍結在七月的 77 筆。沒有這個界線，這支程式會每天
#: 花掉 3 次 Scrape.do（每月 90 次＝免費層 1,000 credits 的 900）去驗證死資料，
#: 而且會持續 26 天把整池磨完。寧可閒置也不要浪費額度在過期的價格上。
#:
#: 同樣的界線也套在 cheap_days 池上，理由相同但觸發情境不同：cheap_days 本身
#: 只收 24 小時內的觀測，但那是 export_web 跑的時候。若 export 停擺，
#: data.json 會凍結在舊的推薦上，這裡是第二道防線。
CANDIDATE_MAX_AGE_DAYS = 14


def _too_old(observed_at: str, max_age_days: int, now: datetime) -> bool:
    """True 若 observed_at 超過 max_age_days。無法解析 → 視為過舊（不猜）。"""
    if not observed_at:
        return True
    try:
        ts = datetime.fromisoformat(str(observed_at))
    except (ValueError, TypeError):
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts < now - timedelta(days=max_age_days)


def pick_candidates(store: Store, limit: int = VERIFICATIONS_PER_DAY,
                    max_age_days: int = CANDIDATE_MAX_AGE_DAYS,
                    data_path: str = "docs/data.json",
                    today: _date | None = None,
                    now: datetime | None = None,
                    now_ref: str | None = None) -> list[dict]:
    """優先驗證看板推薦的「特別便宜」日，其次補 carriers='' 的觀測。

    每航線最多一筆（3 次額度攤在不同航線上，資訊量最大）。today / now /
    now_ref 是單一時間來源的三個面：日期窗口、Python 時間比較、SQL julianday；
    production 全部不傳（＝真實時鐘，行為不變），測試傳固定值。
    """
    today = today or _date.today()
    now = now or datetime.now(timezone.utc)
    now_ref = now_ref or "now"
    out: list[dict] = []
    claimed_routes: set[tuple[str, str]] = set()

    for c in cheap_days_candidates(data_path, today,
                                   require_notable=False,
                                   min_discount_pct=DROP_PCT):
        if len(out) >= limit:
            break
        o, d = c["origin"], c["destination"]
        if (o, d) in claimed_routes:
            continue
        if _too_old(c.get("observed_at", ""), max_age_days, now):
            log.warning("看板候選 %s→%s %s 的觀測已超過 %d 天，跳過"
                        "（export_web 是否停擺？）", o, d, c["depart_date"],
                        max_age_days)
            continue
        if _is_cooled(store.conn, o, d, c["depart_date"], now_ref=now_ref):
            continue                  # 72h 內已有實價（可能是 fsc 剛驗過）
        claimed_routes.add((o, d))
        out.append({"origin": o, "destination": d,
                    "depart_date": c["depart_date"],
                    "return_date": c["return_date"],
                    "price": c["price"], "kind": "cheap_day",
                    "discount_pct": c.get("discount_pct") or 0.0})
    if out:
        log.info("看板候選 %d 筆：%s", len(out),
                 ", ".join(f"{c['origin']}→{c['destination']} {c['depart_date']}"
                           f"(-{c['discount_pct']:.0f}%)" for c in out))
    if len(out) >= limit:
        return out

    for c in _unverified_candidates(store, limit - len(out), max_age_days):
        if (c["origin"], c["destination"]) in claimed_routes:
            continue
        claimed_routes.add((c["origin"], c["destination"]))
        out.append({**c, "kind": "unverified"})

    # 額度上限保護，比照 fsc_snapshot.build_plans 的 assert。上面有三道各自
    # 獨立的界線（提早 return、limit-len(out)、SQL LIMIT），任一道被改壞時
    # 其他兩道會遮住問題——實測突變測試中單獨拔掉任何一道都沒有測試會紅。
    # 這行把「絕不超花」這個真正的不變量釘在一個地方。
    assert len(out) <= limit, "Scrape.do 每日上限保護:候選超額"
    return out


def _unverified_candidates(store: Store, limit: int,
                           max_age_days: int) -> list[dict]:
    """Cheapest unverified google-priced future dates, max one per route.

    只取 max_age_days 內的觀測——見 CANDIDATE_MAX_AGE_DAYS 的說明。
    """
    if limit <= 0:
        return []
    rows = store.conn.execute(
        """WITH latest_google AS (
             SELECT origin, destination, depart_date, return_date, price, carriers,
                    ROW_NUMBER() OVER (PARTITION BY origin, destination, depart_date
                                       ORDER BY observed_at DESC, rowid DESC) AS rk
             FROM observations
             WHERE source='google' AND fare_class='any'
               AND depart_date BETWEEN date('now','+1 day') AND date('now','+330 days')
               AND julianday(observed_at) >= julianday('now') - ?),
           unverified AS (
             SELECT *, ROW_NUMBER() OVER (PARTITION BY origin, destination
                                          ORDER BY price ASC) AS pr
             FROM latest_google WHERE rk=1 AND carriers='' AND return_date != '')
           SELECT origin, destination, depart_date, return_date, price
           FROM unverified WHERE pr=1 ORDER BY price ASC LIMIT ?""",
        (max_age_days, limit)).fetchall()
    if not rows:
        # 「沒有新鮮的待驗目標」與「壞掉了」是兩件事，要能分辨。
        stale = store.conn.execute(
            """SELECT COUNT(*) FROM observations
               WHERE source='google' AND fare_class='any' AND carriers=''
                 AND depart_date > date('now')""").fetchone()[0]
        if stale:
            # 已知且預期：SearchApi 日曆於 2026-08 移除，池底那批七月觀測不會
            # 再更新。用 info 而非 warning——這不是待處理的異常，每天發警報
            # 只會讓真正的警報被忽略。
            log.info("carriers='' 池只剩 %d 筆超過 %d 天的觀測（SearchApi 日曆"
                     "已移除，屬預期），跳過以免浪費額度", stale, max_age_days)
        else:
            log.info("無待驗證候選——所有 google 觀測都已帶有航空公司")
    return [dict(r) for r in rows]


def _gap_note(cand: dict, real_price: float) -> str:
    """看板估價 vs 實價的落差說明。這是整支程式的產出重點：使用者問的是
    「看板還準嗎」，答案就在這個百分比裡。估價缺漏時只說沒得比，不猜。"""
    est = cand.get("price")
    label = "看板估價" if cand.get("kind") == "cheap_day" else "原觀測"
    try:
        est = float(est)
    except (TypeError, ValueError):
        return f"（無{label}可比）"
    if est <= 0:
        return f"（無{label}可比）"
    diff = (real_price - est) / est * 100.0
    return f"（{label} {est:.0f} → 實價差 {diff:+.1f}%）"


def run(db_path: str = "prices.db",
        data_path: str = "docs/data.json") -> dict:
    store = Store(db_path)
    summary = {"searched": 0, "verified": 0, "errors": 0,
               "cheap_day": 0, "unverified": 0}
    report = {"errors": [], "probe": None, "verified": []}
    try:
        for cand in pick_candidates(store, data_path=data_path):
            o, d = cand["origin"], cand["destination"]
            summary["searched"] += 1
            summary[cand["kind"]] = summary.get(cand["kind"], 0) + 1
            try:
                payload = search_flights(o, d, cand["depart_date"], cand["return_date"])
            except ScrapeDoError as exc:
                log.error("Verify failed %s→%s %s: %s", o, d, cand["depart_date"], exc)
                summary["errors"] += 1
                report["errors"].append(f"{o}→{d} {cand['depart_date']}: {exc}")
                continue
            if report["probe"] is None:
                report["probe"] = {"keys": sorted(payload.keys())}
            offer = parse_cheapest_direct(payload, o, d,
                                          cand["depart_date"], cand["return_date"])
            pi = payload.get("price_insights") or {}
            if pi.get("price_level"):
                rng = pi.get("typical_price_range") or [None, None]
                store.record_insight(o, d, cand["depart_date"],
                                     str(pi["price_level"]), rng[0], rng[1])
            if offer is None:
                log.info("No direct itinerary %s→%s %s", o, d, cand["depart_date"])
                report["errors"].append(f"{o}→{d} {cand['depart_date']}: 無直飛結果")
                continue
            store.record(offer)
            summary["verified"] += 1
            report["verified"].append(
                f"[{cand['kind']}] {o}→{d} {offer.depart_date} "
                f"{offer.price:.0f} {offer.carriers}{_gap_note(cand, offer.price)}")
            log.info("Verified [%s] %s→%s %s: %.0f TWD %s %s",
                     cand["kind"], o, d, offer.depart_date, offer.price,
                     offer.carriers, _gap_note(cand, offer.price))
            time.sleep(1)
    finally:
        store.close()
    report["summary"] = summary
    Path("docs/verify-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("Verify summary: %s", summary)
    return summary


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = run(sys.argv[1] if len(sys.argv) > 1 else "prices.db")
    print(f"航空驗證完成: 查詢 {s['searched']} 次（看板 {s['cheap_day']}"
          f"/補航班 {s['unverified']}）, 確認 {s['verified']} 筆, "
          f"錯誤 {s['errors']} 次")
    # 全軍覆沒時以非零結束碼失敗——見 health.sweep_exit_code 的說明：
    # SearchApi 額度用完後，這支程式曾連續五週回 0 記錄 16 錯誤而 workflow 全綠。
    raise SystemExit(health.sweep_exit_code(s, "verify_airlines"))
