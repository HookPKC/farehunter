"""Amadeus Self-Service API client.

Uses OAuth2 client-credentials flow. Environment is selected with
AMADEUS_ENV=test|production (default: test).

Docs: https://developers.amadeus.com/self-service/category/flights/api-doc/flight-offers-search
"""
from __future__ import annotations

import os
import time
import logging
from dataclasses import dataclass
from typing import Optional

import requests

log = logging.getLogger(__name__)

BASE_URLS = {
    "test": "https://test.api.amadeus.com",
    "production": "https://api.amadeus.com",
}


class AmadeusError(RuntimeError):
    def __init__(self, status_code: int, body: str):
        super().__init__(f"Amadeus API error {status_code}: {body[:500]}")
        self.status_code = status_code
        self.body = body


@dataclass
class Offer:
    """A single flight offer, flattened to the fields we track."""
    origin: str
    destination: str
    depart_date: str          # YYYY-MM-DD
    return_date: Optional[str]
    price: float
    currency: str
    carriers: str             # e.g. "CI" or "CI,BR"
    stops: int                # max stops across itineraries
    duration: str             # outbound duration, ISO8601 e.g. PT3H10M


class AmadeusClient:
    def __init__(self, client_id: Optional[str] = None,
                 client_secret: Optional[str] = None,
                 env: Optional[str] = None,
                 session: Optional[requests.Session] = None):
        self.client_id = client_id or os.environ.get("AMADEUS_CLIENT_ID", "")
        self.client_secret = client_secret or os.environ.get("AMADEUS_CLIENT_SECRET", "")
        env = (env or os.environ.get("AMADEUS_ENV", "test")).lower()
        if env not in BASE_URLS:
            raise ValueError(f"AMADEUS_ENV must be 'test' or 'production', got {env!r}")
        self.base_url = BASE_URLS[env]
        self.session = session or requests.Session()
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0

    # ---- auth -------------------------------------------------------------
    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        if not self.client_id or not self.client_secret:
            raise RuntimeError(
                "Missing AMADEUS_CLIENT_ID / AMADEUS_CLIENT_SECRET environment variables."
            )
        resp = self.session.post(
            f"{self.base_url}/v1/security/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise AmadeusError(resp.status_code, resp.text)
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expiry = time.time() + int(payload.get("expires_in", 1799))
        return self._token

    # ---- flight offers search ---------------------------------------------
    def search_flight_offers(self, origin: str, destination: str,
                             depart_date: str, return_date: Optional[str] = None,
                             adults: int = 1, currency: str = "TWD",
                             non_stop: bool = False, max_results: int = 20,
                             max_retries: int = 3) -> dict:
        """Call GET /v2/shopping/flight-offers and return the raw JSON payload."""
        params = {
            "originLocationCode": origin,
            "destinationLocationCode": destination,
            "departureDate": depart_date,
            "adults": adults,
            "currencyCode": currency,
            "max": max_results,
        }
        if return_date:
            params["returnDate"] = return_date
        if non_stop:
            params["nonStop"] = "true"

        for attempt in range(1, max_retries + 1):
            token = self._get_token()
            resp = self.session.get(
                f"{self.base_url}/v2/shopping/flight-offers",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=60,
            )
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 401:          # token expired mid-flight
                self._token = None
                continue
            if resp.status_code == 429:          # rate limited
                wait = 2 ** attempt
                log.warning("Rate limited by Amadeus, sleeping %ss", wait)
                time.sleep(wait)
                continue
            raise AmadeusError(resp.status_code, resp.text)
        raise AmadeusError(429, "Exceeded retry budget (rate limited)")


# ---- parsing (pure function, unit-testable without network) ---------------
def parse_offers(payload: dict, origin: str, destination: str,
                 depart_date: str, return_date: Optional[str]) -> list[Offer]:
    """Flatten an Amadeus flight-offers response into Offer records."""
    offers: list[Offer] = []
    for item in payload.get("data", []):
        try:
            price = float(item["price"]["grandTotal"])
            currency = item["price"]["currency"]
            itineraries = item.get("itineraries", [])
            if not itineraries:
                continue
            carriers = sorted({
                seg["carrierCode"]
                for itin in itineraries
                for seg in itin.get("segments", [])
            })
            stops = max(len(itin.get("segments", [])) - 1 for itin in itineraries)
            duration = itineraries[0].get("duration", "")
            offers.append(Offer(
                origin=origin, destination=destination,
                depart_date=depart_date, return_date=return_date,
                price=price, currency=currency,
                carriers=",".join(carriers), stops=stops, duration=duration,
            ))
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("Skipping malformed offer: %s", exc)
    return offers


def cheapest(offers: list[Offer]) -> Optional[Offer]:
    return min(offers, key=lambda o: o.price) if offers else None
