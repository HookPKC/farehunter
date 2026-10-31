"""fli 觀察期（src/farehunter/fli_calendar.py）。

兩層：
1. 純邏輯，用假的後端：時段切法、重試、Breaker（封鎖即停、預算、連線錯誤、時限）、
   影子比價的挑選與去重、輸出檔、紅燈判定、報告的判斷標準。CI（monitor）不裝 fli，
   這一層一定要跑得動。
2. FliBackend 本身（`pytest.importorskip("fli")`，只在裝了 fli 的 fli-observe workflow
   裡跑）：用自己組的 Google 回應驗請求數、錯誤分類、OKA 別名繞法的請求端與回應端。
   fli 的日期驗證用真實時鐘，所以這一層的日期一律由 date.today() 推算。
"""
import gzip
import itertools
import json
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

import pytest

from farehunter import fli_calendar as fc
from farehunter.fli_calendar import Breaker, FliError
from farehunter.storage import Store

NOW = datetime(2026, 10, 26, 3, 41, tzinfo=timezone.utc)
TODAY = NOW.date()
NO_SLEEP = lambda s: None  # noqa: E731


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


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _config(tmp_path, routes=("KHH-NGO",), months=10):
    lines = ["defaults:", "  currency: twd", f"  months_ahead: {months}", "routes:"]
    for r in routes:
        o, d = r.split("-")
        lines += [f"  - origin: {o}", f"    destination: {d}"]
    p = tmp_path / "config.yaml"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


def _db(tmp_path, rows=(), name="prices.db"):
    """建一個有真實 schema 的 prices.db，再以明確的 observed_at 寫入觀測。"""
    path = tmp_path / name
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


def _target(**kw):
    t = {"route": "KHH-NGO", "depart": "2026-12-07", "return": "2026-12-12",
         "ref_price": 14891.0, "ref_provider": "serpapi", "ref_observed_at": _at(2),
         "ref_carriers": "IT"}
    t.update(kw)
    return t


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


# ---- 日曆重試與 Breaker -----------------------------------------------------------

def test_calendar_empty_then_priced_is_retried_once():
    sleeps = []
    backend = FakeBackend(calendar_plan=[{}, {"2026-11-02": 8691.0}])
    recs = fc.sweep_calendar(backend, [("KHH", "NGO")], TODAY, 61, sleep=sleeps.append)
    assert len(recs) == 1
    assert recs[0]["status"] == "ok" and recs[0]["attempts"] == 2
    assert recs[0]["days"] == {"2026-11-02": 8691.0} and recs[0]["errors"] == []
    assert sleeps == [fc.RETRY_WAIT_S]


def test_calendar_ok_first_try_does_not_sleep():
    sleeps = []
    recs = fc.sweep_calendar(FakeBackend(), [("KHH", "NGO")], TODAY, 61, sleep=sleeps.append)
    assert recs[0]["attempts"] == 1 and sleeps == []


def test_calendar_empty_twice_is_empty_not_error():
    recs = fc.sweep_calendar(FakeBackend(calendar_plan=[{}, {}]), [("KHH", "NGO")], TODAY, 61,
                             sleep=NO_SLEEP)
    assert recs[0]["status"] == "empty" and recs[0]["attempts"] == 2


def test_calendar_errors_are_recorded_not_raised():
    backend = FakeBackend(calendar_plan=[FliError("google_error: HTTP 200 324B")] * 2)
    recs = fc.sweep_calendar(backend, [("KHH", "OKA")], TODAY, 61, sleep=NO_SLEEP)
    assert recs[0]["status"] == "error" and recs[0]["attempts"] == 1 + fc.CAL_RETRIES
    assert recs[0]["errors"] == ["google_error: HTTP 200 324B"] * 2


def test_error_then_success_keeps_the_error():
    # 重試成功也要留下第一次的錯誤——報告靠它數「曾被擋幾次」
    backend = FakeBackend(calendar_plan=[FliError("google_error: x"), {"2026-12-07": 1.0}])
    rec = fc.sweep_calendar(backend, [("KHH", "NGO")], TODAY, 61, sleep=NO_SLEEP)[0]
    assert rec["status"] == "ok" and rec["errors"] == ["google_error: x"]


def test_blocked_stops_the_whole_run_immediately():
    backend = FakeBackend(calendar_plan=[{"2026-11-02": 1.0}, FliError("blocked: HTTP 429")])
    breaker = Breaker()
    recs = fc.sweep_calendar(backend, [("KHH", "NGO"), ("TPE", "NRT")], TODAY, 300,
                             breaker=breaker, sleep=NO_SLEEP)
    assert backend.requests == 2                              # 封鎖後一個請求都不再發
    assert breaker.reason.startswith("blocked")
    assert recs[1]["status"] == "error" and recs[1]["attempts"] == 1
    assert all(r["errors"] == [f"skipped: {breaker.reason}"] for r in recs[2:])
    shadow = fc.run_shadow(backend, [_target()], NOW, breaker=breaker, sleep=NO_SLEEP)
    assert backend.requests == 2 and shadow[0]["errors"][0].startswith("skipped")


def test_budget_exhaustion_stops_retrying():
    backend = FakeBackend(calendar_plan=[FliError("budget: 200 requests used")] * 9)
    recs = fc.sweep_calendar(backend, [("KHH", "NGO")], TODAY, 122, sleep=NO_SLEEP)
    assert backend.requests == 1
    assert recs[1]["errors"][0].startswith("skipped: budget")


def test_consecutive_transport_errors_trip_the_breaker():
    backend = FakeBackend(calendar_plan=[FliError("transport: ConnectTimeout")] * 20)
    breaker = Breaker()
    fc.sweep_calendar(backend, [("KHH", "NGO")], TODAY, 300, breaker=breaker, sleep=NO_SLEEP)
    assert backend.requests == fc.MAX_TRANSPORT_ERRORS
    assert breaker.reason.startswith("transport")


def test_transport_streak_resets_on_any_answer():
    plan = [FliError("transport: x")] * (fc.MAX_TRANSPORT_ERRORS - 1) + [{}] + \
        [FliError("transport: x")] * (fc.MAX_TRANSPORT_ERRORS - 1) + [{"2026-12-07": 1.0}] * 9
    breaker = Breaker()
    fc.sweep_calendar(FakeBackend(calendar_plan=plan), [("KHH", "NGO")], TODAY, 300,
                      breaker=breaker, sleep=NO_SLEEP)
    assert breaker.reason is None


def test_deadline_stops_further_requests():
    clock = FakeClock()
    breaker = Breaker(deadline_s=100, clock=clock)

    class Slow(FakeBackend):
        def calendar(self, *a):
            clock.t += 60                                    # 每次請求 60 秒
            return super().calendar(*a)
    backend = Slow()
    recs = fc.sweep_calendar(backend, [("KHH", "NGO")], TODAY, 300, breaker=breaker,
                             sleep=NO_SLEEP)
    assert backend.requests == 2 and breaker.reason.startswith("deadline")
    assert [r["status"] for r in recs] == ["ok", "ok", "error", "error", "error"]


def test_calendar_records_window_index():
    recs = fc.sweep_calendar(FakeBackend(), [("KHH", "NGO")], TODAY, 300, sleep=NO_SLEEP)
    assert [r["idx"] for r in recs] == [0, 1, 2, 3, 4]


# ---- 機場別名 bug 的繞法（請求端；回應端見下方 FliBackend 測試）---------------------

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


# ---- 影子比價的挑選 ---------------------------------------------------------------

def test_shadow_targets_pick_recent_google_nonstop_any(tmp_path):
    db = _db(tmp_path, [
        {"o": "KHH", "d": "NGO", "dep": "2026-12-07", "at": _at(2)},                       # ✓
        {"o": "KHH", "d": "NGO", "dep": "2026-12-07", "at": _at(2), "price": 9000.0,
         "fare_class": "full"},          # full-service 比較便宜也不能被 MIN 選到
        {"o": "KHH", "d": "CTS", "dep": "2026-11-13", "at": _at(25)},                      # 太舊
        {"o": "TPE", "d": "NRT", "dep": "2026-11-30", "at": _at(1), "source": "aviasales",
         "provider": None},                                                              # 不是 Google
        {"o": "TPE", "d": "NRT", "dep": TODAY.isoformat(), "at": _at(1)},                  # 今天出發
        {"o": "KHH", "d": "FUK", "dep": "2026-11-27", "at": _at(3), "stops": 1},           # 轉機
        {"o": "KHH", "d": "KIX", "dep": "2026-11-20", "at": _at(3), "ret": None},          # 單程
        {"o": "KHH", "d": "NRT", "dep": "2026-10-28", "at": _at(5), "provider": "scrapedo",
         "ret": "2026-11-04", "price": 7560.0, "carriers": "GK"},                          # ✓
    ])
    seen = set()
    got = fc.shadow_targets(db, NOW, seen)
    assert [(t["route"], t["depart"], t["ref_provider"]) for t in got] == [
        ("KHH-NGO", "2026-12-07", "serpapi"), ("KHH-NRT", "2026-10-28", "scrapedo")]
    assert got[0]["ref_price"] == 14891.0
    assert len(seen) == 2
    assert fc.shadow_targets(db, NOW, seen) == []            # 比過的不再比


def test_shadow_targets_lookback_boundary(tmp_path):
    db = _db(tmp_path, [{"o": "KHH", "d": "NGO", "dep": "2026-12-07", "at": _at(24)},
                        {"o": "KHH", "d": "NGO", "dep": "2026-12-08", "at": _at(24.01)}])
    assert [t["depart"] for t in fc.shadow_targets(db, NOW, set())] == ["2026-12-07"]


def test_shadow_targets_respect_limit(tmp_path):
    db = _db(tmp_path, [{"o": "TPE", "d": "NRT", "dep": f"2026-12-{d:02d}", "at": _at(1)}
                        for d in range(1, 21)])
    assert len(fc.shadow_targets(db, NOW, set(), limit=5)) == 5


def test_shadow_targets_never_create_the_db(tmp_path):
    missing = tmp_path / "nope" / "prices.db"
    with pytest.raises(sqlite3.OperationalError):
        fc.shadow_targets(str(missing), NOW, set())
    assert not missing.exists()


def test_shadow_targets_open_a_read_only_connection(tmp_path, monkeypatch):
    db = _db(tmp_path)
    real = sqlite3.connect
    opened = []

    def spy(*a, **kw):
        opened.append((a, kw))
        return real(*a, **kw)
    monkeypatch.setattr(fc.sqlite3, "connect", spy)
    fc.shadow_targets(db, NOW, set())
    (uri,), kw = opened[0]
    assert uri.endswith("?mode=ro") and kw == {"uri": True}
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        real(uri, uri=True).execute(
            "INSERT INTO observations (origin, destination, depart_date, price, currency, "
            "observed_at) VALUES ('X','Y','2026-12-01',1,'TWD','x')")


# ---- 影子比價的執行 ---------------------------------------------------------------

def test_run_shadow_does_not_retry_in_the_same_run_by_default():
    """main 10-26～30：同一輪的重試 0/21 救回，只是多打兩次。"""
    backend = FakeBackend(cheapest_plan=[FliError("google_error: x"), (15040.0, "IT")])
    rec = fc.run_shadow(backend, [_target()], NOW, sleep=NO_SLEEP)[0]
    assert rec["status"] == "error" and rec["attempts"] == 1 and backend.requests == 1


def test_run_shadow_retries_then_records_diff(monkeypatch):
    monkeypatch.setattr(fc, "SHADOW_RETRIES", 1)       # 重試的路徑仍在，只是預設不用
    sleeps = []
    backend = FakeBackend(cheapest_plan=[FliError("google_error: x"), (15040.0, "IT")])
    rec = fc.run_shadow(backend, [_target()], NOW, sleep=sleeps.append)[0]
    assert rec["status"] == "ok" and rec["attempts"] == 2
    assert rec["errors"] == ["google_error: x"]
    assert rec["fli_price"] == 15040.0 and rec["fli_carriers"] == "IT"
    assert rec["diff_pct"] == pytest.approx(1.0, abs=0.01)
    assert sleeps == [fc.RETRY_WAIT_S]


def test_run_shadow_lag_is_measured_at_comparison_time():
    rec = fc.run_shadow(FakeBackend(), [_target()], NOW, sleep=NO_SLEEP,
                        elapsed=lambda: 1800.0)[0]
    assert rec["lag_hours"] == 2.5                            # 參考 2 小時前 + 跑了 30 分鐘


def test_run_shadow_none_and_exhausted_errors():
    t = _target(route="KHH-OKA", ref_price=5805.0)
    none = fc.run_shadow(FakeBackend(cheapest_plan=[None]), [t], NOW, sleep=NO_SLEEP)[0]
    assert none["status"] == "none" and none["diff_pct"] is None and none["attempts"] == 1
    err = fc.run_shadow(FakeBackend(cheapest_plan=[FliError("http: 503")] * 5), [t], NOW,
                        sleep=NO_SLEEP)[0]
    assert err["status"] == "error" and err["attempts"] == 1 + fc.SHADOW_RETRIES
    assert len(err["errors"]) == 1 + fc.SHADOW_RETRIES


def test_shadow_budget_exhaustion_stops():
    backend = FakeBackend(cheapest_plan=[FliError("budget: 200 requests used")] * 9)
    recs = fc.run_shadow(backend, [_target(), _target(depart="2026-12-08")], NOW,
                         sleep=NO_SLEEP)
    assert backend.requests == 1 and recs[1]["errors"][0].startswith("skipped: budget")


# ---- run()：輸出檔、去重、跳過、紅燈判定 -----------------------------------------

def test_run_writes_gz_jsonl_and_summary(tmp_path):
    cfg = _config(tmp_path, routes=("KHH-NGO", "TPE-NRT"), months=2)
    db = _db(tmp_path, [{"o": "KHH", "d": "NGO", "dep": "2026-12-07", "at": _at(2)}])
    out = tmp_path / "fli"
    summary = fc.run(cfg, db, str(out), now=NOW, backend=FakeBackend(), sleep=NO_SLEEP)
    files = list(out.glob("*.jsonl.gz"))
    assert [f.name for f in files] == ["2026-10-26T0341Z.jsonl.gz"]  # 檔名來自注入的 now
    recs = fc.read_records(files[0])
    kinds = [r["kind"] for r in recs]
    assert kinds.count("calendar") == 2 * len(fc.windows(TODAY, 62))
    assert kinds.count("shadow") == 1 and kinds[-1] == "summary"
    assert summary["all_failed"] is False and fc.failed(summary) is False
    assert summary["calendar_ok"] == kinds.count("calendar")
    assert summary["started_at"] == NOW.isoformat(timespec="seconds")
    assert summary["finished_at"] >= summary["started_at"]
    assert summary["shadow"] == [
        "KHH-NGO 2026-12-07/2026-12-12 ok +0.0% (fli 14891 vs serpapi 14891, lag 2.0h)"]


def test_filename_is_utc_even_for_non_utc_now(tmp_path):
    taipei = timezone(timedelta(hours=8))
    fc.run(_config(tmp_path, months=1), _db(tmp_path), str(tmp_path / "fli"),
           now=NOW.astimezone(taipei), backend=FakeBackend(), sleep=NO_SLEEP)
    assert [p.name for p in (tmp_path / "fli").glob("*")] == ["2026-10-26T0341Z.jsonl.gz"]


def test_second_run_within_gap_skips_calendar_and_dedupes_shadow(tmp_path):
    cfg = _config(tmp_path, months=1)
    db = _db(tmp_path, [{"o": "KHH", "d": "NGO", "dep": "2026-12-07", "at": _at(2)}])
    out = tmp_path / "fli"
    fc.run(cfg, db, str(out), now=NOW, backend=FakeBackend(), sleep=NO_SLEEP)
    backend = FakeBackend()
    second = fc.run(cfg, db, str(out), now=NOW + timedelta(minutes=10), backend=backend,
                    sleep=NO_SLEEP)
    assert second["calendar_skipped"] == "last run 10 min ago"
    assert second["calendar_windows"] == 0 and second["shadow_targets"] == 0
    assert backend.requests == 0 and second["all_failed"] is False   # 沒做事不算全滅
    third = fc.run(cfg, db, str(out), now=NOW + timedelta(minutes=10), backend=FakeBackend(),
                   sleep=NO_SLEEP)
    names = sorted(p.name for p in out.glob("*.jsonl.gz"))
    assert names == ["2026-10-26T0341Z.jsonl.gz", "2026-10-26T0351Z-2.jsonl.gz",
                     "2026-10-26T0351Z.jsonl.gz"]                    # 絕不覆寫
    assert third["calendar_skipped"] is not None


def test_run_after_gap_sweeps_again(tmp_path):
    cfg = _config(tmp_path, months=1)
    out = tmp_path / "fli"
    fc.run(cfg, _db(tmp_path), str(out), now=NOW, backend=FakeBackend(), sleep=NO_SLEEP)
    later = fc.run(cfg, _db(tmp_path), str(out), now=NOW + timedelta(minutes=31),
                   backend=FakeBackend(), sleep=NO_SLEEP)
    assert later["calendar_skipped"] is None and later["calendar_windows"] == 1


def test_load_seen_only_looks_back_dedupe_days(tmp_path):
    out = tmp_path / "fli"
    old = NOW - timedelta(days=fc.DEDUPE_DAYS, minutes=1)
    fc.write_records(out, old, [{"kind": "shadow", "key": "old"}])
    fc.write_records(out, NOW - timedelta(days=1), [{"kind": "shadow", "key": "new"}])
    assert fc.load_seen(out, NOW) == {"new"}


def test_failed_comparisons_are_retried_next_run(tmp_path):
    """出錯、參考觀測還不到 6 小時的不算比過；拿到價或 Google 明確說沒有航班的才算。

    超過 6 小時的就不再試：那時就算拿到價，報告也不拿它評準確度（main 資料重播：
    舊寫法會多打 26 次，其中 11 次 lag 已在 22–24 小時）。
    """
    out = tmp_path / "fli"
    fc.write_records(out, NOW - timedelta(hours=1), [
        {"kind": "shadow", "key": "ok", "status": "ok", "ref_observed_at": _at(2)},
        {"kind": "shadow", "key": "none", "status": "none", "ref_observed_at": _at(2)},
        {"kind": "shadow", "key": "err", "status": "error", "ref_observed_at": _at(5.9)},
        {"kind": "shadow", "key": "stale", "status": "error", "ref_observed_at": _at(6.1)},
        {"kind": "shadow", "key": "garbled", "status": "error", "ref_observed_at": "x"}])
    assert fc.load_seen(out, NOW) == {"ok", "none", "stale", "garbled"}


def test_run_retries_a_failed_comparison_in_the_next_run(tmp_path):
    """10-28／29／30：早上那輪即時查詢失敗，一小時後換台主機的下一輪都成功。"""
    cfg = _config(tmp_path, months=1)
    db = _db(tmp_path, [{"o": "KHH", "d": "NGO", "dep": "2026-12-07", "at": _at(2)}])
    out = str(tmp_path / "fli")
    first = fc.run(cfg, db, out, now=NOW, sleep=NO_SLEEP,
                   backend=FakeBackend(cheapest_plan=[FliError("google_error: x")]))
    assert first["shadow_targets"] == 1 and first["shadow_ok"] == 0
    second = fc.run(cfg, db, out, now=NOW + timedelta(hours=1), sleep=NO_SLEEP,
                    backend=FakeBackend())
    assert second["shadow_targets"] == 1 and second["shadow_ok"] == 1
    third = fc.run(cfg, db, out, now=NOW + timedelta(hours=2), sleep=NO_SLEEP,
                   backend=FakeBackend())
    assert third["shadow_targets"] == 0                     # 比到了就不再比
    # 同一個參考觀測出現在兩輪：日曆價對參考價只算一次；比價數算行程，不算嘗試
    res = fc.report(out, now=NOW + timedelta(hours=2))
    assert res["calendar_vs_ref_pairs"] == 1
    assert res["shadow_pairs"] == 1 and res["shadow_attempts"] == 2 and res["shadow_ok"] == 1


def test_run_all_empty_counts_as_failed(tmp_path):
    # Google 有回應但全部時段都沒價、也沒有比價：8 條航線同時全空只會是封鎖或 fli
    # 解析悄悄失效（不會丟例外），所以也要讓 workflow 變紅
    summary = fc.run(_config(tmp_path, months=1), _db(tmp_path), str(tmp_path / "fli"), now=NOW,
                     backend=FakeBackend(calendar_plan=[{}, {}]), sleep=NO_SLEEP)
    assert summary["calendar_ok"] == 0 and summary["all_failed"] is True


def test_shadow_none_alone_does_not_count_as_answered(tmp_path):
    # 解析失效時即時查詢會變成「查無航班」；只有拿到價格才算數
    db = _db(tmp_path, [{"o": "KHH", "d": "NGO", "dep": "2026-12-07", "at": _at(2)}])
    summary = fc.run(_config(tmp_path, months=1), db, str(tmp_path / "fli"), now=NOW,
                     backend=FakeBackend(calendar_plan=[{}, {}], cheapest_plan=[None]),
                     sleep=NO_SLEEP)
    assert summary["shadow_none"] == 1 and summary["all_failed"] is True


def test_failure_before_any_request_is_still_failed(tmp_path):
    # 例如 fli 升版搬走私有模組：每次呼叫都在發請求前就失敗，requests 一直是 0
    class NeverSends:
        requests = 0

        def calendar(self, *a):
            raise FliError("fli: ImportError: cannot import name '_wire'")
    summary = fc.run(_config(tmp_path, months=1), _db(tmp_path), str(tmp_path / "fli"),
                     now=NOW, backend=NeverSends(), sleep=NO_SLEEP)
    assert summary["requests"] == 0
    assert summary["all_failed"] is True and fc.failed(summary)


def test_run_one_priced_window_is_not_all_failed(tmp_path):
    cfg = _config(tmp_path, routes=("KHH-NGO", "KHH-OKA"), months=1)
    backend = FakeBackend(calendar_plan=[{}, {}, {"2026-12-01": 5692.0}])
    summary = fc.run(cfg, _db(tmp_path), str(tmp_path / "fli"), now=NOW, backend=backend,
                     sleep=NO_SLEEP)
    assert summary["calendar_ok"] == 1 and fc.failed(summary) is False


def test_blocked_makes_the_run_red_even_with_data(tmp_path):
    backend = FakeBackend(calendar_plan=[{"2026-11-02": 1.0}, FliError("blocked: HTTP 429")])
    summary = fc.run(_config(tmp_path, months=3), _db(tmp_path), str(tmp_path / "fli"),
                     now=NOW, backend=backend, sleep=NO_SLEEP)
    assert summary["calendar_ok"] == 1 and summary["blocked"] == 1
    assert summary["stopped"].startswith("blocked") and fc.failed(summary)


def test_db_problem_still_sweeps_calendar_but_turns_red(tmp_path):
    summary = fc.run(_config(tmp_path, months=1), str(tmp_path / "missing.db"),
                     str(tmp_path / "fli"), now=NOW, backend=FakeBackend(), sleep=NO_SLEEP)
    assert summary["calendar_ok"] == 1
    assert summary["problems"][0].startswith("shadow_targets: OperationalError")
    assert fc.failed(summary)
    assert not (tmp_path / "missing.db").exists()


def test_crash_writes_partial_records_then_raises(tmp_path):
    class Boom(FakeBackend):
        def calendar(self, *a):
            if self.requests == 1:
                raise ZeroDivisionError("unexpected")
            return super().calendar(*a)
    out = tmp_path / "fli"
    with pytest.raises(ZeroDivisionError):
        fc.run(_config(tmp_path, months=3), _db(tmp_path), str(out), now=NOW, backend=Boom(),
               sleep=NO_SLEEP)
    recs = fc.read_records(next(out.glob("*.jsonl.gz")))
    assert recs[0]["kind"] == "calendar" and recs[0]["status"] == "ok"
    assert recs[-1]["kind"] == "summary" and recs[-1]["problems"][0].startswith("crash")


def test_main_exit_code(tmp_path, monkeypatch):
    # 真實時鐘：DB 是空的，所以結果不隨日期變化（別在這裡加有日期的觀測）
    cfg = _config(tmp_path, months=1)
    db = _db(tmp_path)
    monkeypatch.setattr(fc, "FliBackend", lambda **kw: FakeBackend(
        calendar_plan=[FliError("transport: ConnectError")] * 9))
    monkeypatch.setattr(fc.time, "sleep", NO_SLEEP)
    assert fc.main(["x", "run", cfg, db, str(tmp_path / "fli")]) == 1
    monkeypatch.setattr(fc, "FliBackend", lambda **kw: FakeBackend())
    assert fc.main(["x", "run", cfg, db, str(tmp_path / "fli2")]) == 0


# ---- 報告（第二階段判斷）---------------------------------------------------------

def _write(out, stamp, recs):
    out.mkdir(parents=True, exist_ok=True)
    with gzip.open(out / f"{stamp}.jsonl.gz", "wt", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")


def _cal(route, idx, status, attempts=1, errors=None, days=None):
    if days is None:
        days = {"2026-12-07": 14891.0} if status == "ok" else {}
    if errors is None:
        errors = ["google_error: x"] if status == "error" else []
    return {"kind": "calendar", "route": route, "idx": idx, "status": status,
            "attempts": attempts, "days": days, "errors": errors}


_SH_KEYS = itertools.count()


def _sh(diff, lag=1.0, status="ok", route="KHH-NGO", dep="2026-12-07", ret="2026-12-12",
        ref=14891.0, errors=(), ref_at=None, provider="serpapi", key=None):
    """一筆影子比價紀錄。key 預設每筆不同（真實紀錄裡每個參考觀測一個 key）。"""
    return {"kind": "shadow", "status": status, "diff_pct": diff, "lag_hours": lag,
            "errors": list(errors), "route": route, "depart": dep, "return": ret,
            "ref_price": ref, "ref_observed_at": ref_at, "ref_provider": provider,
            "key": key or f"k{next(_SH_KEYS)}"}


def _sum(**kw):
    s = {"kind": "summary", "requests": 10, "all_failed": False, "blocked": 0, "problems": []}
    s.update(kw)
    return s


def test_report_skips_only_windows_google_always_said_have_no_flights(tmp_path):
    out = tmp_path / "fli"
    _write(out, "2026-10-25T0341Z", [
        _cal("KHH-NGO", 0, "ok"), _cal("KHH-NGO", 1, "empty"),      # #1 從沒價 → 沒班表
        _cal("TPE-NRT", 0, "ok", attempts=2), _sh(0.5), _sh(1.9), _sh(5.0, lag=12.0),
        _sum()])
    _write(out, "2026-10-26T0341Z", [
        _cal("KHH-NGO", 0, "error"), _cal("KHH-NGO", 1, "empty"),   # #0 曾有價 → 漏報
        _cal("TPE-NRT", 0, "ok"), _sh(-3.0), _sum(requests=8)])
    res = fc.report(str(out), days=14, now=NOW)
    assert res["runs"] == 2 and res["requests"] == 18 and res["red_runs"] == 0
    assert res["calendar_no_schedule"] == ["KHH-NGO#1"]
    assert res["calendar_scored"] == 4
    assert res["calendar_by_route"] == {"KHH-NGO": "1/2", "TPE-NRT": "2/2"}
    assert res["calendar_ok_rate"] == 0.75                    # 3/4
    assert res["calendar_first_try_rate"] == 0.5              # 2/4（一筆重試才成功）
    assert res["calendar_error_rate"] == 0.25
    assert res["shadow_fresh"] == 3                           # lag 12h 那筆不評準確度
    assert res["shadow_within_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert res["criteria"]["calendar_ok_rate>=95%"] == "FAIL"
    assert res["criteria"]["no_blocking"] == "PASS"
    assert res["error_kinds"] == {"google_error": 1}


def test_report_counts_windows_that_only_ever_failed(tmp_path):
    """2026-10-26 main 第一輪：KHH-NGO 5 段都是 google_error、從沒拿到價。

    舊規則把「從沒拿到價」一律當成沒有班表剔除，報告寫 error_rate 0、PASS。
    錯誤不是沒有班表的證據——只有 Google 每次都明確回空才剔除。
    """
    out = tmp_path / "fli"
    _write(out, "2026-10-26T0226Z", [
        *[_cal("TPE-NRT", i, "ok") for i in range(5)],
        *[_cal("KHH-NGO", i, "error", attempts=2,
               errors=["google_error: x", "google_error: x"]) for i in range(5)],
        _cal("KHH-OKA", 4, "error", attempts=2,
             errors=["transport: Timeout", "google_error: x"]),
        _cal("KHH-CTS", 4, "empty"),                              # 真的沒班表
        _sum()])
    res = fc.report(str(out), days=14, now=NOW)
    assert res["calendar_no_schedule"] == ["KHH-CTS#4"]
    assert res["calendar_scored"] == 11
    assert res["calendar_ok_rate"] == pytest.approx(5 / 11, abs=1e-4)
    assert res["calendar_error_rate"] == pytest.approx(6 / 11, abs=1e-4)
    assert res["calendar_by_route"] == {"KHH-NGO": "0/5", "KHH-OKA": "0/1", "TPE-NRT": "5/5"}
    assert res["criteria"]["calendar_ok_rate>=95%"] == "FAIL"


def test_report_counts_a_window_that_was_empty_once_and_failed_once(tmp_path):
    """同一段一輪回空、一輪出錯：不能確定沒有班表，照算。"""
    out = tmp_path / "fli"
    _write(out, "2026-10-25T0341Z", [_cal("KHH-NGO", 3, "empty"), _sum()])
    _write(out, "2026-10-26T0341Z", [_cal("KHH-NGO", 3, "error"), _sum()])
    res = fc.report(str(out), days=14, now=NOW)
    assert res["calendar_no_schedule"] == []
    assert res["calendar_scored"] == 2 and res["calendar_ok_rate"] == 0.0


def test_report_lag_boundary_is_inclusive(tmp_path):
    out = tmp_path / "fli"
    _write(out, "2026-10-26T0341Z", [_sh(0.1, lag=fc.SHADOW_MAX_LAG_H),
                                     _sh(0.1, lag=fc.SHADOW_MAX_LAG_H + 0.01), _sum()])
    assert fc.report(str(out), now=NOW)["shadow_fresh"] == 1


def test_report_counts_blocks_even_when_the_retry_succeeded(tmp_path):
    out = tmp_path / "fli"
    _write(out, "2026-10-26T0341Z", [
        _cal("TPE-NRT", 0, "ok", attempts=2, errors=["blocked: HTTP 429"]), _sum()])
    res = fc.report(str(out), now=NOW)
    assert res["blocked"] == 1 and res["criteria"]["no_blocking"] == "FAIL"


def test_report_compares_calendar_prices_with_reference(tmp_path):
    out = tmp_path / "fli"
    ref_at = "2026-10-26T03:30:00+00:00"                        # 日曆那輪 03:41 開始
    _write(out, "2026-10-26T0341Z", [
        _cal("KHH-NGO", 0, "ok", days={"2026-12-07": 15000.0, "2026-12-08": 1.0}),
        _sh(0.0, ref=14891.0, ref_at=ref_at),                    # 住 5 晚 → 可比，+0.7%
        _sh(0.0, dep="2026-12-08", ret="2026-12-15", ref_at=ref_at),  # 住 7 晚 → 不可比
        _sh(0.0, dep="2026-12-09", ret="2026-12-14", ref_at=ref_at),  # 日曆那天沒價
        _sh(0.0, ref_at="2026-10-25T18:00:00+00:00"),            # 參考價比日曆早 9.7h → 不評
        _sh(0.0),                                                # 沒有參考時刻 → 不評
        _sum()])
    res = fc.report(str(out), now=NOW)
    assert res["calendar_vs_ref_pairs"] == 1
    assert res["calendar_vs_ref_within_rate"] == 1.0
    assert res["criteria"]["calendar_vs_ref_within_2%>=90%"] == "PASS"


def test_report_pairs_the_calendar_from_another_run(tmp_path):
    """2026-10-26：12:26 那輪掃了日曆；12:49 那輪（FSC 剛跑完）30 分鐘內不重掃，
    只做影子比價。原本只配同一輪的日曆，那輪的參考價全部沒得比。"""
    out = tmp_path / "fli"
    now = datetime(2026, 10, 26, 14, 0, tzinfo=timezone.utc)
    _write(out, "2026-10-26T0600Z", [                                   # 6.8h 前：太遠
        _cal("TPE-NRT", 0, "ok", days={"2026-12-17": 1.0}), _sum()])
    _write(out, "2026-10-26T0900Z", [                                   # 3.8h 前：可用但較遠
        _cal("TPE-NRT", 0, "ok", days={"2026-12-17": 9999.0}), _sum()])
    _write(out, "2026-10-26T1226Z", [
        _cal("TPE-NRT", 0, "ok", days={"2026-12-17": 8499.0}), _sum()])
    _write(out, "2026-10-26T1249Z", [
        _sh(0.0, route="TPE-NRT", dep="2026-12-17", ret="2026-12-22", ref=8499.0,
            ref_at="2026-10-26T12:48:00+00:00"),
        _sum(calendar_skipped="last run 23 min ago")])
    res = fc.report(str(out), days=1, now=now)
    assert res["calendar_vs_ref_pairs"] == 1
    assert res["calendar_vs_ref_within_rate"] == 1.0                   # 用的是 12:26 那輪
    assert res["calendar_vs_ref_by_provider"] == {"serpapi": "1/1"}


def test_report_splits_shadow_accuracy_by_reference_source(tmp_path):
    """10-28／29：fli 與 SerpAPI 一致，只有 Scrape.do 低約 7%——分開列才看得出來。"""
    out = tmp_path / "fli"
    _write(out, "2026-10-26T0341Z", [
        _sh(0.0), _sh(0.0), _sh(6.95, provider="scrapedo"), _sh(0.0, provider="scrapedo"),
        _sum()])
    res = fc.report(str(out), now=NOW)
    assert res["shadow_within_by_provider"] == {"scrapedo": "1/2", "serpapi": "2/2"}
    assert res["shadow_within_rate"] == 0.75


def test_report_survives_a_corrupt_file(tmp_path):
    out = tmp_path / "fli"
    _write(out, "2026-10-26T0341Z", [_cal("KHH-NGO", 0, "ok"), _sum()])
    (out / "2026-10-26T0500Z.jsonl.gz").write_bytes(b"not gzip")
    res = fc.report(str(out), now=NOW)
    assert res["runs"] == 1 and res["unreadable_files"] == 1
    assert fc.load_seen(out, NOW) == set()                       # run() 也不會被它拖垮


def test_report_ignores_old_files_and_handles_no_data(tmp_path):
    out = tmp_path / "fli"
    _write(out, "2026-09-01T0000Z", [_cal("KHH-NGO", 0, "ok"), _sum()])
    res = fc.report(str(out), days=14, now=NOW)
    assert res["calendar_windows"] == 0 and res["runs"] == 0
    assert set(res["criteria"].values()) == {"NO DATA"}


def test_report_counts_red_runs(tmp_path):
    out = tmp_path / "fli"
    _write(out, "2026-10-26T0341Z", [_sum(blocked=1)])
    _write(out, "2026-10-26T0500Z", [_sum(problems=["shadow_targets: x"])])
    _write(out, "2026-10-26T0600Z", [_sum()])
    assert fc.report(str(out), now=NOW)["red_runs"] == 2


# ---- 不裝 fli 也能 import（CI 的 monitor 就是這樣）---------------------------------

def test_module_imports_and_runs_without_fli():
    # 在子行程裡擋掉 fli：不能用 importlib.reload，那會換掉本檔已 import 的 FliError，
    # 讓排在後面的測試莫名失敗（而 monitor 每小時都先跑這套測試）
    code = (
        "import sys; sys.modules['fli'] = None\n"
        "from datetime import date\n"
        "import farehunter.fli_calendar as fc\n"
        "class B:\n"
        "    requests = 0\n"
        "    def calendar(self, *a): return {'2026-12-07': 1.0}\n"
        "recs = fc.sweep_calendar(B(), [('KHH', 'NGO')], date(2026, 10, 26), 61,"
        " sleep=lambda s: None)\n"
        "assert recs[0]['status'] == 'ok'\n"
        "try:\n"
        "    fc.FliBackend()\n"
        "except ImportError:\n"
        "    print('ok')\n")
    src = str(Path(fc.__file__).resolve().parents[1])
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={"PYTHONPATH": src}, timeout=60)
    assert res.returncode == 0 and res.stdout.strip() == "ok", res.stderr


# ---- FliBackend 本身（只在裝了 fli 時跑）------------------------------------------

def _wrb(inner):
    return ")]}'\n\n" + json.dumps([["wrb.fr", None, json.dumps(inner)]])


GOOGLE_ERR = ")]}'\n\n" + json.dumps([["wrb.fr", None, None, None, None, [13]], ["di", 50]])


def _cal_body(dep0, n=3, price=8691):
    items = [[(dep0 + timedelta(days=i)).isoformat(), (dep0 + timedelta(days=i + 5)).isoformat(),
              [[None, price + i], "tok"]] for i in range(n)]
    return _wrb([None, items])


def _leg(o, d, day, code="IT"):
    fl = [None] * 32
    fl[3], fl[6] = o, d
    fl[8], fl[10], fl[11] = [10, 5], [14, 10], 185
    fl[20] = fl[21] = [day.year, day.month, day.day]
    fl[22] = [code, "720"]
    return fl


def _shop_body(o, d, day, prices=(14891, 15500), code="IT"):
    rows = []
    for p in prices:
        detail = [None] * 23
        detail[0], detail[1], detail[2], detail[9] = code, ["X"], [_leg(o, d, day, code)], 185
        rows.append([detail, [[None, p], "tok"]])
    return _wrb([[None, None, None, None, "sess"], None, [rows], None])


class _Resp:
    def __init__(self, text, status=200, url="https://www.google.com/_/FlightsFrontendUi/data/x"):
        self.text, self.status_code, self.url = text, status, url

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _Session:
    def __init__(self, plan):
        self.plan, self.sent = list(plan), []

    def post(self, url, **kw):
        self.sent.append((url, kw))
        item = self.plan.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    get = post


@pytest.fixture
def backend_with(monkeypatch):
    pytest.importorskip("fli")
    monkeypatch.setattr(fc, "SPACING_S", 0)

    def make(plan, budget=fc.REQUEST_BUDGET):
        b = fc.FliBackend(currency="TWD", budget=budget)
        session = _Session(plan)
        b.client._session = lambda: session
        b.session = session
        return b
    return make


def _days(n):
    return date.today() + timedelta(days=n)


def test_backend_calendar_ok_sends_oka_not_nah(backend_with):
    b = backend_with([_Resp(_cal_body(_days(1)))])
    days = b.calendar("KHH", "OKA", _days(1), _days(61))
    assert days == {_days(1).isoformat(): 8691.0, _days(2).isoformat(): 8692.0,
                    _days(3).isoformat(): 8693.0}
    assert b.requests == 1
    sent = json.dumps(b.session.sent[0][1], default=str)
    assert "OKA" in sent and "NAH" not in sent
    assert b.session.sent[0][1]["timeout"] == fc.TIMEOUT_S


def test_backend_calendar_error_kinds(backend_with):
    cases = [
        (_Resp(GOOGLE_ERR), "google_error"),
        (_Resp("<html>unusual traffic</html>", 429, "https://www.google.com/sorry/index"),
         "blocked"),
        (_Resp("", 403), "blocked"),
        (_Resp("", 503), "http"),
        (ConnectionError("reset"), "transport"),
    ]
    for item, kind in cases:
        b = backend_with([item])
        with pytest.raises(FliError) as info:
            b.calendar("KHH", "NGO", _days(1), _days(61))
        assert str(info.value).startswith(kind), (kind, str(info.value))
        assert b.requests == 1


def test_backend_calendar_empty_payload_is_not_an_error(backend_with):
    b = backend_with([_Resp(_wrb([None, None]))])
    assert b.calendar("KHH", "NGO", _days(1), _days(61)) == {}


def test_backend_budget_and_pre_request_failures(backend_with):
    b = backend_with([_Resp(_cal_body(_days(1)))] * 2, budget=1)
    b.calendar("KHH", "NGO", _days(1), _days(61))
    with pytest.raises(FliError, match="^budget"):
        b.calendar("KHH", "NGO", _days(1), _days(61))
    with pytest.raises(FliError, match="^fli: KeyError"):
        b.calendar("KHH", "XXX", _days(1), _days(61))
    assert b.requests == 1


def test_backend_cheapest_uses_one_request_and_round_trip_total(backend_with):
    b = backend_with([_Resp(_shop_body("KHH", "NGO", _days(43)))])
    assert b.cheapest("KHH", "NGO", _days(43), _days(48)) == (14891.0, "IT")
    assert b.requests == 1                                    # 不展開回程


def test_backend_cheapest_decodes_legs_arriving_at_oka(backend_with):
    # 回應端的別名 bug：fli 解析時查 _AIRPORT_BY_CODE，沒登記 OKA 就整列丟掉
    b = backend_with([_Resp(_shop_body("KHH", "OKA", _days(43), prices=(5805, 6100)))])
    assert b.cheapest("KHH", "OKA", _days(43), _days(48)) == (5805.0, "IT")


def test_backend_cheapest_none_and_error(backend_with):
    b = backend_with([_Resp(_wrb([[None, None, None, None, "sess"], None, None, None]))])
    assert b.cheapest("KHH", "NGO", _days(43), _days(48)) is None
    b = backend_with([_Resp(GOOGLE_ERR)])
    with pytest.raises(FliError, match="^google_error") as info:
        b.cheapest("KHH", "NGO", _days(43), _days(48))
    # 短回應整段記下（2026-10-26～29 即時查詢大多這樣失敗，只有長度查不出原因）
    assert 'body=)]}\' [["wrb.fr", null, null, null, null, [13]], ["di", 50]]' in str(info.value)


def test_backend_diag_keeps_only_short_bodies(backend_with):
    b = backend_with([])
    b.client.last = {"status": 200, "path": "/x/GetShoppingResults", "bytes": 300}
    b.client.last_text = "e" * 300
    assert b.client.diag().endswith("body=" + "e" * 300)
    b.client.last, b.client.last_text = dict(b.client.last, bytes=301), "e" * 301
    assert "body=" not in b.client.diag()             # 正常大小的回應不塞進錯誤訊息


def test_backend_currency_check(backend_with):
    b = backend_with([])
    b._check_currency(["TWD", None])
    b._check_currency([])
    with pytest.raises(FliError, match="^currency"):
        b._check_currency(["USD"])
