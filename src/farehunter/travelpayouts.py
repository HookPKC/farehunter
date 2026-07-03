"""Travelpayouts / Aviasales Data API client.

Endpoint: GET https://api.travelpayouts.com/aviasales/v3/prices_for_dates
Auth: token in X-Access-Token header (env var TRAVELPAYOUTS_TOKEN).
Data comes from the Aviasales search cache (recent user searches), so it's a
"cheapest seen" feed rather than a live availability quote — ideal for price
monitoring, but always verify before booking.

Docs: https://support.travelpayouts.com/hc/en-us/articles/203956163
"""
from __future__ import annotations

import os
import time
import logging
from typing import Optional

import requests

from .models import Offer

log = logging.getLogger(__name__)

BASE_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
AVIASALES_WEB = "https://www.aviasales.com"


class TravelpayoutsError(RuntimeError):
    def __init__(self, status_code: int, body: str):
        super().__init__(f"Travelpayouts API error {status_code}: {body[:500]}")
        self.status_code = status_code
        self.body = body


class TravelpayoutsClient:
    def __init__(self, token: Optional[str] = None,
                 session: Optional[requests.Session] = None):
        self.token = token or os.environ.get("TRAVELPAYOUTS_TOKEN", "")
        self.session = session or requests.Session()

    def search_month(self, origin: str, destination: str, month: str,
                     currency: str = "twd", market: Optional[str] = None,
                     direct: bool = False, one_way: bool = False,
                     limit: int = 100, max_retries: int = 3) -> dict:
        """Fetch cached cheapest fares for a route in a month (YYYY-MM)."""
        if not self.token:
            raise RuntimeError("Missing TRAVELPAYOUTS_TOKEN environment variable.")
        params = {
            "origin": origin,
            "destination": destination,
            "departure_at": month,          # YYYY-MM => all days in month
            "one_way": "true" if one_way else "false",
            "direct": "true" if direct else "false",
            "unique": "false",
            "sorting": "price",
            "currency": currency,
            "cy": currency,                  # docs use both names across endpoints
            "limit": limit,
            "page": 1,
        }
        if market:
            params["market"] = market
        headers = {"X-Access-Token": self.token,
                   "Accept-Encoding": "gzip, deflate"}

        for attempt in range(1, max_retries + 1):
            resp = self.session.get(BASE_URL, params=params, headers=headers,
                                    timeout=60)
            if resp.status_code == 200:
                payload = resp.json()
                if not payload.get("success", False):
                    raise TravelpayoutsError(200, str(payload.get("error")))
                return payload
            if resp.status_code == 429:
                wait = 2 ** attempt
                log.warning("Rate limited by Travelpayouts, sleeping %ss", wait)
                time.sleep(wait)
                continue
            raise TravelpayoutsError(resp.status_code, resp.text)
        raise TravelpayoutsError(429, "Exceeded retry budget (rate limited)")


# ---- parsing (pure function, unit-testable without network) ----------------
def parse_offers(payload: dict, origin: str, destination: str,
                 currency: str = "TWD") -> list[Offer]:
    """Flatten a prices_for_dates response; keep the cheapest fare per
    departure date.

    Route identity (origin/destination) is stamped from the caller's config,
    NOT from the response: the API returns city codes (e.g. TYO) while config
    uses airport codes (e.g. NRT), and mixing them breaks history matching."""
    best: dict[str, Offer] = {}
    for item in payload.get("data", []):
        try:
            price = float(item["price"])
            depart_date = str(item["departure_at"])[:10]
            return_date = str(item["return_at"])[:10] if item.get("return_at") else None
            link = item.get("link") or ""
            offer = Offer(
                origin=origin, destination=destination,
                depart_date=depart_date, return_date=return_date,
                price=price,
                currency=str(payload.get("currency", currency)).upper(),
                carriers=item.get("airline", "") or "",
                stops=int(item.get("transfers", 0) or 0),
                duration=str(item.get("duration", "") or ""),
                link=(AVIASALES_WEB + link) if link.startswith("/") else link,
            )
            prev = best.get(depart_date)
            if prev is None or offer.price < prev.price:
                best[depart_date] = offer
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("Skipping malformed offer: %s", exc)
    return [best[k] for k in sorted(best)]
