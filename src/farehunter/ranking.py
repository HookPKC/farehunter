"""FIE v2 — Flight ranking engine.

Turns a set of NormalizedOffers for one route into a decision:
    score = price_score + duration_score + stops_score
          + airline_quality + freshness_score + provider_reliability
each component normalised to 0..1 (higher = better) and combined with a
configurable weight profile. Lowest price is NOT automatically rank #1 —
time, stops, airline quality, data freshness and source reliability all count.

Outputs: ranked_results[] plus best_option, value_option, fastest_option,
cheapest_option.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional

from .normalize import NormalizedOffer
from .freshness import freshness_score
from .decision import RouteContext, evaluate, value_total, Decision

# Airline service-quality prior (0..1). Full-service > LCC; unknown -> neutral.
AIRLINE_QUALITY = {
    # full service
    "CI": 0.90, "BR": 0.92, "JX": 0.88, "JL": 0.95, "NH": 0.95, "CX": 0.90,
    "KE": 0.88, "OZ": 0.86, "SQ": 0.96, "TG": 0.84, "UA": 0.80, "VN": 0.78,
    # low cost
    "IT": 0.62, "TR": 0.60, "MM": 0.60, "AK": 0.58, "SL": 0.55, "GK": 0.58,
    "VZ": 0.55, "JW": 0.55, "D7": 0.58, "TW": 0.58, "7C": 0.55, "LJ": 0.55,
}
DEFAULT_AIRLINE_QUALITY = 0.65
NEUTRAL = 0.5


@dataclass
class WeightConfig:
    price: float = 0.35
    duration: float = 0.15
    stops: float = 0.10
    airline: float = 0.10
    freshness: float = 0.20
    reliability: float = 0.10

    def normalized(self) -> "WeightConfig":
        total = (self.price + self.duration + self.stops + self.airline
                 + self.freshness + self.reliability) or 1.0
        return WeightConfig(self.price / total, self.duration / total,
                            self.stops / total, self.airline / total,
                            self.freshness / total, self.reliability / total)


# Preset profiles
BALANCED = WeightConfig()
VALUE = WeightConfig(price=0.30, duration=0.15, stops=0.10, airline=0.20,
                     freshness=0.15, reliability=0.10)   # quality-tilted value


@dataclass
class ScoredOffer:
    offer: NormalizedOffer
    total: float
    components: dict = field(default_factory=dict)
    decision: Optional[Decision] = None       # FIE v1 explainable decision

    def to_dict(self, rule: Optional[str] = None, full: bool = True) -> dict:
        d = self.offer.to_dict()
        d["score"] = round(self.total, 4)      # kept for backward compatibility
        d["score_components"] = {k: round(v, 4) for k, v in self.components.items()}
        if self.decision:
            if full:                           # named picks: full explainability + provenance
                self.decision.merge_into(d, rule=rule)
            else:                              # ranked_results: keep it lean
                d["decision_score"] = self.decision.total
                d["stars"] = self.decision.stars
                d["confidence"] = self.decision.confidence
        return d


def _airline_quality(codes: list[str]) -> float:
    if not codes:
        return NEUTRAL
    vals = [AIRLINE_QUALITY.get(c, DEFAULT_AIRLINE_QUALITY) for c in codes]
    # a mixed itinerary is only as good as its weakest carrier
    return min(vals)


def _linear(value, lo, hi, invert=False) -> float:
    """Map value into 0..1 across [lo,hi]. invert=True -> lower value is better."""
    if value is None:
        return NEUTRAL
    if hi <= lo:
        return 1.0
    frac = (value - lo) / (hi - lo)
    frac = max(0.0, min(1.0, frac))
    return 1.0 - frac if invert else frac


def score_offers(offers: list[NormalizedOffer], weights: WeightConfig,
                 reliability_of=None, now=None) -> list[ScoredOffer]:
    """reliability_of: callable(source)->0..1; defaults to freshness-only if None."""
    if not offers:
        return []
    w = weights.normalized()
    prices = [o.price for o in offers]
    durs = [o.duration for o in offers if o.duration]
    p_lo, p_hi = min(prices), max(prices)
    d_lo, d_hi = (min(durs), max(durs)) if durs else (0, 0)

    scored = []
    for o in offers:
        price_s = _linear(o.price, p_lo, p_hi, invert=True)
        dur_s = _linear(o.duration, d_lo, d_hi, invert=True) if o.duration else NEUTRAL
        stops_s = 1.0 if o.stops == 0 else (NEUTRAL if o.stops is None
                                            else max(0.0, 1.0 - 0.4 * o.stops))
        air_s = _airline_quality(o.airline)
        fresh_s = freshness_score(o.source, o.observed_at, now)
        rel_s = reliability_of(o.source) if reliability_of else NEUTRAL
        comps = {"price": price_s, "duration": dur_s, "stops": stops_s,
                 "airline": air_s, "freshness": fresh_s, "reliability": rel_s}
        total = (w.price * price_s + w.duration * dur_s + w.stops * stops_s
                 + w.airline * air_s + w.freshness * fresh_s
                 + w.reliability * rel_s)
        scored.append(ScoredOffer(o, total, comps))
    scored.sort(key=lambda s: s.total, reverse=True)
    return scored


@dataclass
class RankedResults:
    ranked_results: list[ScoredOffer]
    best_option: Optional[ScoredOffer]
    value_option: Optional[ScoredOffer]
    fastest_option: Optional[ScoredOffer]
    cheapest_option: Optional[ScoredOffer]

    def to_dict(self) -> dict:
        def s(x, rule): return x.to_dict(rule=rule) if x else None
        return {
            "best_option": s(self.best_option, "BEST · 綜合最高分"),
            "value_option": s(self.value_option, "VALUE · 高CP值"),
            "fastest_option": s(self.fastest_option, "FASTEST · 飛行時間最短"),
            "cheapest_option": s(self.cheapest_option, "CHEAPEST · 價格最低"),
            "ranked_results": [x.to_dict(full=False) for x in self.ranked_results],
        }


def rank(offers: list[NormalizedOffer], weights: WeightConfig = BALANCED,
         reliability_of=None, now=None, ctx: RouteContext = None) -> RankedResults:
    scored = score_offers(offers, weights, reliability_of, now)
    if not scored:
        return RankedResults([], None, None, None, None)

    # FIE v1: attach the explainable Decision Score to every offer and make it
    # the primary ranking signal. Without ctx (older callers/tests) fall back to
    # the legacy weighted score so behaviour is unchanged.
    if ctx is not None:
        durs = [o.duration for o in offers if o.duration]
        for s in scored:
            s.decision = evaluate(s.offer, ctx, durs)
        scored.sort(key=lambda s: (s.decision.total, -s.offer.price), reverse=True)
        best = scored[0]
        value = max(scored, key=lambda s: (value_total(s.offer, ctx, durs),
                                           -s.offer.price))
    else:
        best = scored[0]
        value_scored = score_offers(offers, VALUE, reliability_of, now)
        value = value_scored[0] if value_scored else None

    cheapest = min(scored, key=lambda s: s.offer.price)
    with_dur = [s for s in scored if s.offer.duration]
    fastest = min(with_dur, key=lambda s: s.offer.duration) if with_dur else None
    return RankedResults(scored, best, value, fastest, cheapest)
