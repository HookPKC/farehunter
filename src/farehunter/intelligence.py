"""FIE v2 — Intelligence orchestrator.

Reads the existing SQLite warehouse (no extra API calls, pipeline untouched),
normalizes stored observations into the unified schema, ranks each route with
the freshness + reliability aware engine, and writes docs/ranked.json.

data.json (consumed by the current frontend) is NOT modified — ranked.json is
a new, additive artifact. Frontend adoption is optional and later.

Usage: python -m farehunter.intelligence [prices.db] [docs/ranked.json]
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .normalize import from_observation, is_valid
from .ranking import rank, BALANCED
from .reliability import ReliabilityStore

log = logging.getLogger(__name__)

# Only rank realistically bookable, direct future departures (mirrors the
# frontend chip policy: skip current month / too-soon).
_SELECT = """
WITH ranked AS (
  SELECT origin, destination, depart_date, return_date, price, currency,
         carriers, stops, duration, observed_at, source,
         ROW_NUMBER() OVER (
           PARTITION BY origin, destination, depart_date
           ORDER BY (source='google' AND observed_at >= datetime('now','-14 days')) DESC,
                    observed_at DESC, id DESC) AS rk
  FROM observations
  WHERE fare_class='any' AND stops=0
    AND depart_date >= date('now','start of month','+1 month')
    AND depart_date >= date('now','+21 days')
    AND depart_date <= date('now','+330 days'))
SELECT origin, destination, depart_date, return_date, price, currency,
       carriers, stops, duration, observed_at, source
FROM ranked WHERE rk=1
ORDER BY origin, destination, depart_date
"""


def build_ranked(db_path: str = "prices.db", weights=BALANCED) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rel = ReliabilityStore(conn)
    routes: dict[str, list] = {}
    for row in conn.execute(_SELECT):
        offer = from_observation(row)
        if is_valid(offer):
            routes.setdefault(offer.route, []).append(offer)
    # snapshot reliability per source BEFORE closing the connection
    sources = {o.source for offers in routes.values() for o in offers}
    rel_map = {s: rel.reliability(s) for s in sources}
    conn.close()
    reliability_of = lambda s: rel_map.get(s, 0.6)

    out_routes = []
    for route, offers in routes.items():
        rr = rank(offers, weights, reliability_of=reliability_of)
        origin, destination = route.split("-")
        out_routes.append({
            "route": route, "origin": origin, "destination": destination,
            "n_offers": len(offers),
            **rr.to_dict(),
        })
    out_routes.sort(key=lambda r: r["route"])
    return {
        "schema": "fie-v2",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "weights": weights.normalized().__dict__,
        "routes": out_routes,
    }


def export(db_path: str = "prices.db", out_path: str = "docs/ranked.json") -> dict:
    data = build_ranked(db_path)
    Path(out_path).write_text(json.dumps(data, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    total = sum(len(r["ranked_results"]) for r in data["routes"])
    log.info("ranked.json: %d routes, %d ranked offers", len(data["routes"]), total)
    return data


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    d = export(sys.argv[1] if len(sys.argv) > 1 else "prices.db",
               sys.argv[2] if len(sys.argv) > 2 else "docs/ranked.json")
    print(f"FIE v2 排名輸出: {len(d['routes'])} 條航線")
