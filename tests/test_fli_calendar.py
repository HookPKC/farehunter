"""fli 觀察期（src/farehunter/fli_calendar.py）。

全部用假的後端：CI 不安裝 fli（它只在 fli-observe workflow 裡裝），所以這裡驗的是
我們自己的邏輯——時段切法、重試、影子比價的挑選與去重、輸出檔、全滅判定、報告的
判斷標準——以及 fli 0.9.0 機場別名 bug 的繞法（用一個同樣有重複值的假 Enum 重現）。
"""
import gzip
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from enum import Enum

import pytest

from farehunter import fli_calendar as fc
from farehunter.fli_calendar import FliError
from farehunter.storage import Store

NOW = datetime(2026, 10, 26, 3, 41, tzinfo=timezone.utc)
TODAY = NOW.date()


class FakeBackend:
    """calendar_plan / cheapest_plan：每次呼叫依序取一個結果；Exception 會被丟出。"""

    def __init__(self, calendar_plan=None, cheapest_plan=None, default_days=None):
        self.calendar_plan = list(calendar_plan or [])
        self.cheapest_plan = list(cheapest_plan or [])
        self.default_days = default_days if default_days is not None else {"2026-12-07": 14891.0}
        self.requests = 0
        self.calls = []

    def calendar(self, origin, dest, start, end, stay):
        self.requests += 1
        self.calls.append(("calendar", origin, dest, start, end, stay))
        item = self.calendar_plan.pop(0) if self.calendar_plan else self.default_days
        if isinstance(item, Exception):
            raise item
        return item

    def cheapest(self, origin, dest, dep, ret):
        self.requests += 1
        self.calls.append(("cheapest", origin, dest, dep, ret))
        item = self.cheapest_plan.pop(0) if self.cheapest_plan else (14891.0, "IT")
        if isinstance(item, Exception):
            raise item
        return item


def _config(tmp_path, routes=("KHH-NGO",), months=10):
    lines = ["defaults:", "  currency: twd", f"  months_ahead: {months}", "routes:"]
    for r in routes:
        o, d = r.split("-")
        lines += [f"  - origin: {o}", f"    destination: {d}"]
    p = tmp_path / "config.yaml"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


def _db(tmp_path, rows=()):
    """建一個有真實 schema 的 prices.db，再以明確的 observed_at 寫入觀測。"""
    path = tmp_path / "prices.db"
    Store(str(path)).close()
    conn = sqlite3.connect(path)
    for r in rows:
        conn.execute(
            """INSERT INTO observations (origin, destination, depart_date, return_date, price,
                   currency, carriers, stops, duration, observed_at, fare_class, source, provider)
               VALUES (?,?,?,?,?,'TWD',?,?,'',?,?,?,?)""",
            (r["o"], r["d"], r["dep"], r.get("ret", "2026-12-12"), r.get("price", 14891.0),
             r.get("carriers", "IT"), r.get("stops", 0), r["at"], r.get("fare_class", "any"),
             r.get("source", "google"), r.get("provider", "serpapi")))
    conn.commit()
    conn.close()
    return str(path)


def _at(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


# ---- 時段切法 -------------------------------------------------------------------

def test_windows_start_tomorrow_and_cap_at_horizon():
    ws = fc.windows(TODAY, 300)
    assert ws[0][0] == TODAY + timedelta(days=1)            # fli 不收今天
    assert all((b - a).days + 1 <= fc.WINDOW_DAYS for a, b in ws)
    assert ws[-1][1] == TODAY + timedelta(days=300)
    assert [(a - TODAY).days for a, _ in ws] == [1, 62, 123, 184, 245]
    for (_, b), (a2, _) in zip(ws, ws[1:]):
        assert a2 == b + timedelta(days=1)                   # 不重疊、不漏日


def test_horizon_follows_months_ahead_but_never_exceeds_fli_limit():
    assert fc.horizon_days({"defaults": {"months_ahead": 2}}) == 62
    assert fc.horizon_days({"defaults": {"months_ahead": 10}}) == fc.MAX_AHEAD_DAYS
    assert fc.horizon_days({}) == fc.MAX_AHEAD_DAYS


# ---- 日曆重試 -------------------------------------------------------------------

def test_calendar_empty_then_priced_is_retried_once():
    sleeps = []
    backend = FakeBackend(calendar_plan=[{}, {"2026-11-02": 8691.0}])
    recs = fc.sweep_calendar(backend, [("KHH", "NGO")], TODAY, 61, sleep=sleeps.append)
    assert len(recs) == 1
    assert recs[0]["status"] == "ok" and recs[0]["attempts"] == 2
    assert recs[0]["days"] == {"2026-11-02": 8691.0}
    assert sleeps == [fc.RETRY_WAIT_S]


def test_calendar_ok_first_try_does_not_sleep():
    sleeps = []
    recs = fc.sweep_calendar(FakeBackend(), [("KHH", "NGO")], TODAY, 61, sleep=sleeps.append)
    assert recs[0]["attempts"] == 1 and sleeps == []


def test_calendar_errors_are_recorded_not_raised():
    backend = FakeBackend(calendar_plan=[FliError("google_error: HTTP 200 324B"),
                                         FliError("google_error: HTTP 200 324B")])
    recs = fc.sweep_calendar(backend, [("KHH", "OKA")], TODAY, 61, sleep=lambda s: None)
    assert recs[0]["status"] == "error" and recs[0]["attempts"] == 1 + fc.CAL_RETRIES
    assert recs[0]["error"].startswith("google_error")


def test_budget_exhaustion_stops_retrying():
    backend = FakeBackend(calendar_plan=[FliError("budget: 200 requests used")] * 9)
    recs = fc.sweep_calendar(backend, [("KHH", "NGO")], TODAY, 61, sleep=lambda s: None)
    assert recs[0]["attempts"] == 1                          # 預算用完就不再重打


def test_calendar_records_window_index():
    recs = fc.sweep_calendar(FakeBackend(), [("KHH", "NGO")], TODAY, 300, sleep=lambda s: None)
    assert [r["idx"] for r in recs] == [0, 1, 2, 3, 4]


# ---- 機場別名 bug 的繞法 ---------------------------------------------------------

def test_resolve_airport_fixes_enum_aliases():
    # 重現 fli 0.9.0：Enum(name, {代碼: 機場名})，名稱重複的代碼變成別名
    Airport = Enum("Airport", {"NAH": "Naha Airport", "OKA": "Naha Airport",
                               "KHH": "Kaohsiung International Airport"})
    assert Airport.OKA.name == "NAH"                          # bug 本身
    oka = fc.resolve_airport(Airport, "OKA")
    assert oka.name == "OKA" and isinstance(oka, Airport)     # fli 序列化只讀 .name
    assert oka.value != Airport.NAH.value                     # 不讓依值查找換回 NAH
    assert fc.resolve_airport(Airport, "KHH") is Airport.KHH  # 沒問題的代碼原樣回傳
    with pytest.raises(KeyError):
        fc.resolve_airport(Airport, "XXX")


# ---- 影子比價 -------------------------------------------------------------------

def test_shadow_targets_pick_recent_google_nonstop_any(tmp_path):
    db = _db(tmp_path, [
        {"o": "KHH", "d": "NGO", "dep": "2026-12-07", "at": _at(2)},                       # ✓
        {"o": "KHH", "d": "NGO", "dep": "2026-12-07", "at": _at(2), "price": 21000.0,
         "fare_class": "full"},                                                          # full-service
        {"o": "KHH", "d": "CTS", "dep": "2026-11-13", "at": _at(30)},                      # 太舊
        {"o": "TPE", "d": "NRT", "dep": "2026-11-30", "at": _at(1), "source": "aviasales",
         "provider": None},                                                              # 不是 Google
        {"o": "TPE", "d": "NRT", "dep": "2026-10-20", "at": _at(1)},                       # 出發日已過
        {"o": "KHH", "d": "FUK", "dep": "2026-11-27", "at": _at(3), "stops": 1},           # 轉機
        {"o": "KHH", "d": "NRT", "dep": "2026-10-28", "at": _at(5), "provider": "scrapedo",
         "ret": "2026-11-04", "price": 7560.0, "carriers": "GK"},                          # ✓
    ])
    seen = set()
    got = fc.shadow_targets(db, NOW, seen)
    assert [(t["route"], t["depart"], t["ref_provider"]) for t in got] == [
        ("KHH-NGO", "2026-12-07", "serpapi"), ("KHH-NRT", "2026-10-28", "scrapedo")]
    assert got[0]["ref_price"] == 14891.0                    # 取 any 的價，不是 full-service
    assert len(seen) == 2
    assert fc.shadow_targets(db, NOW, seen) == []            # 比過的不再比


def test_shadow_targets_respect_limit(tmp_path):
    db = _db(tmp_path, [{"o": "TPE", "d": "NRT", "dep": f"2026-12-{d:02d}", "at": _at(1)}
                        for d in range(1, 21)])
    assert len(fc.shadow_targets(db, NOW, set(), limit=5)) == 5


def test_shadow_targets_open_db_read_only(tmp_path):
    db = _db(tmp_path, [{"o": "KHH", "d": "NGO", "dep": "2026-12-07", "at": _at(2)}])
    before = (tmp_path / "prices.db").read_bytes()
    fc.shadow_targets(db, NOW, set())
    assert (tmp_path / "prices.db").read_bytes() == before


def test_run_shadow_retries_then_records_diff():
    target = {"route": "KHH-NGO", "depart": "2026-12-07", "return": "2026-12-12",
              "ref_price": 14891.0, "ref_provider": "serpapi", "ref_observed_at": _at(2),
              "ref_carriers": "IT"}
    sleeps = []
    backend = FakeBackend(cheapest_plan=[FliError("google_error: x"), (15040.0, "IT")])
    rec = fc.run_shadow(backend, [target], NOW, sleep=sleeps.append)[0]
    assert rec["status"] == "ok" and rec["attempts"] == 2 and rec["error"] is None
    assert rec["fli_price"] == 15040.0 and rec["fli_carriers"] == "IT"
    assert rec["diff_pct"] == pytest.approx(1.0, abs=0.01)
    assert rec["lag_hours"] == 2.0
    assert sleeps == [fc.RETRY_WAIT_S]


def test_run_shadow_none_and_exhausted_errors():
    t = {"route": "KHH-OKA", "depart": "2026-12-07", "return": "2026-12-12", "ref_price": 5805.0,
         "ref_provider": "serpapi", "ref_observed_at": _at(1), "ref_carriers": "IT"}
    none = fc.run_shadow(FakeBackend(cheapest_plan=[None]), [t], NOW, sleep=lambda s: None)[0]
    assert none["status"] == "none" and none["diff_pct"] is None
    err = fc.run_shadow(FakeBackend(cheapest_plan=[FliError("http: 503")] * 5), [t], NOW,
                        sleep=lambda s: None)[0]
    assert err["status"] == "error" and err["attempts"] == 1 + fc.SHADOW_RETRIES


# ---- run()：輸出檔、去重、全滅判定 ----------------------------------------------

def test_run_writes_gz_jsonl_and_summary(tmp_path):
    cfg = _config(tmp_path, routes=("KHH-NGO", "TPE-NRT"), months=2)
    db = _db(tmp_path, [{"o": "KHH", "d": "NGO", "dep": "2026-12-07", "at": _at(2)}])
    out = tmp_path / "fli"
    summary = fc.run(cfg, db, str(out), now=NOW, backend=FakeBackend(), sleep=lambda s: None)
    files = list(out.glob("*.jsonl.gz"))
    assert [f.name for f in files] == ["2026-10-26T0341Z.jsonl.gz"]  # 檔名來自注入的 now
    recs = fc.read_records(files[0])
    kinds = [r["kind"] for r in recs]
    assert kinds.count("calendar") == 2 * len(fc.windows(TODAY, 62))
    assert kinds.count("shadow") == 1 and kinds[-1] == "summary"
    assert summary["all_failed"] is False and summary["calendar_ok"] == kinds.count("calendar")
    assert summary["shadow"] == [
        "KHH-NGO 2026-12-07/2026-12-12 ok +0.0% (fli 14891 vs serpapi 14891, lag 2.0h)"]
    assert summary["started_at"] == NOW.isoformat(timespec="seconds")
    assert summary["finished_at"] >= summary["started_at"]


def test_run_never_overwrites_and_dedupes_shadow(tmp_path):
    cfg = _config(tmp_path, months=1)
    db = _db(tmp_path, [{"o": "KHH", "d": "NGO", "dep": "2026-12-07", "at": _at(2)}])
    out = tmp_path / "fli"
    fc.run(cfg, db, str(out), now=NOW, backend=FakeBackend(), sleep=lambda s: None)
    second = fc.run(cfg, db, str(out), now=NOW, backend=FakeBackend(), sleep=lambda s: None)
    names = sorted(f.name for f in out.glob("*.jsonl.gz"))
    assert names == ["2026-10-26T0341Z-2.jsonl.gz", "2026-10-26T0341Z.jsonl.gz"]
    assert second["shadow_targets"] == 0                     # 上一輪比過，這輪不再比


def test_run_all_failed_when_nothing_answered(tmp_path):
    cfg = _config(tmp_path, months=1)
    db = _db(tmp_path)
    backend = FakeBackend(calendar_plan=[FliError("blocked: captcha page HTTP 429")] * 9)
    summary = fc.run(cfg, db, str(tmp_path / "fli"), now=NOW, backend=backend,
                     sleep=lambda s: None)
    assert summary["all_failed"] is True


def test_run_all_empty_counts_as_failed(tmp_path):
    # Google 有回應但全部時段都沒價、也沒有比價：8 條航線同時全空只會是封鎖或 fli
    # 解析悄悄失效（不會丟例外），所以也要讓 workflow 變紅
    cfg = _config(tmp_path, months=1)
    summary = fc.run(cfg, _db(tmp_path), str(tmp_path / "fli"), now=NOW,
                     backend=FakeBackend(calendar_plan=[{}, {}]), sleep=lambda s: None)
    assert summary["calendar_ok"] == 0 and summary["all_failed"] is True


def test_run_one_priced_window_is_not_all_failed(tmp_path):
    cfg = _config(tmp_path, routes=("KHH-NGO", "KHH-OKA"), months=1)
    backend = FakeBackend(calendar_plan=[{}, {}, {"2026-12-01": 5692.0}])
    summary = fc.run(cfg, _db(tmp_path), str(tmp_path / "fli"), now=NOW, backend=backend,
                     sleep=lambda s: None)
    assert summary["calendar_ok"] == 1 and summary["all_failed"] is False


def test_main_exit_code(tmp_path, monkeypatch):
    cfg = _config(tmp_path, months=1)
    db = _db(tmp_path)
    monkeypatch.setattr(fc, "FliBackend", lambda **kw: FakeBackend(
        calendar_plan=[FliError("transport: ConnectError")] * 9))
    monkeypatch.setattr(fc.time, "sleep", lambda s: None)
    assert fc.main(["x", "run", cfg, db, str(tmp_path / "fli")]) == 1
    monkeypatch.setattr(fc, "FliBackend", lambda **kw: FakeBackend())
    assert fc.main(["x", "run", cfg, db, str(tmp_path / "fli")]) == 0


# ---- 報告（第二階段判斷）---------------------------------------------------------

def _write(out, stamp, recs):
    out.mkdir(parents=True, exist_ok=True)
    with gzip.open(out / f"{stamp}.jsonl.gz", "wt", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")


def _cal(route, idx, status, attempts=1):
    return {"kind": "calendar", "route": route, "idx": idx, "status": status,
            "attempts": attempts, "days": {"2026-12-07": 1.0} if status == "ok" else {},
            "error": "google_error: x" if status == "error" else None}


def _sh(diff, lag=1.0, status="ok"):
    return {"kind": "shadow", "status": status, "diff_pct": diff, "lag_hours": lag, "error": None}


def test_report_scores_only_windows_that_ever_had_prices(tmp_path):
    out = tmp_path / "fli"
    _write(out, "2026-10-25T0341Z", [
        _cal("KHH-NGO", 0, "ok"), _cal("KHH-NGO", 1, "empty"),      # #1 從沒價 → 沒班表
        _cal("TPE-NRT", 0, "ok", attempts=2), _sh(0.5), _sh(1.9), _sh(5.0, lag=12.0),
        {"kind": "summary", "requests": 10, "all_failed": False}])
    _write(out, "2026-10-26T0341Z", [
        _cal("KHH-NGO", 0, "error"), _cal("KHH-NGO", 1, "empty"),   # #0 曾有價 → 漏報
        _cal("TPE-NRT", 0, "ok"), _sh(-3.0),
        {"kind": "summary", "requests": 8, "all_failed": False}])
    res = fc.report(str(out), days=14, now=NOW)
    assert res["runs"] == 2 and res["requests"] == 18
    assert res["calendar_never_priced"] == ["KHH-NGO#1"]
    assert res["calendar_scored"] == 4
    assert res["calendar_ok_rate"] == 0.75                    # 3/4
    assert res["calendar_first_try_rate"] == 0.5              # 2/4（一筆重試才成功）
    assert res["shadow_fresh"] == 3                           # lag 12h 那筆不評準確度
    assert res["shadow_within_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert res["criteria"]["calendar_ok_rate>=95%"] == "FAIL"
    assert res["criteria"]["no_blocking"] == "PASS"
    assert res["error_kinds"] == {"google_error": 1}


def test_report_ignores_files_older_than_window_and_handles_no_data(tmp_path):
    out = tmp_path / "fli"
    _write(out, "2026-09-01T0000Z", [_cal("KHH-NGO", 0, "ok")])
    res = fc.report(str(out), days=14, now=NOW)
    assert res["calendar_windows"] == 0
    assert res["criteria"]["calendar_ok_rate>=95%"] == "NO DATA"


def test_module_imports_and_runs_without_fli(monkeypatch):
    # CI 不裝 fli：模組 import 與所有純邏輯都不能碰 fli；只有真的建 FliBackend 才需要
    import importlib
    import sys
    for name in [m for m in sys.modules if m == "fli" or m.startswith("fli.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "fli", None)          # 讓 import fli 必定失敗
    mod = importlib.reload(fc)
    recs = mod.sweep_calendar(FakeBackend(), [("KHH", "NGO")], TODAY, 61, sleep=lambda s: None)
    assert recs[0]["status"] == "ok"
    with pytest.raises(ImportError):
        mod.FliBackend()
