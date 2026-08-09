"""Deal detection: decide whether an observed price warrants an alert.

Rules (any one triggers, checked in order):
  1. absolute   — price <= route's configured absolute threshold
  2. new_low    — price below the ROUTE's historical minimum (all departure dates)
  3. big_drop   — price <= THIS DEPARTURE DATE's median * (1 - drop_pct/100)

每條規則的比較基準刻意不同，因為它們問的是不同的問題：

  new_low  「這條航線史上最便宜」——極值事件，罕見而明確，用整條航線的資料
           才成立。改用單日基準會退化成「這天又刷新自己的紀錄」，那在累積初期
           天天發生（實測 5 週從 3 次暴增到 147 次）。

  big_drop 「這一天相對它自己平常的價格反常地便宜」——必須用單日基準。用整條
           航線的中位數會把淡旺季混算，對任何一天都不具代表性（實測 100 則
           big_drop 有 100 則的價格高於使用者自己設定的門檻）。

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
             date_stats: Optional[dict] = None,
             absolute_threshold: Optional[float] = None,
             drop_pct: float = 25.0,
             min_history: int = 30) -> Verdict:
    """stats = 整條航線的歷史（new_low 用）；date_stats = 該出發日的歷史（big_drop 用）。

    date_stats 為 None 時 big_drop 不觸發——寧可少發一則，也不要退回「拿全航線
    中位數當單日基準」那種靜默錯誤的比較。
    """
    price = offer.price

    if absolute_threshold is not None and price <= absolute_threshold:
        return Verdict(True, "absolute",
                       f"{price:,.0f} {offer.currency} <= 門檻 {absolute_threshold:,.0f}")

    n = stats.get("n") or 0
    if n >= min_history:
        hist_min = stats.get("min")
        if hist_min is not None and price < hist_min:
            return Verdict(True, "new_low",
                           f"{price:,.0f} {offer.currency} 低於此航線歷史最低 "
                           f"{hist_min:,.0f}（樣本 {n}）")

    dn = (date_stats or {}).get("n") or 0
    if dn >= min_history:
        median = date_stats.get("median")
        if median and price <= median * (1 - drop_pct / 100.0):
            pct = (1 - price / median) * 100
            return Verdict(True, "big_drop",
                           f"{price:,.0f} {offer.currency} 比 {offer.depart_date} 這天的"
                           f"中位數 {median:,.0f} 低 {pct:.0f}%（該日樣本 {dn}）")

    return Verdict(False, "", "")
