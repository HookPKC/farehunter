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
            status="unverified", ref=None):
    return st.recently_alerted(BASE["origin"], BASE["destination"],
                               BASE["depart_date"], price,
                               return_date=ret, carrier_signature=csig,
                               price_status=status, reference_price=ref)


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


def test_status_windows_are_configured(tmp_path):
    st = _store(tmp_path)
    assert st.DEDUP_HOURS["verified"] == 24
    assert st.DEDUP_HOURS["unverified"] == 24
    assert st.DEDUP_HOURS["conflict"] == 72
    st.close()


def test_conflict_uses_longer_window(tmp_path):
    """CONFLICT 於 30 小時前發過 → 仍在 72h 窗內,應 dedup;
    同樣時間點的 unverified(24h 窗)則已過窗。"""
    st = _store(tmp_path)
    for status in ("conflict", "unverified"):
        st.conn.execute(
            "INSERT INTO alerts (origin,destination,depart_date,price,reason,"
            "sent_at,return_date,carrier_signature,price_status) "
            "VALUES (?,?,?,?,?,datetime('now','-30 hours'),?,?,?)",
            (BASE["origin"], BASE["destination"], BASE["depart_date"], 7761.0,
             "absolute", "2026-09-08", "GK", status))
    st.conn.commit()
    assert _recent(st, status="conflict") is True      # 72h 窗內
    assert _recent(st, status="unverified") is False   # 超出 24h 窗
    st.close()


# ---- 7/8/9 允許重新通知的情況 --------------------------------------------

def test_price_improvement_reopens_notification(tmp_path):
    st = _store(tmp_path)
    _alert(st, price=8000.0)
    assert _recent(st, price=7900.0) is True     # 只降 1.25%
    assert _recent(st, price=7100.0) is False    # 降 11.25% → 重新通知
    st.close()


def test_status_upgrade_to_verified_reopens(tmp_path):
    st = _store(tmp_path)
    _alert(st, status="unverified")
    assert _recent(st, status="verified") is False
    st.close()


def test_reference_price_material_change_reopens(tmp_path):
    st = _store(tmp_path)
    _alert(st, status="conflict", ref=8778.0)
    assert _recent(st, status="conflict", ref=8800.0) is True   # 幾乎沒變
    assert _recent(st, status="conflict", ref=10500.0) is False  # 參考價大變
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
