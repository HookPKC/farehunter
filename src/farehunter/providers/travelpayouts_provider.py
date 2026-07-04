"""TravelpayoutsProvider — wraps the Aviasales cache client."""
from __future__ import annotations

from typing import Optional

from .base import FlightProvider, RawOffer
from ..normalize import NormalizedOffer, from_travelpayouts
from ..travelpayouts import TravelpayoutsClient


class TravelpayoutsProvider(FlightProvider):
    source = "travelpayouts"

    def __init__(self, client: Optional[TravelpayoutsClient] = None, **kw):
        super().__init__(**kw)
        self._client = client or TravelpayoutsClient()

    def search(self, route, date) -> list[RawOffer]:
        origin, destination = route
        depart_date, return_date = (date + (None,))[:2]
        month = str(depart_date)[:7]
        payload = self._client.search_month(origin, destination, month)
        return [RawOffer(self.source, item, origin, destination,
                         str(item.get("departure_at", ""))[:10],
                         str(item.get("return_at", ""))[:10] or None)
                for item in (payload.get("data") or [])]

    def normalize(self, raw: RawOffer) -> Optional[NormalizedOffer]:
        try:
            return from_travelpayouts(raw.raw, raw.origin, raw.destination)
        except (KeyError, ValueError, TypeError):
            return None
