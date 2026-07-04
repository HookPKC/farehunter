import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from farehunter.scrapedo_flights import parse_cheapest_direct, _itineraries
from farehunter.verify_airlines import pick_candidates
from farehunter.storage import Store
from farehunter.models import Offer

PAYLOAD_SPLIT = {
    "best_flights": [
        {"flights": [{"flight_number": "IT 200", "airline": "Tigerair"},
                     {"flight_number": "MM 512", "airline": "Peach"}], "price": 8000},
        {"flights": [{"flight_number": "MM 620", "airline": "Peach"}], "price": 9100},
    ],
    "other_flights": [
        {"flights": [{"flight_number": "IT 202", "airline": "Tigerair"}], "price": 8900},
    ],
}
PAYLOAD_FLAT = {"flights": [
    {"flights": [{"flight_number": "CI 100", "airline": "China Airlines"}], "price": 14000},
]}


def test_parse_cheapest_direct_skips_connections():
    o = parse_cheapest_direct(PAYLOAD_SPLIT, "TPE", "NRT", "2099-08-01", "2099-08-06")
    assert o.price == 8900 and o.carriers == "IT"   # 8000 是轉機，排除
    assert o.source == "google" and o.stops == 0


def test_parse_handles_flat_shape():
    o = parse_cheapest_direct(PAYLOAD_FLAT, "TPE", "NRT", "2099-08-01", "2099-08-06")
    assert o.carriers == "CI" and o.price == 14000


def test_pick_candidates_prefers_cheapest_unverified_one_per_route(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    import datetime as dt
    d1 = (dt.date.today() + dt.timedelta(days=5)).isoformat()
    d2 = (dt.date.today() + dt.timedelta(days=6)).isoformat()
    r1 = (dt.date.today() + dt.timedelta(days=10)).isoformat()
    store.record(Offer("TPE", "NRT", d1, r1, 9000, "TWD", "", 0, "", source="google"))
    store.record(Offer("TPE", "NRT", d2, r1, 7000, "TWD", "", 0, "", source="google"))
    store.record(Offer("KHH", "KIX", d1, r1, 8000, "TWD", "", 0, "", source="google"))
    store.record(Offer("KHH", "FUK", d1, r1, 6000, "TWD", "IT", 0, "", source="google"))  # 已驗證
    cands = pick_candidates(store)
    got = [(c["origin"], c["destination"], c["price"]) for c in cands]
    assert got == [("TPE", "NRT", 7000), ("KHH", "KIX", 8000)]  # 每航線取最便宜、跳過已驗證
    store.close()
