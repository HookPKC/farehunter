"""FIE v2 — Unified normalization layer.

Every provider's raw response is converted into ONE schema (NormalizedOffer)
regardless of source. Field availability differs by source (calendar sources
carry no times/airline), so each offer also gets a raw_quality_score that
reflects how complete it is. Downstream ranking degrades gracefully on the
fields a given source cannot supply.

Unified schema (as required by FIE v2 spec):
    price, currency, duration, stops, airline[], departure_time,
    arrival_time, route, source, raw_quality_score
plus operational fields the engine needs: depart_date, return_date,
observed_at, link.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

_ROUTE_RE = re.compile(r"^[A-Z]{3}-[A-Z]{3}$")

# Cache aggregators return an unreliable 'duration' (often ~2x the real
# single-flight time on these short-haul direct routes), so it must never be
# shown or ranked as a real flight duration.
CACHE_SOURCES = {"aviasales", "travelpayouts"}


@dataclass
class NormalizedOffer:
    price: float
    currency: str
    route: str                         # "KHH-KIX"
    source: str
    stops: Optional[int] = None
    duration: Optional[int] = None     # minutes
    airline: list[str] = field(default_factory=list)
    departure_time: Optional[str] = None   # ISO 8601 if known
    arrival_time: Optional[str] = None
    depart_date: Optional[str] = None      # YYYY-MM-DD
    return_date: Optional[str] = None
    observed_at: Optional[str] = None      # ISO 8601 UTC
    link: str = ""
    raw_quality_score: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def _codes(value) -> list[str]:
    """Any airline representation -> deduped, upper, order-stable list of codes."""
    if not value:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,\s/]+", value)
    else:
        parts = list(value)
    out, seen = [], set()
    for p in parts:
        c = str(p).strip().upper()
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _int_or_none(value) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (ValueError, TypeError):
        return None


def _quality(price, stops, duration, airline, dep_time) -> float:
    score = 0.40 if price and price > 0 else 0.0
    if stops is not None:
        score += 0.15
    if duration:
        score += 0.15
    if airline:
        score += 0.15
    if dep_time:
        score += 0.15
    return round(min(score, 1.0), 3)


def _build(price, currency, route, source, *, stops=None, duration=None,
           airline=None, dep_time=None, arr_time=None, depart_date=None,
           return_date=None, observed_at=None, link="") -> NormalizedOffer:
    airline = _codes(airline)
    stops = _int_or_none(stops)
    duration = _int_or_none(duration)
    return NormalizedOffer(
        price=float(price), currency=(currency or "TWD").upper(),
        route=route, source=source, stops=stops, duration=duration,
        airline=airline, departure_time=dep_time, arrival_time=arr_time,
        depart_date=depart_date, return_date=return_date,
        observed_at=observed_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        link=link,
        raw_quality_score=_quality(price, stops, duration, airline, dep_time))


# ---- per-source adapters -------------------------------------------------

def from_travelpayouts(item: dict, origin: str, destination: str) -> NormalizedOffer:
    dep_at = str(item.get("departure_at", "") or "")
    return _build(
        item["price"], item.get("currency", "TWD"),
        f"{origin}-{destination}", "travelpayouts",
        stops=item.get("transfers", 0), duration=None,   # cache duration unreliable
        airline=item.get("airline"), dep_time=dep_at or None,
        depart_date=dep_at[:10] or None,
        return_date=str(item.get("return_at", "") or "")[:10] or None,
        link=item.get("link", ""))


def _itinerary(it: dict, origin: str, destination: str, source: str,
               depart_date: str, return_date: str) -> NormalizedOffer:
    segs = it.get("flights") or []
    codes = [str(s.get("flight_number", "")).split(" ")[0] for s in segs]
    dep_time = (segs[0].get("departure_airport") or {}).get("time") if segs else None
    arr_time = (segs[-1].get("arrival_airport") or {}).get("time") if segs else None
    return _build(
        it["price"], it.get("currency", "TWD"),
        f"{origin}-{destination}", source,
        stops=max(len(segs) - 1, 0), duration=it.get("total_duration"),
        airline=codes, dep_time=dep_time, arr_time=arr_time,
        depart_date=depart_date, return_date=return_date)


def from_serpapi(it: dict, origin, destination, depart_date, return_date):
    return _itinerary(it, origin, destination, "serpapi", depart_date, return_date)


def from_scrapedo(it: dict, origin, destination, depart_date, return_date):
    return _itinerary(it, origin, destination, "scrapedo", depart_date, return_date)


def from_searchapi_calendar(row: dict, origin: str, destination: str) -> NormalizedOffer:
    # calendar rows: {departure, return, price} — no times/airline/duration
    return _build(
        row["price"], row.get("currency", "TWD"),
        f"{origin}-{destination}", "searchapi",
        depart_date=str(row.get("departure", "") or "") or None,
        return_date=str(row.get("return", "") or "") or None)


def from_observation(row) -> NormalizedOffer:
    """A stored warehouse row (observations table / Offer) -> NormalizedOffer.
    source in the warehouse is 'aviasales' or 'google'; times are not stored."""
    g = row.__getitem__ if hasattr(row, "keys") else (lambda k: getattr(row, k))
    src = g("source")
    duration = None if src in CACHE_SOURCES else g("duration")
    return _build(
        g("price"), g("currency"),
        f"{g('origin')}-{g('destination')}", src,
        stops=g("stops"), duration=duration, airline=g("carriers"),
        depart_date=g("depart_date"), return_date=g("return_date"),
        observed_at=g("observed_at"),
        link=(g("link") if _has(row, "link") else ""))


def _has(row, key) -> bool:
    try:
        row[key]; return True
    except Exception:
        return hasattr(row, key)


# ---- validation & consistency -------------------------------------------

def validate(offer: NormalizedOffer) -> list[str]:
    """Return a list of schema violations (empty == valid)."""
    errs = []
    if not isinstance(offer.price, (int, float)) or offer.price <= 0:
        errs.append(f"price invalid: {offer.price!r}")
    if not offer.currency:
        errs.append("currency empty")
    if not _ROUTE_RE.match(offer.route or ""):
        errs.append(f"route malformed: {offer.route!r}")
    if not offer.source:
        errs.append("source empty")
    if offer.stops is not None and offer.stops < 0:
        errs.append(f"stops negative: {offer.stops}")
    if offer.duration is not None and offer.duration <= 0:
        errs.append(f"duration non-positive: {offer.duration}")
    if not isinstance(offer.airline, list):
        errs.append("airline not a list")
    return errs


def is_valid(offer: NormalizedOffer) -> bool:
    return not validate(offer)
