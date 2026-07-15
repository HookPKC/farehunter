"""Export prices.db into docs/data.json for the web dashboard (GitHub Pages)."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

try:                                    # works both as script and as package module
    from farehunter.models import NEAR_TERM_DAYS
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from farehunter.models import NEAR_TERM_DAYS


# 首頁「權威近期價格日曆」的單一真相來源(SSOT)。每個 depart_date 一格,
# 排序規則:14 天內的 fresh google 無條件優先,其次最新觀測。Hero 大字 =
# 本清單價格最低那格(前端 index.html reduce 同義)。FSC 的 Hero 驗證候選
# 必須呼叫本 helper,不得複製另一份 SQL,以免與首頁顯示漂移。
# 視窗與排序若要修改,改這裡一處即可,export 與 FSC 同步生效。
_AUTHORITATIVE_LATEST_SQL = """WITH ranked AS (
                 SELECT depart_date, return_date, price, currency, carriers,
                        stops, observed_at, source,
                        ROW_NUMBER() OVER (
                          PARTITION BY depart_date
                          ORDER BY (source='google'
                                    AND observed_at >= datetime('now','-14 days')) DESC,
                                   observed_at DESC, id DESC) AS rk
                 FROM observations
                 WHERE origin=? AND destination=? AND fare_class='any' AND stops=0
                   AND depart_date >= date('now','start of month','+1 month')
                   AND depart_date >= date('now','+21 days')
                   AND depart_date <= date('now','+{near_term_days} days'))
               SELECT depart_date, return_date, price, currency, carriers,
                      stops, observed_at, source
               FROM ranked WHERE rk=1
               ORDER BY depart_date LIMIT 100"""


def authoritative_latest(conn, o: str, d: str,
                         near_term_days: int = NEAR_TERM_DAYS) -> list[dict]:
    """回傳某航線近期價格日曆:每個 depart_date 一格權威價(dict list)。
    這是 Hero/chips 的單一真相來源;export 與 FSC 共用此函式。"""
    return [dict(r) for r in conn.execute(
        _AUTHORITATIVE_LATEST_SQL.format(near_term_days=near_term_days), (o, d))]


def hero_from_latest(latest: list[dict]) -> dict | None:
    """從權威日曆取 Hero 那一格:價格最低者。對應前端 index.html 的
    `latest.reduce((a,b)=>a.price<=b.price?a:b)`。空清單回 None。"""
    return min(latest, key=lambda x: x["price"]) if latest else None


def _route_insight(conn, o, d):
    try:
        row = conn.execute(
            """SELECT depart_date, price_level, typical_low, typical_high
               FROM route_insights WHERE origin=? AND destination=?
                 AND updated_at >= datetime('now','-14 days')""", (o, d)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None      # 表尚未建立（快照還沒跑過）


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

        # fare calendar chips: a genuine NEAR-TERM calendar. Same window + same
        # per-date dedup ordering as intelligence._SELECT (see NEAR_TERM_DAYS) so
        # Hero/Cheapest here == Recommended cheapest there, by construction. Far
        # months live in the monthly strip, not here.
        latest = authoritative_latest(conn, o, d)

        # google-sourced chips carry no airline; attach the aviasales
        # reference airline seen for the same departure date (approximate)
        route_common = conn.execute(
            """SELECT carriers FROM observations
               WHERE origin=? AND destination=? AND fare_class='any'
                 AND carriers != '' AND stops=0
                 AND observed_at >= datetime('now','-30 days')
               GROUP BY carriers ORDER BY COUNT(*) DESC LIMIT 1""",
            (o, d)).fetchone()
        for item in latest:
            if item.get("source") == "google" and not item.get("carriers"):
                ref = conn.execute(
                    """SELECT carriers FROM observations
                       WHERE origin=? AND destination=? AND fare_class='any'
                         AND carriers != ''
                         AND abs(julianday(depart_date) - julianday(?)) <= 3
                       ORDER BY abs(julianday(depart_date) - julianday(?)) ASC,
                                observed_at DESC, rowid DESC LIMIT 1""",
                    (o, d, item["depart_date"], item["depart_date"])).fetchone()
                if ref:
                    item["ref_carriers"] = ref["carriers"]
                elif route_common:
                    item["ref_carriers"] = route_common["carriers"]

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

        # monthly low across the planning horizon. Two-stage so a stale cache low
        # can't beat a current real price: (1) per departure date, take the most
        # authoritative fare — a fresh (<=14d) google/verified real price wins over
        # cache regardless of being higher; (2) per month, take the cheapest date.
        monthly = [dict(r) for r in conn.execute(
            """WITH per_date AS (
                 SELECT depart_date, return_date, price, carriers, source,
                        ROW_NUMBER() OVER (
                          PARTITION BY depart_date
                          ORDER BY (source='google'
                                    AND observed_at >= datetime('now','-14 days')) DESC,
                                   observed_at DESC, rowid DESC) AS rk
                 FROM observations
                 WHERE origin=? AND destination=? AND fare_class='any' AND stops=0
                   AND depart_date BETWEEN date('now') AND date('now','+330 days')),
               best_date AS (SELECT * FROM per_date WHERE rk=1),
               per_month AS (
                 SELECT substr(depart_date, 1, 7) AS ym, depart_date, return_date,
                        price, carriers, source,
                        ROW_NUMBER() OVER (PARTITION BY substr(depart_date, 1, 7)
                                           ORDER BY price ASC) AS mrk
                 FROM best_date)
               SELECT ym, depart_date, return_date, price, carriers, source
               FROM per_month WHERE mrk=1 ORDER BY ym""", (o, d))]

        routes.append({
            "origin": o, "destination": d,
            "stats": {"n": srow["n"], "min": srow["mn"],
                      "avg": round(srow["av"], 1) if srow["av"] else None,
                      "median": median},
            "latest": latest,
            "fsc_latest": dict(fsc_latest) if fsc_latest else None,
            "insight": _route_insight(conn, o, d),
            "monthly": monthly,
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
