"""FIE v2 — ProviderManager.

Coordinates multiple FlightProviders:
  * parallel query across providers (ThreadPoolExecutor)
  * fallback: a provider raising/timeout does NOT fail the batch — the others
    still return; failures are recorded for reliability tracking
  * weight control: each provider carries a reliability_score used by the
    ranking engine; providers can be enabled/disabled
Zero-refactor extensibility: register any new FlightProvider instance.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional

from .base import FlightProvider
from ..normalize import NormalizedOffer, is_valid

log = logging.getLogger(__name__)


@dataclass
class QueryResult:
    offers: list[NormalizedOffer] = field(default_factory=list)
    ok_sources: list[str] = field(default_factory=list)
    failed_sources: list[str] = field(default_factory=list)
    errors: dict = field(default_factory=dict)
    weights: dict = field(default_factory=dict)   # source -> reliability


class ProviderManager:
    def __init__(self, providers: Optional[list[FlightProvider]] = None,
                 max_workers: int = 4, reliability_store=None):
        self._providers: dict[str, FlightProvider] = {}
        self._max_workers = max_workers
        self._store = reliability_store
        for p in (providers or []):
            self.register(p)

    def register(self, provider: FlightProvider) -> None:
        self._providers[provider.source] = provider

    def unregister(self, source: str) -> None:
        self._providers.pop(source, None)

    @property
    def sources(self) -> list[str]:
        return list(self._providers)

    def reliability_of(self, source: str) -> float:
        if self._store is not None:
            return self._store.reliability(source)
        p = self._providers.get(source)
        return p.reliability_score if p else 0.6

    def _query_one(self, provider: FlightProvider, route, date):
        return provider.search_normalized(route, date)

    def search(self, route, date) -> QueryResult:
        """Query all providers in parallel; aggregate valid normalized offers."""
        result = QueryResult()
        result.weights = {s: self.reliability_of(s) for s in self._providers}
        if not self._providers:
            return result
        with ThreadPoolExecutor(max_workers=self._max_workers) as ex:
            futures = {ex.submit(self._query_one, p, route, date): p.source
                       for p in self._providers.values()}
            for fut in as_completed(futures):
                src = futures[fut]
                try:
                    offers = [o for o in fut.result() if is_valid(o)]
                    result.offers.extend(offers)
                    result.ok_sources.append(src)
                    if self._store is not None:
                        self._store.record(src, ok=True)
                except Exception as exc:                      # fallback: isolate failure
                    log.error("Provider %s failed: %s", src, exc)
                    result.failed_sources.append(src)
                    result.errors[src] = str(exc)
                    if self._store is not None:
                        self._store.record(src, ok=False)
        return result
