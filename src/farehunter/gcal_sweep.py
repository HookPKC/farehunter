"""Weekly real-price calendar sweep. Usage: python -m farehunter.gcal_sweep"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, timedelta

from .runner import load_config
from .searchapi_calendar import fetch_calendar, parse_calendar, SearchApiError
from .storage import Store
from .analyzer import evaluate
from .notify import notify

log = logging.getLogger(__name__)

MONTH_CHUNKS = 2      # 未來 2 個 30 天窗口


def run(config_path: str = "config.yaml", db_path: str = "prices.db") -> dict:
    cfg = load_config(config_path)
    defaults = cfg.get("defaults", {})
    store = Store(db_path)
    summary = {"searched": 0, "recorded": 0, "alerts": 0, "errors": 0}
    today = date.today()
    probed = False
    try:
        for route in cfg["routes"]:
            o, d = route["origin"], route["destination"]
            merged = {**defaults, **route}
            stats = store.route_stats(o, d)
            for chunk in range(MONTH_CHUNKS):
                start = today + timedelta(days=1 + 30 * chunk)
                end = start + timedelta(days=29)
                summary["searched"] += 1
                try:
                    payload = fetch_calendar(o, d, start, end,
                                             currency=merged.get("currency", "twd").upper())
                except SearchApiError as exc:
                    log.error("Calendar failed %s→%s: %s", o, d, exc)
                    summary["errors"] += 1
                    continue
                offers = parse_calendar(payload, o, d)
                if not probed:   # 首次回應印出結構供人工驗證
                    probed = True
                    sample = (payload.get("calendar") or [])[:3]
                    log.info("PROBE keys=%s calendar_rows=%d sample=%s",
                             sorted(payload.keys()),
                             len(payload.get("calendar") or []),
                             json.dumps(sample, ensure_ascii=False))
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
                        notify(offer, verdict)
                        store.record_alert(o, d, offer.depart_date,
                                           offer.price, verdict.reason)
                        summary["alerts"] += 1
                time.sleep(1)
    finally:
        store.close()
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
