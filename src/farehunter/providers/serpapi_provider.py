"""SerpApiProvider — wraps the SerpAPI google_flights client."""
from __future__ import annotations

from typing import Optional

from .base import FlightProvider, RawOffer
from ..normalize import NormalizedOffer, from_serpapi
from ..serpapi_flights import search_google_flights


class SerpApiProvider(FlightProvider):
    source = "serpapi"

    def __init__(self, search_fn=search_google_flights, **kw):
        super().__init__(**kw)
        self._search_fn = search_fn

    def search(self, route, date) -> list[RawOffer]:
        origin, destination = route
        depart_date, return_date = (date + (None,))[:2]
        payload = self._search_fn(origin, destination, depart_date, return_date)
        items = (payload.get("best_flights") or []) + (payload.get("other_flights") or [])
        return [RawOffer(self.source, it, origin, destination, depart_date, return_date)
                for it in items]

    def normalize(self, raw: RawOffer) -> Optional[NormalizedOffer]:
        try:
            return from_serpapi(raw.raw, raw.origin, raw.destination,
                                raw.depart_date, raw.return_date)
        except (KeyError, ValueError, TypeError):
            return None
