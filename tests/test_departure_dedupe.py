"""同一條線、同一出發日 7 天內只發一次，除非再便宜 10%（2026-11-23）。零真實 API。"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import farehunter.runner as runner_mod                              # noqa: E402
from farehunter.models import Offer                                 # noqa: E402
from farehunter.storage import Store                                # noqa: E402

NOW = datetime.now(timezone.utc).replace(microsecond=0)
STAMP = NOW.isoformat(timespec="seconds")
CFG = {"defaults": {"currency": "twd"},
       "routes": [{"origin": "KHH", "destination": "FUK", "absolute_threshold": 7100,
                   "sweep_alerts": True}]}


def _d(n):
    return (NOW.date() + timedelta(days=n)).isoformat()


def _alert(st, dep, ret, price, reason="absolute", days_ago=2):
    st.conn.execute(
        "INSERT INTO alerts (origin, destination, depart_date, return_date, price, reason, sent_at,"
        " carrier_signature, price_status) VALUES ('KHH','FUK',?,?,?,?,datetime('now', ?),'IT','verified')",
        (dep, ret, price, reason, f"-{days_ago} days"))
    st.conn.commit()


def _google(st, dep, ret, price):
    st.record(Offer("KHH", "FUK", dep, ret, price, "TWD", "IT", 0, "", source="google",
                    provider="brightdata"), observed_at=STAMP)


def _sweep(st, monkeypatch):
    sent = []
    monkeypatch.setattr(runner_mod, "notify",
                        lambda o, v, s, **k: sent.append((o.depart_date, o.return_date, o.price))
                        or ["telegram"])
    monkeypatch.setattr(runner_mod, "channels_configured", lambda: True)
    runner_mod._sweep_alerts(st, CFG, NOW, line_budget=None)
    return sent


def test_a_different_return_on_the_same_departure_is_not_news(tmp_path, monkeypatch):
    """實例：KHH-FUK 11/25 出發，11/21 發過回程 12/02 的 6,781，11/23 又發回程 12/01 的 6,781。"""
    st = Store(str(tmp_path / "p.db"))
    _alert(st, _d(5), _d(12), 6781.0)
    _google(st, _d(5), _d(11), 6781.0)
    assert _sweep(st, monkeypatch) == []


def test_10pct_cheaper_on_the_same_departure_is_sent(tmp_path, monkeypatch):
    st = Store(str(tmp_path / "p.db"))
    _alert(st, _d(5), _d(12), 6781.0)
    _google(st, _d(5), _d(11), 6100.0)                       # 6,781 × 0.9 ＝ 6,103
    assert _sweep(st, monkeypatch) == [(_d(5), _d(11), 6100.0)]


def test_another_departure_or_an_old_alert_does_not_block(tmp_path, monkeypatch):
    st = Store(str(tmp_path / "p.db"))
    _alert(st, _d(5), _d(12), 6781.0)
    _alert(st, _d(9), _d(14), 6781.0, days_ago=8)            # 8 天前：過了 7 天
    _google(st, _d(6), _d(11), 6781.0)                       # 別的出發日
    _google(st, _d(9), _d(13), 6781.0)
    assert sorted(_sweep(st, monkeypatch)) == [(_d(6), _d(11), 6781.0), (_d(9), _d(13), 6781.0)]


def test_buckets_new_low_and_full_service_are_separate(tmp_path):
    st = Store(str(tmp_path / "p.db"))
    _alert(st, _d(5), _d(12), 6781.0, reason="absolute")
    _alert(st, _d(5), _d(12), 15000.0, reason="full_drop")
    assert st.departure_alert_floor("KHH", "FUK", _d(5), reason="big_drop") == 6781.0   # 一般理由共用
    assert st.departure_alert_floor("KHH", "FUK", _d(5), reason="full_drop") == 15000.0
    assert st.departure_alert_floor("KHH", "FUK", _d(5), reason="new_low") is None
    assert st.departure_alert_floor("KHH", "FUK", _d(6), reason="absolute") is None


def test_full_service_alerts_are_not_blocked_by_a_cheap_airline_alert(tmp_path, monkeypatch):
    st = Store(str(tmp_path / "p.db"))
    _alert(st, _d(5), _d(10), 6781.0, reason="absolute")
    pstate = type("S", (), {"selected_price": 13000.0})()
    verdict = type("V", (), {"reason": "full_drop"})()
    offer = Offer("KHH", "FUK", _d(5), _d(10), 13000.0, "TWD", "CI", 0, "")
    assert runner_mod._same_departure_no_news(st, offer, pstate, verdict) is False
    verdict.reason = "absolute"
    assert runner_mod._same_departure_no_news(st, offer, pstate, verdict) is True
