"""Daily full-service fare snapshot runner. Usage: python -m farehunter.fsc_snapshot"""
from __future__ import annotations

import logging
import time

from .runner import load_config
from .serpapi_flights import (search_google_flights, parse_full_service,
                              pick_routes_for_today, snapshot_dates,
                              horizon_for_slot, SerpApiError)
from .storage import Store

log = logging.getLogger(__name__)


def run(config_path: str = "config.yaml", db_path: str = "prices.db") -> dict:
    cfg = load_config(config_path)
    from datetime import date as _date
    today = _date.today()
    routes = pick_routes_for_today(cfg["routes"], today=today)
    store = Store(db_path)
    summary = {"searched": 0, "recorded": 0, "errors": 0}
    try:
        for slot, route in enumerate(routes):
            o, d = route["origin"], route["destination"]
            weeks = horizon_for_slot(len(cfg["routes"]), today, slot)
            dep, ret = snapshot_dates(today, horizon_weeks=weeks)
            summary["searched"] += 1
            try:
                payload = search_google_flights(o, d, dep, ret)
            except SerpApiError as exc:
                log.error("Snapshot failed %s→%s: %s", o, d, exc)
                summary["errors"] += 1
                continue
            offer = parse_full_service(payload, o, d, dep, ret)
            if offer is None:
                log.info("No all-full-service itinerary %s→%s %s", o, d, dep)
                continue
            store.record(offer)
            summary["recorded"] += 1
            log.info("FSC snapshot %s→%s %s: %.0f TWD (%s)",
                     o, d, dep, offer.price, offer.carriers)
            time.sleep(1)
    finally:
        store.close()
    log.info("Snapshot summary: %s", summary)
    return summary


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = run(sys.argv[1] if len(sys.argv) > 1 else "config.yaml",
            sys.argv[2] if len(sys.argv) > 2 else "prices.db")
    print(f"快照完成: 查詢 {s['searched']} 次, 記錄 {s['recorded']} 筆, 錯誤 {s['errors']} 次")
