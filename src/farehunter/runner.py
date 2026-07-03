"""Runner: load config, sample dates, search each route, store, evaluate, notify."""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from pathlib import Path

import yaml

from .amadeus import AmadeusClient, parse_offers, cheapest, AmadeusError
from .storage import Store
from .analyzer import evaluate
from .notify import notify

log = logging.getLogger(__name__)


def load_config(path: str = "config.yaml") -> dict:
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not cfg or "routes" not in cfg:
        raise ValueError(f"{path} must define a 'routes' list")
    return cfg


def sample_departure_dates(days_ahead_start: int, days_ahead_end: int,
                           step_days: int, today: date | None = None) -> list[date]:
    """Evenly sample departure dates in [today+start, today+end]."""
    today = today or date.today()
    out, d = [], days_ahead_start
    while d <= days_ahead_end:
        out.append(today + timedelta(days=d))
        d += step_days
    return out


def run(config_path: str = "config.yaml", db_path: str = "prices.db") -> dict:
    cfg = load_config(config_path)
    defaults = cfg.get("defaults", {})
    client = AmadeusClient()
    store = Store(db_path)
    summary = {"searched": 0, "recorded": 0, "alerts": 0, "errors": 0}

    try:
        for route in cfg["routes"]:
            origin, dest = route["origin"], route["destination"]
            merged = {**defaults, **route}
            dates = sample_departure_dates(
                merged.get("days_ahead_start", 14),
                merged.get("days_ahead_end", 90),
                merged.get("sample_step_days", 7),
            )
            trip_len = merged.get("trip_length_days")  # None => one-way
            for dep in dates:
                ret = (dep + timedelta(days=trip_len)).isoformat() if trip_len else None
                summary["searched"] += 1
                try:
                    payload = client.search_flight_offers(
                        origin, dest, dep.isoformat(), ret,
                        currency=merged.get("currency", "TWD"),
                        non_stop=merged.get("non_stop", False),
                    )
                except AmadeusError as exc:
                    log.error("Search failed %s→%s %s: %s", origin, dest, dep, exc)
                    summary["errors"] += 1
                    continue

                offers = parse_offers(payload, origin, dest, dep.isoformat(), ret)
                best = cheapest(offers)
                if best is None:
                    log.info("No offers %s→%s %s", origin, dest, dep)
                    continue

                stats = store.route_stats(origin, dest)  # stats BEFORE this obs
                store.record(best)
                summary["recorded"] += 1

                verdict = evaluate(
                    best, stats,
                    absolute_threshold=merged.get("absolute_threshold"),
                    drop_pct=merged.get("drop_pct", 25.0),
                    min_history=merged.get("min_history", 30),
                )
                if verdict.is_deal and not store.recently_alerted(
                        origin, dest, best.depart_date, best.price):
                    notify(best, verdict)
                    store.record_alert(origin, dest, best.depart_date,
                                       best.price, verdict.reason)
                    summary["alerts"] += 1

                time.sleep(merged.get("pause_seconds", 1.0))  # be polite to the API
    finally:
        store.close()

    log.info("Run summary: %s", summary)
    return summary
