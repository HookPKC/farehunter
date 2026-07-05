"""FIE v1 — Recommendation Engine (decision layer).

Turns a NormalizedOffer + route context into a fully explainable recommendation:

    decision_score (0-100) = weighted blend of five 0-100 subscores
        price · freshness · duration · source_reliability · confidence

plus a star rating (1-5), a High/Medium/Low confidence level, a natural-language
explanation that answers *why* this is recommended, and a provenance record that
says exactly which API, which departure date, which observation/update and which
ranking rule produced the pick.

This is intelligence only — it enriches ranked.json. No UI.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from .normalize import NormalizedOffer, CACHE_SOURCES
from .freshness import freshness_score, age_hours

# Canonical decision weights (sum = 1.0). Price dominates for a fare monitor;
# the other four keep a cheap-but-stale or cheap-but-unverifiable pick honest.
WEIGHTS = {"price": 0.40, "freshness": 0.15, "duration": 0.15,
           "source_reliability": 0.15, "confidence": 0.15}
# Value profile: tilted toward price efficiency. Used ONLY to *select* the
# value_option; the score it displays stays the canonical one.
VALUE_WEIGHTS = {"price": 0.55, "freshness": 0.10, "duration": 0.10,
                 "source_reliability": 0.10, "confidence": 0.15}

# Friendly API label per exact provider; falls back to source for rows that
# predate provider tracking.
API_LABEL = {
    "serpapi": "Google Flights (SerpAPI)",
    "scrapedo": "Google Flights (Scrape.do)",
    "searchapi": "Google Flights 日曆 (SearchApi)",
    "travelpayouts": "Travelpayouts / Aviasales 快取",
}
SOURCE_LABEL = {"google": "Google Flights (即時快照)",
                "aviasales": "Aviasales 快取", "travelpayouts": "Travelpayouts 快取"}


@dataclass
class RouteContext:
    """Historical anchors for one route (match export_web's displayed stats)."""
    price_min: float
    price_median: float
    n: int
    reliability_of: Optional[Callable[[str], float]] = None
    now: Optional[datetime] = None


def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def _price_subscore(price: float, ctx: RouteContext) -> float:
    """100 at/below the historical low, 50 at the median, decays above it."""
    mn, md = ctx.price_min, ctx.price_median
    if not md or md <= mn:
        return 100.0 if price <= (mn or price) else 50.0
    if price <= mn:
        return 100.0
    if price <= md:                       # min..median  -> 100..50
        return 50.0 + 50.0 * (md - price) / (md - mn)
    hi = 2 * md - mn                       # median..(2*median-min) -> 50..0
    if price >= hi:
        return 0.0
    return 50.0 * (hi - price) / (hi - md)


def _duration_subscore(offer: NormalizedOffer, durs) -> tuple[float, bool]:
    """0-100 within the day-set duration range; neutral 50 when unknown."""
    if not offer.duration:
        return 50.0, False
    if not durs or max(durs) <= min(durs):
        return 100.0, True
    lo, hi = min(durs), max(durs)
    return _clamp(100.0 * (1.0 - (offer.duration - lo) / (hi - lo))), True


def _completeness(offer: NormalizedOffer) -> float:
    c = 0.40 if offer.duration else 0.0
    c += 0.30 if offer.airline else 0.0
    c += 0.30 if offer.return_date else 0.0
    return c


def _confidence_subscore(offer: NormalizedOffer, fresh01: float,
                         ctx: RouteContext) -> float:
    """Certainty of THIS pick: completeness + corroboration + freshness + sample."""
    realness = 1.0 if offer.source not in CACHE_SOURCES else 0.55
    sample = min(1.0, (ctx.n or 0) / 30.0)
    blend = (0.35 * _completeness(offer) + 0.25 * realness
             + 0.20 * fresh01 + 0.20 * sample)
    return _clamp(100.0 * blend)


def _stars(total: float) -> int:
    return (5 if total >= 90 else 4 if total >= 75 else 3 if total >= 60
            else 2 if total >= 45 else 1)


def _confidence_level(conf: float, source: str) -> str:
    # Cache-only picks can never be "High" — an explicit source-reliability rule.
    if conf >= 78 and source not in CACHE_SOURCES:
        return "High"
    if conf >= 55:
        return "Medium"
    return "Low"


def _human_age(hrs: Optional[float]) -> str:
    if hrs is None:
        return "更新時間未知"
    if hrs < 1:
        return f"{max(1, int(hrs * 60))} 分鐘前"
    if hrs < 48:
        return f"{int(round(hrs))} 小時前"
    return f"{int(hrs // 24)} 天前"


def _api_label(offer: NormalizedOffer) -> str:
    if offer.provider:
        lab = API_LABEL.get(offer.provider.lower())
        if lab:
            return lab
    return SOURCE_LABEL.get(offer.source, offer.source)


@dataclass
class Decision:
    total: int
    subscores: dict
    stars: int
    confidence: str
    explanation: str
    provenance: dict

    def merge_into(self, d: dict, rule: Optional[str] = None) -> dict:
        d["decision_score"] = self.total
        d["subscores"] = dict(self.subscores)
        d["stars"] = self.stars
        d["confidence"] = self.confidence
        d["explanation"] = self.explanation
        prov = dict(self.provenance)
        if rule:
            prov["ranking_rule"] = f"FIE v1 · {rule}"
        d["provenance"] = prov
        return d


def _explain(offer, ctx, subs, total, level, api, hrs, dur_known) -> str:
    parts = []
    md = ctx.price_median
    if md and md > 0:
        diff = (offer.price - md) / md * 100
        if diff <= -15:
            parts.append(f"比中位價便宜 {abs(diff):.0f}%，屬近期低點（價格 {subs['price']}）")
        elif diff <= -3:
            parts.append(f"比中位價便宜 {abs(diff):.0f}%（價格 {subs['price']}）")
        elif diff < 3:
            parts.append(f"約在中位價（價格 {subs['price']}）")
        else:
            parts.append(f"高於中位價 {diff:.0f}%（價格 {subs['price']}）")
    else:
        parts.append(f"價格 {subs['price']}")

    if offer.stops == 0:
        parts.append("直飛")
    elif offer.stops:
        parts.append(f"轉機 {offer.stops} 次")

    if dur_known and offer.duration:
        parts.append(f"飛行 {offer.duration // 60}h{offer.duration % 60:02d}m"
                     f"（時間 {subs['duration']}）")
    else:
        parts.append("飛行時間資料累積中")

    parts.append(f"資料{_human_age(hrs)}由 {api} 更新"
                 f"（新鮮度 {subs['freshness']}、來源可信度 {subs['source_reliability']}）")
    return f"{'、'.join(parts)}。綜合決策分 {total}、信心 {level}。"


def evaluate(offer: NormalizedOffer, ctx: RouteContext, durs,
             *, weights=WEIGHTS, rule: str = "BALANCED") -> Decision:
    """Score one offer and produce its full explainable, traceable decision."""
    fresh01 = freshness_score(offer.source, offer.observed_at, ctx.now)
    dur_s, dur_known = _duration_subscore(offer, durs)
    rel01 = ctx.reliability_of(offer.source) if ctx.reliability_of else 0.6
    conf_s = _confidence_subscore(offer, fresh01, ctx)

    subs = {"price": _price_subscore(offer.price, ctx),
            "freshness": 100.0 * fresh01,
            "duration": dur_s,
            "source_reliability": 100.0 * rel01,
            "confidence": conf_s}
    total = sum(weights[k] * subs[k] for k in weights)

    sub_i = {k: int(round(v)) for k, v in subs.items()}
    total_i = int(round(total))
    level = _confidence_level(conf_s, offer.source)
    api = _api_label(offer)
    hrs = age_hours(offer.observed_at, ctx.now)

    provenance = {
        "api": api,
        "source": offer.source,
        "provider": offer.provider,                 # exact API key (null on legacy rows)
        "depart_date": offer.depart_date,           # which day
        "return_date": offer.return_date,
        "observed_at": offer.observed_at,           # which update produced the price
        "observed_age": _human_age(hrs),
        "ranking_rule": f"FIE v1 · {rule}",         # which rule produced this pick
        "weights": {k: round(weights[k], 2) for k in weights},
        "decided_at": (ctx.now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
    }
    explanation = _explain(offer, ctx, sub_i, total_i, level, api, hrs, dur_known)
    return Decision(total_i, sub_i, _stars(total), level, explanation, provenance)


def value_total(offer: NormalizedOffer, ctx: RouteContext, durs) -> float:
    """Selection-only metric for value_option (price-tilted); not displayed."""
    return evaluate(offer, ctx, durs, weights=VALUE_WEIGHTS, rule="VALUE").total
