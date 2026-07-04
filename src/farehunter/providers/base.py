"""FlightProvider interface + RawOffer container.

Python translation of the required FIE v2 interface:
    interface FlightProvider {
      search(route, date): Promise<RawOffer[]>
      source: string
      reliability_score: number
    }
Every concrete provider wraps one existing API client, returns RawOffers, and
knows how to normalize its own raw payload into the unified schema.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from ..normalize import NormalizedOffer
from ..reliability import base_reliability


@dataclass
class RawOffer:
    source: str
    raw: dict                          # untouched provider payload item
    origin: str
    destination: str
    depart_date: str
    return_date: Optional[str] = None
    fetched_at: float = field(default_factory=time.time)


# route = (origin, destination); date = (depart_date, return_date|None)
Route = tuple
DateSpec = tuple


class FlightProvider(ABC):
    source: str = "base"

    def __init__(self, reliability_score: Optional[float] = None):
        self.reliability_score = (reliability_score
                                  if reliability_score is not None
                                  else base_reliability(self.source))

    @abstractmethod
    def search(self, route: Route, date: DateSpec) -> list[RawOffer]:
        """Query the provider for a route+date; return RawOffers (may be [])."""

    @abstractmethod
    def normalize(self, raw: RawOffer) -> Optional[NormalizedOffer]:
        """Convert one RawOffer into the unified schema (None if unusable)."""

    def search_normalized(self, route: Route, date: DateSpec) -> list[NormalizedOffer]:
        out = []
        for r in self.search(route, date):
            n = self.normalize(r)
            if n is not None:
                out.append(n)
        return out
