"""Monthly long-range sweep: 180-day one-way calendars both directions,
combined into cheapest 4-6 night roundtrip sums per departure date.
Usage: python -m farehunter.longrange_sweep"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, timedelta
from pathlib import Path

from .runner import load_config
from .searchapi_calendar import (fetch_oneway_calendar, parse_oneway_prices,
                                 combine_roundtrips, SearchApiError)
from .storage import Store

log = logging.getLogger(__name__)

HORIZON_DAYS = 180


def run(config_path: str = "config.yaml", db_path: str = "prices.db") -> dict:
    cfg = load_config(config_path)
    store = Store(db_path)
    summary = {"searched": 0, "dates_covered": 0, "errors": 0}
    report = {"errors": [], "probe": None}
    start = date.today() + timedelta(days=1)
    end = start + timedelta(days=HORIZON_DAYS - 1)
    try:
        for route in cfg["routes"]:
            o, d = route["origin"], route["destination"]
            legs = {}
            for a, b, tag in ((o, d, "out"), (d, o, "back")):
                summary["searched"] += 1
                try:
                    payload = fetch_oneway_calendar(a, b, start, end)
                except SearchApiError as exc:
                    log.error("One-way calendar failed %s→%s: %s", a, b, exc)
                    summary["errors"] += 1
                    report["errors"].append(f"{a}→{b}: {exc}")
                    break
                if report["probe"] is None:
                    report["probe"] = {"keys": sorted(payload.keys()),
                                       "rows": len(payload.get("calendar") or [])}
                legs[tag] = parse_oneway_prices(payload)
                time.sleep(1)
            if "out" not in legs or "back" not in legs:
                continue
            combos = combine_roundtrips(legs["out"], legs["back"])
            for c in combos:
                store.record_longrange(o, d, c["depart_date"], c["return_date"],
                                       c["total"], c["out_price"], c["ret_price"])
            summary["dates_covered"] += len(combos)
            log.info("Long-range %s→%s: %d dates, cheapest %s",
                     o, d, len(combos),
                     min(combos, key=lambda x: x["total"]) if combos else "-")
    finally:
        store.close()
    report["summary"] = summary
    Path("docs/longrange-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("Long-range summary: %s", summary)
    return summary


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = run(sys.argv[1] if len(sys.argv) > 1 else "config.yaml",
            sys.argv[2] if len(sys.argv) > 2 else "prices.db")
    print(f"長程掃描完成: 查詢 {s['searched']} 次, 覆蓋 {s['dates_covered']} 個出發日, "
          f"錯誤 {s['errors']} 次")
