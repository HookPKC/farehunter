import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from farehunter.serpapi_flights import (parse_full_service, pick_routes_for_today,
                                        snapshot_dates, _airline_code)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "serpapi_flights.json").read_text()
)


def test_parse_picks_cheapest_all_full_service():
    offer = parse_full_service(FIXTURE, "TPE", "NRT", "2026-07-31", "2026-08-05")
    # IT 8900 is LCC (excluded); BR+MM mixed itinerary excluded;
    # cheapest all-FSC is BR 13540 (beats CI 14203)
    assert offer is not None
    assert offer.price == 13540
    assert offer.carriers == "BR"
    assert offer.fare_class == "full"
    assert offer.depart_date == "2026-07-31"
    assert "google.com/travel/flights" in offer.link


def test_parse_returns_none_when_no_full_service():
    lcc_only = {"best_flights": FIXTURE["best_flights"][:1], "other_flights": []}
    assert parse_full_service(lcc_only, "TPE", "NRT", "2026-07-31", "2026-08-05") is None


def test_airline_code_extraction():
    assert _airline_code("BR 198") == "BR"
    assert _airline_code("") == ""


def test_route_rotation_covers_all_routes():
    routes = [{"origin": "A", "destination": str(i)} for i in range(10)]
    seen = set()
    for day in range(4):     # 4 days x 3/day covers 10 routes with wraparound
        picks = pick_routes_for_today(routes, today=date(2026, 7, 1 + day))
        assert len(picks) == 3
        seen.update(p["destination"] for p in picks)
    assert seen == {str(i) for i in range(10)}


def test_snapshot_dates_snap_to_friday():
    dep, ret = snapshot_dates(today=date(2026, 7, 3))
    d = date.fromisoformat(dep)
    assert d.weekday() == 4                      # Friday
    assert (d - date(2026, 7, 3)).days >= 28
    assert (date.fromisoformat(ret) - d).days == 5
