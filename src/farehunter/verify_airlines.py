"""Daily airline verification. Usage: python -m farehunter.verify_airlines"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .scrapedo_flights import (search_flights, parse_cheapest_direct,
                               VERIFICATIONS_PER_DAY, ScrapeDoError)
from .storage import Store

log = logging.getLogger(__name__)


def pick_candidates(store: Store, limit: int = VERIFICATIONS_PER_DAY) -> list[dict]:
    """Cheapest unverified google-priced future dates, max one per route."""
    rows = store.conn.execute(
        """WITH latest_google AS (
             SELECT origin, destination, depart_date, return_date, price, carriers,
                    ROW_NUMBER() OVER (PARTITION BY origin, destination, depart_date
                                       ORDER BY observed_at DESC, rowid DESC) AS rk
             FROM observations
             WHERE source='google' AND fare_class='any'
               AND depart_date BETWEEN date('now','+1 day') AND date('now','+330 days')),
           unverified AS (
             SELECT *, ROW_NUMBER() OVER (PARTITION BY origin, destination
                                          ORDER BY price ASC) AS pr
             FROM latest_google WHERE rk=1 AND carriers='' AND return_date != '')
           SELECT origin, destination, depart_date, return_date, price
           FROM unverified WHERE pr=1 ORDER BY price ASC LIMIT ?""",
        (limit,)).fetchall()
    return [dict(r) for r in rows]


def run(db_path: str = "prices.db") -> dict:
    store = Store(db_path)
    summary = {"searched": 0, "verified": 0, "errors": 0}
    report = {"errors": [], "probe": None, "verified": []}
    try:
        for cand in pick_candidates(store):
            o, d = cand["origin"], cand["destination"]
            summary["searched"] += 1
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
                f"{o}→{d} {offer.depart_date} {offer.price:.0f} {offer.carriers}"
                f"（日曆價 {cand['price']:.0f}）")
            log.info("Verified %s→%s %s: %.0f TWD %s",
                     o, d, offer.depart_date, offer.price, offer.carriers)
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
    print(f"航空驗證完成: 查詢 {s['searched']} 次, 確認 {s['verified']} 筆, 錯誤 {s['errors']} 次")
