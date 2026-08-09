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
        """整條航線的歷史統計（跨所有出發日）。供 new_low 使用。

        「這條航線史上最便宜」是一個極值事件——罕見、明確、值得打擾使用者。
        實測 5 週只觸發 3 次。這個語意必須用整條航線的資料才成立；改成單日
        基準會讓它退化成「這個日期又刷新自己的紀錄」，實測暴增到 147 次。
        """
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

    def route_stats_by_date(self, origin: str, destination: str) -> dict:
        """每個出發日各自的歷史統計：{depart_date: {n, min, avg, median}}。供 big_drop 使用。

        big_drop 問的是「這一天相對於它自己平常的價格，是不是反常地便宜」，
        所以基準必須是單日。原本比的是整條航線的中位數，但同一條航線不同出發
        日的均價差距極大——TPE→KIX 從 5,853 到 45,862（7.8 倍），TPE→NRT 從
        6,019 到 25,354（4.2 倍）。淡旺季混算出的中位數對任何一天都不具代表性：
        實測 100 則 big_drop 通知，100 則的價格都高於該航線設定的門檻。

        歷史不足 min_history 的出發日不觸發 big_drop（absolute 與 new_low
        不受影響），這正是期望行為。
        """
        out: dict[str, dict] = {}
        cur_date: Optional[str] = None
        prices: list[float] = []

        def _flush() -> None:
            if cur_date is None or not prices:
                return
            n = len(prices)
            mid = n // 2
            median = prices[mid] if n % 2 else (prices[mid - 1] + prices[mid]) / 2
            out[cur_date] = {"n": n, "min": prices[0],
                             "avg": sum(prices) / n, "median": median}

        # 依 (depart_date, price) 排序 → 同一天的價格已排好，中位數直接取中間值
        for row in self.conn.execute(
                """SELECT depart_date, price FROM observations
                   WHERE origin=? AND destination=? AND fare_class='any'
                   ORDER BY depart_date, price""",
                (origin, destination)):
            if row["depart_date"] != cur_date:
                _flush()
                cur_date, prices = row["depart_date"], []
            prices.append(row["price"])
        _flush()
        return out

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

    #: 抑制窗（天）。舊版是 24 小時（CONFLICT 72 小時），意思是「同一天內不重複」,
    #: 但過了窗又 1 分鐘,一模一樣的價格就再叫一次。實測 TPE→NRT 2026-09-15 在
    #: 07-29~08-04 連續六天收到同一個 5,623；220 則通知只涵蓋 56 個行程（平均每
    #: 個行程被叫 3.9 次,最慘的一個 24 次）。價格沒變就不是新消息,窗改長。
    SUPPRESS_DAYS = 30

    #: dedup 分桶。verified 自成一桶——「已驗證」比「疑似」更有把握,值得再說
    #: 一次。unverified 與 conflict 共用一桶:兩者的差別只在於當下有沒有一筆
    #: 落在 48 小時參考窗內的 Google 觀測,價格本身沒有變。實測 TPE→NRT
    #: 2026-09-17 的 5,963 就是因為 status 在這兩者間擺動而重複發了 5 次。
    _UNCONFIRMED = ("unverified", "conflict")

    @classmethod
    def _status_bucket(cls, price_status: str | None) -> tuple[str, tuple]:
        """回傳 (SQL 條件片段, 參數)。片段只由本方法的字面量組成,不含外部輸入。"""
        if price_status is None:
            return "price_status IS NULL", ()          # 歷史列:僅與 NULL 相符
        if price_status.lower() == "verified":
            return "LOWER(price_status) = ?", ("verified",)
        return "LOWER(price_status) IN (?, ?)", cls._UNCONFIRMED

    def recently_alerted(self, origin: str, destination: str,
                         depart_date: str, price: float,
                         within_hours: int = SUPPRESS_DAYS * 24,
                         improvement_pct: float = 10.0,
                         *, return_date: str | None = None,
                         carrier_signature: str | None = None,
                         price_status: str | None = None,
                         reference_price: float | None = None) -> bool:
        """True 表示「已通知過同一行程且沒有更好的消息」,應跳過。

        identity 至少為 (origin, destination, depart_date, return_date,
        carrier_signature, price_status)。不同回程日、不同 carrier、不同狀態
        各自獨立 dedup,不互相阻擋。

        比較基準是**窗內最便宜的已通知價**,不是最近一則。否則價格在窗內來回
        波動時（8000 → 7000 → 7600）會把基準推回較貴的那一筆,同一個行程又能
        重新通知一輪。以最低價為基準,只有真正更好的消息才會再叫。

        向後相容:歷史列的 return_date / carrier_signature 為 NULL,SQL 等值
        比對自然不會匹配帶有明確行程的新 alert——即舊資料不會錯誤抑制新的明確
        行程(規格要求)。不猜測回填舊列。

        允許重新通知:價格較基準改善 >= improvement_pct、狀態改變(例如升級為
        verified)、或 CONFLICT 的參考價出現同等幅度的變化。
        """
        status_clause, status_params = self._status_bucket(price_status)
        row = self.conn.execute(
            f"""SELECT price, reference_price FROM alerts
                WHERE origin=? AND destination=? AND depart_date=?
                  AND return_date IS ? AND carrier_signature IS ?
                  AND {status_clause}
                  AND sent_at >= datetime('now', ?)
                ORDER BY price ASC, sent_at DESC LIMIT 1""",
            (origin, destination, depart_date, return_date, carrier_signature,
             *status_params, f"-{within_hours} hours"),
        ).fetchone()
        if row is None:
            return False
        if price <= row["price"] * (1 - improvement_pct / 100.0):
            return False                      # 比已通知的最低價再便宜一截 → 重新通知
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
