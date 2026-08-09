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

from .freshness import _parse as _parse_ts
from .normalize import CACHE_SOURCES
from .price_state import (carrier_signature,
                          CONFLICT_WINDOW_HOURS, CONFLICT_PCT)

FRESH_VERIFIED = "fresh_verified"
FRESH_ESTIMATE = "fresh_estimate"
PRICE_CONFLICT = "price_conflict"
NO_RECENT_PRICE = "no_recent_price"

#: 各 current surface 的新鮮度 SLA(小時)
SURFACE_SLA_HOURS = {"hero": 24.0, "cta": 24.0, "route_primary": 48.0}

# 衝突窗與差幅門檻與 Alert 端共用同一組定義(price_state)——通知說「有落差」
# 而網站說「沒問題」是使用者看得見的不一致,兩邊必須永遠同步。此處刻意
# re-export,讓既有的 `from .current_price import CONFLICT_PCT` 仍然可用。


@dataclass(frozen=True)
class CurrentPolicy:
    hero_sla_hours: float = SURFACE_SLA_HOURS["hero"]
    cta_sla_hours: float = SURFACE_SLA_HOURS["cta"]
    route_primary_sla_hours: float = SURFACE_SLA_HOURS["route_primary"]
    conflict_window_hours: float = CONFLICT_WINDOW_HOURS
    conflict_pct: float = CONFLICT_PCT


DEFAULT_CURRENT_POLICY = CurrentPolicy()


def _is_cache(source: str | None) -> bool:
    return (source or "").lower() in CACHE_SOURCES


def _pct(a: float, b: float) -> float:
    return abs(b - a) / a * 100.0 if a else 0.0


def _valid_price(row: dict) -> bool:
    """price 必須是正數才算有效報價。None / 0 / 負數一律不得成為 current 主價。"""
    p = row.get("price")
    return isinstance(p, (int, float)) and not isinstance(p, bool) and p > 0


def _cheapest(rows: list[dict]) -> dict | None:
    rows = [r for r in rows if _valid_price(r)]
    return min(rows, key=lambda r: r["price"]) if rows else None


#: same-itinerary key 中「若任一側出現就必須兩側都有且相等」的欄位。
#: 這些欄位目前由 export 的 SQL 視窗保證(origin/destination 為查詢參數、
#: fare_class='any' 與 stops=0 為字面量),但 resolver 不能依賴看不見的呼叫端
#: 不變式——SQL 一旦放寬就會靜默產生跨產品比較。因此在此顯式比對:兩側都有
#: 才比,只有一側有就 fail closed。
_OPTIONAL_KEYS = ("origin", "destination", "stops", "fare_class", "passengers")


def _same_trip(a: dict, b: dict) -> bool:
    """current 與 reference 是否為同一行程(保守、fail closed)。

    必要欄位(兩側都必須存在且相等):depart_date、return_date、currency、
    normalized carrier signature。任一缺失即回 False。

    條件欄位(_OPTIONAL_KEYS):只要任一側提供就必須兩側都提供且相等。
    passenger count 目前未存於 observations——全站為單人查詢,兩側皆無此欄位
    時視為同一前提;但只要有一側開始提供,就必須相符,避免未來擴充時靜默錯配。
    """
    sig_a = carrier_signature(a.get("carriers"))
    sig_b = carrier_signature(b.get("carriers"))
    if sig_a is None or sig_b is None:
        return False
    if a.get("return_date") is None or b.get("return_date") is None:
        return False
    if not (a.get("depart_date") == b.get("depart_date")
            and a.get("return_date") == b.get("return_date")
            and (a.get("currency") or "").upper() == (b.get("currency") or "").upper()
            and sig_a == sig_b):
        return False
    for key in _OPTIONAL_KEYS:
        in_a, in_b = key in a, key in b
        if in_a != in_b:
            return False              # 只有一側有該欄位 → 無法確認 → fail closed
        if in_a and a.get(key) != b.get(key):
            return False
    return True


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
    def _usable_age(row) -> float | None:
        """回傳可用於 current 判定的 age(小時);不可用時回 None。

        age 恰為 0.0(觀測與 export 同一秒)是**合法的最新資料**,必須視為
        fresh。因此一律以 `is None` 判斷缺失,不得使用 `age or fallback`
        這類會把 0.0 當成缺失值的寫法。

        這裡不直接用 freshness.age_hours,因為它會把未來時間 clamp 成 0.0,
        導致「observation 在未來」(時鐘或資料異常)被誤判成剛剛觀測。改以
        共用的 _parse 取得有號差值,負值一律保守排除於 current candidate。
        """
        dt = _parse_ts(row.get("observed_at"))
        if dt is None:
            return None
        a = (now - dt).total_seconds() / 3600.0
        return None if a < 0 else a

    fresh = [r for r in _valid
             if (lambda a: a is not None and a <= max_sla)(_usable_age(r))]
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

    age = _usable_age(pick)          # 已通過 fresh 過濾,必為非 None 且 >= 0
    ref = gmap.get(pick["depart_date"])
    if ref is not None and not (_valid_price(ref) and _same_trip(pick, ref)):
        ref = None            # 不同行程/無效價 → 不得作為衝突參考
    ref_age = _usable_age(ref) if ref else None   # None/負數 → 不作為參考

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
