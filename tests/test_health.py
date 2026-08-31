"""Route health 偵測：斷線航線必須被分級、被數到、被 export 出去。

時鐘一律注入，observed_at 一律直接以 SQL 寫入（Store.record 內部寫死
datetime.now，無法用來製造「9 天前的觀測」）。
"""
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import farehunter.runner as runner_mod
from farehunter import health
from farehunter.export_web import export
from farehunter.storage import Store
from farehunter.models import Offer

NOW = datetime(2026, 8, 7, 1, 36, 0, tzinfo=timezone.utc)


def _seed(db_path, rows):
    """rows: (origin, destination, hours_ago | None)。None = 該航線完全沒有列。"""
    store = Store(str(db_path))          # 建 schema
    store.close()
    conn = sqlite3.connect(str(db_path))
    for origin, dest, hours_ago in rows:
        if hours_ago is None:
            continue
        observed = (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO observations (origin, destination, depart_date, "
            "return_date, price, currency, carriers, stops, duration, "
            "observed_at, fare_class, source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (origin, dest, "2026-09-20", "2026-09-25", 9000.0, "TWD",
             "IT", 0, "PT3H", observed, "any", "aviasales"))
    conn.commit()
    conn.close()


# ---- classify 邊界 ---------------------------------------------------------

def test_classify_boundaries():
    assert health.classify(0.0) == health.OK
    assert health.classify(23.9) == health.OK
    assert health.classify(24.0) == health.STALE       # 邊界含左
    assert health.classify(71.9) == health.STALE
    assert health.classify(72.0) == health.DEAD        # 邊界含左
    assert health.classify(201.3) == health.DEAD
    assert health.classify(None) == health.NEVER


def test_classify_respects_injected_thresholds():
    assert health.classify(5.0, stale_after_hours=4.0,
                           dead_after_hours=8.0) == health.STALE
    assert health.classify(9.0, stale_after_hours=4.0,
                           dead_after_hours=8.0) == health.DEAD


def test_classify_future_timestamp_is_ok():
    """時鐘偏移造成的負齡不應被當成故障——健康檢查不負責偵測時鐘問題。"""
    assert health.classify(-3.0) == health.OK


def test_parse_ts_failopen():
    assert health.parse_ts(None) is None
    assert health.parse_ts("") is None
    assert health.parse_ts("not-a-timestamp") is None
    assert health.parse_ts("2026-08-07T01:00:00Z").tzinfo is not None
    naive = health.parse_ts("2026-08-07T01:00:00")
    assert naive.tzinfo == timezone.utc          # naive 視為 UTC，與 SQLite 'now' 一致


# ---- build_health ---------------------------------------------------------

def test_build_health_grades_each_route(tmp_path):
    db = tmp_path / "t.db"
    _seed(db, [
        ("KHH", "NRT", 0.3),        # ok
        ("KHH", "CTS", 30.0),       # stale
        ("KHH", "NGO", 201.3),      # dead — 重現 2026-08-07 盤點的實況
        ("TPE", "KIX", None),       # config 有列但 DB 無資料 → never_observed
    ])
    conn = sqlite3.connect(str(db))
    block = health.build_health(
        conn,
        [("KHH", "NRT"), ("KHH", "CTS"), ("KHH", "NGO"), ("TPE", "KIX")],
        NOW)
    conn.close()

    by_route = {e["route"]: e for e in block["routes"]}
    assert by_route["KHH-NRT"]["status"] == health.OK
    assert by_route["KHH-CTS"]["status"] == health.STALE
    assert by_route["KHH-NGO"]["status"] == health.DEAD
    assert by_route["TPE-KIX"]["status"] == health.NEVER

    assert by_route["KHH-NGO"]["age_hours"] == 201.3
    assert by_route["TPE-KIX"]["age_hours"] is None
    assert by_route["TPE-KIX"]["last_observed_at"] is None

    assert block["counts"] == {health.OK: 1, health.STALE: 1,
                               health.DEAD: 1, health.NEVER: 1}
    assert block["degraded"] == ["KHH-CTS", "KHH-NGO", "TPE-KIX"]
    assert block["checked_at"] == "2026-08-07T01:36:00+00:00"
    assert block["thresholds"] == {"stale_after_hours": 24.0,
                                   "dead_after_hours": 72.0}


def test_build_health_uses_given_route_list_not_db(tmp_path):
    """config 列了但從未觀測過的航線，一定要出現在報告裡。

    若改用 SELECT DISTINCT 從 observations 推導，這條會直接消失——
    那正是 KHH→NGO 這類問題最容易被漏掉的地方。
    """
    db = tmp_path / "t.db"
    _seed(db, [("KHH", "NRT", 1.0)])
    conn = sqlite3.connect(str(db))
    block = health.build_health(conn, [("KHH", "NRT"), ("KHH", "OKA")], NOW)
    conn.close()
    assert [e["route"] for e in block["routes"]] == ["KHH-NRT", "KHH-OKA"]
    assert block["counts"][health.NEVER] == 1


def test_build_health_ignores_source_and_fare_class(tmp_path):
    """問的是「還有沒有任何管道在供料」，不是主價新鮮度，故不濾 source。"""
    db = tmp_path / "t.db"
    store = Store(str(db))
    store.close()
    conn = sqlite3.connect(str(db))
    observed = (NOW - timedelta(hours=2)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO observations (origin, destination, depart_date, price, "
        "currency, observed_at, fare_class, source) VALUES (?,?,?,?,?,?,?,?)",
        ("KHH", "FUK", "2026-09-20", 13302.0, "TWD", observed, "full", "google"))
    conn.commit()
    block = health.build_health(conn, [("KHH", "FUK")], NOW)
    conn.close()
    assert block["routes"][0]["status"] == health.OK


def test_log_health_levels(tmp_path, caplog):
    db = tmp_path / "t.db"
    _seed(db, [("KHH", "NRT", 0.5), ("KHH", "CTS", 30.0), ("KHH", "NGO", 201.3)])
    conn = sqlite3.connect(str(db))
    block = health.build_health(
        conn, [("KHH", "NRT"), ("KHH", "CTS"), ("KHH", "NGO")], NOW)
    conn.close()
    with caplog.at_level("INFO"):
        health.log_health(block)
    levels = {r.levelname for r in caplog.records}
    assert "ERROR" in levels and "WARNING" in levels
    text = caplog.text
    assert "KHH-NGO" in text and "KHH-CTS" in text
    # NRT 健康，不該出現在任何一行——否則噪音會讓真正的異常被淹掉
    assert not any("KHH-NRT" in r.getMessage() for r in caplog.records)


def test_log_health_all_ok_is_info(tmp_path, caplog):
    db = tmp_path / "t.db"
    _seed(db, [("KHH", "NRT", 0.5)])
    conn = sqlite3.connect(str(db))
    block = health.build_health(conn, [("KHH", "NRT")], NOW)
    conn.close()
    with caplog.at_level("INFO"):
        health.log_health(block)
    assert {r.levelname for r in caplog.records} == {"INFO"}


# ---- export 整合 ----------------------------------------------------------

def test_export_includes_health_block(tmp_path):
    db = tmp_path / "t.db"
    _seed(db, [("KHH", "NRT", 0.3), ("KHH", "NGO", 201.3)])
    cfg = tmp_path / "config.yaml"
    cfg.write_text("routes:\n  - origin: KHH\n    destination: NRT\n"
                   "  - origin: KHH\n    destination: NGO\n"
                   "  - origin: TPE\n    destination: KIX\n", encoding="utf-8")

    payload = export(str(db), str(tmp_path / "data.json"), now=NOW,
                     config_path=str(cfg))

    h = payload["health"]
    assert h["counts"] == {health.OK: 1, health.STALE: 0,
                           health.DEAD: 1, health.NEVER: 1}
    assert h["degraded"] == ["KHH-NGO", "TPE-KIX"]
    # config 有 3 條、observations 只有 2 條 → health 必須看得到第 3 條
    assert len(h["routes"]) == 3
    assert len(payload["routes"]) == 2


def test_export_health_failopen_without_config(tmp_path):
    """讀不到 config 時退回已觀測航線，export 不得整個掛掉。"""
    db = tmp_path / "t.db"
    _seed(db, [("KHH", "NRT", 0.3), ("KHH", "NGO", 201.3)])
    payload = export(str(db), str(tmp_path / "data.json"), now=NOW,
                     config_path=str(tmp_path / "does-not-exist.yaml"))
    assert [e["route"] for e in payload["health"]["routes"]] == ["KHH-NGO", "KHH-NRT"]
    assert payload["health"]["degraded"] == ["KHH-NGO"]


# ---- fail-open：健康檢查不得擋掉資料寫入 ----------------------------------

def test_broken_conn_degrades_to_never_observed(tmp_path, caplog):
    """查詢層失敗 → 該航線降級為 never_observed，而不是整個報告消失。"""
    class Boom:
        def execute(self, *a, **kw):
            raise RuntimeError("db exploded")

    with caplog.at_level("WARNING"):
        block = health.build_health(Boom(), [("KHH", "NRT")], NOW)
    assert block["routes"][0]["status"] == health.NEVER
    assert block["degraded"] == ["KHH-NRT"]
    assert "route health 查詢失敗" in caplog.text


def test_build_health_safe_returns_none_on_hard_failure(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(health, "build_health",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    with caplog.at_level("WARNING"):
        assert health.build_health_safe(None, [("KHH", "NRT")], NOW) is None
    assert "route health 計算失敗" in caplog.text


def test_log_health_accepts_none():
    health.log_health(None)          # 不得拋出


def test_export_health_null_when_unavailable(tmp_path, monkeypatch):
    """健康報告算不出來時，payload 帶 health: null，export 本身仍成功。

    這是最重要的一條：monitor.yml 的 commit 步驟是 if: success()，
    export 掛掉等於整輪 prices.db 不進 commit。
    """
    db = tmp_path / "t.db"
    _seed(db, [("KHH", "NRT", 0.3)])
    import farehunter.export_web as ew
    monkeypatch.setattr(ew.health, "build_health",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    payload = export(str(db), str(tmp_path / "data.json"), now=NOW)
    assert payload["health"] is None
    assert payload["totals"]["observations"] == 1     # 其餘輸出完全不受影響
    assert len(payload["routes"]) == 1


# ---- runner 整合 ----------------------------------------------------------

class EmptyClient:
    """API 呼叫成功但回傳無可用票價——KHH→NGO 連續 9 天的實際型態。"""
    def __init__(self, *a, **kw):
        pass

    def search_month(self, *a, **kw):
        return {"success": True, "data": {}}


def test_runner_counts_empty_and_zero_record_routes(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "defaults:\n  currency: twd\n  months_ahead: 3\n  pause_seconds: 0\n"
        "routes:\n  - origin: KHH\n    destination: NGO\n", encoding="utf-8")
    monkeypatch.setattr(runner_mod, "TravelpayoutsClient", EmptyClient)

    summary = runner_mod.run(str(cfg), str(tmp_path / "p.db"),
                             now=NOW)

    assert summary["searched"] == 3
    assert summary["recorded"] == 0
    assert summary["errors"] == 0          # 空結果不是錯誤
    assert summary["empty"] == 3           # ...但必須被數到
    assert summary["zero_record_routes"] == ["KHH-NGO"]
    assert summary["health"]["counts"][health.NEVER] == 1
    assert summary["health"]["degraded"] == ["KHH-NGO"]


def test_runner_survives_health_failure(tmp_path, monkeypatch):
    """健康檢查掛掉時 run() 仍須正常回傳——否則 commit 步驟被連帶擋掉。"""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "defaults:\n  currency: twd\n  months_ahead: 1\n  pause_seconds: 0\n"
        "routes:\n  - origin: KHH\n    destination: NGO\n", encoding="utf-8")
    monkeypatch.setattr(runner_mod, "TravelpayoutsClient", EmptyClient)
    monkeypatch.setattr(runner_mod.health, "build_health",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))

    summary = runner_mod.run(str(cfg), str(tmp_path / "p.db"),
                             now=NOW)
    assert summary["health"] is None
    assert summary["searched"] == 1
    assert summary["zero_record_routes"] == ["KHH-NGO"]


def test_runner_guard_skip_keeps_summary_shape(tmp_path, monkeypatch):
    """guard 跳過時的回傳也要有新欄位，呼叫端不必到處 .get() 防身。"""
    monkeypatch.delenv("FAREHUNTER_FORCE", raising=False)
    # guard 的時鐘是 prices.db 裡 source='aviasales' 的最新觀測（不是 data.json，
    # 那個欄位會被四支 sweep 覆寫）。寫一筆「剛剛」的觀測讓 guard 判定跳過。
    db_path = tmp_path / "p.db"
    store = Store(str(db_path))
    store.record(Offer(origin="TPE", destination="NRT", depart_date="2099-09-18",
                       return_date="2099-09-23", price=8000.0, currency="TWD",
                       carriers="CI", stops=0, duration="190"))
    store.close()
    summary = runner_mod.run(str(tmp_path / "c.yaml"), str(db_path))
    assert summary["skipped"] is True
    assert summary["empty"] == 0
    assert summary["zero_record_routes"] == []


# ---- sweep 全軍覆沒必須以非零結束碼失敗 --------------------------------------

def test_total_failure_is_detected():
    """實際發生過：SearchApi 額度用完後 gcal_sweep 連續五週回這個 summary，
    而進入點無條件 exit 0，workflow 每週照樣綠燈。"""
    assert health.sweep_failed_entirely(
        {"searched": 16, "recorded": 0, "alerts": 0, "errors": 16}) is True
    assert health.sweep_exit_code(
        {"searched": 16, "recorded": 0, "errors": 16}, "gcal_sweep") == 1


def test_thin_route_empty_result_is_not_a_failure():
    """查得到但解析後回空是薄航線的正常現象（errors=0），不得誤判成故障。

    PLAYBOOK 明訂它該被數但不該當成錯誤。
    """
    assert health.sweep_failed_entirely(
        {"searched": 16, "recorded": 0, "alerts": 0, "errors": 0}) is False
    assert health.sweep_exit_code({"searched": 16, "recorded": 0, "errors": 0}) == 0


def test_partial_failure_is_not_a_failure():
    """部分失敗屬正常波動——只有「每一次都失敗」才算壞掉。"""
    assert health.sweep_failed_entirely(
        {"searched": 16, "recorded": 5, "errors": 11}) is False
    assert health.sweep_failed_entirely(
        {"searched": 16, "recorded": 0, "errors": 15}) is False


def test_no_queries_is_not_a_failure():
    """沒查就沒失敗（例如當週沒有掃描窗）。"""
    assert health.sweep_failed_entirely({"searched": 0, "recorded": 0, "errors": 0}) is False


def test_alternative_recorded_field_names_count():
    """不同 sweep 用不同欄位表示「有進帳」：dates_covered / verified。"""
    assert health.sweep_failed_entirely(
        {"searched": 8, "dates_covered": 3, "errors": 8}) is False
    assert health.sweep_failed_entirely(
        {"searched": 8, "verified": 2, "errors": 8}) is False
    assert health.sweep_failed_entirely(
        {"searched": 8, "dates_covered": 0, "verified": 0, "errors": 8}) is True


def test_missing_keys_do_not_crash():
    """summary 缺欄位時不得拋錯——這是觀測性程式碼，不能自己變成故障源。"""
    assert health.sweep_failed_entirely({}) is False
    assert health.sweep_exit_code({}) == 0
    assert health.sweep_failed_entirely({"searched": None, "errors": None}) is False
