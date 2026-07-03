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


def make_offer(price, origin="TPE", dest="NRT", dep="2099-09-18", link=""):
    return Offer(origin=origin, destination=dest, depart_date=dep,
                 return_date="2099-09-23", price=price, currency="TWD",
                 carriers="CI", stops=0, duration="190", link=link)


# ---- parsing ----------------------------------------------------------------
def test_parse_keeps_cheapest_per_date_and_skips_malformed():
    offers = parse_offers(FIXTURE, "TPE", "NRT")
    assert len(offers) == 2                     # 2 valid dates; malformed skipped
    o1, o2 = offers
    assert o1.destination == "NRT"              # config code, not city code TYO
    assert o1.depart_date == "2099-09-18"
    assert o1.price == 8540.0                   # min of 9820 / 8540 on same date
    assert o1.carriers == "IT"
    assert o1.currency == "TWD"
    assert o1.return_date == "2099-09-23"
    assert o1.link.startswith("https://www.aviasales.com/search/")
    assert o2.depart_date == "2099-09-25"
    assert o2.stops == 1


def test_parse_empty_payload():
    assert parse_offers({"success": True, "data": []}, "TPE", "NRT") == []


# ---- storage ----------------------------------------------------------------
def test_store_record_and_stats(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    for p in [10000, 9000, 8000, 12000, 11000]:
        store.record(make_offer(p))
    stats = store.route_stats("TPE", "NRT")
    assert stats["n"] == 5
    assert stats["min"] == 8000
    assert stats["median"] == 10000
    assert stats["avg"] == pytest.approx(10000)
    assert store.route_stats("KHH", "KIX")["n"] == 0
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
    assert len(cfg["routes"]) == 4
    for r in cfg["routes"]:
        assert "origin" in r and "destination" in r


# ---- notify -----------------------------------------------------------------
def test_format_alert_prefers_deep_link():
    from farehunter.analyzer import Verdict
    v = Verdict(True, "absolute", "6,800 TWD <= 門檻 7,000")
    with_link = format_alert(make_offer(6800, link="https://www.aviasales.com/search/x"), v)
    assert "訂票: https://www.aviasales.com/search/x" in with_link
    without = format_alert(make_offer(6800), v)
    assert "google.com/travel/flights" in without
    assert "TPE→NRT" in without and "6,800 TWD" in without
