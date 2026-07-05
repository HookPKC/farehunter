"""Shared data models."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Near-term planning window (days ahead) shared by the fare calendar (export_web)
# AND the intelligence ranking (intelligence.py) so Hero / Recommended / Cheapest
# are computed over an IDENTICAL set of offers and can never diverge. Anything
# beyond this window belongs to the year-view monthly strip, not the calendar.
NEAR_TERM_DAYS = 90


@dataclass
class Offer:
    """A single fare observation, flattened to the fields we track."""
    origin: str
    destination: str
    depart_date: str          # YYYY-MM-DD
    return_date: Optional[str]
    price: float
    currency: str
    carriers: str             # e.g. "CI" or "CI,BR"
    stops: int
    duration: str             # minutes as string, e.g. "190" (source-dependent)
    link: str = ""            # booking deep link, if the source provides one
    fare_class: str = "any"   # "any" = cheapest overall, "full" = full-service carriers
    source: str = "aviasales" # "aviasales" cache | "google" real-price calendar
