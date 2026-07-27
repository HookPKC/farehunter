"""Commit 3A:current-surface freshness eligibility 測試。

背景:`authoritative_latest()` 的「14 天內 google 無條件優先」對 C″ planner 正確,
但當成網站目前價格會讓 2–14 天前的 google 蓋掉剛觀測到的快取價(實測七條航線
主價為 19.8–90.8 小時前的 google)。本層在 export 階段另算 current 狀態與
eligibility,**不動** planner 共用的 authoritative_latest / hero_from_latest。

全部使用注入時鐘。
"""
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from farehunter.current_price import (
    FRESH_ESTIMATE, FRESH_VERIFIED, NO_RECENT_PRICE, PRICE_CONFLICT,
    CurrentPolicy, resolve_current_price,
)

NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)


def _row(price, hours_ago, source="aviasales", *, depart="2026-09-05",
         ret="2026-09-08", carriers="GK", currency="TWD"):
    return {
        "depart_date": depart, "return_date": ret, "price": price,
        "currency": currency, "carriers": carriers, "stops": 0,
        "observed_at": (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds"),
        "source": source,
    }


# ---- 1-3 三個 surface 的 SLA 邊界 -----------------------------------------

def test_hero_over_24h_not_eligible():
    cur = resolve_current_price([_row(9000.0, 24.5)], [], NOW)
    assert cur.eligible_for_hero is False
    assert cur.eligible_for_route_primary is True      # 48h 內仍可作主價


def test_cta_over_24h_not_eligible():
    cur = resolve_current_price([_row(9000.0, 30.0)], [], NOW)
    assert cur.eligible_for_cta is False


def test_route_primary_over_48h_not_eligible():
    cur = resolve_current_price([_row(9000.0, 48.5)], [], NOW)
    assert cur.state == NO_RECENT_PRICE
    assert cur.eligible_for_route_primary is False


def test_sla_boundaries_are_inclusive():
    assert resolve_current_price([_row(9000.0, 24.0)], [], NOW).eligible_for_hero is True
    assert resolve_current_price([_row(9000.0, 48.0)], [], NOW).eligible_for_route_primary is True


# ---- 4/5 fresh aviasales 不被舊 google 蓋掉 -------------------------------

def test_fresh_aviasales_not_overridden_by_old_google():
    """KHH→KIX 真實情境:0.3h 快取 10,291 vs 90.8h google 9,465。"""
    cur = resolve_current_price(
        [_row(10291.0, 0.3, "aviasales")],
        [_row(9465.0, 90.8, "google")], NOW)
    assert cur.price == 10291.0 and cur.source == "aviasales"
    assert cur.state == FRESH_ESTIMATE          # 90.8h 超出 48h 參考窗
    assert cur.reference_price is None


def test_two_week_old_google_gets_no_current_eligibility():
    cur = resolve_current_price([], [_row(9000.0, 24 * 14, "google")], NOW)
    assert cur.state == NO_RECENT_PRICE


def test_newer_google_is_fresh_verified():
    cur = resolve_current_price([_row(13873.0, 20.6, "google")],
                                [_row(13873.0, 20.6, "google")], NOW)
    assert cur.state == FRESH_VERIFIED
    assert cur.eligible_for_hero is True and cur.age_hours == 20.6


# ---- 6/7 conflict 與 estimate ---------------------------------------------

def test_price_conflict_when_recent_google_differs_over_10pct():
    """KHH→NGO 真實情境:快取 9,872 vs 43h 前 google 12,047(+22%)。"""
    cur = resolve_current_price(
        [_row(9872.0, 0.5, "aviasales", depart="2026-08-26", ret="2026-08-31",
              carriers="IT")],
        [_row(12047.0, 43.0, "google", depart="2026-08-26", ret="2026-08-31",
              carriers="IT")], NOW)
    assert cur.state == PRICE_CONFLICT
    assert cur.price == 9872.0                  # 主價仍用最新快取,不用舊 google
    assert cur.reference_price == 12047.0
    assert 21.0 < cur.conflict_percentage < 23.0


def test_fresh_estimate_when_no_valid_reference():
    cur = resolve_current_price([_row(7120.0, 0.5)], [], NOW)
    assert cur.state == FRESH_ESTIMATE and cur.reference_price is None


def test_gap_below_threshold_is_not_conflict():
    cur = resolve_current_price([_row(9000.0, 0.5)],
                                [_row(9400.0, 20.0, "google")], NOW)
    assert cur.state == FRESH_ESTIMATE          # 只差 4.4%


# ---- 8/9 無 fresh price ---------------------------------------------------

def test_no_recent_price_keeps_last_observed_but_not_as_current():
    cur = resolve_current_price([_row(9465.0, 90.8)], [], NOW)
    assert cur.state == NO_RECENT_PRICE
    assert cur.price is None                    # 不得 silent fallback
    assert cur.last_observed_price == 9465.0    # 但保留歷史參考
    assert cur.last_observed_at is not None


def test_empty_input_is_no_recent_price():
    cur = resolve_current_price([], [], NOW)
    assert cur.state == NO_RECENT_PRICE
    assert cur.price is None and cur.last_observed_price is None
    assert (cur.eligible_for_hero, cur.eligible_for_cta,
            cur.eligible_for_route_primary) == (False, False, False)


# ---- 13 不同行程不得互相比較 ----------------------------------------------

def test_different_return_date_reference_is_ignored():
    cur = resolve_current_price(
        [_row(9872.0, 0.5)],
        [_row(12047.0, 20.0, "google", ret="2026-09-12")], NOW)
    assert cur.state == FRESH_ESTIMATE and cur.reference_price is None


def test_different_carrier_reference_is_ignored():
    cur = resolve_current_price(
        [_row(9872.0, 0.5, carriers="GK")],
        [_row(12047.0, 20.0, "google", carriers="CI")], NOW)
    assert cur.state == FRESH_ESTIMATE and cur.reference_price is None


def test_missing_carrier_reference_is_ignored():
    cur = resolve_current_price(
        [_row(9872.0, 0.5)],
        [_row(12047.0, 20.0, "google", carriers=None)], NOW)
    assert cur.state == FRESH_ESTIMATE


def test_different_currency_reference_is_ignored():
    cur = resolve_current_price(
        [_row(9872.0, 0.5)],
        [_row(12047.0, 20.0, "google", currency="JPY")], NOW)
    assert cur.state == FRESH_ESTIMATE


# ---- 16 無效價格 ----------------------------------------------------------

def test_zero_and_none_prices_are_not_eligible():
    for bad in (0, 0.0, None, -100):
        cur = resolve_current_price([_row(bad, 0.5)], [], NOW)
        assert cur.state == NO_RECENT_PRICE, bad
        assert cur.price is None


def test_zero_price_reference_is_ignored():
    cur = resolve_current_price([_row(9872.0, 0.5)],
                                [_row(0, 20.0, "google")], NOW)
    assert cur.state == FRESH_ESTIMATE


# ---- 14 timezone ----------------------------------------------------------

def test_timezone_aware_and_naive_are_both_handled():
    naive = _row(9000.0, 0.5)
    naive["observed_at"] = (NOW - timedelta(hours=0.5)).replace(
        tzinfo=None).isoformat(timespec="seconds")
    cur = resolve_current_price([naive], [], NOW)
    assert cur.state == FRESH_ESTIMATE and cur.age_hours is not None


# ---- policy 可注入 --------------------------------------------------------

def test_policy_is_injectable():
    strict = CurrentPolicy(hero_sla_hours=6.0, cta_sla_hours=6.0,
                           route_primary_sla_hours=6.0)
    cur = resolve_current_price([_row(9000.0, 10.0)], [], NOW, strict)
    assert cur.state == NO_RECENT_PRICE


# ============ export 整合(11/17/18/19/20)=================================

from farehunter.export_web import export, latest_any, latest_google  # noqa: E402
from farehunter.storage import Store                                  # noqa: E402


def _seed(db, rows):
    st = Store(str(db))
    for r in rows:
        st.conn.execute(
            "INSERT INTO observations (origin,destination,depart_date,return_date,"
            "price,currency,carriers,stops,duration,observed_at,fare_class,source,"
            "provider) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("KHH", "NRT", r["depart_date"], r["return_date"], r["price"],
             r["currency"], r["carriers"], r["stops"], 180, r["observed_at"],
             "any", r["source"], None))
    st.conn.commit()
    st.close()


def _future(days):
    """落在 export SQL 視窗內的出發日。

    視窗以 SQLite 真實 date('now') 為基準(next month 1st / now+21d / now+90d),
    +45 天偏移對全年任一天都滿足三個邊界(離最近邊界仍有約 24 天緩衝),
    因此測試結果不隨執行日期改變。"""
    from datetime import date as _date
    return (_date.today() + timedelta(days=45 + days)).isoformat()


def test_export_emits_current_block_with_all_fields(tmp_path):
    db = tmp_path / "prices.db"
    _seed(db, [_row(7761.0, 0.5, depart=_future(1), ret=_future(6))])
    data = export(str(db), str(tmp_path / "data.json"), now=NOW)
    cur = data["routes"][0]["current"]
    for key in ("state", "price", "source", "observed_at", "age_hours",
                "reference_price", "reference_observed_at",
                "conflict_percentage", "eligible_for_hero", "eligible_for_cta",
                "eligible_for_route_primary", "last_observed_price",
                "last_observed_at"):
        assert key in cur, key


def test_export_generated_at_new_but_observation_old_still_stale(tmp_path):
    """generated_at 是現在,但觀測是 5 天前 → 仍判 no_recent_price。"""
    db = tmp_path / "prices.db"
    _seed(db, [_row(9000.0, 24 * 5, depart=_future(1), ret=_future(6))])
    data = export(str(db), str(tmp_path / "data.json"), now=NOW)
    assert data["generated_at"].startswith("2026-07-27")
    assert data["routes"][0]["current"]["state"] == NO_RECENT_PRICE


def test_export_keeps_legacy_fields_for_backward_compatibility(tmp_path):
    db = tmp_path / "prices.db"
    _seed(db, [_row(7761.0, 0.5, depart=_future(1), ret=_future(6))])
    data = export(str(db), str(tmp_path / "data.json"), now=NOW)
    route = data["routes"][0]
    for key in ("latest", "stats", "monthly", "history", "insight", "fsc_latest"):
        assert key in route, key


def test_monthly_and_history_do_not_enter_current(tmp_path):
    """monthly / history 來自不同查詢,不得成為 current candidate。"""
    db = tmp_path / "prices.db"
    _seed(db, [_row(9000.0, 24 * 30, depart=_future(1), ret=_future(6))])
    data = export(str(db), str(tmp_path / "data.json"), now=NOW)
    route = data["routes"][0]
    assert route["current"]["state"] == NO_RECENT_PRICE
    assert route["current"]["price"] is None


def test_latest_any_has_no_source_priority(tmp_path):
    """latest_any 純依 observed_at,不給 google 優先權(與 SSOT 的差別)。"""
    db = tmp_path / "prices.db"
    dep, ret = _future(1), _future(6)
    _seed(db, [_row(9465.0, 90.0, "google", depart=dep, ret=ret),
               _row(10291.0, 0.3, "aviasales", depart=dep, ret=ret)])
    st = Store(str(db))
    rows = latest_any(st.conn, "KHH", "NRT")
    goog = latest_google(st.conn, "KHH", "NRT")
    st.close()
    assert len(rows) == 1 and rows[0]["source"] == "aviasales"
    assert len(goog) == 1 and goog[0]["source"] == "google"
