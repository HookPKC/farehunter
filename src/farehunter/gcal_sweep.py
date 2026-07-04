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
            stats = store.route_stats(o, d)
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
                                       absolute_threshold=merged.get("absolute_threshold"),
                                       drop_pct=merged.get("drop_pct", 25.0),
                                       min_history=merged.get("min_history", 30))
                    if verdict.is_deal and not store.recently_alerted(
                            o, d, offer.depart_date, offer.price):
                        sent = notify(offer, verdict)
                        if not sent and channels_configured():
                            log.error("通知發送失敗，保留至下一輪重試: %s→%s %s",
                                      o, d, offer.depart_date)
                        else:
                            store.record_alert(o, d, offer.depart_date,
                                               offer.price, verdict.reason)
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
