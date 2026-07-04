"""Runner: load config, iterate months per route, store, evaluate, notify."""
from __future__ import annotations

import logging
import time
from datetime import date
from pathlib import Path

import yaml

from .travelpayouts import TravelpayoutsClient, parse_offers, TravelpayoutsError
from .storage import Store
from .analyzer import evaluate
from .notify import notify, channels_configured

log = logging.getLogger(__name__)


def load_config(path: str = "config.yaml") -> dict:
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not cfg or "routes" not in cfg:
        raise ValueError(f"{path} must define a 'routes' list")
    return cfg


def upcoming_months(n: int, today: date | None = None) -> list[str]:
    """Current month plus the next n-1 months, as YYYY-MM strings."""
    today = today or date.today()
    out = []
    y, m = today.year, today.month
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def run(config_path: str = "config.yaml", db_path: str = "prices.db") -> dict:
    cfg = load_config(config_path)
    defaults = cfg.get("defaults", {})
    client = TravelpayoutsClient()
    store = Store(db_path)
    summary = {"searched": 0, "recorded": 0, "alerts": 0, "errors": 0}
    today_iso = date.today().isoformat()

    try:
        for route in cfg["routes"]:
            origin, dest = route["origin"], route["destination"]
            merged = {**defaults, **route}
            months = upcoming_months(merged.get("months_ahead", 6))
            stats = store.route_stats(origin, dest)   # stats BEFORE this run

            for month in months:
                summary["searched"] += 1
                try:
                    payload = client.search_month(
                        origin, dest, month,
                        currency=merged.get("currency", "twd"),
                        market=merged.get("market"),
                        direct=merged.get("non_stop", False),
                        one_way=merged.get("one_way", False),
                    )
                except TravelpayoutsError as exc:
                    log.error("Search failed %s→%s %s: %s", origin, dest, month, exc)
                    summary["errors"] += 1
                    continue

                offers = parse_offers(payload, origin, dest,
                    max_stops=0 if merged.get("non_stop") else None)
                if not offers:
                    log.info("No cached fares %s→%s %s", origin, dest, month)
                    continue

                for offer in offers:
                    if offer.depart_date < today_iso:
                        continue                      # stale cache entry
                    store.record(offer)
                    summary["recorded"] += 1

                    verdict = evaluate(
                        offer, stats,
                        absolute_threshold=merged.get("absolute_threshold"),
                        drop_pct=merged.get("drop_pct", 25.0),
                        min_history=merged.get("min_history", 30),
                    )
                    if verdict.is_deal and not store.recently_alerted(
                            origin, dest, offer.depart_date, offer.price):
                        sent = notify(offer, verdict)
                        if not sent and channels_configured():
                            log.error("通知發送失敗，保留至下一輪重試: %s→%s %s",
                                      origin, dest, offer.depart_date)
                            continue
                        store.record_alert(origin, dest, offer.depart_date,
                                           offer.price, verdict.reason)
                        summary["alerts"] += 1

                time.sleep(merged.get("pause_seconds", 0.6))
    finally:
        store.close()

    log.info("Run summary: %s", summary)
    return summary
