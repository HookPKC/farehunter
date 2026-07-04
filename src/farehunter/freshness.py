"""FIE v2 — Data freshness & TTL layer.

Each source has a TTL (how long its data stays trustworthy). Freshness score
decays with age; past the TTL an offer is penalised (stale) but NEVER removed —
it can still surface if nothing fresher exists.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

# Hours a source's observation is considered "fresh". Cache decays fast;
# real-price captures stay useful longer but the calendar rotates slowly so
# 'google' warehouse rows are given a generous window.
TTL_HOURS = {
    "travelpayouts": 6,
    "aviasales": 6,          # warehouse tag for Travelpayouts cache
    "serpapi": 24 * 7,
    "scrapedo": 24 * 7,
    "searchapi": 24 * 10,
    "google": 24 * 14,       # warehouse tag for any real-price capture
}
DEFAULT_TTL_HOURS = 24
STALE_FLOOR = 0.10           # stale offers keep a small non-zero freshness


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def age_hours(observed_at: Optional[str], now: Optional[datetime] = None) -> Optional[float]:
    dt = _parse(observed_at)
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max((now - dt).total_seconds() / 3600.0, 0.0)


def ttl_for(source: str) -> float:
    return TTL_HOURS.get((source or "").lower(), DEFAULT_TTL_HOURS)


def freshness_score(source: str, observed_at: Optional[str],
                    now: Optional[datetime] = None) -> float:
    """1.0 = just observed, decays linearly to STALE_FLOOR at the TTL and
    stays at the floor beyond it (penalised, not deleted)."""
    age = age_hours(observed_at, now)
    if age is None:
        return 0.5                      # unknown timestamp -> neutral
    ttl = ttl_for(source)
    if age >= ttl:
        return STALE_FLOOR
    return round(STALE_FLOOR + (1.0 - STALE_FLOOR) * (1.0 - age / ttl), 4)


def is_stale(source: str, observed_at: Optional[str],
             now: Optional[datetime] = None) -> bool:
    age = age_hours(observed_at, now)
    return age is not None and age >= ttl_for(source)
