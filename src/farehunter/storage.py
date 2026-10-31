"""SQLite storage for price observations and sent alerts."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from . import archive
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

-- 便宜日推播的去重紀錄（cheap_day_notify）。刻意不塞進 alerts：
-- alerts 的身分/抑制邏輯是為了三種 reason 調校過的（實測把噪音從每週 42 則
-- 降到 22 則），混進第四種語意會污染那套判斷。每個(航線,出發日)只留最新一次。
--
-- 也刻意寫在 SCHEMA 裡而不是用 CREATE TABLE IF NOT EXISTS 藏在方法內：
-- long_range 就是那樣長出來的，結果沒人知道它存在、累積 1,277 筆零讀者。
CREATE TABLE IF NOT EXISTS cheap_day_notices (
    origin      TEXT NOT NULL,
    destination TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    price       REAL NOT NULL,
    notified_at TEXT NOT NULL,
    PRIMARY KEY (origin, destination, depart_date)
);

-- 外部 API 的用量帳本（每日每來源）。Bright Data 每月 5,000 次免費、用完即停：配速要知道
-- 本月已經用了多少（brightdata_flights.run_allowance）。寫在 prices.db 才會隨每輪 commit
-- 留下時間序列。requests＝送出的次數（含失敗，保守估算），ok＝拿到可解析頁面的次數。
CREATE TABLE IF NOT EXISTS api_usage (
    provider TEXT NOT NULL,
    day      TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    ok       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (provider, day)
);

-- 巡檢最近查過哪些行程（Bright Data 巡檢用，ADR 0004）。查到頁面卻沒有可寫入的直飛價
-- （那天沒有直飛、航空公司對不上）時 observations 裡不會留下任何東西，沒有這張表的話
-- 下一輪又會查同一趟、每小時白花一次額度。每輪巡檢前刪掉超過 24 小時的，所以只有幾百筆。
CREATE TABLE IF NOT EXISTS api_checks (
    provider    TEXT NOT NULL,
    origin      TEXT NOT NULL,
    destination TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    return_date TEXT NOT NULL,
    checked_at  TEXT NOT NULL,
    PRIMARY KEY (provider, origin, destination, depart_date, return_date)
);
""" + archive.MANIFEST_DDL   # 封存檔的筆數帳本，定義與說明在 archive.py


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
        # migration: long_range 是已移除的 SearchApi 長程日曆層留下的表——2026-07-18
        # 一分鐘內寫進 1,277 筆後就沒有任何讀者（HANDOFF §1）。使用者 2026-10-26 同意
        # 刪除；資料仍在 git 歷史的 prices.db 裡（checkout 2026-10-26 以前的版本）。
        # 寫明 main.：此時封存檔還沒 attach，但不靠呼叫順序保證只刪主檔的表。
        self.conn.execute("DROP TABLE IF EXISTS main.long_range")
        self.conn.commit()
        # 一開檔就核對封存檔（ADR 0002）。所有 writer（monitor／FSC／Verify／
        # cheap_day_notify）都在打任何付費 API 之前建 Store，封存檔不完整時在
        # 這裡就停，不會先花掉額度、最後才在 export 報錯把結果丟掉。也確保
        # view 一定建在交易之外（建在交易裡的 TEMP VIEW 會隨 rollback 消失）。
        archive.attach(self.conn)

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

        「整條航線」包含已封存的歷史，所以讀 observations_all（見 archive.py）。
        刻意 fail-closed、不包 try/except：封存檔不完整時少算歷史會讓 new_low
        誤發，寧可整輪失敗（ADR 0002 決定 6）。health 是唯一 fail-open 的讀者。
        """
        archive.attach(self.conn)
        row = self.conn.execute(
            """SELECT COUNT(*) AS n, MIN(price) AS min_price, AVG(price) AS avg_price
               FROM observations_all WHERE origin=? AND destination=? AND fare_class='any'""",
            (origin, destination),
        ).fetchone()
        median = None
        if row["n"]:
            prices = [r["price"] for r in self.conn.execute(
                "SELECT price FROM observations_all WHERE origin=? AND destination=? "
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

        刻意只讀現行的 observations：封存的是出發日已過 30 天的列，而
        big_drop 只拿未來出發日來查，那些日期一筆都不會被封存。
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

    @staticmethod
    def _reason_bucket(reason: str | None) -> tuple[str, tuple]:
        """new_low 自成一桶；其餘（含 NULL 歷史列）共用一桶。

        片段只由本方法的字面量組成,不含外部輸入。
        """
        if (reason or "").lower() == "new_low":
            return "LOWER(reason) = ?", ("new_low",)
        return "(reason IS NULL OR LOWER(reason) <> ?)", ("new_low",)

    def recently_alerted(self, origin: str, destination: str,
                         depart_date: str, price: float,
                         within_hours: int = SUPPRESS_DAYS * 24,
                         improvement_pct: float = 10.0,
                         *, return_date: str | None = None,
                         carrier_signature: str | None = None,
                         price_status: str | None = None,
                         reference_price: float | None = None,
                         reason: str | None = None) -> bool:
        """True 表示「已通知過同一行程且沒有更好的消息」,應跳過。

        identity 至少為 (origin, destination, depart_date, return_date,
        carrier_signature, price_status)。不同回程日、不同 carrier、不同狀態
        各自獨立 dedup,不互相阻擋。

        **價格基準**取窗內最便宜的已通知價,不是最近一則。否則價格在窗內來回
        波動時（8000 → 7000 → 7600）會把基準推回較貴的那一筆,同一個行程又能
        重新通知一輪。

        **參考價基準**必須另外取最近一則,不能跟價格基準共用同一列。共用會導致
        永不收斂:新寫入的 alert 價格較貴,永遠不會成為新的「最便宜」基準列,
        於是參考價的比較對象被凍結在舊值,一組完全靜止的 (價格, 參考價) 每輪
        都會判定為「參考價有意義變化」而重發。實測連問 6 次會發 6 次。

        **new_low 自成一桶**（reason 參數）。它是「這條航線史上最便宜」的極值
        事件,5 週只出現數次,卻最容易被每天都可能觸發的 absolute 壓掉——窗拉到
        30 天之後,一則 absolute 會讓接下來 30 天的 new_low 全部消失,除非再便宜
        10%。其餘 reason 仍共用一桶,避免同一個價格換個理由再吵一次。

        向後相容:歷史列的 return_date / carrier_signature 為 NULL,SQL 等值
        比對自然不會匹配帶有明確行程的新 alert——即舊資料不會錯誤抑制新的明確
        行程(規格要求)。不猜測回填舊列。

        允許重新通知:價格較基準改善 >= improvement_pct、狀態改變(例如升級為
        verified)、或 CONFLICT 的參考價出現同等幅度的變化。
        """
        status_clause, status_params = self._status_bucket(price_status)
        reason_clause, reason_params = self._reason_bucket(reason)
        where = (f"origin=? AND destination=? AND depart_date=?"
                 f" AND return_date IS ? AND carrier_signature IS ?"
                 f" AND {status_clause} AND {reason_clause}"
                 f" AND sent_at >= datetime('now', ?)")
        params = (origin, destination, depart_date, return_date,
                  carrier_signature, *status_params, *reason_params,
                  f"-{within_hours} hours")

        cheapest = self.conn.execute(
            f"SELECT price FROM alerts WHERE {where}"
            " ORDER BY price ASC, sent_at DESC LIMIT 1", params).fetchone()
        if cheapest is None:
            return False
        if price <= cheapest["price"] * (1 - improvement_pct / 100.0):
            return False                      # 比已通知的最低價再便宜一截 → 重新通知

        latest = self.conn.execute(
            f"SELECT reference_price FROM alerts WHERE {where}"
            " ORDER BY sent_at DESC, id DESC LIMIT 1", params).fetchone()
        prev_ref, new_ref = latest["reference_price"], reference_price
        if prev_ref and new_ref and \
                abs(new_ref - prev_ref) >= prev_ref * (improvement_pct / 100.0):
            return False                      # 參考價相對「上一則」有意義變化 → 重新通知
        return True

    def record_api_usage(self, provider: str, day: str, *, ok: bool) -> None:
        self.conn.execute(
            """INSERT INTO api_usage (provider, day, requests, ok) VALUES (?, ?, 1, ?)
               ON CONFLICT(provider, day) DO UPDATE SET
                 requests = requests + 1, ok = ok + excluded.ok""",
            (provider, day, int(ok)))
        self.conn.commit()

    def api_usage_since(self, provider: str, first_day: str) -> int:
        """first_day（含）之後送出的總次數。"""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(requests), 0) FROM api_usage WHERE provider=? AND day>=?",
            (provider, first_day)).fetchone()
        return int(row[0])

    def has_google_since(self, origin: str, destination: str, depart_date: str,
                         return_date: str, since: str) -> bool:
        """同一行程（航線＋去回日）在 since 之後有沒有 Google 實價（任何供應商）。"""
        return self.conn.execute(
            """SELECT 1 FROM observations
               WHERE origin=? AND destination=? AND depart_date=? AND return_date=?
                 AND source='google' AND observed_at >= ? LIMIT 1""",
            (origin, destination, depart_date, return_date, since)).fetchone() is not None

    def record_api_check(self, provider: str, origin: str, destination: str,
                         depart_date: str, return_date: str, checked_at: str) -> None:
        self.conn.execute(
            """INSERT INTO api_checks VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(provider, origin, destination, depart_date, return_date)
               DO UPDATE SET checked_at = excluded.checked_at""",
            (provider, origin, destination, depart_date, return_date, checked_at))
        self.conn.commit()

    def api_checked_since(self, provider: str, origin: str, destination: str,
                          depart_date: str, return_date: str, since: str) -> bool:
        return self.conn.execute(
            """SELECT 1 FROM api_checks
               WHERE provider=? AND origin=? AND destination=? AND depart_date=?
                 AND return_date=? AND checked_at >= ?""",
            (provider, origin, destination, depart_date, return_date, since)
        ).fetchone() is not None

    def prune_api_checks(self, provider: str, before: str) -> None:
        self.conn.execute("DELETE FROM api_checks WHERE provider=? AND checked_at < ?",
                          (provider, before))
        self.conn.commit()

    def record_cheap_day_notice(self, origin: str, destination: str,
                                depart_date: str, price: float,
                                notified_at: str) -> None:
        """記下「這個便宜日已經推播過」。每個(航線,出發日)只留最新一次——
        cheap_day_notify.should_notify 只需要「上次推的時間與價格」來決定
        要不要重推，保留完整歷史沒有讀者（long_range 的教訓）。"""
        self.conn.execute(
            """INSERT INTO cheap_day_notices VALUES (?,?,?,?,?)
               ON CONFLICT(origin, destination, depart_date) DO UPDATE SET
                 price=excluded.price, notified_at=excluded.notified_at""",
            (origin, destination, depart_date, price, notified_at))
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
