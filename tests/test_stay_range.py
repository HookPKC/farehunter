"""只通知、只推薦住 2–10 晚的行程（使用者 2026-11-12 決定，models.MIN/MAX_STAY_NIGHTS）。

原本每個出發日只留最便宜的一筆、不看停留天數：近 30 天 50 則警報有 7 則是 14 晚以上
（含 2 則「30 天來回」），看板的推薦也出現 30 天來回。
"""
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import farehunter.runner as runner_mod                     # noqa: E402
from farehunter import brightdata_flights as bd            # noqa: E402
from farehunter.export_web import export                   # noqa: E402
from farehunter.intelligence import build_ranked           # noqa: E402
from farehunter.models import Offer, stay_ok, MIN_STAY_NIGHTS, MAX_STAY_NIGHTS   # noqa: E402
from farehunter.storage import Store                       # noqa: E402
from farehunter.travelpayouts import parse_offers          # noqa: E402

NOW = datetime(2026, 11, 15, 6, 17, tzinfo=timezone.utc)


def _d(n):
    return (NOW.date() + timedelta(days=n)).isoformat()


def test_the_range_is_two_to_ten_nights_inclusive():
    assert (MIN_STAY_NIGHTS, MAX_STAY_NIGHTS) == (2, 10)
    for nights, ok in ((0, False), (1, False), (2, True), (10, True), (11, False), (30, False)):
        assert stay_ok(_d(40), _d(40 + nights)) is ok, nights
    assert stay_ok(_d(40), None) and stay_ok(_d(40), "")      # 沒有回程日：無從判斷，放行


def _item(dep, ret, price, airline="IT"):
    return {"price": price, "airline": airline, "departure_at": f"{dep}T08:00:00+08:00",
            "return_at": f"{ret}T15:00:00+09:00", "transfers": 0, "duration": 400, "link": ""}


def test_a_cheaper_long_trip_does_not_hide_a_usable_one():
    """同一個出發日：30 晚的 5,000 不能蓋掉 5 晚的 6,200；只有超出範圍的行程時照樣留著（歷史不斷）。"""
    payload = {"success": True, "currency": "twd", "data": [
        _item(_d(40), _d(70), 5000), _item(_d(40), _d(45), 6200), _item(_d(40), _d(44), 6500),
        _item(_d(41), _d(71), 5100)]}
    got = {o.depart_date: (o.return_date, o.price) for o in parse_offers(payload, "KHH", "OKA")}
    assert got[_d(40)] == (_d(45), 6200.0)
    assert got[_d(41)] == (_d(71), 5100.0)


def _run(tmp_path, monkeypatch, offers, fetch=lambda *a: None):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("defaults:\n  currency: twd\n  months_ahead: 1\n  pause_seconds: 0\n"
                   "routes:\n  - origin: KHH\n    destination: OKA\n    absolute_threshold: 6500\n",
                   encoding="utf-8")
    monkeypatch.setattr(runner_mod, "TravelpayoutsClient",
                        type("C", (), {"__init__": lambda s, *a, **k: None,
                                       "search_month": lambda s, *a, **k: {}}))
    monkeypatch.setattr(runner_mod, "parse_offers", lambda *a, **k: list(offers))
    sent = []
    monkeypatch.setattr(runner_mod, "notify",
                        lambda o, v, st=None, **k: sent.append((o.depart_date, o.return_date)) or ["line"])
    monkeypatch.setattr(runner_mod, "channels_configured", lambda: True)
    monkeypatch.setattr(bd, "fetch", fetch)
    monkeypatch.setenv("FAREHUNTER_FORCE", "1")
    summary = runner_mod.run(str(cfg), str(tmp_path / "prices.db"), now=NOW)
    return summary, sent


def test_only_trips_in_range_alert_and_get_swept(tmp_path, monkeypatch):
    """30 晚與 1 晚的低價照樣記進資料庫，但不發警報、巡檢也不花額度去查。"""
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "k")
    asked = []

    def fetch(o, d, dep, ret):
        asked.append((dep, ret))
        return "<html></html>"

    offers = [Offer("KHH", "OKA", _d(40), _d(70), 5000.0, "TWD", "IT", 0, "90"),
              Offer("KHH", "OKA", _d(41), _d(42), 5200.0, "TWD", "IT", 0, "90"),
              Offer("KHH", "OKA", _d(42), _d(47), 6000.0, "TWD", "IT", 0, "90")]
    summary, sent = _run(tmp_path, monkeypatch, offers, fetch)
    assert sent == [(_d(42), _d(47))] and summary["recorded"] == 3
    assert (_d(40), _d(70)) not in asked and (_d(41), _d(42)) not in asked
    assert (_d(42), _d(47)) in asked


def test_the_board_and_the_recommendation_only_show_trips_in_range(tmp_path):
    db = tmp_path / "p.db"
    st = Store(str(db))
    real_now = datetime.now(timezone.utc)
    day = lambda n: (real_now.date() + timedelta(days=n)).isoformat()      # noqa: E731
    for n, nights, price in ((40, 30, 5000.0), (41, 5, 7000.0), (42, 1, 5500.0), (43, 10, 7200.0)):
        st.record(Offer("KHH", "OKA", day(n), day(n + nights), price, "TWD", "IT", 0, "90"))
    st.close()
    payload = export(str(db), str(tmp_path / "data.json"))
    route = payload["routes"][0]
    assert {c["depart_date"] for c in route["latest"]} == {day(41), day(43)}
    assert route["current"]["price"] == 7000.0                              # 不是 30 晚的 5,000
    assert all(m["price"] >= 7000.0 for m in route["monthly"])
    assert route["stats"]["min"] == 5000.0                                  # 歷史統計不受影響
    ranked = build_ranked(str(db))
    best = ranked["routes"]["KHH-OKA"]["best_option"] if isinstance(ranked.get("routes"), dict) \
        else next(r for r in ranked["routes"] if r.get("origin") == "KHH")["best_option"]
    assert best["depart_date"] in (day(41), day(43))
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 4
    con.close()
    assert json.loads((tmp_path / "data.json").read_text(encoding="utf-8"))["routes"]
