"""防重複 guard 測試：fail-open 是鐵律——guard 只能跳過，絕不能擋路。

時鐘來源是 prices.db 裡 source='aviasales' 的最新觀測，不是 docs/data.json。
原因見 runner.py 的註解：data.json 的 generated_at 被全站四支 sweep 共用，
它們一跑完就重置 monitor 的新鮮度時鐘，實測每天固定漏抓 2 小時。
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from datetime import datetime, timedelta, timezone

from farehunter.runner import GUARD_MINUTES, guard_decision, _emit_skip_output, run

SCHEMA = ("CREATE TABLE observations (id INTEGER PRIMARY KEY AUTOINCREMENT,"
          " origin TEXT, destination TEXT, depart_date TEXT, price REAL,"
          " currency TEXT, observed_at TEXT NOT NULL, source TEXT)")


def _db(path, *, minutes_ago=None, observed_at=None, source="aviasales",
        no_table=False, empty=False):
    conn = sqlite3.connect(str(path))
    if not no_table:
        conn.execute(SCHEMA)
        if not empty:
            if observed_at is None:
                ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
                observed_at = ts.isoformat(timespec="seconds")
            conn.execute("INSERT INTO observations (origin,destination,depart_date,"
                         "price,currency,observed_at,source) VALUES (?,?,?,?,?,?,?)",
                         ("TPE", "NRT", "2099-09-18", 8000.0, "TWD",
                          observed_at, source))
    conn.commit()
    conn.close()
    return path


# ---- 規格要求的四態 ---------------------------------------------------------

def test_fresh_data_skips(tmp_path):
    p = _db(tmp_path / "p.db", minutes_ago=10)
    skip, age = guard_decision(str(p), force=False)
    assert skip is True
    assert 9 <= age <= 11


def test_stale_data_runs(tmp_path):
    p = _db(tmp_path / "p.db", minutes_ago=120)
    skip, age = guard_decision(str(p), force=False)
    assert skip is False
    assert age >= 119


def test_missing_db_runs_fail_open(tmp_path):
    skip, age = guard_decision(str(tmp_path / "nope.db"), force=False)
    assert skip is False and age is None


def test_unreadable_db_runs_fail_open(tmp_path):
    """沒有 observations 表、表是空的、時間戳壞掉——全部照常執行。"""
    assert guard_decision(str(_db(tmp_path / "a.db", no_table=True)),
                          force=False) == (False, None)
    assert guard_decision(str(_db(tmp_path / "b.db", empty=True)),
                          force=False) == (False, None)
    assert guard_decision(str(_db(tmp_path / "c.db", observed_at="not-a-timestamp")),
                          force=False) == (False, None)


# ---- 時鐘只認 monitor 自己寫的觀測 -------------------------------------------

def test_other_sources_do_not_reset_the_clock(tmp_path):
    """核心回歸：sweep 寫入的 google 觀測不得影響 monitor 的新鮮度判斷。

    這正是舊版讀 docs/data.json 的病根——fsc-snapshot / verify-airlines /
    gcal-sweep / longrange-sweep 跑完都會重置那個時鐘，害下一輪抓價被跳過。
    """
    p = tmp_path / "p.db"
    _db(p, minutes_ago=120)                       # monitor 的觀測：2 小時前
    conn = sqlite3.connect(str(p))                # sweep 剛剛寫入的 google 觀測
    conn.execute("INSERT INTO observations (origin,destination,depart_date,price,"
                 "currency,observed_at,source) VALUES (?,?,?,?,?,?,?)",
                 ("TPE", "NRT", "2099-09-18", 7000.0, "TWD",
                  datetime.now(timezone.utc).isoformat(timespec="seconds"), "google"))
    conn.commit(); conn.close()

    skip, age = guard_decision(str(p), force=False)
    assert skip is False, "sweep 的觀測不該讓 monitor 以為資料還新"
    assert age >= 119


# ---- 邊界與旁路 -------------------------------------------------------------

def test_boundary_at_guard_minutes(tmp_path):
    assert guard_decision(str(_db(tmp_path / "a.db", minutes_ago=GUARD_MINUTES + 1)),
                          force=False)[0] is False
    assert guard_decision(str(_db(tmp_path / "b.db", minutes_ago=GUARD_MINUTES - 2)),
                          force=False)[0] is True


def test_future_timestamp_runs(tmp_path):
    """時鐘漂移/異常的未來時間戳：資料齡為負 → 照常執行，不得永久卡跳過。"""
    p = _db(tmp_path / "p.db", minutes_ago=-30)
    assert guard_decision(str(p), force=False)[0] is False


def test_force_bypasses_guard(tmp_path, monkeypatch):
    p = _db(tmp_path / "p.db", minutes_ago=5)
    assert guard_decision(str(p), force=True)[0] is False
    monkeypatch.setenv("FAREHUNTER_FORCE", "1")
    assert guard_decision(str(p))[0] is False
    monkeypatch.setenv("FAREHUNTER_FORCE", "0")
    assert guard_decision(str(p))[0] is True


def test_run_short_circuits_before_any_side_effect(tmp_path, monkeypatch):
    """run() 在建立 client 之前就短路：無網路、無 config 也能跳過。"""
    monkeypatch.delenv("FAREHUNTER_FORCE", raising=False)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "gh_out"))  # 隔離 CI 測試步驟
    p = _db(tmp_path / "p.db", minutes_ago=10)
    summary = run(config_path=str(tmp_path / "no.yaml"), db_path=str(p))
    assert summary["skipped"] is True
    assert summary["searched"] == summary["recorded"] == 0


def test_emit_skip_output_writes_github_output(tmp_path, monkeypatch):
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    _emit_skip_output()
    assert "skip=true" in out.read_text(encoding="utf-8")
    monkeypatch.delenv("GITHUB_OUTPUT")
    _emit_skip_output()  # 本地無 GITHUB_OUTPUT：不得拋錯
