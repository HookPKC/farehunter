import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from farehunter.models import Offer
from farehunter.travelpayouts import parse_offers
from farehunter.storage import Store
from farehunter.analyzer import evaluate
from farehunter.notify import format_alert
from farehunter.runner import upcoming_months, load_config

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "prices_for_dates.json").read_text()
)


def make_offer(price, origin="TPE", dest="NRT", dep="2099-09-18", link="", fc="any"):
    return Offer(origin=origin, destination=dest, depart_date=dep,
                 return_date="2099-09-23", price=price, currency="TWD",
                 carriers="CI", stops=0, duration="190", link=link, fare_class=fc)


# ---- parsing ----------------------------------------------------------------
def test_parse_direct_only_drops_transfers():
    offers = parse_offers(FIXTURE, "TPE", "NRT", max_stops=0)
    assert all(o.stops == 0 for o in offers)
    assert "2099-09-25" not in [o.depart_date for o in offers]   # BR 轉機票被剔除


def test_parse_keeps_cheapest_per_date_and_skips_malformed():
    offers = parse_offers(FIXTURE, "TPE", "NRT")
    by = {(o.depart_date, o.fare_class): o for o in offers}
    assert len(offers) == 4                     # 2 dates x (any + full); malformed skipped
    o1 = by[("2099-09-18", "any")]
    assert o1.destination == "NRT"              # config code, not city code TYO
    assert o1.price == 8540.0                   # LCC wins the cheapest slot
    assert o1.carriers == "IT"
    assert o1.currency == "TWD"
    assert o1.return_date == "2099-09-23"
    assert o1.link.startswith("https://www.aviasales.com/search/")
    full = by[("2099-09-18", "full")]
    assert full.price == 9820.0 and full.carriers == "CI"   # 傳統航空另計
    assert by[("2099-09-25", "any")].stops == 1
    assert by[("2099-09-25", "full")].carriers == "BR"


def test_parse_empty_payload():
    assert parse_offers({"success": True, "data": []}, "TPE", "NRT") == []


# ---- storage ----------------------------------------------------------------
def test_store_record_and_stats(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    for p in [10000, 9000, 8000, 12000, 11000]:
        store.record(make_offer(p))
    store.record(make_offer(20000, fc="full"))   # full-service rows excluded from stats
    stats = store.route_stats_by_date("TPE", "NRT")["2099-09-18"]
    assert stats["n"] == 5
    assert stats["min"] == 8000
    assert stats["median"] == 10000
    assert stats["avg"] == pytest.approx(10000)
    assert store.route_stats_by_date("KHH", "KIX") == {}
    store.close()


def test_stats_are_kept_separate_per_departure_date(tmp_path):
    """核心回歸：便宜的日期不得被昂貴的日期拉高基準。

    舊版把整條航線混在一起算中位數。實測 TPE→KIX 各出發日均價從 5,853 到
    45,862（7.8 倍），混算出的中位數對任何一天都不具代表性。
    """
    store = Store(str(tmp_path / "t.db"))
    for p in [6000, 6200, 6400]:                       # 淡季那天
        store.record(make_offer(p, dep="2099-09-18"))
    for p in [30000, 32000, 34000]:                    # 旺季那天
        store.record(make_offer(p, dep="2099-12-31"))

    by_date = store.route_stats_by_date("TPE", "NRT")
    assert by_date["2099-09-18"]["median"] == 6200
    assert by_date["2099-12-31"]["median"] == 32000

    # 混算的話中位數會是 18,200，於是 9/18 那天要跌到 13,650 才算 big_drop——
    # 遠低於它自己的歷史最低 6,000，等於這天永遠不可能觸發；反過來旺季那天
    # 只要低於 13,650 就會誤報。分開算之後兩邊各自合理。
    assert by_date["2099-09-18"]["median"] * 0.75 < 6000
    store.close()


def test_alert_dedup(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.record_alert("TPE", "NRT", "2099-09-18", 7000, "absolute")
    assert store.recently_alerted("TPE", "NRT", "2099-09-18", 7000)
    assert not store.recently_alerted("TPE", "NRT", "2099-09-18", 6300)
    assert not store.recently_alerted("TPE", "NRT", "2099-09-25", 7000)
    store.close()


# ---- analyzer -----------------------------------------------------------------
def test_absolute_threshold_fires_without_history():
    v = evaluate(make_offer(6800), {"n": 0, "min": None, "avg": None, "median": None},
                 absolute_threshold=7000)
    assert v.is_deal and v.reason == "absolute"


def test_statistical_rules_need_history():
    stats = {"n": 5, "min": 9000, "avg": 10000, "median": 10000}
    v = evaluate(make_offer(7000), stats, absolute_threshold=None, min_history=30)
    assert not v.is_deal


def test_new_low_fires_with_history():
    stats = {"n": 60, "min": 8000, "avg": 10000, "median": 10000}
    v = evaluate(make_offer(7500), stats, absolute_threshold=None, min_history=30)
    assert v.is_deal and v.reason == "new_low"


def test_big_drop_fires_with_history():
    stats = {"n": 60, "min": 7000, "avg": 10000, "median": 10000}
    v = evaluate(make_offer(7400), stats, absolute_threshold=None,
                 drop_pct=25, min_history=30)
    assert v.is_deal and v.reason == "big_drop"


def test_normal_price_no_alert():
    stats = {"n": 60, "min": 7000, "avg": 10000, "median": 10000}
    v = evaluate(make_offer(9500), stats, absolute_threshold=6000, min_history=30)
    assert not v.is_deal


# ---- runner helpers -------------------------------------------------------------
def test_upcoming_months_spans_year_boundary():
    months = upcoming_months(4, today=date(2026, 11, 15))
    assert months == ["2026-11", "2026-12", "2027-01", "2027-02"]


def test_load_config_validates(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("defaults: {}")
    with pytest.raises(ValueError):
        load_config(str(bad))


def test_repo_config_is_valid():
    cfg = load_config(str(Path(__file__).resolve().parents[1] / "config.yaml"))
    assert len(cfg["routes"]) == 8
    for r in cfg["routes"]:
        assert "origin" in r and "destination" in r


# ---- notify -----------------------------------------------------------------
def test_format_alert_uses_google_link_with_dates():
    from farehunter.analyzer import Verdict
    v = Verdict(True, "absolute", "6,800 TWD <= 門檻 7,000")
    # 即使來源附帶 aviasales 深連結，也統一用 Google Flights（帶去回日期）
    text = format_alert(make_offer(6800, link="https://www.aviasales.com/search/x"), v)
    assert "google.com/travel/flights" in text
    assert "aviasales" not in text
    assert "TPE⇄NRT" in text and "約 6,800 TWD" in text   # 快取來源 → 約值
    # UNVERIFIED 文案:標題必須同時帶出「疑似低價」與「尚未經 Google 驗證」
    assert "疑似低價" in text and "尚未經 Google 驗證" in text
    assert "偵測於" in text and "台灣時間" in text
    assert "比價:" in text and "非即時報價" in text
    assert "Aviasales 快取估價" in text
    assert "天來回" in text
    # 無航空資訊（google 日曆來源）時優雅顯示
    o2 = make_offer(6800)
    o2.carriers = ""
    assert "多家航空" in format_alert(o2, v)
    # google 觀測價 → 保留精確數字、來源標觀測價
    o3 = make_offer(6814); o3.source = "google"
    t3 = format_alert(o3, v)
    assert "6,814 TWD" in t3 and "約" not in t3.split("觀測到:")[1].split("\n")[0]
    assert "Google Flights 觀測價" in t3
