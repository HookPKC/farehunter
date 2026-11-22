"""傳統航空特價警報（runner._full_alerts，2026-11-22）與每日摘要的傳統航空段落。零真實 API。"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from farehunter import daily_digest as dd                           # noqa: E402
import farehunter.runner as runner_mod                              # noqa: E402
from farehunter.models import Offer                                 # noqa: E402
from farehunter.storage import Store                                # noqa: E402

NOW = datetime(2026, 12, 2, 0, 17, tzinfo=timezone.utc)             # 台灣 12/02 08:17
STAMP = NOW.isoformat(timespec="seconds")
ROUTES = [{"origin": "KHH", "destination": "OKA", "absolute_threshold": 5000},
          {"origin": "KHH", "destination": "NGO", "absolute_threshold": 7700}]
ON = {"defaults": {"currency": "twd", "full_alerts": True}, "routes": ROUTES}
OFF = {"defaults": {"currency": "twd", "full_alerts": False}, "routes": ROUTES}


def _d(n):
    return (NOW.date() + timedelta(days=n)).isoformat()


def _store(tmp_path):
    return Store(str(tmp_path / "prices.db"))


def _full(st, price, dep=30, nights=5, carriers="CI", hours_ago=0, o="KHH", d="OKA",
          fare_class="full", provider="brightdata"):
    at = (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    st.record(Offer(o, d, _d(dep), _d(dep + nights), price, "TWD", carriers, 0, "",
                    fare_class=fare_class, source="google", provider=provider), observed_at=at)


def _history(st, n=runner_mod.FULL_MIN_HISTORY, price=9000.0, o="KHH", d="OKA"):
    """近 14 天每 6 小時一筆、價格 price 的傳統航空實價（底價＝price）。"""
    for i in range(n):
        _full(st, price + i % 3, dep=20 + i % 20, hours_ago=6 * (i + 1), o=o, d=d)


def _run(st, monkeypatch, cfg=ON):
    sent = []
    monkeypatch.setattr(runner_mod, "notify",
                        lambda o, v, s, **k: sent.append((o.origin, o.destination, o.price,
                                                          o.fare_class, v.reason, v.detail,
                                                          s.state)) or ["telegram"])
    monkeypatch.setattr(runner_mod, "channels_configured", lambda: True)
    return runner_mod._full_alerts(st, cfg, NOW, line_budget=None), sent


def test_a_price_5pct_under_the_14_day_floor_is_sent_as_verified(tmp_path, monkeypatch):
    st = _store(tmp_path)
    _history(st)
    _full(st, 8500.0)                                          # 9,000 × 0.95 ＝ 8,550 → 觸發
    n, sent = _run(st, monkeypatch)
    assert n == 1
    (o, d, price, fc, reason, detail, state), = sent
    assert (o, d, price, fc, reason, state) == ("KHH", "OKA", 8500.0, "full", "full_drop", "verified")
    assert detail == "傳統航空 8,500 TWD ≤ 近 14 天最低 9,000 再低 5%"
    row = st.conn.execute("SELECT reason, price_status, carrier_signature FROM alerts").fetchone()
    assert tuple(row) == ("full_drop", "verified", "CI")


def test_only_a_real_drop_counts(tmp_path, monkeypatch):
    st = _store(tmp_path)
    _history(st)
    _full(st, 8600.0)                                          # 只低 4.4%
    assert _run(st, monkeypatch) == (0, [])


def test_the_switch_off_never_alerts(tmp_path, monkeypatch):
    st = _store(tmp_path)
    _history(st)
    _full(st, 5000.0)
    assert _run(st, monkeypatch, cfg=OFF) == (0, [])
    assert _run(st, monkeypatch, cfg={"routes": ROUTES}) == (0, [])     # 沒寫開關也當關
    assert st.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0


def test_routes_with_too_little_history_are_not_judged(tmp_path, monkeypatch):
    st = _store(tmp_path)
    _history(st, n=runner_mod.FULL_MIN_HISTORY - 1)
    _full(st, 5000.0)
    assert _run(st, monkeypatch) == (0, [])


def test_history_older_than_14_days_and_this_run_do_not_set_the_floor(tmp_path, monkeypatch):
    st = _store(tmp_path)
    _history(st)
    _full(st, 7000.0, hours_ago=24 * 15)                       # 15 天前的低價不算
    _full(st, 8500.0)
    n, sent = _run(st, monkeypatch)
    assert n == 1 and "近 14 天最低 9,000" in sent[0][5]


def test_cheap_airline_rows_and_out_of_range_trips_are_ignored(tmp_path, monkeypatch):
    st = _store(tmp_path)
    _history(st)
    _full(st, 4000.0, fare_class="any", carriers="IT")         # 廉航那一列不算
    _full(st, 8000.0, nights=14)                               # 住 14 晚
    _full(st, 8000.0, dep=1, nights=3)                         # 明天出發
    assert _run(st, monkeypatch) == (0, [])


def test_the_same_trip_is_not_sent_twice(tmp_path, monkeypatch):
    st = _store(tmp_path)
    _history(st)
    _full(st, 8500.0)
    assert _run(st, monkeypatch)[0] == 1
    assert _run(st, monkeypatch)[0] == 0


# ---- 每日摘要的傳統航空段落 ---------------------------------------------------------

def test_digest_lists_full_service_lows_sorted_by_price(tmp_path):
    st = _store(tmp_path)
    _full(st, 9002.0, carriers="JX", hours_ago=1)
    _full(st, 9314.0, carriers="JX", hours_ago=24 * 8)          # 上週
    _full(st, 12547.0, carriers="UA", hours_ago=2, d="NGO")
    text = dd.build(st, ROUTES, NOW)
    lines = text.splitlines()
    i = lines.index("傳統航空最低（華航、長榮、星宇等）")
    m30, m35 = dd._md(_d(30)), dd._md(_d(35))
    assert lines[i + 1] == f"KHH-OKA 9,002　{m30}–{m35} 星宇航空（上週 9,314，↓3%）"
    assert lines[i + 2].startswith("KHH-NGO 12,547") and "聯合航空" in lines[i + 2]


def test_digest_omits_the_section_without_full_service_data(tmp_path):
    st = _store(tmp_path)
    _full(st, 5000.0, fare_class="any", carriers="IT", hours_ago=1)
    assert "傳統航空最低" not in dd.build(st, ROUTES, NOW)
