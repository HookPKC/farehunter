"""End-to-end test: runner pipeline with a mocked Amadeus API layer."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import farehunter.runner as runner_mod
from farehunter.storage import Store

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "flight_offers.json").read_text()
)


class FakeClient:
    """Returns the recorded fixture for every search."""
    def __init__(self, *a, **kw):
        self.calls = 0

    def search_flight_offers(self, *a, **kw):
        self.calls += 1
        return FIXTURE


def test_full_pipeline(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "defaults:\n"
        "  currency: TWD\n"
        "  days_ahead_start: 14\n"
        "  days_ahead_end: 28\n"
        "  sample_step_days: 7\n"
        "  trip_length_days: 5\n"
        "  pause_seconds: 0\n"
        "routes:\n"
        "  - origin: TPE\n"
        "    destination: NRT\n"
        "    absolute_threshold: 9000\n",   # fixture cheapest = 8540 -> fires
        encoding="utf-8",
    )
    db = tmp_path / "prices.db"
    monkeypatch.setattr(runner_mod, "AmadeusClient", FakeClient)

    summary = runner_mod.run(str(cfg), str(db))

    # 3 sampled dates (14, 21, 28 days ahead)
    assert summary["searched"] == 3
    assert summary["recorded"] == 3
    assert summary["errors"] == 0
    # 3 distinct departure dates, each below threshold -> 3 alerts
    assert summary["alerts"] == 3
    # no channel configured -> alerts printed to stdout
    out = capsys.readouterr().out
    assert out.count("低價警報") == 3
    assert "8,540 TWD" in out

    # observations persisted
    store = Store(str(db))
    assert store.route_stats("TPE", "NRT")["n"] == 3
    store.close()

    # second run within 24h: same prices -> dedup suppresses all alerts
    summary2 = runner_mod.run(str(cfg), str(db))
    assert summary2["alerts"] == 0
    assert Store(str(db)).route_stats("TPE", "NRT")["n"] == 6
