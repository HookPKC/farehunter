"""config.yaml 的門檻與 sweep_alerts（2026-11-23 起八條線都用 Google 直飛實價重算過）。"""
from pathlib import Path

import yaml

CFG = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text("utf-8"))


def _routes():
    return {f"{r['origin']}-{r['destination']}": r for r in CFG["routes"]}


def test_thresholds_recalculated_from_google_prices():
    got = {k: r["absolute_threshold"] for k, r in _routes().items()}
    assert got == {"TPE-NRT": 7800, "TPE-KIX": 6000, "KHH-NRT": 7500, "KHH-KIX": 8000,
                   "KHH-CTS": 12000, "KHH-FUK": 7100, "KHH-OKA": 5000, "KHH-NGO": 7700}


def test_every_route_alerts_on_sweep_prices():
    """門檻全部照實價定，巡檢查到的實價 ≤ 門檻都要發（否則新門檻只對快取價有作用）。"""
    assert [k for k, r in _routes().items() if not r.get("sweep_alerts")] == []
