"""cheap_days：跨出發日的相對便宜判定。

設計要點都用測試釘住，特別是「樣本不足時不下判斷」與「窗要窄」——後者是
big_drop 原本拿全航線中位數當基準時踩過的坑，不能在這裡重演。
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from farehunter.cheap_days import (find_cheap_days, WINDOW_DAYS, DROP_PCT,
                                   MIN_NEIGHBOURS, NOTIFY_PCT)

BASE = date(2026, 10, 1)
TODAY = "2026-09-01"


def _flat(n=21, price=10000.0, start=BASE):
    """n 天、價格全部一樣的資料。"""
    return {(start + timedelta(days=i)).isoformat(): price for i in range(n)}


def _find(prices, **kw):
    kw.setdefault("today", TODAY)
    return find_cheap_days(prices, "TPE", "NRT", **kw)


# ---- 基本行為 ---------------------------------------------------------------

def test_flat_prices_flag_nothing():
    assert _find(_flat()) == []


def test_one_cheap_day_is_flagged():
    p = _flat()
    target = (BASE + timedelta(days=10)).isoformat()
    p[target] = 7000.0                       # 比 10000 便宜 30%
    hits = _find(p)
    assert len(hits) == 1
    assert hits[0].depart_date == target
    assert hits[0].discount_pct == 30.0
    assert hits[0].price == 7000.0
    assert hits[0].neighbour_median == 10000.0


def test_a_day_just_above_the_threshold_is_not_flagged():
    p = _flat()
    p[(BASE + timedelta(days=10)).isoformat()] = 10000.0 * (1 - (DROP_PCT - 1) / 100)
    assert _find(p) == []


def test_results_are_sorted_by_discount():
    p = _flat(n=41)
    p[(BASE + timedelta(days=10)).isoformat()] = 7000.0    # 30%
    p[(BASE + timedelta(days=30)).isoformat()] = 5000.0    # 50%
    hits = _find(p)
    assert [h.discount_pct for h in hits] == [50.0, 30.0]


# ---- 樣本不足時不下判斷 ------------------------------------------------------

def test_too_few_neighbours_yields_no_verdict():
    """鄰近只有 3 天 → 不列出。這不是「不便宜」，是「無法判斷」。

    對照 big_drop：它要求該出發日自己累積 30 筆，實測讓 40% 的日期完全沒有
    答案。這裡的門檻低得多（鄰近 6 天，每天 1 筆即可），但仍必須有下限。
    """
    p = {(BASE + timedelta(days=i)).isoformat(): 10000.0 for i in range(4)}
    p[(BASE + timedelta(days=1)).isoformat()] = 5000.0
    assert _find(p) == []


def test_exactly_min_neighbours_is_enough():
    p = {(BASE + timedelta(days=i)).isoformat(): 10000.0
         for i in range(MIN_NEIGHBOURS + 1)}
    p[BASE.isoformat()] = 6000.0
    hits = _find(p)
    assert len(hits) == 1 and hits[0].neighbours == MIN_NEIGHBOURS


# ---- 窗要窄：不得把淡旺季混在一起比 ------------------------------------------

def test_window_does_not_reach_across_seasons():
    """核心設計：一個平常 8000 的十月日期，不得因為「比十二月旺季便宜」而上榜。

    窗是 ±10 天，所以 12 月的高價根本不在比較範圍內。這正是 big_drop 原本
    拿全航線中位數當基準的錯誤——實測 100 則 big_drop 有 100 則的價格高於
    使用者自己設定的門檻。
    """
    p = {(BASE + timedelta(days=i)).isoformat(): 8000.0 for i in range(21)}
    for i in range(60, 81):                              # 約兩個月後的旺季
        p[(BASE + timedelta(days=i)).isoformat()] = 30000.0
    assert _find(p) == [], "十月的正常價不該因為十二月很貴而被標成便宜"


def test_a_cheap_day_inside_a_peak_is_still_found():
    """反向：旺季裡面某天明顯比鄰居便宜，仍應被找出來。

    這是「春節後一週價格砍半」那類真實案例的形狀。
    """
    p = {(BASE + timedelta(days=i)).isoformat(): 30000.0 for i in range(21)}
    target = (BASE + timedelta(days=10)).isoformat()
    p[target] = 18000.0                                   # 便宜 40%
    hits = _find(p)
    assert len(hits) == 1 and hits[0].depart_date == target
    assert hits[0].discount_pct == 40.0


# ---- 過去的日期與無效價格 ----------------------------------------------------

def test_past_departure_dates_are_excluded():
    """已經飛走的日子不是建議。"""
    past = date(2026, 7, 1)
    p = {(past + timedelta(days=i)).isoformat(): 10000.0 for i in range(21)}
    p[(past + timedelta(days=10)).isoformat()] = 5000.0
    assert _find(p, today="2026-09-01") == []


def test_invalid_prices_are_ignored_entirely():
    """0 / 負數 / None / 布林不是報價：既不上榜，也不得污染中位數。"""
    p = _flat()
    p[(BASE + timedelta(days=3)).isoformat()] = 0
    p[(BASE + timedelta(days=4)).isoformat()] = -500
    p[(BASE + timedelta(days=5)).isoformat()] = None
    p[(BASE + timedelta(days=6)).isoformat()] = True
    target = (BASE + timedelta(days=10)).isoformat()
    p[target] = 7000.0
    hits = _find(p)
    assert len(hits) == 1
    assert hits[0].neighbour_median == 10000.0, "無效價不得把中位數拉低"


def test_malformed_date_key_does_not_break_the_batch():
    p = _flat()
    p["not-a-date"] = 100.0
    p[(BASE + timedelta(days=10)).isoformat()] = 7000.0
    assert len(_find(p)) == 1


# ---- 推播門檻 ---------------------------------------------------------------

def test_notable_marks_only_the_bigger_drops():
    """網站列 DROP_PCT 以上（看板不吵人），推播只取 NOTIFY_PCT 以上。"""
    p = _flat(n=41)
    p[(BASE + timedelta(days=10)).isoformat()] = 10000.0 * (1 - (DROP_PCT + 1) / 100)
    p[(BASE + timedelta(days=30)).isoformat()] = 10000.0 * (1 - (NOTIFY_PCT + 5) / 100)
    hits = _find(p)
    assert [h.notable for h in hits] == [True, False]
    assert sum(h.notable for h in hits) == 1


def test_window_and_thresholds_are_overridable():
    p = _flat()
    p[(BASE + timedelta(days=10)).isoformat()] = 9000.0     # 只便宜 10%
    assert _find(p) == []
    assert len(_find(p, drop_pct=5.0)) == 1
