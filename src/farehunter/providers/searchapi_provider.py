"""SearchApiProvider — wraps the SearchApi google_flights_calendar client."""
from __future__ import annotations

from datetime import date as _date, timedelta
from typing import Optional

from .base import FlightProvider, RawOffer
from ..normalize import NormalizedOffer, from_searchapi_calendar
from ..searchapi_calendar import fetch_calendar


class SearchApiProvider(FlightProvider):
    source = "searchapi"

    def __init__(self, fetch_fn=fetch_calendar, window_days: int = 14, **kw):
        super().__init__(**kw)
        self._fetch_fn = fetch_fn
        self._window = window_days

    def search(self, route, date) -> list[RawOffer]:
        origin, destination = route
        depart_date = (date + (None,))[0]
        start = _date.fromisoformat(str(depart_date)[:10])
        end = start + timedelta(days=self._window - 1)
        payload = self._fetch_fn(origin, destination, start, end)
        return [RawOffer(self.source, row, origin, destination,
                         str(row.get("departure", "")) or None,
                         str(row.get("return", "")) or None)
                for row in (payload.get("calendar") or []) if "price" in row]

    def normalize(self, raw: RawOffer) -> Optional[NormalizedOffer]:
        try:
            return from_searchapi_calendar(raw.raw, raw.origin, raw.destination)
        except (KeyError, ValueError, TypeError):
            return None
