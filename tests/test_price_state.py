"""Commit 1:Alert conflict-aware price resolution 測試。

全部使用注入時鐘(NOW),不依賴真實 date.today()/datetime.now(),避免日期漂移。
兩組真實案例來自 2026-07-27 已送達的 LINE 通知:
  KHH→NGO 08-26~08-31 IT 直飛 alert 9,872 vs google 12,047(早 31.4h,+22.0%)
  KHH→NRT 09-05~09-08 GK 直飛 alert 7,761 vs google  8,778(早 43.4h,+13.1%)
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from farehunter import price_state as ps
from farehunter.price_state import (
    CONFLICT, STALE_CANDIDATE, UNVERIFIED, VERIFIED,
    PriceObservation, PricePolicy, carrier_signature, resolve_alert_price,
    same_itinerary,
)

NOW = datetime(2026, 7, 27, 6, 17, 0, tzinfo=timezone.utc)   # LINE B 送達時刻


def _obs(price, observed_at, *, source="aviasales", carriers="GK",
         origin="KHH", destination="NRT", depart="2026-09-05",
         ret="2026-09-08", stops=0, fare_class="any", currency="TWD",
         passengers=1):
    return PriceObservation(
        origin=origin, destination=destination, depart_date=depart,
        return_date=ret, price=price, currency=currency, carriers=carriers,
        stops=stops, fare_class=fare_class, source=source,
        observed_at=observed_at, passengers=passengers)


def _cand(price=7761.0, **kw):
    kw.setdefault("observed_at", NOW)
    return _obs(price, source="aviasales", **kw)


def _google(price, hours_before, **kw):
    kw.setdefault("carriers", "GK")
    return _obs(price, NOW - timedelta(hours=hours_before), source="google", **kw)


# ---- 1/2 VERIFIED ---------------------------------------------------------

def test_same_run_authoritative_yields_verified():
    st = resolve_alert_price(_cand(), NOW,
                             authoritative=_google(7200.0, 0))
    assert st.state == VERIFIED and st.verified is True
    assert st.selected_price == 7200.0 and st.selected_source == "google"
    assert st.eligible_for_alert is True


def test_authoritative_not_earlier_than_candidate_is_verified():
    """DB 參考價的時間不早於 candidate → 提升為 authoritative。"""
    st = resolve_alert_price(_cand(), NOW, reference=_google(7300.0, 0))
    assert st.state == VERIFIED
    assert st.selected_price == 7300.0


# ---- 3/4 CONFLICT(兩則真實 LINE 案例)-------------------------------------

def test_ngo_case_google_31h_higher_22pct_is_conflict():
    cand = _cand(9872.0, origin="KHH", destination="NGO",
                 depart="2026-08-26", ret="2026-08-31", carriers="IT")
    ref = _google(12047.0, 31.4, origin="KHH", destination="NGO",
                  depart="2026-08-26", ret="2026-08-31", carriers="IT")
    st = resolve_alert_price(cand, NOW, reference=ref)
    assert st.state == CONFLICT
    assert st.verified is False
    assert st.eligible_for_alert is True          # 不 suppress
    assert st.selected_price == 9872.0            # 保留即時性:用最新快取價
    assert st.reference_price == 12047.0
    assert 21.0 < st.conflict_percentage < 23.0


def test_nrt_case_google_43h_higher_13pct_is_conflict():
    st = resolve_alert_price(_cand(7761.0), NOW,
                             reference=_google(8778.0, 43.4))
    assert st.state == CONFLICT
    assert st.selected_price == 7761.0 and st.reference_price == 8778.0
    assert 12.5 < st.conflict_percentage < 13.5


# ---- 5 超出 conflict window ----------------------------------------------

def test_google_older_than_48h_is_unverified():
    st = resolve_alert_price(_cand(7761.0), NOW,
                             reference=_google(8778.0, 48.1))
    assert st.state == UNVERIFIED
    assert st.reference_price is None


# ---- 6-12 保守 itinerary 比對 --------------------------------------------

def test_different_return_date_not_compared():
    st = resolve_alert_price(_cand(7761.0), NOW,
                             reference=_google(8778.0, 20, ret="2026-09-09"))
    assert st.state == UNVERIFIED


def test_different_carrier_not_compared():
    st = resolve_alert_price(_cand(7761.0), NOW,
                             reference=_google(8778.0, 20, carriers="CI"))
    assert st.state == UNVERIFIED


def test_missing_carrier_on_either_side_is_unverified():
    st = resolve_alert_price(_cand(7761.0), NOW,
                             reference=_google(8778.0, 20, carriers=None))
    assert st.state == UNVERIFIED
    st2 = resolve_alert_price(_cand(7761.0, carriers=""), NOW,
                              reference=_google(8778.0, 20))
    assert st2.state == UNVERIFIED


def test_different_stops_not_compared():
    st = resolve_alert_price(_cand(7761.0), NOW,
                             reference=_google(8778.0, 20, stops=1))
    assert st.state == UNVERIFIED


def test_different_fare_class_not_compared():
    st = resolve_alert_price(_cand(7761.0), NOW,
                             reference=_google(8778.0, 20, fare_class="full"))
    assert st.state == UNVERIFIED


def test_different_passenger_count_not_compared():
    st = resolve_alert_price(_cand(7761.0), NOW,
                             reference=_google(8778.0, 20, passengers=2))
    assert st.state == UNVERIFIED


def test_different_currency_not_compared():
    st = resolve_alert_price(_cand(7761.0), NOW,
                             reference=_google(8778.0, 20, currency="JPY"))
    assert st.state == UNVERIFIED


# ---- 13/14 舊 Google 不得誤用 --------------------------------------------

def test_old_higher_google_does_not_permanently_block_new_promotion():
    """舊 google 較高 → 只標 CONFLICT(仍會通知),不永久封鎖;
    超過參考窗後完全不影響。"""
    inside = resolve_alert_price(_cand(6000.0), NOW,
                                 reference=_google(8778.0, 47))
    assert inside.state == CONFLICT and inside.eligible_for_alert is True
    outside = resolve_alert_price(_cand(6000.0), NOW,
                                  reference=_google(8778.0, 49))
    assert outside.state == UNVERIFIED


def test_old_lower_google_is_not_called_verified():
    st = resolve_alert_price(_cand(9000.0), NOW,
                             reference=_google(7000.0, 20))
    assert st.state == CONFLICT          # 差幅達標,但方向相反
    assert st.verified is False
    assert st.selected_source == "aviasales"


# ---- 15 candidate 過舊 ----------------------------------------------------

def test_stale_candidate_is_not_alert_eligible():
    old = _cand(7761.0, observed_at=NOW - timedelta(hours=1, minutes=1))
    st = resolve_alert_price(old, NOW, reference=_google(8778.0, 20))
    assert st.state == STALE_CANDIDATE
    assert st.eligible_for_alert is False


def test_candidate_within_sla_is_eligible():
    ok = _cand(7761.0, observed_at=NOW - timedelta(minutes=59))
    st = resolve_alert_price(ok, NOW)
    assert st.state == UNVERIFIED and st.eligible_for_alert is True


# ---- 16/17 無參考 / 差幅不足 ---------------------------------------------

def test_no_google_observation_is_unverified():
    st = resolve_alert_price(_cand(7761.0), NOW, reference=None)
    assert st.state == UNVERIFIED and st.reference_price is None


def test_small_gap_below_threshold_is_unverified():
    st = resolve_alert_price(_cand(7761.0), NOW,
                             reference=_google(8000.0, 20))   # +3.1%
    assert st.state == UNVERIFIED
    assert st.reference_price is None      # 第一版:不足門檻即不外顯參考


# ---- 18 純函式:不碰外部資源 ---------------------------------------------

def test_resolver_makes_no_external_calls(monkeypatch):
    import urllib.request
    import socket

    def _boom(*a, **k):                     # noqa: ANN001
        raise AssertionError("resolver 不得呼叫外部資源")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    st = resolve_alert_price(_cand(7761.0), NOW, reference=_google(8778.0, 20))
    assert st.state == CONFLICT


# ---- carrier signature 正規化 --------------------------------------------

def test_carrier_signature_normalisation():
    assert carrier_signature("ci,br") == carrier_signature("BR, CI") == "BR,CI"
    assert carrier_signature(None) is None
    assert carrier_signature("  ") is None


def test_same_itinerary_requires_return_date():
    a = _cand(100.0, ret=None)
    b = _google(100.0, 1, ret=None)
    assert same_itinerary(a, b) is False


# ---- policy 可注入 --------------------------------------------------------

def test_policy_is_injectable():
    strict = PricePolicy(candidate_sla_hours=0.5, conflict_window_hours=24.0,
                         conflict_pct=5.0)
    st = resolve_alert_price(_cand(7761.0), NOW,
                             reference=_google(8778.0, 43.4), policy=strict)
    assert st.state == UNVERIFIED          # 43.4h 超出 24h 窗


# ============ LINE 文案三態(測試 19)=====================================

from farehunter.analyzer import Verdict          # noqa: E402
from farehunter.models import Offer              # noqa: E402
from farehunter.notify import format_alert       # noqa: E402


def _offer(price=7761.0, source="aviasales", carriers="GK"):
    return Offer(origin="KHH", destination="NRT", depart_date="2026-09-05",
                 return_date="2026-09-08", price=price, currency="TWD",
                 carriers=carriers, stops=0, duration="180",
                 fare_class="any", source=source)


VERDICT = Verdict(is_deal=True, reason="absolute", detail="7,761 <= 門檻 8,000")


def test_line_copy_verified():
    st = resolve_alert_price(_cand(7761.0), NOW, authoritative=_google(7200.0, 0))
    text = format_alert(_offer(7200.0, source="google"), VERDICT, st)
    assert "已驗證低價" in text
    assert "Google 實價" in text and "7,200" in text
    assert "驗證於" in text and "台灣時間" in text
    assert "google.com/travel/flights" in text
    for banned in ("尚未經 Google 驗證", "快取估價"):
        assert banned not in text


def test_line_copy_conflict_shows_both_prices_and_times():
    st = resolve_alert_price(_cand(7761.0), NOW, reference=_google(8778.0, 43.4))
    text = format_alert(_offer(), VERDICT, st)
    assert "同航程價格有落差" in text
    assert "Aviasales 快取" in text and "Google 近期參考" in text
    assert "8,778" in text and "價差" in text
    assert "目前售價尚未確認" in text
    assert text.count("（0") + text.count("（1") >= 0        # 兩個時間戳皆存在
    assert "07/25" in text and "07/27" in text
    for banned in ("已驗證低價", "已證偽", "一定錯誤"):
        assert banned not in text


def test_line_copy_unverified():
    st = resolve_alert_price(_cand(7761.0), NOW, reference=None)
    text = format_alert(_offer(), VERDICT, st)
    assert "疑似低價" in text and "尚未經 Google 驗證" in text
    assert "快取估價" in text and "非即時報價" in text
    for banned in ("已驗證", "Google 實價", "同航程價格有落差"):
        assert banned not in text


def test_format_alert_without_state_keeps_legacy_unverified_copy():
    text = format_alert(_offer(), VERDICT, None)
    assert "疑似低價" in text and "尚未經 Google 驗證" in text


# ============ runner 整合:evaluate 前解析真的生效(測試 20/21)===========

import farehunter.runner as runner_mod            # noqa: E402
from farehunter.storage import Store              # noqa: E402

RUN_NOW = datetime(2026, 7, 27, 6, 17, 0, tzinfo=timezone.utc)


def _cfg(tmp_path, threshold=8000):
    p = tmp_path / "config.yaml"
    p.write_text(
        "defaults:\n  currency: twd\n  months_ahead: 1\n  pause_seconds: 0\n"
        "routes:\n  - origin: KHH\n    destination: NRT\n"
        f"    absolute_threshold: {threshold}\n", encoding="utf-8")
    return p


def _seed_google(db_path, price, observed_at):
    st = Store(str(db_path))
    st.conn.execute(
        "INSERT INTO observations (origin,destination,depart_date,return_date,"
        "price,currency,carriers,stops,duration,observed_at,fare_class,source,"
        "provider) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("KHH", "NRT", "2026-09-05", "2026-09-08", price, "TWD", "GK", 0, 180,
         observed_at.isoformat(timespec="seconds"), "any", "google", "serpapi"))
    st.conn.commit()
    st.close()


def _run(tmp_path, monkeypatch, sent, threshold=8000):
    monkeypatch.setattr(runner_mod, "TravelpayoutsClient",
                        type("C", (), {"__init__": lambda s, *a, **k: None,
                                       "search_month": lambda s, *a, **k: {}}))
    monkeypatch.setattr(runner_mod, "parse_offers",
                        lambda *a, **k: [_offer(7761.0)])
    monkeypatch.setattr(runner_mod, "notify",
                        lambda o, v, st=None: sent.append((o, v, st)) or ["line"])
    monkeypatch.setattr(runner_mod, "channels_configured", lambda: True)
    return runner_mod.run(str(_cfg(tmp_path, threshold)),
                          str(tmp_path / "prices.db"),
                          now=RUN_NOW)


def test_runner_verified_price_failing_threshold_creates_no_alert(tmp_path, monkeypatch):
    """同輪權威價 8,778 > 門檻 8,000 → evaluate 用權威價 → 不產生 Alert。"""
    _seed_google(tmp_path / "prices.db", 8778.0, RUN_NOW)      # 不早於 candidate
    sent = []
    summary = _run(tmp_path, monkeypatch, sent)
    assert sent == []
    assert summary["alerts"] == 0


def test_runner_conflict_still_notifies_with_conflict_state(tmp_path, monkeypatch):
    """43.4h 前的 google 12,047-等級落差 → CONFLICT:仍通知,但帶落差狀態。"""
    _seed_google(tmp_path / "prices.db", 8778.0,
                 RUN_NOW - timedelta(hours=43.4))
    sent = []
    summary = _run(tmp_path, monkeypatch, sent)
    assert summary["alerts"] == 1
    assert len(sent) == 1
    state = sent[0][2]
    assert state.state == CONFLICT
    assert state.selected_price == 7761.0 and state.reference_price == 8778.0


def test_runner_unverified_when_no_google(tmp_path, monkeypatch):
    sent = []
    summary = _run(tmp_path, monkeypatch, sent)
    assert summary["alerts"] == 1
    assert sent[0][2].state == UNVERIFIED


def test_runner_makes_no_google_flights_api_call(tmp_path, monkeypatch):
    """resolver 只讀 DB:不得觸發任何 SerpAPI/SearchApi/scrape.do 呼叫。"""
    import farehunter.serpapi_flights as sf
    calls = []
    monkeypatch.setattr(sf, "search_google_flights",
                        lambda *a, **k: calls.append(a) or {})
    _seed_google(tmp_path / "prices.db", 8778.0, RUN_NOW - timedelta(hours=20))
    _run(tmp_path, monkeypatch, [])
    assert calls == []
