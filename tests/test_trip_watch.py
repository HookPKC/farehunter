"""使用者指定行程的專屬追蹤（trip_watch，2026-11-27）。零真實 API。"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import farehunter.runner as runner_mod                              # noqa: E402
from farehunter import brightdata_flights as bd                     # noqa: E402
from farehunter import trip_watch as tw                             # noqa: E402
from farehunter.storage import Store                                # noqa: E402

NOW = datetime(2026, 11, 27, 6, 17, tzinfo=timezone.utc)
CFG = {"routes": [], "watch_trips": [{"origin": "KHH", "destination": "KMJ", "depart": "2027-03-02",
                                       "return": "2027-03-09", "note": "熊本"}]}
KEY = ("KHH", "KMJ", "2027-03-02", "2027-03-09")


def _page(price, airline="Tigerair Taiwan", nonstop=True):
    stop = "Nonstop flight" if nonstop else "1 stop flight"
    return (f'<html><li aria-label="From {price} New Taiwan dollars round trip total. {stop} '
            f'with {airline}. Leaves Kaohsiung at 6:50 AM."></li></html>')


def _run(st, now, page, sent):
    return tw.run(st, CFG, now, fetcher=lambda *a: page, sender=lambda text: sent.append(text) or True)


def test_first_check_says_it_started_and_later_only_real_changes_are_sent(tmp_path):
    st = Store(str(tmp_path / "p.db"))
    sent = []
    s = _run(st, NOW, _page("9,752"), sent)
    assert s == {"trips": 1, "checked": 1, "notified": 1, "errors": 0}
    assert "開始追蹤" in sent[0] and "9,752" in sent[0] and "高雄⇄熊本 3/2（二）–3/9（二） 7 晚" in sent[0]
    assert "q=Flights%20from%20KHH%20to%20KMJ%20on%202027-03-02%20through%202027-03-09" in sent[0]
    # 12 小時內不再查
    assert _run(st, NOW + timedelta(hours=11), _page("9,000"), sent)["checked"] == 0
    # 便宜不到 3%：查了、不通知
    assert _run(st, NOW + timedelta(hours=12), _page("9,600"), sent)["notified"] == 0
    # 便宜 3% 以上（跟上次「通知」的 9,752 比）
    _run(st, NOW + timedelta(hours=24), _page("9,400"), sent)
    assert "變便宜了" in sent[-1] and "比上次通知的 9,752 便宜 352" in sent[-1]
    # 貴 5% 以上
    _run(st, NOW + timedelta(hours=36), _page("9,900"), sent)
    assert "漲價了" in sent[-1] and "9,400" in sent[-1]
    assert len(sent) == 3


def test_no_direct_is_reported_once(tmp_path):
    st = Store(str(tmp_path / "p.db"))
    sent = []
    _run(st, NOW, _page("9,752"), sent)
    _run(st, NOW + timedelta(hours=12), _page("12,000", nonstop=False), sent)
    assert "查不到直飛" in sent[-1]
    _run(st, NOW + timedelta(hours=24), _page("12,000", nonstop=False), sent)
    assert len(sent) == 2                                          # 不重複通知
    _run(st, NOW + timedelta(hours=36), _page("9,752"), sent)
    assert "開始追蹤" in sent[-1]                                   # 又查得到：重新當作基準


def test_a_failed_fetch_is_retried_next_hour_and_counted_in_the_ledger(tmp_path):
    st = Store(str(tmp_path / "p.db"))
    sent = []

    def boom(*a):
        raise bd.BrightDataError("HTTP 502")

    s = tw.run(st, CFG, NOW, fetcher=boom, sender=lambda t: sent.append(t) or True)
    assert s["errors"] == 1 and sent == []
    s = _run(st, NOW + timedelta(hours=1), "<html>no flights</html>", sent)   # 解析不到也算失敗
    assert s["errors"] == 1 and s["checked"] == 0
    s = _run(st, NOW + timedelta(hours=2), _page("9,752"), sent)
    assert s["checked"] == 1
    assert st.api_usage_since(bd.PROVIDER, "2026-11-27") == 3       # 三次都記進 Bright Data 帳本


def test_departed_trips_stop_and_results_never_reach_observations(tmp_path):
    st = Store(str(tmp_path / "p.db"))
    sent = []
    assert _run(st, datetime(2027, 3, 1, 17, 0, tzinfo=timezone.utc), _page("9,752"), sent)["trips"] == 0
    _run(st, NOW, _page("9,752"), sent)
    con = sqlite3.connect(tmp_path / "p.db")
    assert con.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    assert con.execute("SELECT price, carriers FROM trip_watch_checks").fetchall() == [(9752.0, "IT")]


def test_runner_runs_it_only_with_bright_data(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(runner_mod.trip_watch, "run", lambda *a, **k: called.append(1) or {"trips": 1})
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"defaults": {"currency": "twd", "months_ahead": 1, "pause_seconds": 0},
                                   "routes": [{"origin": "TPE", "destination": "NRT"}],
                                   "watch_trips": CFG["watch_trips"]}), encoding="utf-8")

    class _Client:
        def __init__(self, *a, **k):
            pass

        def search_month(self, *a, **k):
            return {}

    monkeypatch.setattr(runner_mod, "TravelpayoutsClient", _Client)
    monkeypatch.setattr(runner_mod, "parse_offers", lambda *a, **k: [])
    monkeypatch.setenv("FAREHUNTER_FORCE", "1")
    runner_mod.run(str(cfg), str(tmp_path / "a.db"))
    assert called == []                                            # 沒有 Bright Data 金鑰
    monkeypatch.setattr(runner_mod.brightdata, "configured", lambda: True)
    monkeypatch.setattr(runner_mod.brightdata, "Verifier", lambda *a, **k: None)
    runner_mod.run(str(cfg), str(tmp_path / "b.db"))
    assert called == [1]


def test_config_watches_the_kumamoto_trip():
    cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text("utf-8"))
    assert tw.trips(cfg)[0] | {"note": ""} == {"origin": "KHH", "destination": "KMJ", "depart": "2027-03-02",
                                               "return": "2027-03-09", "note": ""}
