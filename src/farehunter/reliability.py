"""FIE v2 — Provider reliability layer.

Two signals combine into a 0..1 reliability score per provider:
  1. Base reliability (static config, our prior trust in the source).
  2. Dynamic success rate from a provider_stats table, updated whenever
     ProviderManager performs a live query.
When no dynamic stats exist yet, the base score is used (warehouse-derived
recency can also be consulted via last_success_age).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

# Static priors: real-price itinerary sources rank above cache aggregators.
BASE_RELIABILITY = {
    "serpapi": 0.90,
    "scrapedo": 0.85,
    "searchapi": 0.80,
    "travelpayouts": 0.55,
    "aviasales": 0.55,     # warehouse tag for Travelpayouts cache
    "google": 0.80,        # warehouse tag for real-price capture
}
DEFAULT_BASE = 0.60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_stats (
    source      TEXT PRIMARY KEY,
    ok_count    INTEGER NOT NULL DEFAULT 0,
    fail_count  INTEGER NOT NULL DEFAULT 0,
    last_ok_at  TEXT,
    last_fail_at TEXT
);
"""


def base_reliability(source: str) -> float:
    return BASE_RELIABILITY.get((source or "").lower(), DEFAULT_BASE)


class ReliabilityStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.executescript(_SCHEMA)

    def record(self, source: str, ok: bool) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT INTO provider_stats(source, ok_count, fail_count, last_ok_at, last_fail_at) "
            "VALUES(?, ?, ?, ?, ?) ON CONFLICT(source) DO UPDATE SET "
            "ok_count = ok_count + ?, fail_count = fail_count + ?, "
            "last_ok_at = COALESCE(?, last_ok_at), last_fail_at = COALESCE(?, last_fail_at)",
            (source, 1 if ok else 0, 0 if ok else 1,
             now if ok else None, None if ok else now,
             1 if ok else 0, 0 if ok else 1,
             now if ok else None, None if ok else now))
        self.conn.commit()

    def stats(self, source: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT ok_count, fail_count, last_ok_at, last_fail_at "
            "FROM provider_stats WHERE source=?", (source,)).fetchone()
        if not row:
            return None
        ok, fail = row[0], row[1]
        total = ok + fail
        return {"ok": ok, "fail": fail, "total": total,
                "success_rate": (ok / total) if total else None,
                "last_ok_at": row[2], "last_fail_at": row[3]}

    def reliability(self, source: str) -> float:
        """Blend static base with observed success rate (weighted by volume)."""
        base = base_reliability(source)
        st = self.stats(source)
        if not st or not st["total"]:
            return base
        rate = st["success_rate"]
        # confidence grows with sample size, capped at 20 observations
        conf = min(st["total"] / 20.0, 1.0)
        return round(base * (1 - conf) + rate * conf, 4)
