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
from farehunter.storage import Store
from farehunter.models import Offer


NOW = datetime(2026, 7, 4, tzinfo=timezone.utc)


# ---- normalize -----------------------------------------------------------

def test_normalize_travelpayouts_drops_unreliable_cache_duration():
    item = {"price": 9000, "airline": "IT", "transfers": 0, "duration": 385,
            "departure_at": "2026-08-07T09:00:00+08:00",
            "return_at": "2026-08-12T10:00:00+08:00", "link": "/x"}
    o = N.from_travelpayouts(item, "TPE", "NRT")
    assert o.route == "TPE-NRT" and o.source == "travelpayouts"
    assert o.stops == 0 and o.airline == ["IT"]
    assert o.duration is None                 # cache duration NOT trusted
    assert o.departure_time.startswith("2026-08-07")
    assert N.is_valid(o)


def test_from_observation_drops_cache_duration_keeps_real():
    class _Row(dict):
        pass
    cache = _Row(origin="KHH", destination="KIX", depart_date="2026-11-04",
                 return_date="2026-11-08", price=8751, currency="TWD",
                 carriers="AK", stops=0, duration="385", source="aviasales",
                 observed_at="2026-07-04T00:00:00+00:00", link="")
    real = _Row(cache); real["source"] = "google"; real["duration"] = "165"
    assert N.from_observation(cache).duration is None      # cache 385min dropped
    assert N.from_observation(real).duration == 165        # real kept


def test_normalize_serpapi_itinerary_times_and_stops():
    it = {"price": 13540, "total_duration": 190, "flights": [
        {"flight_number": "BR 198", "departure_airport": {"id": "TPE", "time": "2026-07-31 14:20"},
         "arrival_airport": {"id": "NRT", "time": "2026-07-31 18:30"}}]}
    o = N.from_serpapi(it, "TPE", "NRT", "2026-07-31", "2026-08-05")
    assert o.airline == ["BR"] and o.stops == 0 and o.duration == 190
    assert o.departure_time == "2026-07-31 14:20"
    assert o.source == "serpapi"


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


# ---- intelligence end-to-end --------------------------------------------

def test_intelligence_end_to_end(tmp_path):
    from farehunter.intelligence import build_ranked
    db = tmp_path / "t.db"
    store = Store(str(db))
    import datetime as dt
    # build_ranked 的過濾窗以 SQLite 真實 date('now') 為基準(見 intelligence._SELECT_TMPL:
    # depart 需 >= 次月一日、>= now+21d、<= now+90d),production 不接受注入時鐘。因此測試
    # 日期必須相對「同一個真實今天」建構,且用固定偏移確保任何執行日都穩定落在窗內——
    # offset=45 對全年 366 天皆滿足三邊界(離次月一日/21d 下界與 90d 上界各留約 10 天緩衝),
    # 使結果與執行日期無關。原本 `.replace(day=10)` 把日子釘在 10 號,與滑動的 now+21d 相對
    # 距離不穩,7 月時 d1 落在 now+21d 前一天被剔除,只剩 9000 入選導致 cheapest 誤為 9000。
    d1 = dt.date.today() + dt.timedelta(days=45)
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
