"""自我檢查：Google 實價斷了、通知連續送不出去時回報問題（health.self_check，2026-11-24）。零真實 API。"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import farehunter.runner as runner_mod                              # noqa: E402
from farehunter import health                                       # noqa: E402
from farehunter.models import Offer                                 # noqa: E402
from farehunter.storage import Store                                # noqa: E402

NOW = datetime(2099, 3, 1, 12, 19, tzinfo=timezone.utc)
ROUTES = [("TPE", "NRT"), ("KHH", "CTS")]
ROOT = Path(__file__).resolve().parents[1]


def _iso(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


def _google(st, o, d, hours_ago, source="google"):
    st.record(Offer(o, d, "2099-04-01", "2099-04-06", 8000.0, "TWD", "IT", 0, "",
                    source=source, provider="brightdata"), observed_at=_iso(hours_ago))


def _run(st, hours_ago, google_requests=None, notify_failures=0):
    st.conn.execute("INSERT INTO monitor_runs (started_at, google_requests, notify_failures)"
                    " VALUES (?,?,?)", (_iso(hours_ago), google_requests, notify_failures))
    st.conn.commit()


def _page(st, o, d, hours_ago, parsed, dep="2099-04-01"):
    st.conn.execute("INSERT OR REPLACE INTO api_checks VALUES ('brightdata',?,?,?,'2099-04-06',?,?)",
                    (o, d, dep, _iso(hours_ago), parsed))
    st.conn.commit()


def _store(tmp_path):
    return Store(str(tmp_path / "p.db"))


def _fresh(st, hours_ago=0.5):
    for o, d in ROUTES:
        _google(st, o, d, hours_ago)


# ---- Google 實價有沒有在更新 -----------------------------------------------------

def test_fresh_google_prices_on_every_route_is_quiet(tmp_path):
    st = _store(tmp_path)
    _fresh(st)
    _run(st, 0.1, google_requests=7)
    assert health.self_check(st.conn, ROUTES, NOW) == []


def test_cache_prices_do_not_count_as_google_prices(tmp_path):
    st = _store(tmp_path)
    _fresh(st, hours_ago=5)
    for o, d in ROUTES:
        _google(st, o, d, 0.2, source="aviasales")
    problems = health.self_check(st.conn, ROUTES, NOW)
    assert len(problems) == 1 and problems[0].startswith("全部 2 條航線已 5 小時沒有新的 Google 實價")


def test_an_all_route_outage_is_one_problem_with_the_likely_cause(tmp_path):
    cases = [
        (lambda st: None, "一輪都沒有跑"),
        (lambda st: _run(st, 1, google_requests=0), "沒有送出任何 Google 實價查詢"),
        (lambda st: _run(st, 1, google_requests=21), "送出 21 次查詢都沒拿到頁面"),
        (lambda st: (_run(st, 1, google_requests=21), _page(st, "TPE", "NRT", 1, 0),
                     _page(st, "KHH", "CTS", 2, 0)), "拿到 2 頁都解析不到航班（Google 可能改版"),
        (lambda st: (_run(st, 1, google_requests=21), _page(st, "TPE", "NRT", 1, 1)),
         "有 1 頁解析到航班，但沒有可寫入的直飛價"),
    ]
    for i, (setup, cause) in enumerate(cases):
        st = Store(str(tmp_path / f"p{i}.db"))
        _fresh(st, hours_ago=30)                     # 每條線都 30 小時前：只報一則全面中斷
        setup(st)
        problems = health.self_check(st.conn, ROUTES, NOW)
        assert len(problems) == 1, problems
        assert problems[0].startswith("全部 2 條航線已 30 小時沒有新的 Google 實價：")
        assert cause in problems[0], (cause, problems[0])
        st.close()


def test_threshold_edges(tmp_path):
    st = _store(tmp_path)
    _google(st, "TPE", "NRT", health.GOOGLE_ALL_ROUTES_HOURS - 0.1)
    _google(st, "KHH", "CTS", health.GOOGLE_ROUTE_HOURS - 0.1)
    assert health.self_check(st.conn, ROUTES, NOW) == []


def test_one_quiet_route_is_listed_on_its_own(tmp_path):
    st = _store(tmp_path)
    _google(st, "TPE", "NRT", 0.5)
    _google(st, "KHH", "CTS", 26)
    _page(st, "KHH", "CTS", 3, 1)
    assert health.self_check(st.conn, ROUTES, NOW) == [
        "KHH-CTS 已 26 小時沒有新的 Google 實價：1 頁有航班但沒有直飛價（可能這段日子沒有直飛）"]


def test_route_causes(tmp_path):
    st = _store(tmp_path)
    _google(st, "TPE", "NRT", 0.5)
    assert health.self_check(st.conn, ROUTES, NOW) == [
        "KHH-CTS 從來沒有 Google 實價：24 小時內沒有拿到這條線的頁面（巡檢沒排到，或查詢失敗）"]
    _page(st, "KHH", "CTS", 2, 0)
    _page(st, "KHH", "CTS", 30, 1, dep="2099-04-02")          # 24 小時以前的不算
    assert health.self_check(st.conn, ROUTES, NOW)[0].endswith("拿到 1 頁都解析不到航班")


def test_rows_written_after_now_are_ignored(tmp_path):
    """重播過去某個時間點時，之後才寫入的實價不能算進去。"""
    st = _store(tmp_path)
    _fresh(st, hours_ago=-2)
    assert health.self_check(st.conn, ROUTES, NOW)[0].startswith("全部航線都沒有 Google 實價")


# ---- 通知送不送得到 ---------------------------------------------------------------

def test_notify_failures_need_two_runs_in_a_row(tmp_path):
    st = _store(tmp_path)
    _fresh(st)
    _run(st, 2, notify_failures=1)
    _run(st, 1, notify_failures=0)
    assert health.self_check(st.conn, ROUTES, NOW) == []          # 中間成功過
    _run(st, 0.1, notify_failures=3)
    assert health.self_check(st.conn, ROUTES, NOW) == []          # 只有最近一輪
    _run(st, 0.05, notify_failures=2)
    assert health.self_check(st.conn, ROUTES, NOW) == [
        "通知連續 2 輪送不出去（最近一輪 2 則；Telegram／LINE 的金鑰或服務的問題）"]


def test_store_counts_failures_on_the_latest_run_and_keeps_two_days(tmp_path):
    st = _store(tmp_path)
    st.note_notify_failure()                                       # 沒有這輪紀錄：不做事
    st.record_monitor_run(_iso(60))
    st.record_monitor_run(_iso(30))
    st.record_monitor_run(_iso(1))
    st.note_notify_failure()
    st.note_notify_failure()
    st.finish_monitor_run(_iso(1), google_requests=9)
    rows = st.conn.execute("SELECT started_at, google_requests, notify_failures FROM monitor_runs"
                           " ORDER BY started_at").fetchall()
    assert [tuple(r) for r in rows] == [(_iso(30), None, 0), (_iso(1), 9, 2)]   # 60 小時前的刪掉


def test_old_monitor_runs_table_gets_the_new_columns(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE monitor_runs (started_at TEXT NOT NULL)")
    con.execute("INSERT INTO monitor_runs VALUES ('2099-03-01T10:17:00+00:00')")
    con.commit()
    con.close()
    st = Store(str(db))
    assert tuple(st.conn.execute("SELECT * FROM monitor_runs").fetchone()) == (
        "2099-03-01T10:17:00+00:00", None, 0)


# ---- runner：記下查詢數與送不出去的通知 ---------------------------------------------

class _Client:
    def __init__(self, *a, **k):
        pass

    def search_month(self, *a, **k):
        return {}


def test_runner_records_requests_and_counts_a_failed_alert(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("defaults:\n  currency: twd\n  months_ahead: 1\n  pause_seconds: 0\n"
                   "routes:\n  - origin: TPE\n    destination: NRT\n    absolute_threshold: 9000\n",
                   encoding="utf-8")
    offer = Offer("TPE", "NRT", (datetime.now(timezone.utc).date() + timedelta(days=40)).isoformat(),
                  (datetime.now(timezone.utc).date() + timedelta(days=45)).isoformat(),
                  8000.0, "TWD", "IT", 0, "190", source="aviasales", provider="travelpayouts")
    monkeypatch.setattr(runner_mod, "TravelpayoutsClient", _Client)
    monkeypatch.setattr(runner_mod, "parse_offers", lambda *a, **k: [offer])
    monkeypatch.setattr(runner_mod, "notify", lambda *a, **k: [])      # 一個管道都沒送到
    monkeypatch.setattr(runner_mod, "channels_configured", lambda: True)
    monkeypatch.setenv("FAREHUNTER_FORCE", "1")
    runner_mod.run(str(cfg), str(tmp_path / "prices.db"))
    con = sqlite3.connect(tmp_path / "prices.db")
    row = con.execute("SELECT google_requests, notify_failures FROM monitor_runs").fetchone()
    assert row == (0, 1)                       # 沒設任何 Google 來源的金鑰 → 0 次查詢


# ---- 命令列（monitor.yml 的心跳步驟呼叫） -----------------------------------------

def test_main_prints_one_line_per_problem(tmp_path, capsys):
    st = _store(tmp_path)
    _google(st, "TPE", "NRT", 0.5)
    st.close()
    cfg = tmp_path / "config.yaml"
    cfg.write_text("routes:\n  - {origin: TPE, destination: NRT}\n  - {origin: KHH, destination: CTS}\n",
                   encoding="utf-8")
    assert health.main([str(tmp_path / "p.db"), str(cfg)], now=NOW) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["KHH-CTS 從來沒有 Google 實價：24 小時內沒有拿到這條線的頁面（巡檢沒排到，或查詢失敗）"]


def test_main_reports_its_own_failure_instead_of_saying_all_good(tmp_path, capsys):
    assert health.main([str(tmp_path / "missing.db"), str(tmp_path / "missing.yaml")], now=NOW) == 0
    assert capsys.readouterr().out.startswith("自我檢查程式出錯（")


def test_heartbeat_step_reports_fail_with_the_reasons_and_never_prints_the_url():
    wf = (ROOT / ".github/workflows/monitor.yml").read_text(encoding="utf-8")
    step = wf[wf.index("- name: Heartbeat"):]
    assert "python -m farehunter.health prices.db config.yaml" in step
    assert '--data-binary @"$CHECK_FILE" "$HC_PING_URL/fail"' in step
    assert 'echo "$HC_PING_URL' not in step and "set -x" not in step
