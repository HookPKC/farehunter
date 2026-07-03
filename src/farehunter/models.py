"""Shared data models."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


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
