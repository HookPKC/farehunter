"""巡檢查到的直飛實價低於門檻 → 警報（runner._sweep_alerts，2026-11-16）。零真實 API。"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from farehunter import brightdata_flights as bd                     # noqa: E402
from farehunter import chocodata_flights as cd                      # noqa: E402
from farehunter import gflights2_flights as g2                      # noqa: E402
import farehunter.runner as runner_mod                              # noqa: E402
from farehunter.models import Offer                                 # noqa: E402
from farehunter.storage import Store                                # noqa: E402

NOW = datetime(2026, 11, 16, 8, 17, tzinfo=timezone.utc)
STAMP = NOW.isoformat(timespec="seconds")


def _d(n):
    return (NOW.date() + timedelta(days=n)).isoformat()


CFG = {"defaults": {"currency": "twd"},
       "routes": [{"origin": "KHH", "destination": "OKA", "absolute_threshold": 6500,
                   "sweep_alerts": True},
                  {"origin": "KHH", "destination": "NGO", "absolute_threshold": 11100}]}


def _store(tmp_path):
    return Store(str(tmp_path / "prices.db"))


def _google(st, o, d, dep, ret, price, carriers="GK", provider="brightdata", at=STAMP,
            stops=0, fare_class="any"):
    st.record(Offer(o, d, dep, ret, price, "TWD", carriers, stops, "", fare_class=fare_class,
                    source="google", provider=provider), observed_at=at)


def _run(st, monkeypatch, cfg=CFG, now=NOW, sent_ok=True):
    sent = []
    monkeypatch.setattr(runner_mod, "notify",
                        lambda o, v, s, **k: sent.append((o.origin, o.destination, o.depart_date,
                                                          o.price, v.reason, s.state))
                        or (["line"] if sent_ok else []))
    monkeypatch.setattr(runner_mod, "channels_configured", lambda: True)
    n = runner_mod._sweep_alerts(st, cfg, now, line_budget=None)
    return n, sent


def _alerts(st):
    return [tuple(r) for r in st.conn.execute(
        "SELECT origin, destination, depart_date, price, reason, price_source, price_status "
        "FROM alerts ORDER BY id")]


def test_a_swept_direct_price_under_the_threshold_is_sent_as_verified(tmp_path, monkeypatch):
    st = _store(tmp_path)
    _google(st, "KHH", "OKA", _d(27), _d(32), 5884.0)
    n, sent = _run(st, monkeypatch)
    assert n == 1 and sent == [("KHH", "OKA", _d(27), 5884.0, "absolute", "verified")]
    assert _alerts(st) == [("KHH", "OKA", _d(27), 5884.0, "absolute", "google", "verified")]


def test_only_routes_with_the_switch_are_alerted(tmp_path, monkeypatch):
    """KHH-NGO 的門檻還是用快取價定的：開關沒開，巡檢低價照舊只進資料庫。"""
    st = _store(tmp_path)
    _google(st, "KHH", "NGO", _d(20), _d(25), 7910.0, "IT")
    assert _run(st, monkeypatch) == (0, [])


def test_only_the_threshold_counts(tmp_path, monkeypatch):
    st = _store(tmp_path)
    _google(st, "KHH", "OKA", _d(20), _d(25), 6501.0)
    for i in range(40):        # 歷史中位數很高：快取那條路會判 big_drop，這裡不看
        _google(st, "KHH", "OKA", _d(20), _d(25), 12000.0,
                at=(NOW - timedelta(days=5, hours=i)).isoformat(timespec="seconds"))
    assert _run(st, monkeypatch) == (0, [])


def test_out_of_range_stays_too_soon_and_old_rows_are_skipped(tmp_path, monkeypatch):
    st = _store(tmp_path)
    _google(st, "KHH", "OKA", _d(20), _d(32), 5000.0)                       # 12 晚
    _google(st, "KHH", "OKA", _d(1), _d(6), 5000.0)                          # 明天出發
    _google(st, "KHH", "OKA", _d(30), _d(35), 5000.0,
            at=(NOW - timedelta(hours=1)).isoformat(timespec="seconds"))     # 上一輪寫的
    _google(st, "KHH", "OKA", _d(40), _d(45), 5000.0, stops=1)               # 轉機
    _google(st, "KHH", "OKA", _d(41), _d(46), 5000.0, "CI", fare_class="full")
    _google(st, "KHH", "OKA", _d(42), _d(47), 5000.0, provider="serpapi")   # 不是巡檢寫的
    assert _run(st, monkeypatch) == (0, [])


def test_one_alert_per_trip_with_the_cheapest_airline(tmp_path, monkeypatch):
    st = _store(tmp_path)
    _google(st, "KHH", "OKA", _d(20), _d(25), 6200.0, "IT")
    _google(st, "KHH", "OKA", _d(20), _d(25), 5800.0, "GK", provider="chocodata")
    n, sent = _run(st, monkeypatch)
    assert n == 1 and sent[0][3] == 5800.0


def test_dedupe_is_shared_with_the_cache_path(tmp_path, monkeypatch):
    """同一趟已經以已驗證通知過（快取那條路驗證後發的）：沒有再便宜 10% 就不再發。"""
    st = _store(tmp_path)
    st.record_alert("KHH", "OKA", _d(20), 5785.0, "absolute", return_date=_d(25),
                    carrier_signature="GK", price_source="google", price_status="verified")
    _google(st, "KHH", "OKA", _d(20), _d(25), 5772.0)
    assert _run(st, monkeypatch) == (0, [])
    later = NOW + timedelta(hours=1)
    _google(st, "KHH", "OKA", _d(20), _d(25), 5100.0, at=later.isoformat(timespec="seconds"))
    n, sent = _run(st, monkeypatch, now=later)
    assert n == 1 and sent[0][3] == 5100.0                                   # 便宜 >10%：新消息


def test_the_same_trip_is_not_resent_every_hour(tmp_path, monkeypatch):
    st = _store(tmp_path)
    for h in range(3):
        t = NOW + timedelta(hours=h)
        _google(st, "KHH", "OKA", _d(20), _d(25), 5884.0, at=t.isoformat(timespec="seconds"))
        _run(st, monkeypatch, now=t)
    assert len(_alerts(st)) == 1


def test_a_failed_send_is_not_recorded(tmp_path, monkeypatch):
    st = _store(tmp_path)
    _google(st, "KHH", "OKA", _d(20), _d(25), 5884.0)
    assert _run(st, monkeypatch, sent_ok=False)[0] == 0 and _alerts(st) == []


def test_no_route_with_the_switch_means_nothing_is_read(tmp_path, monkeypatch):
    st = _store(tmp_path)
    monkeypatch.setattr(st, "google_rows_at", lambda *a: (_ for _ in ()).throw(AssertionError))
    cfg = {"defaults": {}, "routes": [{"origin": "KHH", "destination": "OKA",
                                       "absolute_threshold": 6500}]}
    assert _run(st, monkeypatch, cfg=cfg) == (0, [])


# ---- runner 整合：巡檢寫入 → 同一輪發警報 ----------------------------------------

def _page(price, airline="Jetstar"):
    return (f'<html><li aria-label="From {price} New Taiwan dollars round trip total. Nonstop '
            f'flight with {airline}. Leaves Kaohsiung at 6:50 AM."></li></html>')


def test_runner_sends_what_the_sweep_found(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "defaults:\n  currency: twd\n  months_ahead: 1\n  pause_seconds: 0\n"
        "routes:\n  - origin: KHH\n    destination: OKA\n    absolute_threshold: 6500\n"
        "    sweep_alerts: true\n", encoding="utf-8")
    cache = [Offer("KHH", "OKA", _d(30), _d(35), 9000.0, "TWD", "GK", 0, "100")]   # 快取價不便宜
    monkeypatch.setattr(runner_mod, "TravelpayoutsClient",
                        type("C", (), {"__init__": lambda s, *a, **k: None,
                                       "search_month": lambda s, *a, **k: {}}))
    monkeypatch.setattr(runner_mod, "parse_offers", lambda *a, **k: list(cache))
    sent = []
    monkeypatch.setattr(runner_mod, "notify",
                        lambda o, v, s, **k: sent.append((o.price, s.state)) or ["line"])
    monkeypatch.setattr(runner_mod, "channels_configured", lambda: True)
    for name in ("CHOCODATA_API_KEY", "RAPIDAPI_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "k")
    monkeypatch.setattr(bd, "fetch", lambda *a: _page("5,884"))          # Google 實價其實很便宜
    monkeypatch.setenv("FAREHUNTER_FORCE", "1")
    summary = runner_mod.run(str(cfg), str(tmp_path / "prices.db"), now=NOW)
    assert summary["sweep_alerts"] >= 1 and summary["alerts"] == summary["sweep_alerts"]
    assert sent and all(s == (5884.0, "verified") for s in sent)


def test_all_three_sweep_providers_count():
    assert cd.PROVIDER in runner_mod.SWEEP_PROVIDERS and g2.PROVIDER in runner_mod.SWEEP_PROVIDERS
