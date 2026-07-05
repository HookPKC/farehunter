"""End-to-end test: runner pipeline with a mocked Travelpayouts API layer."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import farehunter.runner as runner_mod
from farehunter.storage import Store

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "prices_for_dates.json").read_text()
)


class FakeClient:
    def __init__(self, *a, **kw):
        pass

    def search_month(self, *a, **kw):
        return FIXTURE


def test_full_pipeline(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "defaults:\n"
        "  currency: twd\n"
        "  months_ahead: 2\n"
        "  pause_seconds: 0\n"
        "routes:\n"
        "  - origin: TPE\n"
        "    destination: NRT\n"
        "    absolute_threshold: 9000\n",   # fixture 8540 fires, 10980 doesn't
        encoding="utf-8",
    )
    db = tmp_path / "prices.db"
    monkeypatch.setattr(runner_mod, "TravelpayoutsClient", FakeClient)

    summary = runner_mod.run(str(cfg), str(db))

    assert summary["searched"] == 2            # 2 months, 1 route
    assert summary["recorded"] == 8            # 2 dates x (any+full) x 2 months
    assert summary["errors"] == 0
    assert summary["alerts"] == 1              # any-class 8540 fires once; rest deduped/above
    out = capsys.readouterr().out
    assert out.count("曾出現低價") == 1
    assert "約 8,500 TWD" in out          # 快取來源 → 約值百位化
    assert "google.com/travel/flights" in out  # 統一 Google 比價連結（帶回程日）
    assert "非即時報價" in out                 # 免責聲明
    assert "through%202099-09-23" in out

    store = Store(str(db))
    assert store.route_stats("TPE", "NRT")["n"] == 4   # stats only count fare_class='any'
    store.close()

    # second run within 24h: same prices -> dedup suppresses the alert
    summary2 = runner_mod.run(str(cfg), str(db))
    assert summary2["alerts"] == 0
