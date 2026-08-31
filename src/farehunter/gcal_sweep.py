"""Weekly real-price calendar sweep. Usage: python -m farehunter.gcal_sweep"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from datetime import date, timedelta

from .runner import load_config
from .searchapi_calendar import fetch_calendar, parse_calendar, SearchApiError
from .storage import Store
from .analyzer import evaluate
from .notify import notify, channels_configured
from . import price_state
from . import health

log = logging.getLogger(__name__)

CHUNK_DAYS = 14
NEAR_CHUNKS = 1       # 未來 14 天：每週必掃（可訂票的迫近區）
DEEP_POSITIONS = 19   # 深掃輪替位置（每週前進 14 天，約 9 個月一輪，額度不變）


def sweep_windows(today: date) -> list[tuple[date, date]]:
    """本次掃描窗口：近端固定 + 一段隨週次前進的深掃窗，
    使未來約 6 個月每個區段都會被真實價格輪到（額度不變，每次 2 窗）。"""
    wins = []
    for i in range(NEAR_CHUNKS):
        start = today + timedelta(days=1 + CHUNK_DAYS * i)
        wins.append((start, start + timedelta(days=CHUNK_DAYS - 1)))
    week = today.isocalendar()[1]
    deep_i = week % DEEP_POSITIONS
    dstart = today + timedelta(days=1 + CHUNK_DAYS * (NEAR_CHUNKS + deep_i))
    wins.append((dstart, dstart + timedelta(days=CHUNK_DAYS - 1)))
    return wins


def run(config_path: str = "config.yaml", db_path: str = "prices.db") -> dict:
    cfg = load_config(config_path)
    defaults = cfg.get("defaults", {})
    store = Store(db_path)
    summary = {"searched": 0, "recorded": 0, "alerts": 0, "errors": 0}
    report = {"errors": [], "probe": None}
    today = date.today()
    windows = sweep_windows(today)
    probed = False
    try:
        for route in cfg["routes"]:
            o, d = route["origin"], route["destination"]
            merged = {**defaults, **route}
            stats = store.route_stats(o, d)              # new_low：整條航線
            stats_by_date = store.route_stats_by_date(o, d)   # big_drop：單一出發日
            for start, end in windows:
                summary["searched"] += 1
                try:
                    payload = fetch_calendar(o, d, start, end,
                                             currency=merged.get("currency", "twd").upper())
                except SearchApiError as exc:
                    log.error("Calendar failed %s→%s: %s", o, d, exc)
                    summary["errors"] += 1
                    if len(report["errors"]) < 5:
                        report["errors"].append(f"{o}→{d} {start}: {exc}")
                    continue
                offers = parse_calendar(payload, o, d)
                if not probed:   # 首次回應印出結構供人工驗證
                    probed = True
                    sample = (payload.get("calendar") or [])[:3]
                    log.info("PROBE keys=%s calendar_rows=%d sample=%s",
                             sorted(payload.keys()),
                             len(payload.get("calendar") or []),
                             json.dumps(sample, ensure_ascii=False))
                    report["probe"] = {"keys": sorted(payload.keys()),
                                       "rows": len(payload.get("calendar") or []),
                                       "sample": sample}
                if not offers:
                    log.info("Calendar empty %s→%s %s..%s", o, d, start, end)
                    continue
                for offer in offers:
                    store.record(offer)
                    summary["recorded"] += 1
                    verdict = evaluate(offer, stats,
                                       date_stats=stats_by_date.get(offer.depart_date),
                                       absolute_threshold=merged.get("absolute_threshold"),
                                       drop_pct=merged.get("drop_pct", 25.0),
                                       min_history=merged.get("min_history", 30))
                    # identity 必須與 runner 一致。原本這裡只傳 4 個位置參數，
                    # return_date / carrier_signature / price_status 全走 NULL，
                    # 於是同一個出發日的不同回程日會落進同一個 dedup 桶互相封鎖
                    # ——正是 alerts 表加這幾個欄位要修掉的那個 bug。窗從 24 小時
                    # 拉到 30 天之後，這個漏傳的影響被放大 30 倍。
                    # price_status 固定為 verified：這條路徑取的就是 Google 實價，
                    # 本身即權威價，不需要再被別的觀測驗證。
                    csig = price_state.carrier_signature(offer.carriers)
                    if verdict.is_deal and not store.recently_alerted(
                            o, d, offer.depart_date, offer.price,
                            return_date=offer.return_date,
                            carrier_signature=csig,
                            price_status=price_state.VERIFIED,
                            reason=verdict.reason):
                        sent = notify(offer, verdict)
                        if not sent and channels_configured():
                            log.error("通知發送失敗，保留至下一輪重試: %s→%s %s",
                                      o, d, offer.depart_date)
                        else:
                            store.record_alert(
                                o, d, offer.depart_date, offer.price,
                                verdict.reason,
                                return_date=offer.return_date,
                                carrier_signature=csig,
                                price_source=offer.source,
                                price_status=price_state.VERIFIED)
                            summary["alerts"] += 1
                time.sleep(1)
    finally:
        store.close()
    report["summary"] = summary
    Path("docs/sweep-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("Sweep summary: %s", summary)
    return summary


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = run(sys.argv[1] if len(sys.argv) > 1 else "config.yaml",
            sys.argv[2] if len(sys.argv) > 2 else "prices.db")
    print(f"日曆掃描完成: 查詢 {s['searched']} 次, 記錄 {s['recorded']} 筆, "
          f"警報 {s['alerts']} 則, 錯誤 {s['errors']} 次")
    # 全軍覆沒時以非零結束碼失敗——見 health.sweep_exit_code 的說明：
    # SearchApi 額度用完後，這支程式曾連續五週回 0 記錄 16 錯誤而 workflow 全綠。
    raise SystemExit(health.sweep_exit_code(s, "gcal_sweep"))
