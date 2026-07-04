import sys
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from farehunter import normalize as N
from farehunter import freshness as F
from farehunter.reliability import ReliabilityStore, base_reliability
from farehunter.ranking import (rank, score_offers, WeightConfig, BALANCED,
                                AIRLINE_QUALITY)
from farehunter.normalize import NormalizedOffer
from farehunter.providers.base import FlightProvider, RawOffer
from farehunter.providers.manager import ProviderManager
from farehunter.storage import Store
from farehunter.models import Offer


NOW = datetime(2026, 7, 4, tzinfo=timezone.utc)


# ---- normalize -----------------------------------------------------------

def test_normalize_travelpayouts_full_fields():
    item = {"price": 9000, "airline": "IT", "transfers": 0, "duration": 190,
            "departure_at": "2026-08-07T09:00:00+08:00",
            "return_at": "2026-08-12T10:00:00+08:00", "link": "/x"}
    o = N.from_travelpayouts(item, "TPE", "NRT")
    assert o.route == "TPE-NRT" and o.source == "travelpayouts"
    assert o.stops == 0 and o.duration == 190 and o.airline == ["IT"]
    assert o.departure_time.startswith("2026-08-07") and o.depart_date == "2026-08-07"
    assert o.raw_quality_score >= 0.9        # price+stops+duration+airline+time
    assert N.is_valid(o)


def test_normalize_serpapi_itinerary_times_and_stops():
    it = {"price": 13540, "total_duration": 190, "flights": [
        {"flight_number": "BR 198", "departure_airport": {"id": "TPE", "time": "2026-07-31 14:20"},
         "arrival_airport": {"id": "NRT", "time": "2026-07-31 18:30"}}]}
    o = N.from_serpapi(it, "TPE", "NRT", "2026-07-31", "2026-08-05")
    assert o.airline == ["BR"] and o.stops == 0 and o.duration == 190
    assert o.departure_time == "2026-07-31 14:20"
    assert o.source == "serpapi"


def test_normalize_searchapi_calendar_sparse_low_quality():
    o = N.from_searchapi_calendar({"departure": "2026-09-01", "return": "2026-09-06",
                                   "price": 8100}, "KHH", "KIX")
    assert o.airline == [] and o.duration is None and o.stops is None
    assert o.departure_time is None
    assert o.raw_quality_score < 0.5         # only price present
    assert N.is_valid(o)                      # still valid, just sparse


def test_airline_codes_consistency():
    assert N._codes("CI,BR") == ["CI", "BR"]
    assert N._codes("ci br  ci") == ["CI", "BR"]     # dedup + upper
    assert N._codes(["MM", "mm"]) == ["MM"]
    assert N._codes(None) == []


def test_validate_rejects_bad_offer():
    bad = NormalizedOffer(price=0, currency="", route="XXX", source="")
    errs = N.validate(bad)
    assert any("price" in e for e in errs)
    assert any("route" in e for e in errs)
    assert not N.is_valid(bad)


# ---- freshness -----------------------------------------------------------

def test_freshness_decays_and_floors_at_ttl():
    fresh = (NOW - timedelta(hours=1)).isoformat()
    stale = (NOW - timedelta(days=30)).isoformat()
    assert F.freshness_score("travelpayouts", fresh, NOW) > 0.8
    assert F.freshness_score("travelpayouts", stale, NOW) == F.STALE_FLOOR
    assert F.is_stale("travelpayouts", stale, NOW)
    assert not F.is_stale("google", fresh, NOW)


def test_freshness_unknown_timestamp_neutral():
    assert F.freshness_score("serpapi", None, NOW) == 0.5


def test_ttl_differs_by_source():
    assert F.ttl_for("travelpayouts") < F.ttl_for("google")


# ---- reliability ---------------------------------------------------------

def test_reliability_blends_base_with_success_rate(tmp_path):
    conn = sqlite3.connect(":memory:")
    rel = ReliabilityStore(conn)
    assert rel.reliability("serpapi") == base_reliability("serpapi")   # no stats yet
    for _ in range(20):
        rel.record("serpapi", ok=True)
    assert rel.reliability("serpapi") > base_reliability("serpapi") - 0.01
    for _ in range(20):
        rel.record("flaky", ok=False)
    assert rel.reliability("flaky") < base_reliability("flaky")


# ---- ranking -------------------------------------------------------------

def _mk(price, source="serpapi", dur=180, stops=0, air=("IT",), obs=None):
    return NormalizedOffer(price=price, currency="TWD", route="KHH-KIX",
                           source=source, stops=stops, duration=dur,
                           airline=list(air),
                           observed_at=(obs or NOW.isoformat()),
                           raw_quality_score=1.0)


def test_cheapest_is_not_always_best():
    # cheapest is a stale LCC; a slightly pricier fresh full-service wins overall
    cheap_stale = _mk(8000, source="travelpayouts", air=("IT",),
                      obs=(NOW - timedelta(days=30)).isoformat())
    pricier_fresh_fs = _mk(9000, source="serpapi", air=("BR",),
                           obs=NOW.isoformat(), dur=170)
    rr = rank([cheap_stale, pricier_fresh_fs],
              reliability_of=lambda s: base_reliability(s), now=NOW)
    assert rr.cheapest_option.offer.price == 8000
    assert rr.best_option.offer.price == 9000        # not the cheapest
    assert rr.best_option is not rr.cheapest_option


def test_fastest_option_by_duration():
    a = _mk(9000, dur=240); b = _mk(9500, dur=150); c = _mk(9200, dur=200)
    rr = rank([a, b, c], reliability_of=lambda s: 0.8, now=NOW)
    assert rr.fastest_option.offer.duration == 150


def test_fastest_ignores_offers_without_duration():
    withd = _mk(9500, dur=150)
    nodur = NormalizedOffer(price=8000, currency="TWD", route="KHH-KIX",
                            source="searchapi", duration=None,
                            observed_at=NOW.isoformat())
    rr = rank([withd, nodur], reliability_of=lambda s: 0.8, now=NOW)
    assert rr.fastest_option.offer.duration == 150      # sparse one excluded
    assert rr.cheapest_option.offer.price == 8000       # but still cheapest


def test_weights_are_configurable():
    cheap = _mk(8000, air=("IT",)); quality = _mk(8600, air=("JL",))
    price_heavy = WeightConfig(price=0.9, airline=0.02, duration=0.02,
                               stops=0.02, freshness=0.02, reliability=0.02)
    rr = rank([cheap, quality], price_heavy, reliability_of=lambda s: 0.8, now=NOW)
    assert rr.best_option.offer.price == 8000           # price dominates
    air_heavy = WeightConfig(price=0.05, airline=0.8, duration=0.05,
                             stops=0.04, freshness=0.03, reliability=0.03)
    rr2 = rank([cheap, quality], air_heavy, reliability_of=lambda s: 0.8, now=NOW)
    assert rr2.best_option.offer.airline == ["JL"]      # quality dominates


def test_stale_data_penalised_in_ranking():
    fresh = _mk(9000, obs=NOW.isoformat())
    stale = _mk(9000, obs=(NOW - timedelta(days=60)).isoformat())
    scored = {s.offer.observed_at: s.total
              for s in score_offers([fresh, stale], BALANCED,
                                    reliability_of=lambda s: 0.8, now=NOW)}
    assert scored[fresh.observed_at] > scored[stale.observed_at]


# ---- provider manager (fallback + parallel) ------------------------------

class _FakeProvider(FlightProvider):
    def __init__(self, source, offers=None, boom=False, **kw):
        self.source = source
        super().__init__(**kw)
        self._offers = offers or []
        self._boom = boom

    def search(self, route, date):
        if self._boom:
            raise RuntimeError("provider down")
        return [RawOffer(self.source, {"price": p}, "KHH", "KIX", "2026-09-01")
                for p in self._offers]

    def normalize(self, raw):
        return NormalizedOffer(price=raw.raw["price"], currency="TWD",
                               route="KHH-KIX", source=self.source,
                               stops=0, duration=180, airline=["IT"],
                               observed_at=NOW.isoformat())


def test_manager_parallel_aggregates_all_providers():
    mgr = ProviderManager([_FakeProvider("a", [9000]),
                           _FakeProvider("b", [8500, 9200])])
    res = mgr.search(("KHH", "KIX"), ("2026-09-01", "2026-09-06"))
    assert sorted(o.price for o in res.offers) == [8500, 9000, 9200]
    assert set(res.ok_sources) == {"a", "b"} and not res.failed_sources


def test_manager_fallback_isolates_failure():
    mgr = ProviderManager([_FakeProvider("good", [8800]),
                           _FakeProvider("bad", boom=True)])
    res = mgr.search(("KHH", "KIX"), ("2026-09-01", None))
    assert [o.price for o in res.offers] == [8800]      # good still returns
    assert res.ok_sources == ["good"] and res.failed_sources == ["bad"]
    assert "bad" in res.errors


def test_manager_reliability_tracking_updates_store():
    conn = sqlite3.connect(":memory:")
    store = ReliabilityStore(conn)
    mgr = ProviderManager([_FakeProvider("good", [8800]),
                           _FakeProvider("bad", boom=True)],
                          reliability_store=store)
    mgr.search(("KHH", "KIX"), ("2026-09-01", None))
    assert store.stats("good")["ok"] == 1
    assert store.stats("bad")["fail"] == 1


def test_new_provider_zero_refactor_registration():
    mgr = ProviderManager([_FakeProvider("a", [9000])])
    mgr.register(_FakeProvider("z", [7000]))            # plug in without touching others
    res = mgr.search(("KHH", "KIX"), ("2026-09-01", None))
    assert 7000 in [o.price for o in res.offers]
    assert set(mgr.sources) == {"a", "z"}


# ---- intelligence end-to-end --------------------------------------------

def test_intelligence_end_to_end(tmp_path):
    from farehunter.intelligence import build_ranked
    db = tmp_path / "t.db"
    store = Store(str(db))
    import datetime as dt
    d1 = (dt.date.today().replace(day=1) + dt.timedelta(days=40)).replace(day=10)
    d2 = d1 + dt.timedelta(days=7)
    # date1: cheap LCC (real); date2: pricier full-service (real), both fresh
    store.record(Offer("KHH", "KIX", d1.isoformat(), (d1+dt.timedelta(days=5)).isoformat(),
                       8000, "TWD", "IT", 0, "185", source="google"))
    store.record(Offer("KHH", "KIX", d2.isoformat(), (d2+dt.timedelta(days=5)).isoformat(),
                       9000, "TWD", "BR", 0, "170", source="google"))
    store.close()
    data = build_ranked(str(db))
    assert data["schema"] == "fie-v2"
    route = [r for r in data["routes"] if r["route"] == "KHH-KIX"][0]
    assert route["cheapest_option"]["price"] == 8000
    assert route["fastest_option"]["price"] == 9000          # BR 170min is faster
    assert route["best_option"] is not None
    assert len(route["ranked_results"]) == 2
    assert route["ranked_results"][0]["score"] >= route["ranked_results"][-1]["score"]
    assert "score_components" in route["ranked_results"][0]
