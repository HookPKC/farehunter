"""C′ 驗證槽測試：額度鎖死、候選規則、回退一致性。全程 mock，零真實 API。"""
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import farehunter.fsc_snapshot as fsc_mod
from farehunter.fsc_snapshot import build_plans, run as fsc_run
from farehunter.serpapi_flights import (pick_verification_candidate,
                                        pick_routes_for_today, snapshot_dates,
                                        horizon_for_slot, SEARCHES_PER_DAY,
                                        SerpApiError)
from farehunter.storage import Store

ROUTES = [{"origin": "TPE", "destination": "NRT", "absolute_threshold": 7000},
          {"origin": "TPE", "destination": "KIX", "absolute_threshold": 6500},
          {"origin": "KHH", "destination": "NRT", "absolute_threshold": 8000},
          {"origin": "KHH", "destination": "KIX", "absolute_threshold": 7500}]
CFG = {"routes": ROUTES}
THRESH = {(r["origin"], r["destination"]): r["absolute_threshold"] for r in ROUTES}


def _store(tmp_path):
    return Store(str(tmp_path / "prices.db"))


def _iso(hours_ago: float) -> str:
    """observed_at 生產格式：ISO-T ＋ +00:00 後綴。"""
    return (datetime.now(timezone.utc)
            - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


def _sqlite(hours_ago: float) -> str:
    """sent_at 生產格式：SQLite datetime('now') 的空格分隔、無時區後綴。"""
    return (datetime.now(timezone.utc)
            - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _obs(store, o, d, dep, ret, price, observed_at,
         source="aviasales", fare_class="any"):
    store.conn.execute(
        "INSERT INTO observations (origin,destination,depart_date,return_date,"
        "price,currency,carriers,stops,duration,observed_at,fare_class,source,"
        "provider) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (o, d, dep, ret, price, "TWD", "CI", 0, 180, observed_at,
         fare_class, source, "test"))
    store.conn.commit()


def _alert(store, o, d, dep, price, reason, sent_at):
    store.conn.execute(
        "INSERT INTO alerts (origin,destination,depart_date,price,reason,"
        "sent_at) VALUES (?,?,?,?,?,?)", (o, d, dep, price, reason, sent_at))
    store.conn.commit()


def _future(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def _seed_candidate(store, o="TPE", d="NRT", dep=None, ret=None,
                    price=6100.0, reason="new_low", sent_hours_ago=2.0):
    """播一組完整可用的候選：警報＋能配對回程日的觀測。"""
    dep = dep or _future(45)
    ret = ret or _future(50)
    _obs(store, o, d, dep, ret, price, _iso(sent_hours_ago + 0.01))
    _alert(store, o, d, dep, price, reason, _sqlite(sent_hours_ago))
    return dep, ret


# ---- 1. 有候選 → 2 輪替＋1 驗證 ---------------------------------------------

def test_candidate_yields_two_rotation_plus_one_verify(tmp_path):
    store = _store(tmp_path)
    dep, ret = _seed_candidate(store)
    plans = build_plans(CFG, store, date(2026, 7, 15))
    kinds = [p["kind"] for p in plans]
    assert kinds == ["rotation", "rotation", "verify"]
    v = plans[-1]
    assert (v["origin"], v["destination"]) == ("TPE", "NRT")
    assert v["depart_date"] == dep and v["return_date"] == ret


# ---- 2. 無候選 → 完整回退舊 3 輪替 ------------------------------------------

def test_no_candidate_falls_back_to_legacy_three_rotation(tmp_path):
    store = _store(tmp_path)  # alerts 為空
    today = date(2026, 7, 15)
    plans = build_plans(CFG, store, today)
    assert len(plans) == 3 and all(p["kind"] == "rotation" for p in plans)
    legacy = []
    for slot, route in enumerate(pick_routes_for_today(ROUTES, today=today,
                                                       per_day=3)):
        weeks = horizon_for_slot(len(ROUTES), today, slot, per_day=3)
        dep, ret = snapshot_dates(today, horizon_weeks=weeks)
        legacy.append((route["origin"], route["destination"], dep, ret))
    got = [(p["origin"], p["destination"], p["depart_date"], p["return_date"])
           for p in plans]
    assert got == legacy


# ---- 3. 72h 內已有 google → 跳過取下一個 ------------------------------------

def test_cooldown_skips_to_next_candidate(tmp_path):
    store = _store(tmp_path)
    dep_a, _ = _seed_candidate(store, o="TPE", d="NRT", dep=_future(30),
                               ret=_future(35), price=6000, sent_hours_ago=1)
    _obs(store, "TPE", "NRT", dep_a, _future(35), 9000,
         _iso(10), source="google")           # 10h 前已驗過 → 冷卻中
    dep_b, ret_b = _seed_candidate(store, o="KHH", d="KIX", dep=_future(40),
                                   ret=_future(45), price=7000,
                                   sent_hours_ago=3)
    cand = pick_verification_candidate(store.conn, THRESH)
    assert cand is not None
    assert (cand["origin"], cand["destination"]) == ("KHH", "KIX")
    assert cand["depart_date"] == dep_b and cand["return_date"] == ret_b


# ---- 4/13. 候選 50 個 → 仍 1 驗證槽；行為層 API 呼叫恰 3 次 ------------------

def test_fifty_candidates_still_three_api_calls(tmp_path, monkeypatch):
    store = _store(tmp_path)
    for i in range(50):
        _seed_candidate(store, o=ROUTES[i % 4]["origin"],
                        d=ROUTES[i % 4]["destination"],
                        dep=_future(20 + i), ret=_future(25 + i),
                        price=5500 + i, sent_hours_ago=1 + i * 0.1)
    plans = build_plans(CFG, store, date.today())
    assert len(plans) == SEARCHES_PER_DAY == 3
    assert sum(p["kind"] == "verify" for p in plans) == 1
    store.conn.close()

    calls = []
    monkeypatch.setattr(fsc_mod, "search_google_flights",
                        lambda o, d, dep, ret: calls.append((o, d)) or
                        {"best_flights": [], "other_flights": []})
    monkeypatch.setattr(fsc_mod, "load_config", lambda p: CFG)
    monkeypatch.setattr(fsc_mod.time, "sleep", lambda s: None)
    summary = fsc_run("unused.yaml", str(tmp_path / "prices.db"))
    assert len(calls) == 3 and summary["searched"] == 3
    assert summary["verified"] == 1


# ---- 5. 既有輪替槽決定性不變 ------------------------------------------------

def test_rotation_slots_deterministic_with_candidate(tmp_path):
    store = _store(tmp_path)
    _seed_candidate(store)
    today = date(2026, 7, 15)
    plans = build_plans(CFG, store, today)
    expected = [(r["origin"], r["destination"])
                for r in pick_routes_for_today(ROUTES, today=today, per_day=2)]
    assert [(p["origin"], p["destination"]) for p in plans[:2]] == expected
    again = build_plans(CFG, store, today)
    assert [(p["origin"], p["destination"]) for p in again[:2]] == expected


# ---- 6. 配不到 return_date → 跳過 -------------------------------------------

def test_missing_return_date_match_skips_candidate(tmp_path):
    store = _store(tmp_path)
    _alert(store, "TPE", "NRT", _future(30), 6200, "new_low", _sqlite(2))
    # 無任何價格相符的觀測 → 配不到回程日
    assert pick_verification_candidate(store.conn, THRESH) is None
    plans = build_plans(CFG, store, date(2026, 7, 15))
    assert all(p["kind"] == "rotation" for p in plans) and len(plans) == 3


# ---- 7. 同日期多行程 → 價格＋時間鄰近配對正確 --------------------------------

def test_return_date_join_picks_correct_itinerary(tmp_path):
    store = _store(tmp_path)
    dep = _future(30)
    _obs(store, "TPE", "NRT", dep, _future(33), 12000, _iso(2.2))  # 3 天團
    _obs(store, "TPE", "NRT", dep, _future(35), 12500, _iso(2.1))  # 5 天團
    _obs(store, "TPE", "NRT", dep, _future(37), 12500, _iso(30))   # 舊的 7 天團同價
    _alert(store, "TPE", "NRT", dep, 12500, "absolute", _sqlite(2))
    cand = pick_verification_candidate(store.conn, THRESH)
    assert cand is not None
    # 價格匹配排除 3 天團；時間鄰近（2.1h vs 30h）選中 5 天團而非舊 7 天團
    assert cand["return_date"] == _future(35)


# ---- 8. reason 優先序：new_low > absolute > big_drop -------------------------

def test_reason_priority_over_price_ratio(tmp_path):
    store = _store(tmp_path)
    # big_drop 比值極低（0.5），new_low 比值較高（0.9）——仍應選 new_low
    _seed_candidate(store, o="TPE", d="KIX", dep=_future(30), ret=_future(35),
                    price=3250, reason="big_drop", sent_hours_ago=1)
    _seed_candidate(store, o="KHH", d="NRT", dep=_future(40), ret=_future(45),
                    price=7200, reason="new_low", sent_hours_ago=5)
    cand = pick_verification_candidate(store.conn, THRESH)
    assert cand["reason"] == "new_low"
    assert (cand["origin"], cand["destination"]) == ("KHH", "NRT")


# ---- 9. julianday 24h 窗：混合格式皆正確 -------------------------------------

def test_julianday_window_mixed_timestamp_formats(tmp_path):
    store = _store(tmp_path)
    dep = _future(30)
    _obs(store, "TPE", "NRT", dep, _future(35), 6100, _iso(23))
    _alert(store, "TPE", "NRT", dep, 6100, "new_low", _sqlite(23))   # 窗內
    assert pick_verification_candidate(store.conn, THRESH) is not None
    store.conn.execute("DELETE FROM alerts")
    _alert(store, "TPE", "NRT", dep, 6100, "new_low", _sqlite(25))   # 窗外
    assert pick_verification_candidate(store.conn, THRESH) is None
    store.conn.execute("DELETE FROM alerts")
    # sent_at 若以 ISO-T 格式寫入（防未來格式漂移），julianday 仍正確
    _alert(store, "TPE", "NRT", dep, 6100, "new_low", _iso(23))
    assert pick_verification_candidate(store.conn, THRESH) is not None


# ---- 10. 71h / 73h 冷卻邊界 --------------------------------------------------

def test_cooldown_boundary_71h_vs_73h(tmp_path):
    store = _store(tmp_path)
    dep, ret = _seed_candidate(store, dep=_future(30), ret=_future(35))
    _obs(store, "TPE", "NRT", dep, ret, 9000, _iso(71), source="google")
    assert pick_verification_candidate(store.conn, THRESH) is None   # 冷卻中
    store.conn.execute("DELETE FROM observations WHERE source='google'")
    store.conn.commit()
    _obs(store, "TPE", "NRT", dep, ret, 9000, _iso(73), source="google")
    cand = pick_verification_candidate(store.conn, THRESH)           # 已過冷卻
    assert cand is not None and cand["depart_date"] == dep


# ---- 11. SerpApiError → 計數不 crash ----------------------------------------

def test_serpapi_error_counted_not_fatal(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _seed_candidate(store)
    store.conn.close()

    def boom(o, d, dep, ret):
        raise SerpApiError("HTTP 401: Invalid API key")
    monkeypatch.setattr(fsc_mod, "search_google_flights", boom)
    monkeypatch.setattr(fsc_mod, "load_config", lambda p: CFG)
    monkeypatch.setattr(fsc_mod.time, "sleep", lambda s: None)
    summary = fsc_run("unused.yaml", str(tmp_path / "prices.db"))
    assert summary["errors"] == 3 and summary["searched"] == 3
    assert summary["recorded"] == summary["real"] == 0


# ---- 12. 全程無真實 API（結構保證）------------------------------------------
# pick_* 與 build_plans 為純 DB 函式（本檔多數測試直接呼叫，未 mock 網路即
# 通過＝零 HTTP）；所有 fsc_run 測試皆 monkeypatch search_google_flights。
