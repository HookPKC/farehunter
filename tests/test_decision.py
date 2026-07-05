"""Tests for FIE v1 recommendation engine (decision.py + intelligence wiring)."""
import sys
import datetime as dt
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from farehunter.decision import (RouteContext, evaluate, WEIGHTS,
                                 _price_subscore, _confidence_level)
from farehunter.normalize import from_observation
from farehunter.storage import Store
from farehunter.models import Offer
from farehunter.intelligence import build_ranked


def _off(**kw):
    base = dict(price=9000, currency="TWD", route="KHH-KIX", source="google",
                stops=0, duration=190, airline=["IT"], depart_date="2026-09-15",
                return_date="2026-09-20",
                observed_at=dt.datetime.now(dt.timezone.utc).isoformat())
    base.update(kw)
    from farehunter.normalize import NormalizedOffer
    return NormalizedOffer(**base)


def _ctx(**kw):
    base = dict(price_min=7000, price_median=10000, n=400,
                reliability_of=lambda s: 0.9 if s == "google" else 0.6)
    base.update(kw)
    return RouteContext(**base)


def test_decision_has_all_five_subscores_and_blended_total():
    d = evaluate(_off(), _ctx(), durs=[150, 190, 240])
    for k in ("price", "freshness", "duration", "source_reliability", "confidence"):
        assert 0 <= d.subscores[k] <= 100
    manual = sum(WEIGHTS[k] * d.subscores[k] for k in WEIGHTS)
    assert abs(d.total - round(manual)) <= 1        # total is the weighted blend
    assert 1 <= d.stars <= 5


def test_price_subscore_anchors():
    ctx = _ctx(price_min=7000, price_median=10000)
    assert _price_subscore(7000, ctx) == 100        # at historical low
    assert _price_subscore(6000, ctx) == 100        # below low, capped
    assert round(_price_subscore(10000, ctx)) == 50  # at median
    assert _price_subscore(13000, ctx) == 0          # far above median


def test_confidence_level_thresholds_and_cache_cap():
    assert _confidence_level(85, "google") == "High"
    assert _confidence_level(85, "aviasales") == "Medium"   # cache can't be High
    assert _confidence_level(60, "google") == "Medium"
    assert _confidence_level(40, "google") == "Low"


def test_cache_source_never_high_confidence_end_to_end():
    # a very fresh, complete cache offer must still cap at Medium
    d = evaluate(_off(source="aviasales", provider="travelpayouts"),
                 _ctx(), durs=[190])
    assert d.confidence in ("Medium", "Low")


def test_explanation_answers_why_recommended():
    d = evaluate(_off(price=7500), _ctx(), durs=[150, 190])
    e = d.explanation
    assert "價格" in e and "決策分" in e and "信心" in e
    assert "直飛" in e                     # stops reflected
    assert "便宜" in e                     # price framing vs median


def test_provenance_is_complete_and_traceable():
    d = evaluate(_off(provider="serpapi"), _ctx(), durs=[190], rule="BEST")
    p = d.provenance
    for k in ("api", "source", "provider", "depart_date", "observed_at",
              "observed_age", "ranking_rule", "weights", "decided_at"):
        assert k in p and p[k] not in (None, "") or k == "provider"
    assert p["provider"] == "serpapi"
    assert p["api"] == "Google Flights (SerpAPI)"      # exact API resolved
    assert "BEST" in p["ranking_rule"]


def test_legacy_row_without_provider_falls_back(tmp_path):
    # a warehouse row without a provider column -> provider None, api from source
    row = {"price": 9000, "currency": "TWD", "origin": "KHH",
           "destination": "KIX", "source": "google", "stops": 0,
           "duration": "190", "carriers": "IT", "depart_date": "2026-09-15",
           "return_date": "2026-09-20",
           "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    offer = from_observation(row)
    assert offer.provider is None
    d = evaluate(offer, _ctx(), durs=[190])
    assert d.provenance["provider"] is None
    assert "Google Flights" in d.provenance["api"]


def test_best_is_highest_decision_score_end_to_end(tmp_path):
    db = tmp_path / "t.db"
    store = Store(str(db))
    nm = dt.date.today().replace(day=1) + dt.timedelta(days=40)
    for i, (price, dur, src, prov) in enumerate([
            (9000, "190", "google", "serpapi"),
            (7500, None, "aviasales", "travelpayouts"),   # cheaper but cache
            (9500, "200", "google", "scrapedo")]):
        dep = nm.replace(day=22 + i).isoformat()
        ret = nm.replace(day=27).isoformat()
        store.record(Offer("KHH", "KIX", dep, ret, price, "TWD", "IT", 0,
                           dur or "", source=src, provider=prov))
    store.close()
    data = build_ranked(str(db))
    r = data["routes"][0]
    best = r["best_option"]
    top = max(x["decision_score"] for x in r["ranked_results"])
    assert best["decision_score"] == top
    # engine metadata + traceability present on the pick
    assert data["engine"] == "recommendation-engine-v1"
    assert best["provenance"]["provider"] in ("serpapi", "scrapedo", "travelpayouts")
    assert best["stars"] >= 1 and best["confidence"] in ("High", "Medium", "Low")


def test_provider_column_populated_by_store(tmp_path):
    db = tmp_path / "t.db"
    store = Store(str(db))
    store.record(Offer("KHH", "KIX", "2026-09-15", "2026-09-20", 9000, "TWD",
                       "IT", 0, "190", source="google", provider="serpapi"))
    store.close()
    conn = sqlite3.connect(str(db))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(observations)")}
    assert "provider" in cols
    assert conn.execute("SELECT provider FROM observations").fetchone()[0] == "serpapi"
    conn.close()
