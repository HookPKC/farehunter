"""Alert 的價格狀態解析（純函式，零 API、零 DB）。

背景：monitor 以 Aviasales 快取價觸發 Alert，但系統可能早已用 SerpAPI 取得
同航程的 Google 觀測價。實測 53 筆 alert 中有 17 筆在發報當下，DB 內已存在
高出 10% 以上的同航程 Google 價（其中兩筆有 LINE 截圖直接證明已送達）。

本模組把「該用哪個價、能不能稱為已驗證」抽成一個可測試的純函式，於 runner
呼叫 evaluate() **之前** 解析，因此 VERIFIED 時是用權威價去判定 deal，
不符合就自然不會產生 Alert——不需要任何 suppression 狀態機。

語意上的重要保留（見 docs 決議）：現有觀測沒有 flight number、行李、fare
family 等欄位，因此同 itinerary key 相符只能稱為「同航程價格有落差」
（same-itinerary price conflict），不得宣稱「完全相同票價產品已被證偽」。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# ---- 狀態常數 --------------------------------------------------------------
VERIFIED = "verified"
CONFLICT = "conflict"
UNVERIFIED = "unverified"
STALE_CANDIDATE = "stale_candidate"

# ---- 第一版時間政策 --------------------------------------------------------
CANDIDATE_SLA_HOURS = 1.0        # candidate 超過此齡不發主動通知
CONFLICT_WINDOW_HOURS = 48.0     # 較舊 Google 價可作衝突參考的上限
CONFLICT_PCT = 10.0              # 觸發 CONFLICT 的絕對差幅門檻（%）


@dataclass(frozen=True)
class PricePolicy:
    candidate_sla_hours: float = CANDIDATE_SLA_HOURS
    conflict_window_hours: float = CONFLICT_WINDOW_HOURS
    conflict_pct: float = CONFLICT_PCT


DEFAULT_POLICY = PricePolicy()


@dataclass(frozen=True)
class PriceObservation:
    """解析所需的最小觀測描述；candidate 與 reference 共用同一型別。"""
    origin: str
    destination: str
    depart_date: str
    return_date: str | None
    price: float
    currency: str
    carriers: str | None
    stops: int | None
    fare_class: str | None
    source: str
    observed_at: datetime
    passengers: int = 1


@dataclass(frozen=True)
class PriceState:
    state: str
    selected_price: float
    selected_source: str
    selected_observed_at: datetime
    selected_carrier_signature: str | None
    reference_price: float | None
    reference_source: str | None
    reference_observed_at: datetime | None
    reference_carrier_signature: str | None
    conflict_percentage: float | None
    verified: bool
    eligible_for_alert: bool
    display_label: str


# ---- itinerary 比對 --------------------------------------------------------

def carrier_signature(carriers: str | None) -> str | None:
    """正規化 carrier 簽章；無法判定時回 None（None 一律不得參與比對）。

    目前觀測只保存單一 carriers 欄位（可能是 "IT" 或 "CI,BR"），沒有分段
    航班號。因此簽章＝去空白、大寫、依字母排序後以逗號連接，讓 "BR,CI" 與
    "CI,BR" 視為同一組合；未來若保存 flight numbers 可在此升級。
    """
    if carriers is None:
        return None
    parts = [p.strip().upper() for p in str(carriers).split(",") if p.strip()]
    if not parts:
        return None
    return ",".join(sorted(parts))


def same_itinerary(a: PriceObservation, b: PriceObservation) -> bool:
    """保守的同航程判定：任一條件無法確定即回 False（fail closed）。

    比對 origin / destination / depart_date / return_date / stops /
    fare_class / currency / passengers / carrier signature。
    carrier 缺失（任一側為 None）一律不比對——寧可走 UNVERIFIED。
    """
    if a.return_date is None or b.return_date is None:
        return False
    if a.stops is None or b.stops is None:
        return False
    if a.fare_class is None or b.fare_class is None:
        return False
    sig_a, sig_b = carrier_signature(a.carriers), carrier_signature(b.carriers)
    if sig_a is None or sig_b is None:
        return False
    return (
        a.origin == b.origin
        and a.destination == b.destination
        and a.depart_date == b.depart_date
        and a.return_date == b.return_date
        and int(a.stops) == int(b.stops)
        and a.fare_class == b.fare_class
        and (a.currency or "").upper() == (b.currency or "").upper()
        and int(a.passengers) == int(b.passengers)
        and sig_a == sig_b
    )


def _pct(candidate_price: float, reference_price: float) -> float:
    if not candidate_price:
        return 0.0
    return abs(reference_price - candidate_price) / candidate_price * 100.0


def _hours(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds() / 3600.0


# ---- 解析主函式 ------------------------------------------------------------

def resolve_alert_price(candidate: PriceObservation,
                        now: datetime,
                        *,
                        authoritative: PriceObservation | None = None,
                        reference: PriceObservation | None = None,
                        policy: PricePolicy | None = None) -> PriceState:
    """回傳 Alert 的價格狀態。純函式：不查 DB、不呼叫任何 API。

    authoritative: 呼叫端於同一輪取得的權威結果（目前 production 不呼叫額外
        API，故常為 None；保留介面供未來使用）。
    reference:     同航程、observed_at 不晚於 candidate 的最新 Google 觀測，
        由呼叫端自 DB 取得。若其時間不早於 candidate，會被提升為 authoritative。
    """
    policy = policy or DEFAULT_POLICY
    cand_sig = carrier_signature(candidate.carriers)

    def _base(state: str, *, selected: PriceObservation, ref: PriceObservation | None,
              pct: float | None, verified: bool, eligible: bool, label: str) -> PriceState:
        return PriceState(
            state=state,
            selected_price=selected.price,
            selected_source=selected.source,
            selected_observed_at=selected.observed_at,
            selected_carrier_signature=carrier_signature(selected.carriers),
            reference_price=ref.price if ref else None,
            reference_source=ref.source if ref else None,
            reference_observed_at=ref.observed_at if ref else None,
            reference_carrier_signature=carrier_signature(ref.carriers) if ref else None,
            conflict_percentage=pct,
            verified=verified,
            eligible_for_alert=eligible,
            display_label=label,
        )

    # D. candidate 過舊 —— 不發主動通知（觀測本身仍由呼叫端保存為歷史）
    if _hours(now, candidate.observed_at) > policy.candidate_sla_hours:
        return _base(STALE_CANDIDATE, selected=candidate, ref=None, pct=None,
                     verified=False, eligible=False, label="候選觀測過舊")

    # 若 DB 參考價的時間不早於 candidate，等同同輪權威結果
    auth = authoritative
    if auth is None and reference is not None \
            and reference.observed_at >= candidate.observed_at:
        auth = reference

    # A. VERIFIED —— 只有同輪／更新且同航程的權威價才算
    if auth is not None and same_itinerary(candidate, auth):
        return _base(VERIFIED, selected=auth, ref=None, pct=None,
                     verified=True, eligible=True, label="已驗證低價")

    # B. CONFLICT —— 較舊但仍在參考窗內、且差幅達門檻
    if reference is not None and same_itinerary(candidate, reference):
        ref_age = _hours(now, reference.observed_at)
        if 0 <= ref_age <= policy.conflict_window_hours:
            pct = _pct(candidate.price, reference.price)
            if pct >= policy.conflict_pct:
                return _base(CONFLICT, selected=candidate, ref=reference, pct=pct,
                             verified=False, eligible=True,
                             label="同航程價格有落差")

    # C. UNVERIFIED —— 其餘全部（含無參考、超窗、差幅不足、產品條件無法比對）
    return _base(UNVERIFIED, selected=candidate, ref=None, pct=None,
                 verified=False, eligible=True,
                 label="疑似低價・尚未經 Google 驗證")
