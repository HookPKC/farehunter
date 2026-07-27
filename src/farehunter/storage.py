"""SQLite storage for price observations and sent alerts."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from .models import Offer

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    origin      TEXT NOT NULL,
    destination TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    return_date TEXT,
    price       REAL NOT NULL,
    currency    TEXT NOT NULL,
    carriers    TEXT,
    stops       INTEGER,
    duration    TEXT,
    observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_route
    ON observations (origin, destination, depart_date);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    origin      TEXT NOT NULL,
    destination TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    price       REAL NOT NULL,
    reason      TEXT NOT NULL,
    sent_at     TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str = "prices.db"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # migration: fare_class distinguishes cheapest-overall from full-service
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(observations)")]
        if "fare_class" not in cols:
            self.conn.execute(
                "ALTER TABLE observations ADD COLUMN fare_class TEXT DEFAULT 'any'")
        if "source" not in cols:
            # migration: source distinguishes aviasales cache from google real prices
            self.conn.execute(
                "ALTER TABLE observations ADD COLUMN source TEXT NOT NULL DEFAULT 'aviasales'")
        if "provider" not in cols:
            # migration: provider records the EXACT API behind each row so every
            # recommendation is traceable to serpapi/scrapedo/searchapi/travelpayouts
            self.conn.execute(
                "ALTER TABLE observations ADD COLUMN provider TEXT")
        # migration: alert trip identity。原本 dedup 只用 (route, depart),實測
        # 已有 6 個 (route, depart) 擁有 2–4 個不同回程日,不同行程會互相阻擋。
        # 全部允許 NULL:歷史列沒有這些資訊,且不得猜測回填。
        acols = [r[1] for r in self.conn.execute("PRAGMA table_info(alerts)")]
        for col, ddl in (
            ("return_date", "ALTER TABLE alerts ADD COLUMN return_date TEXT"),
            ("carrier_signature",
             "ALTER TABLE alerts ADD COLUMN carrier_signature TEXT"),
            ("price_source", "ALTER TABLE alerts ADD COLUMN price_source TEXT"),
            ("price_status", "ALTER TABLE alerts ADD COLUMN price_status TEXT"),
            ("reference_price",
             "ALTER TABLE alerts ADD COLUMN reference_price REAL"),
            ("reference_observed_at",
             "ALTER TABLE alerts ADD COLUMN reference_observed_at TEXT"),
        ):
            if col not in acols:
                self.conn.execute(ddl)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ---- observations ------------------------------------------------------
    def record(self, offer: Offer) -> None:
        self.conn.execute(
            """INSERT INTO observations
               (origin, destination, depart_date, return_date, price,
                currency, carriers, stops, duration, observed_at, fare_class,
                source, provider)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (offer.origin, offer.destination, offer.depart_date,
             offer.return_date, offer.price, offer.currency,
             offer.carriers, offer.stops, offer.duration,
             datetime.now(timezone.utc).isoformat(timespec="seconds"),
             offer.fare_class, offer.source, offer.provider),
        )
        self.conn.commit()

    def route_stats(self, origin: str, destination: str) -> dict:
        """Historical stats across ALL departure dates for a route."""
        row = self.conn.execute(
            """SELECT COUNT(*) AS n, MIN(price) AS min_price, AVG(price) AS avg_price
               FROM observations WHERE origin=? AND destination=? AND fare_class='any'""",
            (origin, destination),
        ).fetchone()
        median = None
        if row["n"]:
            prices = [r["price"] for r in self.conn.execute(
                "SELECT price FROM observations WHERE origin=? AND destination=? "
                "AND fare_class='any' ORDER BY price",
                (origin, destination))]
            mid = len(prices) // 2
            median = prices[mid] if len(prices) % 2 else (prices[mid - 1] + prices[mid]) / 2
        return {"n": row["n"], "min": row["min_price"],
                "avg": row["avg_price"], "median": median}

    # ---- alert dedup ---------------------------------------------------------
    def latest_itinerary_google(self, *, origin: str, destination: str,
                                depart_date: str, return_date: str | None,
                                stops: int, fare_class: str, currency: str,
                                not_after: str):
        """同航程、observed_at 不晚於 not_after 的最新一筆 google 觀測。

        供 Alert 的價格狀態解析使用（見 price_state）。比對鍵刻意保守:
        route + 去回程日 + stops + fare_class + currency 全等,carrier 由
        呼叫端以 carrier_signature 再比一次(本查詢不做 carrier 過濾,因為
        carriers 欄位可能是逗號組合,字串比對不可靠)。

        return_date 為 None 時直接回 None——單程無法與來回總價相比。
        """
        if not return_date:
            return None
        return self.conn.execute(
            """SELECT id, origin, destination, depart_date, return_date, price,
                      currency, carriers, stops, fare_class, source, provider,
                      observed_at
               FROM observations
               WHERE origin=? AND destination=? AND depart_date=? AND return_date=?
                 AND stops=? AND fare_class=? AND currency=? AND source='google'
                 AND observed_at <= ?
               ORDER BY observed_at DESC, id DESC LIMIT 1""",
            (origin, destination, depart_date, return_date, stops, fare_class,
             currency, not_after),
        ).fetchone()

    #: 各價格狀態的 dedup 窗。CONFLICT 較長,避免同一個未變化的落差每天重複騷擾。
    DEDUP_HOURS = {"verified": 24, "conflict": 72, "unverified": 24}

    def recently_alerted(self, origin: str, destination: str,
                         depart_date: str, price: float,
                         within_hours: int = 24,
                         improvement_pct: float = 10.0,
                         *, return_date: str | None = None,
                         carrier_signature: str | None = None,
                         price_status: str | None = None,
                         reference_price: float | None = None) -> bool:
        """True 表示「近期已通知過同一行程且無顯著變化」,應跳過。

        identity 至少為 (origin, destination, depart_date, return_date,
        carrier_signature, price_status)。不同回程日、不同 carrier、不同狀態
        各自獨立 dedup,不互相阻擋。

        向後相容:歷史列的 return_date / carrier_signature 為 NULL,SQL 等值
        比對自然不會匹配帶有明確行程的新 alert——即舊資料不會錯誤抑制新的明確
        行程(規格要求)。不猜測回填舊列。

        允許重新通知:價格改善 >= improvement_pct、狀態改變(例如升級為
        verified)、或 CONFLICT 的參考價出現同等幅度的變化。
        """
        hours = self.DEDUP_HOURS.get((price_status or "").lower(), within_hours)
        row = self.conn.execute(
            """SELECT price, reference_price FROM alerts
               WHERE origin=? AND destination=? AND depart_date=?
                 AND return_date IS ? AND carrier_signature IS ?
                 AND price_status IS ?
                 AND sent_at >= datetime('now', ?)
               ORDER BY sent_at DESC LIMIT 1""",
            (origin, destination, depart_date, return_date, carrier_signature,
             price_status, f"-{hours} hours"),
        ).fetchone()
        if row is None:
            return False
        if price <= row["price"] * (1 - improvement_pct / 100.0):
            return False                      # 顯著更便宜 → 重新通知
        prev_ref, new_ref = row["reference_price"], reference_price
        if prev_ref and new_ref and \
                abs(new_ref - prev_ref) >= prev_ref * (improvement_pct / 100.0):
            return False                      # 參考價有意義變化 → 重新通知
        return True

    def record_longrange(self, origin: str, destination: str, depart_date: str,
                         return_date: str, total: float,
                         out_price: float, ret_price: float) -> None:
        """Long-horizon one-way-sum roundtrip estimates (upsert per date)."""
        self.conn.execute("""CREATE TABLE IF NOT EXISTS long_range (
            origin TEXT NOT NULL, destination TEXT NOT NULL,
            depart_date TEXT NOT NULL, return_date TEXT NOT NULL,
            total REAL NOT NULL, out_price REAL NOT NULL, ret_price REAL NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (origin, destination, depart_date))""")
        self.conn.execute(
            """INSERT INTO long_range VALUES (?,?,?,?,?,?,?,datetime('now'))
               ON CONFLICT(origin, destination, depart_date) DO UPDATE SET
                 return_date=excluded.return_date, total=excluded.total,
                 out_price=excluded.out_price, ret_price=excluded.ret_price,
                 updated_at=excluded.updated_at""",
            (origin, destination, depart_date, return_date, total, out_price, ret_price))
        self.conn.commit()

    def record_insight(self, origin: str, destination: str, depart_date: str,
                       price_level: str, typical_low: float | None,
                       typical_high: float | None) -> None:
        """Google price_insights per route — latest wins (upsert)."""
        self.conn.execute("""CREATE TABLE IF NOT EXISTS route_insights (
            origin TEXT NOT NULL, destination TEXT NOT NULL,
            depart_date TEXT NOT NULL, price_level TEXT NOT NULL,
            typical_low REAL, typical_high REAL, updated_at TEXT NOT NULL,
            PRIMARY KEY (origin, destination))""")
        self.conn.execute(
            """INSERT INTO route_insights VALUES (?,?,?,?,?,?,datetime('now'))
               ON CONFLICT(origin, destination) DO UPDATE SET
                 depart_date=excluded.depart_date, price_level=excluded.price_level,
                 typical_low=excluded.typical_low, typical_high=excluded.typical_high,
                 updated_at=excluded.updated_at""",
            (origin, destination, depart_date, price_level, typical_low, typical_high))
        self.conn.commit()

    def record_alert(self, origin: str, destination: str,
                     depart_date: str, price: float, reason: str,
                     *, return_date: str | None = None,
                     carrier_signature: str | None = None,
                     price_source: str | None = None,
                     price_status: str | None = None,
                     reference_price: float | None = None,
                     reference_observed_at: str | None = None) -> None:
        """寫入 alert 事件。

        新增欄位皆為選填,呼叫端未提供時寫入 NULL,與歷史列語意一致。
        不保存 reference_carrier_signature——同航程比對要求兩側 carrier
        signature 相等,故它恆等於 carrier_signature 欄位,無稽核價值。
        """
        self.conn.execute(
            "INSERT INTO alerts (origin, destination, depart_date, price, reason,"
            " sent_at, return_date, carrier_signature, price_source, price_status,"
            " reference_price, reference_observed_at)"
            " VALUES (?,?,?,?,?,datetime('now'),?,?,?,?,?,?)",
            (origin, destination, depart_date, price, reason, return_date,
             carrier_signature, price_source, price_status, reference_price,
             reference_observed_at),
        )
        self.conn.commit()
