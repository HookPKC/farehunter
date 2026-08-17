"""Commit 2:Alert trip identity 與 dedup 測試。

背景:原 dedup key 只有 (origin, destination, depart_date)。實測已發過 alert 的
組合中有 6 個 (route, depart) 擁有 2–4 個不同回程日,不同行程會互相阻擋或被
誤認為重複。本輪讓 identity 包含 return_date / carrier_signature / price_status。

全部使用注入時鐘,不依賴真實日期。
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from farehunter.storage import Store

BASE = dict(origin="KHH", destination="NRT", depart_date="2026-09-05")


def _store(tmp_path):
    return Store(str(tmp_path / "prices.db"))


def _alert(st, *, price=7761.0, ret="2026-09-08", csig="GK",
           status="unverified", ref=None, ref_at=None, reason="absolute"):
    st.record_alert(BASE["origin"], BASE["destination"], BASE["depart_date"],
                    price, reason, return_date=ret, carrier_signature=csig,
                    price_source="aviasales", price_status=status,
                    reference_price=ref, reference_observed_at=ref_at)


def _recent(st, *, price=7761.0, ret="2026-09-08", csig="GK",
            status="unverified", ref=None, reason="absolute"):
    return st.recently_alerted(BASE["origin"], BASE["destination"],
                               BASE["depart_date"], price,
                               return_date=ret, carrier_signature=csig,
                               price_status=status, reference_price=ref,
                               reason=reason)


# ---- 1/2 不同 identity 各自獨立 ------------------------------------------

def test_different_return_date_can_each_notify(tmp_path):
    st = _store(tmp_path)
    _alert(st, ret="2026-09-08")
    assert _recent(st, ret="2026-09-08") is True     # 同行程 → dedup
    assert _recent(st, ret="2026-09-12") is False    # 不同回程日 → 各自可通知
    st.close()


def test_different_carrier_can_each_notify(tmp_path):
    st = _store(tmp_path)
    _alert(st, csig="GK")
    assert _recent(st, csig="GK") is True
    assert _recent(st, csig="IT") is False
    st.close()


# ---- 3-6 dedup 窗 ---------------------------------------------------------

def test_same_itinerary_same_status_is_deduped(tmp_path):
    st = _store(tmp_path)
    _alert(st, status="unverified")
    assert _recent(st, status="unverified") is True
    st.close()


def _alert_ago(st, sql_offset, *, price=7761.0, status="unverified"):
    """在過去某個時間點寫入一則 alert（sql_offset 例如 '-30 hours'）。"""
    st.conn.execute(
        "INSERT INTO alerts (origin,destination,depart_date,price,reason,"
        "sent_at,return_date,carrier_signature,price_status) "
        f"VALUES (?,?,?,?,?,datetime('now','{sql_offset}'),?,?,?)",
        (BASE["origin"], BASE["destination"], BASE["depart_date"], price,
         "absolute", "2026-09-08", "GK", status))
    st.conn.commit()


def test_unchanged_price_does_not_renotify_next_day(tmp_path):
    """核心回歸：舊版 24h 窗一過就重發,實測同一個 5,623 連叫六天。

    價格沒變就不是新消息——30 小時前發過的同一價格必須仍被抑制。
    """
    st = _store(tmp_path)
    _alert_ago(st, "-30 hours", price=5623.0)
    assert _recent(st, price=5623.0) is True
    st.close()


def test_unchanged_price_still_suppressed_after_a_week(tmp_path):
    st = _store(tmp_path)
    _alert_ago(st, "-7 days", price=5623.0)
    assert _recent(st, price=5623.0) is True
    st.close()


def test_suppression_expires_after_window(tmp_path):
    """抑制不是永久的：超過 SUPPRESS_DAYS 後同一個好康可以再提醒一次。"""
    st = _store(tmp_path)
    assert st.SUPPRESS_DAYS == 30
    _alert_ago(st, "-31 days", price=5623.0)
    assert _recent(st, price=5623.0) is False
    st.close()


def test_baseline_is_cheapest_alerted_not_latest(tmp_path):
    """基準取窗內最低價。8000 → 7000 之後,7600 不得因為「比最近一則便宜」
    而重新通知——它比你已經被告知過的 7000 還貴。"""
    st = _store(tmp_path)
    _alert_ago(st, "-3 days", price=8000.0)
    _alert_ago(st, "-2 days", price=7000.0)
    assert _recent(st, price=7600.0) is True     # 比 7000 貴 → 不是新消息
    assert _recent(st, price=6200.0) is False    # 比 7000 便宜 11.4% → 通知
    st.close()


# ---- 7/8/9 允許重新通知的情況 --------------------------------------------

def test_price_improvement_reopens_notification(tmp_path):
    st = _store(tmp_path)
    _alert(st, price=8000.0)
    assert _recent(st, price=7900.0) is True     # 只降 1.25%
    assert _recent(st, price=7100.0) is False    # 降 11.25% → 重新通知
    st.close()


def test_status_upgrade_to_verified_reopens(tmp_path):
    """「已驗證」比「疑似」更有把握,是真正的新消息 → 可以再說一次。"""
    st = _store(tmp_path)
    _alert(st, status="unverified")
    assert _recent(st, status="verified") is False
    st.close()


def test_unverified_conflict_flapping_does_not_renotify(tmp_path):
    """unverified ↔ conflict 來回擺動不是新消息——價格一毛沒變,差別只在當下
    有沒有一筆落在參考窗內的 Google 觀測。實測這讓同一個 5,963 重發了 5 次。
    """
    st = _store(tmp_path)
    _alert(st, price=5963.0, status="unverified")
    assert _recent(st, price=5963.0, status="conflict") is True
    st.close()


def test_conflict_then_unverified_also_suppressed(tmp_path):
    st = _store(tmp_path)
    _alert(st, price=5963.0, status="conflict")
    assert _recent(st, price=5963.0, status="unverified") is True
    st.close()


def test_verified_stays_its_own_bucket(tmp_path):
    """反向:已用 verified 通知過,之後降級成 unverified 不得再吵一次。"""
    st = _store(tmp_path)
    _alert(st, price=5963.0, status="verified")
    assert _recent(st, price=5963.0, status="verified") is True
    st.close()


def test_reference_price_material_change_reopens(tmp_path):
    st = _store(tmp_path)
    _alert(st, status="conflict", ref=8778.0)
    assert _recent(st, status="conflict", ref=8800.0) is True   # 幾乎沒變
    assert _recent(st, status="conflict", ref=10500.0) is False  # 參考價大變
    st.close()


def test_reference_price_comparison_converges(tmp_path):
    """回歸：一組完全靜止的 (價格, 參考價) 不得每輪重發。

    價格基準取「窗內最便宜」、參考價基準若跟它共用同一列就永不收斂——新寫入的
    alert 價格較貴，永遠不會成為新的最便宜基準列，於是參考價的比較對象被凍結
    在舊值，同一個沒有變化的落差每小時重發一次。實測連問 6 次會發 6 次。
    參考價必須改跟「最近一則」比。
    """
    st = _store(tmp_path)
    _alert(st, price=7000.0, status="conflict", ref=8000.0)
    sent = 0
    for _ in range(6):
        if not _recent(st, price=7500.0, status="conflict", ref=9000.0):
            sent += 1
            _alert(st, price=7500.0, status="conflict", ref=9000.0)
    assert sent == 1, f"靜止狀態重發了 {sent} 次，應該只有第一次"
    st.close()


# ---- new_low 自成一桶 -------------------------------------------------------

def test_new_low_is_not_suppressed_by_an_earlier_absolute(tmp_path):
    """回歸：absolute 每天都可能觸發，new_low 5 週只出現數次。

    窗拉到 30 天之後，若兩者共用一個 dedup 桶，一則 absolute 會讓接下來 30 天
    的 new_low 全部消失——最有價值的訊號被最廉價的訊號擋住。
    """
    st = _store(tmp_path)
    _alert(st, price=6364.0, reason="absolute")
    assert _recent(st, price=6270.0, reason="new_low") is False    # 史上最低 → 放行
    assert _recent(st, price=6270.0, reason="absolute") is True    # 同理由 → 照擋
    st.close()


def test_new_low_still_dedupes_against_other_new_lows(tmp_path):
    """自成一桶不等於不去重：連續刷新 0.1% 仍應被擋。"""
    st = _store(tmp_path)
    _alert(st, price=6270.0, reason="new_low")
    assert _recent(st, price=6265.0, reason="new_low") is True
    assert _recent(st, price=5600.0, reason="new_low") is False    # 便宜 10.7%
    st.close()


def test_gcal_shaped_alert_keeps_return_dates_independent(tmp_path):
    """回歸：gcal_sweep 曾經只傳 4 個位置參數，identity 全走 NULL，

    導致同一個出發日的不同回程日落進同一個桶互相封鎖 30 天。這裡用它現在的
    呼叫形狀（帶 return_date、carrier_signature=None、status=verified）確認
    不同回程日各自獨立。
    """
    st = _store(tmp_path)
    st.record_alert("KHH", "NRT", "2026-10-01", 9000.0, "absolute",
                    return_date="2026-10-05", carrier_signature=None,
                    price_source="google", price_status="verified")
    same = st.recently_alerted("KHH", "NRT", "2026-10-01", 8800.0,
                               return_date="2026-10-05", carrier_signature=None,
                               price_status="verified", reason="absolute")
    other = st.recently_alerted("KHH", "NRT", "2026-10-01", 8800.0,
                                return_date="2026-10-12", carrier_signature=None,
                                price_status="verified", reason="absolute")
    assert same is True and other is False
    st.close()


# ---- 10-12 向後相容 -------------------------------------------------------

def test_legacy_null_return_date_does_not_block_explicit_trip(tmp_path):
    """歷史列沒有 return_date;不得抑制帶有明確行程的新 alert。"""
    st = _store(tmp_path)
    st.conn.execute(
        "INSERT INTO alerts (origin,destination,depart_date,price,reason,sent_at)"
        " VALUES (?,?,?,?,?,datetime('now'))",
        (BASE["origin"], BASE["destination"], BASE["depart_date"], 7761.0, "absolute"))
    st.conn.commit()
    assert _recent(st, ret="2026-09-08", csig="GK") is False
    st.close()


def test_legacy_null_carrier_does_not_block(tmp_path):
    st = _store(tmp_path)
    _alert(st, csig=None)
    assert _recent(st, csig="GK") is False
    assert _recent(st, csig=None) is True     # 兩側皆 NULL 才視為同一列
    st.close()


def test_no_backfill_of_historical_identity(tmp_path):
    """migration 不得猜測回填歷史 identity。"""
    st = _store(tmp_path)
    st.conn.execute(
        "INSERT INTO alerts (origin,destination,depart_date,price,reason,sent_at)"
        " VALUES ('TPE','NRT','2026-10-01',6310.0,'absolute',datetime('now'))")
    st.conn.commit()
    st.close()
    st2 = _store(tmp_path)                     # 再次開啟 → migration 重跑
    row = st2.conn.execute(
        "SELECT return_date, carrier_signature, price_status FROM alerts"
        " WHERE origin='TPE'").fetchone()
    assert row["return_date"] is None
    assert row["carrier_signature"] is None
    assert row["price_status"] is None
    st2.close()


# ---- 13/14 migration 安全性 ----------------------------------------------

def test_migration_is_idempotent(tmp_path):
    for _ in range(3):
        st = _store(tmp_path)
        cols = [r[1] for r in st.conn.execute("PRAGMA table_info(alerts)")]
        st.close()
    for c in ("return_date", "carrier_signature", "price_source",
              "price_status", "reference_price", "reference_observed_at"):
        assert c in cols


def test_legacy_db_without_new_columns_upgrades_cleanly(tmp_path):
    """模擬舊 DB(只有原始 alerts 欄位)→ 開啟後自動補欄位,舊列仍可讀。"""
    p = tmp_path / "prices.db"
    conn = sqlite3.connect(str(p))
    conn.executescript(
        "CREATE TABLE alerts (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " origin TEXT NOT NULL, destination TEXT NOT NULL,"
        " depart_date TEXT NOT NULL, price REAL NOT NULL,"
        " reason TEXT NOT NULL, sent_at TEXT NOT NULL);"
        "INSERT INTO alerts (origin,destination,depart_date,price,reason,sent_at)"
        " VALUES ('KHH','OKA','2026-09-15',8273.0,'big_drop','2026-07-16 04:17:42');")
    conn.commit(); conn.close()
    st = Store(str(p))
    row = st.conn.execute("SELECT * FROM alerts").fetchone()
    assert row["price"] == 8273.0 and row["return_date"] is None
    _alert(st)                                  # 新列可正常寫入
    assert st.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 2
    st.close()


def test_record_alert_persists_identity_and_status(tmp_path):
    st = _store(tmp_path)
    _alert(st, status="conflict", ref=8778.0, ref_at="2026-07-25T10:55:00+00:00")
    row = st.conn.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT 1").fetchone()
    assert row["return_date"] == "2026-09-08"
    assert row["carrier_signature"] == "GK"
    assert row["price_source"] == "aviasales"
    assert row["price_status"] == "conflict"
    assert row["reference_price"] == 8778.0
    assert row["reference_observed_at"].startswith("2026-07-25")
    st.close()


def test_record_alert_without_identity_still_works(tmp_path):
    """舊呼叫方式(只給位置參數)不得崩潰。"""
    st = _store(tmp_path)
    st.record_alert("KHH", "KIX", "2026-09-01", 8000.0, "absolute")
    row = st.conn.execute("SELECT * FROM alerts").fetchone()
    assert row["return_date"] is None and row["price_status"] is None
    st.close()
