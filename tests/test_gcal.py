import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from farehunter.searchapi_calendar import parse_calendar
from farehunter.storage import Store
from farehunter.models import Offer
from farehunter.export_web import export

PAYLOAD = {"calendar": [
    {"departure": "2099-08-01", "return": "2099-08-06", "price": 9800},
    {"departure": "2099-08-01", "return": "2099-08-05", "price": 9200},   # 4 晚更便宜
    {"departure": "2099-08-01", "return": "2099-08-20", "price": 7000},   # 19 晚，排除
    {"departure": "2099-08-02", "return": "2099-08-02", "has_no_flights": True},
    {"departure": "2099-08-03", "return": "2099-08-08", "price": 8100,
     "is_lowest_price": True},
    {"departure": "2020-01-01", "return": "2020-01-06", "price": 1},      # 過去，排除
]}


def test_parse_calendar_min_per_departure_within_trip_window():
    offers = parse_calendar(PAYLOAD, "TPE", "NRT", today=date(2099, 7, 1))
    assert [(o.depart_date, o.price) for o in offers] == \
        [("2099-08-01", 9200), ("2099-08-03", 8100)]
    o = offers[0]
    assert o.source == "google" and o.fare_class == "any"
    assert o.carriers == "" and o.stops == 0
    assert "through%202099-08-05" in o.link


def test_chip_prefers_fresh_google_price(tmp_path):
    db = tmp_path / "t.db"
    store = Store(str(db))
    store.record(Offer("TPE", "NRT", "2099-08-01", "2099-08-06", 9999, "TWD",
                       "IT", 0, "190", source="aviasales"))
    store.record(Offer("TPE", "NRT", "2099-08-01", "2099-08-06", 9200, "TWD",
                       "", 0, "", source="google"))
    # aviasales 之後又寫入一筆（模擬每小時監控）——google 價 8 天內仍應優先
    store.record(Offer("TPE", "NRT", "2099-08-01", "2099-08-06", 9950, "TWD",
                       "MM", 0, "190", source="aviasales"))
    store.close()
    payload = export(str(db), str(tmp_path / "d.json"))
    chip = payload["routes"][0]["latest"][0]
    assert chip["source"] == "google"
    assert chip["price"] == 9200
    assert chip["ref_carriers"] == "MM"     # 最近一筆快取所見航空作為參考


def test_route_insight_upsert_and_export(tmp_path):
    db = tmp_path / "t.db"
    store = Store(str(db))
    store.record(Offer("TPE", "NRT", "2099-08-01", "2099-08-06", 9200, "TWD",
                       "IT", 0, "190"))
    store.record_insight("TPE", "NRT", "2099-08-01", "high", 12000, 19000)
    store.record_insight("TPE", "NRT", "2099-08-07", "low", 12000, 19000)  # 覆蓋
    store.close()
    payload = export(str(db), str(tmp_path / "d.json"))
    ins = payload["routes"][0]["insight"]
    assert ins["price_level"] == "low" and ins["depart_date"] == "2099-08-07"
    assert ins["typical_low"] == 12000


def test_ref_carriers_falls_back_to_route_common(tmp_path):
    db = tmp_path / "t.db"
    store = Store(str(db))
    # 快取只有很遠的日期（>3 天差），但航線常見航空是 IT
    store.record(Offer("KHH", "CTS", "2099-09-20", "2099-09-24", 12000, "TWD",
                       "IT", 0, "250", source="aviasales"))
    store.record(Offer("KHH", "CTS", "2099-08-01", "2099-08-05", 10765, "TWD",
                       "", 0, "", source="google"))
    store.close()
    payload = export(str(db), str(tmp_path / "d.json"))
    chips = {c["depart_date"]: c for c in payload["routes"][0]["latest"]}
    assert chips["2099-08-01"]["ref_carriers"] == "IT"   # 航線常見航空退階


def test_monthly_low_picks_cheapest_per_month(tmp_path):
    db = tmp_path / "t.db"
    store = Store(str(db))
    import datetime as dt
    base = dt.date.today().replace(day=1)
    def d(month_offset, day, price, carriers="", source="aviasales"):
        m = base.month - 1 + month_offset
        y = base.year + m // 12
        dep = dt.date(y, m % 12 + 1, day)
        ret = dep + dt.timedelta(days=5)
        store.record(Offer("TPE", "NRT", dep.isoformat(), ret.isoformat(),
                           price, "TWD", carriers, 0, "", source=source))
    d(1, 10, 12000); d(1, 20, 9500, "IT")      # 次月最低 9500
    d(3, 5, 6800, "", "google"); d(3, 15, 7200) # 第3月最低 6800（google）
    store.close()
    payload = export(str(db), str(tmp_path / "d.json"))
    monthly = payload["routes"][0]["monthly"]
    prices = {m["ym"][-2:]: m["price"] for m in monthly}
    assert min(m["price"] for m in monthly) == 6800   # 全期最低
    # 每月只留一筆、取當月最低
    assert len([m for m in monthly]) == len({m["ym"] for m in monthly})
    cheapest = min(monthly, key=lambda m: m["price"])
    assert cheapest["source"] == "google"


def test_sweep_windows_tile_six_months_no_gaps():
    from farehunter.gcal_sweep import (sweep_windows, DEEP_POSITIONS,
                                       CHUNK_DAYS, NEAR_CHUNKS)
    import datetime as dt
    base = dt.date(2026, 1, 5)
    starts = set()
    for wk in range(DEEP_POSITIONS):
        wins = sweep_windows(base + dt.timedelta(weeks=wk))
        assert len(wins) == NEAR_CHUNKS + 1          # 每次固定 2 窗 = 16/週
        for s, e in wins:
            assert (e - s).days == CHUNK_DAYS - 1
            starts.add((s - (base + dt.timedelta(weeks=wk))).days)
    # 近端 day1 + 深掃 day15,29,...：相鄰窗起點間距恰為 CHUNK_DAYS（無縫、無重疊）
    deep = sorted(x for x in starts if x >= 15)
    assert deep[0] == 15
    assert all(b - a == CHUNK_DAYS for a, b in zip(deep, deep[1:]))
    assert deep[-1] + CHUNK_DAYS >= 180              # 覆蓋達 6 個月
