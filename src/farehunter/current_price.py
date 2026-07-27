"""Current-surface 新鮮度閘門(presentation 層純函式,零 API、零 DB)。

問題:`export_web.authoritative_latest()` 用「14 天內 google 無條件勝出」挑每個
出發日的權威價。那對 C″ planner 是正確的(它要找最久未驗證的行程),但拿來當
網站的「目前價格」就會讓 2–14 天前的 google 價蓋掉剛觀測到的 aviasales 價。
實測七條航線的主價全部是 19.8–90.8 小時前的 google 觀測。

本模組**不動** planner 共用的 authoritative_latest / hero_from_latest,只在
export 階段另外算一組 current 狀態與 eligibility,供 Hero / CTA / route card 用。

狀態:
  FRESH_VERIFIED   最新一筆就是 google(或不早於最新快取觀測),且在 surface SLA 內
  FRESH_ESTIMATE   最新一筆是快取觀測且在 SLA 內,無近期 google 參考
  PRICE_CONFLICT   最新是快取觀測,另有 ≤48h 的同日 google 參考且差幅 ≥10%
  NO_RECENT_PRICE  沒有任何觀測符合該 surface 的 SLA
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime

from .freshness import age_hours
from .price_state import carrier_signature

FRESH_VERIFIED = "fresh_verified"
FRESH_ESTIMATE = "fresh_estimate"
PRICE_CONFLICT = "price_conflict"
NO_RECENT_PRICE = "no_recent_price"

#: 各 current surface 的新鮮度 SLA(小時)
SURFACE_SLA_HOURS = {"hero": 24.0, "cta": 24.0, "route_primary": 48.0}

CONFLICT_WINDOW_HOURS = 48.0
CONFLICT_PCT = 10.0

_CACHE_SOURCES = {"aviasales", "travelpayouts"}


@dataclass(frozen=True)
class CurrentPolicy:
    hero_sla_hours: float = SURFACE_SLA_HOURS["hero"]
    cta_sla_hours: float = SURFACE_SLA_HOURS["cta"]
    route_primary_sla_hours: float = SURFACE_SLA_HOURS["route_primary"]
    conflict_window_hours: float = CONFLICT_WINDOW_HOURS
    conflict_pct: float = CONFLICT_PCT


DEFAULT_CURRENT_POLICY = CurrentPolicy()


def _is_cache(source: str | None) -> bool:
    return (source or "").lower() in _CACHE_SOURCES


def _pct(a: float, b: float) -> float:
    return abs(b - a) / a * 100.0 if a else 0.0


def _valid_price(row: dict) -> bool:
    """price 必須是正數才算有效報價。None / 0 / 負數一律不得成為 current 主價。"""
    p = row.get("price")
    return isinstance(p, (int, float)) and not isinstance(p, bool) and p > 0


def _cheapest(rows: list[dict]) -> dict | None:
    rows = [r for r in rows if _valid_price(r)]
    return min(rows, key=lambda r: r["price"]) if rows else None


def _same_trip(a: dict, b: dict) -> bool:
    """current 與 reference 是否為同一行程。

    兩者都來自同一組視窗查詢(已固定 fare_class='any' 且 stops=0),因此這裡
    只需再確認出發日、回程日、幣別與 carrier signature 相符。任何一項缺失或
    不同即回 False——寧可不標 conflict,也不要拿不同行程互相比較。
    """
    sig_a = carrier_signature(a.get("carriers"))
    sig_b = carrier_signature(b.get("carriers"))
    if sig_a is None or sig_b is None:
        return False
    if a.get("return_date") is None or b.get("return_date") is None:
        return False
    return (a.get("depart_date") == b.get("depart_date")
            and a.get("return_date") == b.get("return_date")
            and (a.get("currency") or "").upper() == (b.get("currency") or "").upper()
            and sig_a == sig_b)


@dataclass(frozen=True)
class CurrentPrice:
    state: str
    price: float | None
    source: str | None
    observed_at: str | None
    age_hours: float | None
    depart_date: str | None
    return_date: str | None
    reference_price: float | None
    reference_source: str | None
    reference_observed_at: str | None
    conflict_percentage: float | None
    eligible_for_hero: bool
    eligible_for_cta: bool
    eligible_for_route_primary: bool
    last_observed_price: float | None
    last_observed_at: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def resolve_current_price(latest_any: list[dict],
                          latest_google: list[dict],
                          now: datetime,
                          policy: CurrentPolicy | None = None) -> CurrentPrice:
    """挑出該航線的 current 主價與 eligibility。

    latest_any:    每個出發日一筆「最新觀測」(不給 google 任何優先權)
    latest_google: 每個出發日一筆「最新 google 觀測」
    兩者皆為 dict list,需含 price / source / observed_at / depart_date /
    return_date。純函式:不查 DB、不呼叫 API。
    """
    policy = policy or DEFAULT_CURRENT_POLICY
    gmap = {g["depart_date"]: g for g in latest_google}

    # last observed 一律保留(即使過期),供歷史參考顯示——但絕不當 current 主價
    _valid = [r for r in latest_any if _valid_price(r)]
    last = max(_valid, key=lambda r: r["observed_at"]) if _valid else None

    max_sla = max(policy.hero_sla_hours, policy.cta_sla_hours,
                  policy.route_primary_sla_hours)
    fresh = [r for r in _valid
             if (age_hours(r["observed_at"], now) or 1e9) <= max_sla]
    pick = _cheapest(fresh)

    if pick is None:
        return CurrentPrice(
            state=NO_RECENT_PRICE, price=None, source=None, observed_at=None,
            age_hours=None, depart_date=None, return_date=None,
            reference_price=None, reference_source=None,
            reference_observed_at=None, conflict_percentage=None,
            eligible_for_hero=False, eligible_for_cta=False,
            eligible_for_route_primary=False,
            last_observed_price=last["price"] if last else None,
            last_observed_at=last["observed_at"] if last else None)

    age = age_hours(pick["observed_at"], now) or 0.0
    ref = gmap.get(pick["depart_date"])
    if ref is not None and not (_valid_price(ref) and _same_trip(pick, ref)):
        ref = None            # 不同行程/無效價 → 不得作為衝突參考
    ref_age = age_hours(ref["observed_at"], now) if ref else None

    state = FRESH_VERIFIED if not _is_cache(pick["source"]) else FRESH_ESTIMATE
    ref_price = ref_source = ref_at = pct = None
    if state == FRESH_ESTIMATE and ref is not None and ref_age is not None \
            and ref_age <= policy.conflict_window_hours \
            and ref["observed_at"] <= pick["observed_at"]:
        p = _pct(pick["price"], ref["price"])
        if p >= policy.conflict_pct:
            state = PRICE_CONFLICT
            ref_price, ref_source = ref["price"], ref["source"]
            ref_at, pct = ref["observed_at"], p

    return CurrentPrice(
        state=state, price=pick["price"], source=pick["source"],
        observed_at=pick["observed_at"], age_hours=round(age, 1),
        depart_date=pick.get("depart_date"), return_date=pick.get("return_date"),
        reference_price=ref_price, reference_source=ref_source,
        reference_observed_at=ref_at,
        conflict_percentage=round(pct, 1) if pct is not None else None,
        eligible_for_hero=age <= policy.hero_sla_hours,
        eligible_for_cta=age <= policy.cta_sla_hours,
        eligible_for_route_primary=age <= policy.route_primary_sla_hours,
        last_observed_price=last["price"] if last else None,
        last_observed_at=last["observed_at"] if last else None)
