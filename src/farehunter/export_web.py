"""Export prices.db into docs/data.json for the web dashboard (GitHub Pages)."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def export(db_path: str = "prices.db", out_path: str = "docs/data.json") -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    routes = []
    route_rows = conn.execute(
        "SELECT DISTINCT origin, destination FROM observations ORDER BY origin, destination"
    ).fetchall()

    for rr in route_rows:
        o, d = rr["origin"], rr["destination"]

        # stats over full history
        srow = conn.execute(
            "SELECT COUNT(*) n, MIN(price) mn, AVG(price) av FROM observations "
            "WHERE origin=? AND destination=? AND fare_class='any'", (o, d)).fetchone()
        prices = [r["price"] for r in conn.execute(
            "SELECT price FROM observations WHERE origin=? AND destination=? "
            "AND fare_class='any' ORDER BY price", (o, d))]
        mid = len(prices) // 2
        median = prices[mid] if len(prices) % 2 else (prices[mid - 1] + prices[mid]) / 2

        # fare calendar: per FUTURE departure date, prefer a fresh (<=8d)
        # google real price; otherwise the most recent aviasales cache price
        latest = [dict(r) for r in conn.execute(
            """WITH ranked AS (
                 SELECT depart_date, return_date, price, currency, carriers,
                        stops, observed_at, source,
                        ROW_NUMBER() OVER (
                          PARTITION BY depart_date
                          ORDER BY (source='google'
                                    AND observed_at >= datetime('now','-8 days')) DESC,
                                   observed_at DESC) AS rk
                 FROM observations
                 WHERE origin=? AND destination=? AND fare_class='any'
                   AND depart_date >= date('now'))
               SELECT depart_date, return_date, price, currency, carriers,
                      stops, observed_at, source
               FROM ranked WHERE rk=1
               ORDER BY depart_date LIMIT 24""", (o, d))]

        # full-service (華航/長榮等) cheapest per date, attached to the same calendar
        full = {r["depart_date"]: r for r in conn.execute(
            """SELECT depart_date, price, carriers, MAX(observed_at) AS observed_at
               FROM observations
               WHERE origin=? AND destination=? AND depart_date >= date('now')
                 AND fare_class='full'
               GROUP BY depart_date""", (o, d))}
        for item in latest:
            f = full.get(item["depart_date"])
            if f:
                item["full_price"] = f["price"]
                item["full_carriers"] = f["carriers"]

        # freshest full-service snapshot (last 7 days), independent of calendar match
        fsc_latest = conn.execute(
            """SELECT depart_date, return_date, price, carriers, observed_at
               FROM observations
               WHERE origin=? AND destination=? AND fare_class='full'
                 AND depart_date >= date('now')
                 AND observed_at >= datetime('now', '-7 days')
               ORDER BY price ASC LIMIT 1""", (o, d)).fetchone()

        # trend: daily minimum across all departure dates
        history = [dict(r) for r in conn.execute(
            """SELECT substr(observed_at, 1, 10) AS day, MIN(price) AS min_price
               FROM observations WHERE origin=? AND destination=? AND fare_class='any'
               GROUP BY day ORDER BY day""", (o, d))]

        routes.append({
            "origin": o, "destination": d,
            "stats": {"n": srow["n"], "min": srow["mn"],
                      "avg": round(srow["av"], 1) if srow["av"] else None,
                      "median": median},
            "latest": latest,
            "fsc_latest": dict(fsc_latest) if fsc_latest else None,
            "history": history,
        })

    alerts = [dict(r) for r in conn.execute(
        """SELECT origin, destination, depart_date, price, reason, sent_at
           FROM alerts ORDER BY sent_at DESC LIMIT 20""")]
    alerts_24h = conn.execute(
        "SELECT COUNT(*) c FROM alerts WHERE sent_at >= datetime('now','-24 hours')"
    ).fetchone()["c"]
    total_obs = conn.execute("SELECT COUNT(*) c FROM observations").fetchone()["c"]
    conn.close()

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "env": os.environ.get("DATA_SOURCE_LABEL", "aviasales"),
        "totals": {"observations": total_obs, "routes": len(routes),
                   "alerts_24h": alerts_24h},
        "routes": routes,
        "alerts": alerts,
    }
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return payload


if __name__ == "__main__":
    import sys
    db = sys.argv[1] if len(sys.argv) > 1 else "prices.db"
    out = sys.argv[2] if len(sys.argv) > 2 else "docs/data.json"
    p = export(db, out)
    print(f"exported {p['totals']['observations']} observations, "
          f"{p['totals']['routes']} routes -> {out}")
