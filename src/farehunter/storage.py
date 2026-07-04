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
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ---- observations ------------------------------------------------------
    def record(self, offer: Offer) -> None:
        self.conn.execute(
            """INSERT INTO observations
               (origin, destination, depart_date, return_date, price,
                currency, carriers, stops, duration, observed_at, fare_class, source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (offer.origin, offer.destination, offer.depart_date,
             offer.return_date, offer.price, offer.currency,
             offer.carriers, offer.stops, offer.duration,
             datetime.now(timezone.utc).isoformat(timespec="seconds"),
             offer.fare_class, offer.source),
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
    def recently_alerted(self, origin: str, destination: str,
                         depart_date: str, price: float,
                         within_hours: int = 24,
                         improvement_pct: float = 5.0) -> bool:
        """True if we already alerted this route+date in the window,
        unless the new price improves on the alerted price by >= improvement_pct."""
        row = self.conn.execute(
            """SELECT price FROM alerts
               WHERE origin=? AND destination=? AND depart_date=?
                 AND sent_at >= datetime('now', ?)
               ORDER BY sent_at DESC LIMIT 1""",
            (origin, destination, depart_date, f"-{within_hours} hours"),
        ).fetchone()
        if row is None:
            return False
        return price > row["price"] * (1 - improvement_pct / 100.0)

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
                     depart_date: str, price: float, reason: str) -> None:
        self.conn.execute(
            "INSERT INTO alerts (origin, destination, depart_date, price, reason, sent_at)"
            " VALUES (?,?,?,?,?,datetime('now'))",
            (origin, destination, depart_date, price, reason),
        )
        self.conn.commit()
