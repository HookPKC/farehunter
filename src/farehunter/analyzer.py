"""Deal detection: decide whether an observed price warrants an alert.

Rules (any one triggers, checked in order):
  1. absolute   — price <= route's configured absolute threshold
  2. new_low    — price is a new historical minimum (needs >= min_history obs)
  3. big_drop   — price <= median * (1 - drop_pct/100)   (needs >= min_history obs)

The history requirement prevents day-one false positives: with no history,
only the absolute threshold can fire.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .models import Offer


@dataclass
class Verdict:
    is_deal: bool
    reason: str          # "absolute" | "new_low" | "big_drop" | ""
    detail: str


def evaluate(offer: Offer, stats: dict, *,
             absolute_threshold: Optional[float] = None,
             drop_pct: float = 25.0,
             min_history: int = 30) -> Verdict:
    price = offer.price

    if absolute_threshold is not None and price <= absolute_threshold:
        return Verdict(True, "absolute",
                       f"{price:,.0f} {offer.currency} <= 門檻 {absolute_threshold:,.0f}")

    n = stats.get("n") or 0
    if n >= min_history:
        hist_min = stats.get("min")
        if hist_min is not None and price < hist_min:
            return Verdict(True, "new_low",
                           f"{price:,.0f} {offer.currency} 低於歷史最低 {hist_min:,.0f}（樣本 {n}）")
        median = stats.get("median")
        if median and price <= median * (1 - drop_pct / 100.0):
            pct = (1 - price / median) * 100
            return Verdict(True, "big_drop",
                           f"{price:,.0f} {offer.currency} 比中位數 {median:,.0f} 低 {pct:.0f}%（樣本 {n}）")

    return Verdict(False, "", "")
