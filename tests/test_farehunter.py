import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from farehunter.amadeus import parse_offers, cheapest, Offer
from farehunter.storage import Store
from farehunter.analyzer import evaluate
from farehunter.notify import format_alert
from farehunter.runner import sample_departure_dates, load_config

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "flight_offers.json").read_text()
)


def make_offer(price, origin="TPE", dest="NRT", dep="2026-09-18"):
    return Offer(origin=origin, destination=dest, depart_date=dep,
                 return_date="2026-09-23", price=price, currency="TWD",
                 carriers="CI", stops=0, duration="PT3H10M")


# ---- parsing ----------------------------------------------------------------
def test_parse_offers_flattens_valid_and_skips_malformed():
    offers = parse_offers(FIXTURE, "TPE", "NRT", "2026-09-18", "2026-09-23")
    assert len(offers) == 2                     # 3rd offer is malformed -> skipped
    assert offers[0].price == 9820.0
    assert offers[0].carriers == "CI"
    assert offers[0].stops == 0
    assert offers[1].price == 8540.0
    assert offers[1].carriers == "BR"
    assert offers[1].stops == 1                 # return via OKA has 2 segments


def test_cheapest_picks_lowest_price():
    offers = parse_offers(FIXTURE, "TPE", "NRT", "2026-09-18", "2026-09-23")
    assert cheapest(offers).price == 8540.0


def test_cheapest_empty_returns_none():
    assert cheapest([]) is None


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
    store.record_alert("TPE", "NRT", "2026-09-18", 7000, "absolute")
    # same price within 24h -> suppressed
    assert store.recently_alerted("TPE", "NRT", "2026-09-18", 7000)
    # 10% cheaper -> allowed through
    assert not store.recently_alerted("TPE", "NRT", "2026-09-18", 6300)
    # different date -> allowed
    assert not store.recently_alerted("TPE", "NRT", "2026-09-25", 7000)
    store.close()


# ---- analyzer -----------------------------------------------------------------
def test_absolute_threshold_fires_without_history():
    v = evaluate(make_offer(6800), {"n": 0, "min": None, "avg": None, "median": None},
                 absolute_threshold=7000)
    assert v.is_deal and v.reason == "absolute"


def test_statistical_rules_need_history():
    stats = {"n": 5, "min": 9000, "avg": 10000, "median": 10000}
    v = evaluate(make_offer(7000), stats, absolute_threshold=None, min_history=30)
    assert not v.is_deal                        # only 5 samples, rules inactive


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
def test_sample_departure_dates():
    dates = sample_departure_dates(14, 90, 7, today=date(2026, 7, 3))
    assert dates[0] == date(2026, 7, 17)
    assert dates[-1] <= date(2026, 10, 1)
    assert len(dates) == 11
    assert all((b - a).days == 7 for a, b in zip(dates, dates[1:]))


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
def test_format_alert_contains_essentials():
    from farehunter.analyzer import Verdict
    text = format_alert(make_offer(6800), Verdict(True, "absolute", "6,800 TWD <= 門檻 7,000"))
    assert "TPE→NRT" in text
    assert "6,800 TWD" in text
    assert "2026-09-18" in text
    assert "https://" in text
