"""FSC 6-slot 測試:3 Rotation + 最多 3 Verification(Alert/CTA/Hero),
claimed 去重、route diversity、fallback 補位、API 硬上限、exit-code 語意。
全程 mock,零真實 API。"""
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import farehunter.fsc_snapshot as fsc_mod
from farehunter.fsc_snapshot import build_plans, run as fsc_run, main as fsc_main
from farehunter.serpapi_flights import (pick_verification_candidate,
                                        alert_candidates, cta_candidates,
                                        hero_candidates, build_verification_plans,
                                        pick_routes_for_today, snapshot_dates,
                                        horizon_for_slot, SEARCHES_PER_DAY,
                                        ROTATION_PER_DAY, SerpApiError)
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
    return (datetime.now(timezone.utc)
            - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


def _sqlite(hours_ago: float) -> str:
    return (datetime.now(timezone.utc)
            - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _obs(store, o, d, dep, ret, price, observed_at,
         source="aviasales", fare_class="any", stops=0):
    store.conn.execute(
        "INSERT INTO observations (origin,destination,depart_date,return_date,"
        "price,currency,carriers,stops,duration,observed_at,fare_class,source,"
        "provider) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (o, d, dep, ret, price, "TWD", "CI", stops, 180, observed_at,
         fare_class, source, "test"))
    store.conn.commit()


def _alert(store, o, d, dep, price, reason, sent_at):
    store.conn.execute(
        "INSERT INTO alerts (origin,destination,depart_date,price,reason,"
        "sent_at) VALUES (?,?,?,?,?,?)", (o, d, dep, price, reason, sent_at))
    store.conn.commit()


def _future(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def _seed_alert(store, o="TPE", d="NRT", dep=None, ret=None,
                price=6100.0, reason="new_low", sent_hours_ago=2.0):
    """一組完整可用的 alert 候選:警報 + 能配對回程日的觀測。"""
    dep = dep or _future(45)
    ret = ret or _future(50)
    _obs(store, o, d, dep, ret, price, _iso(sent_hours_ago + 0.01))
    _alert(store, o, d, dep, price, reason, _sqlite(sent_hours_ago))
    return dep, ret


def _hero_window_dep(offset_days=40):
    """落在 Hero 視窗(次月1日 ~ now+90d、且 >= now+21d)的出發日。"""
    d = date.today() + timedelta(days=offset_days)
    # 確保 >= 次月一日
    nm = (date.today().replace(day=1) + timedelta(days=32)).replace(day=1)
    if d < nm:
        d = nm + timedelta(days=7)
    return d.isoformat()


def _ranked_file(tmp_path, entries):
    """寫一個最小 ranked.json;entries: list of (o,d,dep,ret,price,observed_at)。"""
    routes = [{"origin": o, "destination": d,
               "best_option": {"depart_date": dep, "return_date": ret,
                               "price": p, "observed_at": obs, "source": "google"}}
              for (o, d, dep, ret, p, obs) in entries]
    path = tmp_path / "ranked.json"
    path.write_text(json.dumps({"routes": routes}, ensure_ascii=False),
                    encoding="utf-8")
    return str(path)


def _no_ranked(tmp_path):
    return str(tmp_path / "does_not_exist.json")


# ============ Rotation 基礎 =================================================

# 3. 三個 Rotation 固定保留
def test_three_rotation_slots_always_present(tmp_path):
    store = _store(tmp_path)
    plans = build_plans(CFG, store, date(2026, 7, 15), ranked_path=_no_ranked(tmp_path))
    rot = [p for p in plans if p["kind"] == "rotation"]
    assert len(rot) == 3


# 既有意圖轉譯:無候選時三輪替與 legacy 完全一致
def test_no_candidate_rotation_matches_legacy(tmp_path):
    store = _store(tmp_path)
    today = date(2026, 7, 15)
    plans = build_plans(CFG, store, today, ranked_path=_no_ranked(tmp_path))
    assert all(p["kind"] == "rotation" for p in plans) and len(plans) == 3
    legacy = []
    for slot, route in enumerate(pick_routes_for_today(ROUTES, today=today,
                                                       per_day=ROTATION_PER_DAY)):
        weeks = horizon_for_slot(len(ROUTES), today, slot, per_day=ROTATION_PER_DAY)
        dep, ret = snapshot_dates(today, horizon_weeks=weeks)
        legacy.append((route["origin"], route["destination"], dep, ret))
    got = [(p["origin"], p["destination"], p["depart_date"], p["return_date"])
           for p in plans]
    assert got == legacy


# 既有意圖轉譯:rotation 決定性不受驗證候選影響
def test_rotation_deterministic_regardless_of_verification(tmp_path):
    store = _store(tmp_path)
    _seed_alert(store)
    today = date(2026, 7, 15)
    expected = [(r["origin"], r["destination"])
                for r in pick_routes_for_today(ROUTES, today=today,
                                               per_day=ROTATION_PER_DAY)]
    plans = build_plans(CFG, store, today, ranked_path=_no_ranked(tmp_path))
    rot = [p for p in plans if p["kind"] == "rotation"]
    assert [(p["origin"], p["destination"]) for p in rot] == expected


# ============ 三決策面各自能產生候選 ========================================

# 4a. Alert 候選能建 verification plan
def test_alert_candidate_creates_verification(tmp_path):
    store = _store(tmp_path)
    _seed_alert(store, o="KHH", d="KIX", dep=_future(60), ret=_future(65))
    plans = build_verification_plans(store.conn, THRESH, ROUTES,
                                     ranked_path=_no_ranked(tmp_path))
    assert any(p["slot_kind"] == "alert" for p in plans)


# 4b/3(裁定). CTA 從 ranked.json best_option 正確取得
def test_cta_candidate_from_ranked_best(tmp_path):
    store = _store(tmp_path)
    rp = _ranked_file(tmp_path, [
        ("KHH", "FUK", _future(40), _future(45), 12000, _iso(300))])
    cands = cta_candidates(rp)
    assert len(cands) == 1
    c = cands[0]
    assert (c["origin"], c["destination"]) == ("KHH", "FUK")
    assert c["depart_date"] == _future(40) and c["return_date"] == _future(45)
    assert c["slot_kind"] == "cta"


# 4c. Hero 候選與前端權威 latest 一致
def test_hero_candidate_matches_frontend_authoritative(tmp_path):
    from farehunter.export_web import authoritative_latest, hero_from_latest
    store = _store(tmp_path)
    dep_lo = _hero_window_dep(40)
    dep_hi = _hero_window_dep(55)
    # 同航線兩格,Hero 應為價格最低者(與前端 reduce 同義)
    _obs(store, "KHH", "NRT", dep_hi, _future(60), 15000, _iso(50))
    _obs(store, "KHH", "NRT", dep_lo, _future(45), 8000, _iso(50))
    latest = authoritative_latest(store.conn, "KHH", "NRT")
    hero = hero_from_latest(latest)
    cands = hero_candidates(store.conn, [{"origin": "KHH", "destination": "NRT"}])
    assert cands and cands[0]["depart_date"] == hero["depart_date"]
    assert cands[0]["price"] == hero["price"] == 8000


# ============ ranked.json fail-soft ========================================

# 5. 檔案缺失
def test_cta_missing_file_failsoft(tmp_path):
    assert cta_candidates(_no_ranked(tmp_path)) == []


# 6a. JSON 損壞
def test_cta_corrupt_json_failsoft(tmp_path):
    p = tmp_path / "ranked.json"
    p.write_text("{ this is not valid json ", encoding="utf-8")
    assert cta_candidates(str(p)) == []


# 6b. 欄位缺失(best_option 缺 return_date / 缺 best_option)
def test_cta_missing_fields_failsoft(tmp_path):
    p = tmp_path / "ranked.json"
    p.write_text(json.dumps({"routes": [
        {"origin": "TPE", "destination": "NRT",
         "best_option": {"depart_date": _future(40), "price": 6000}},  # 缺 return_date
        {"origin": "KHH", "destination": "KIX"},                       # 缺 best_option
    ]}), encoding="utf-8")
    assert cta_candidates(str(p)) == []


# fsc 端整合:ranked.json 缺失不讓 build_plans 崩潰
def test_build_plans_survives_missing_ranked(tmp_path):
    store = _store(tmp_path)
    _seed_alert(store)
    plans = build_plans(CFG, store, date.today(), ranked_path=_no_ranked(tmp_path))
    assert len(plans) >= 3            # 至少三輪替,不崩


# ============ claimed 去重 & diversity =====================================

# 7. Alert/CTA/Hero 指向同一 trip 時只驗證一次
def test_same_trip_across_pools_verified_once(tmp_path):
    store = _store(tmp_path)
    dep, ret = _future(40), _future(45)
    _seed_alert(store, o="TPE", d="NRT", dep=dep, ret=ret, price=6100)
    rp = _ranked_file(tmp_path, [("TPE", "NRT", dep, ret, 6100, _iso(300))])
    # Hero 也指同一 trip
    _obs(store, "TPE", "NRT", dep, ret, 6100, _iso(50))
    plans = build_verification_plans(store.conn, THRESH,
                                     [{"origin": "TPE", "destination": "NRT"}],
                                     ranked_path=rp)
    trips = [(p["origin"], p["destination"], p["depart_date"], p["return_date"])
             for p in plans]
    assert trips.count(("TPE", "NRT", dep, ret)) == 1


# 8. claimed_trips 包含 return_date(同 route/date 不同 return 視為不同 trip)
def test_claimed_key_includes_return_date(tmp_path):
    store = _store(tmp_path)
    dep = _future(40)
    claimed = {("TPE", "NRT", dep, _future(45))}   # 佔用 5 天行程
    # 候選是同 depart 但 7 天行程 → 不同 trip,不應被 claimed 擋掉
    _seed_alert(store, o="TPE", d="NRT", dep=dep, ret=_future(47), price=6100)
    plans = build_verification_plans(store.conn, THRESH,
                                     [{"origin": "TPE", "destination": "NRT"}],
                                     ranked_path=_no_ranked(tmp_path),
                                     claimed_trips=claimed)
    assert any(p["return_date"] == _future(47) for p in plans)


# 9. route diversity:三驗證優先分散不同 route
def test_verification_prefers_route_diversity(tmp_path):
    store = _store(tmp_path)
    # Alert pool 有兩個 TPE-NRT(同 route),CTA/Hero 提供其他 route
    _seed_alert(store, o="TPE", d="NRT", dep=_future(40), ret=_future(45),
                price=6000, reason="new_low", sent_hours_ago=1)
    _seed_alert(store, o="TPE", d="NRT", dep=_future(50), ret=_future(55),
                price=6100, reason="new_low", sent_hours_ago=2)
    rp = _ranked_file(tmp_path, [("KHH", "KIX", _future(60), _future(65),
                                  7000, _iso(300))])
    dep_h = _hero_window_dep(42)
    _obs(store, "KHH", "FUK", dep_h, _future(47), 11000, _iso(50))
    plans = build_verification_plans(store.conn, THRESH, [
        {"origin": "KHH", "destination": "FUK"}], ranked_path=rp)
    routes_used = [(p["origin"], p["destination"]) for p in plans]
    assert len(set(routes_used)) == len(routes_used)   # 全不同 route


# 10. 候選不足 → 可少於 3 個 verification(不硬湊)
def test_insufficient_candidates_fewer_than_three(tmp_path):
    store = _store(tmp_path)
    _seed_alert(store, o="TPE", d="NRT", dep=_future(40), ret=_future(45))
    # 無 CTA、無 Hero 候選
    plans = build_verification_plans(store.conn, THRESH, [],
                                     ranked_path=_no_ranked(tmp_path))
    assert len(plans) == 1 and plans[0]["slot_kind"] == "alert"


# 11. 某 pool 缺候選時由其他 pool 補位(仍受 max_slots 限制)
def test_pool_shortfall_backfilled_by_others(tmp_path):
    store = _store(tmp_path)
    # 無 alert;CTA 兩筆不同 route → 應產生 CTA 槽(補上 alert 的空缺)
    rp = _ranked_file(tmp_path, [
        ("KHH", "FUK", _future(40), _future(45), 12000, _iso(300)),
        ("KHH", "OKA", _future(50), _future(55), 9000, _iso(200))])
    plans = build_verification_plans(store.conn, THRESH, [],
                                     ranked_path=rp, max_slots=3)
    kinds = [p["slot_kind"] for p in plans]
    assert "cta" in kinds and "alert" not in kinds


# ============ 冷卻 =========================================================

# 12a. 72h cooling 對 alert 生效
def test_cooldown_alert(tmp_path):
    store = _store(tmp_path)
    dep, ret = _seed_alert(store, o="TPE", d="NRT", dep=_future(40), ret=_future(45))
    _obs(store, "TPE", "NRT", dep, ret, 9000, _iso(10), source="google")
    plans = build_verification_plans(store.conn, THRESH, [],
                                     ranked_path=_no_ranked(tmp_path))
    assert not any(p["slot_kind"] == "alert" for p in plans)


# 12b. 72h cooling 對 CTA 生效
def test_cooldown_cta(tmp_path):
    store = _store(tmp_path)
    dep, ret = _future(40), _future(45)
    rp = _ranked_file(tmp_path, [("KHH", "FUK", dep, ret, 12000, _iso(300))])
    _obs(store, "KHH", "FUK", dep, ret, 12000, _iso(10), source="google")
    plans = build_verification_plans(store.conn, THRESH, [], ranked_path=rp)
    assert not any(p["slot_kind"] == "cta" for p in plans)


# 12c. 72h cooling 對 Hero 生效
def test_cooldown_hero(tmp_path):
    store = _store(tmp_path)
    dep = _hero_window_dep(42)
    _obs(store, "KHH", "NRT", dep, _future(47), 8000, _iso(50))
    _obs(store, "KHH", "NRT", dep, _future(47), 8000, _iso(10), source="google")
    plans = build_verification_plans(store.conn, THRESH,
                                     [{"origin": "KHH", "destination": "NRT"}],
                                     ranked_path=_no_ranked(tmp_path))
    assert not any(p["slot_kind"] == "hero" for p in plans)


# 71h/73h 邊界(沿用 C′)
def test_cooldown_boundary_71h_73h(tmp_path):
    store = _store(tmp_path)
    dep, ret = _seed_alert(store, o="TPE", d="NRT", dep=_future(40), ret=_future(45))
    _obs(store, "TPE", "NRT", dep, ret, 9000, _iso(71), source="google")
    assert pick_verification_candidate(store.conn, THRESH) is None
    store.conn.execute("DELETE FROM observations WHERE source='google'")
    store.conn.commit()
    _obs(store, "TPE", "NRT", dep, ret, 9000, _iso(73), source="google")
    assert pick_verification_candidate(store.conn, THRESH) is not None


# 混格式 julianday 24h 窗(沿用 C′)
def test_julianday_window_mixed_formats(tmp_path):
    store = _store(tmp_path)
    dep = _future(40)
    _obs(store, "TPE", "NRT", dep, _future(45), 6100, _iso(23))
    _alert(store, "TPE", "NRT", dep, 6100, "new_low", _sqlite(23))
    assert alert_candidates(store.conn, THRESH)
    store.conn.execute("DELETE FROM alerts")
    _alert(store, "TPE", "NRT", dep, 6100, "new_low", _sqlite(25))
    assert not alert_candidates(store.conn, THRESH)


# ============ Rotation vs Verification 不重複 ===============================

# 13. rotation 佔用的 trip 不會被 verification 選中
def test_rotation_trip_not_reused_by_verification(tmp_path):
    store = _store(tmp_path)
    today = date(2026, 7, 15)
    # 找出 rotation 會選的第一個 trip,做成 alert 候選
    r0 = pick_routes_for_today(ROUTES, today=today, per_day=ROTATION_PER_DAY)[0]
    w = horizon_for_slot(len(ROUTES), today, 0, per_day=ROTATION_PER_DAY)
    dep, ret = snapshot_dates(today, horizon_weeks=w)
    _obs(store, r0["origin"], r0["destination"], dep, ret, 6000, _iso(2))
    _alert(store, r0["origin"], r0["destination"], dep, 6000, "new_low", _sqlite(1))
    plans = build_plans(CFG, store, today, ranked_path=_no_ranked(tmp_path))
    verify = [p for p in plans if p["kind"] == "verify"]
    assert not any((p["origin"], p["destination"], p["depart_date"],
                    p["return_date"]) == (r0["origin"], r0["destination"], dep, ret)
                   for p in verify)


# ============ API 硬上限 ===================================================

# 14. 候選充足 → 總 plans 恰好 6
def test_full_candidates_yield_six_plans(tmp_path):
    store = _store(tmp_path)
    # alert(TPE-NRT)、CTA(KHH-FUK)、Hero(KHH-OKA)三個不同 route
    _seed_alert(store, o="TPE", d="NRT", dep=_future(40), ret=_future(45))
    rp = _ranked_file(tmp_path, [("KHH", "FUK", _future(50), _future(55),
                                  12000, _iso(300))])
    dep_h = _hero_window_dep(42)
    _obs(store, "KHH", "OKA", dep_h, _future(47), 9000, _iso(50))
    cfg = {"routes": ROUTES + [
        {"origin": "KHH", "destination": "FUK", "absolute_threshold": 12000},
        {"origin": "KHH", "destination": "OKA", "absolute_threshold": 9000}]}
    plans = build_plans(cfg, store, date(2026, 7, 15), ranked_path=rp)
    assert len(plans) == 6
    assert sum(p["kind"] == "rotation" for p in plans) == 3
    assert sum(p["kind"] == "verify" for p in plans) == 3


# 15. 候選再多,實際 API 呼叫仍 <= 6(行為層 mock 計數)
def test_many_candidates_api_calls_capped_six(tmp_path, monkeypatch):
    store = _store(tmp_path)
    for i in range(50):
        rt = ROUTES[i % 4]
        _seed_alert(store, o=rt["origin"], d=rt["destination"],
                    dep=_future(20 + i), ret=_future(25 + i),
                    price=5500 + i, sent_hours_ago=1 + i * 0.05)
    rp = _ranked_file(tmp_path, [("KHH", "FUK", _future(200), _future(205),
                                  12000, _iso(300))])
    plans = build_plans(CFG, store, date.today(), ranked_path=rp)
    assert len(plans) <= SEARCHES_PER_DAY == 6
    store.conn.close()

    calls = []
    monkeypatch.setattr(fsc_mod, "search_google_flights",
                        lambda o, d, dep, ret: calls.append((o, d)) or
                        {"best_flights": [], "other_flights": []})
    monkeypatch.setattr(fsc_mod, "load_config", lambda p: CFG)
    monkeypatch.setattr(fsc_mod.time, "sleep", lambda s: None)
    summary = fsc_run("x.yaml", str(tmp_path / "prices.db"), ranked_path=rp)
    assert len(calls) <= 6 and summary["planned"] == len(calls)


# assert 硬上限存在
def test_plans_never_exceed_daily_cap(tmp_path):
    store = _store(tmp_path)
    for i in range(20):
        rt = ROUTES[i % 4]
        _seed_alert(store, o=rt["origin"], d=rt["destination"],
                    dep=_future(30 + i), ret=_future(35 + i),
                    price=5000 + i, sent_hours_ago=1 + i * 0.1)
    plans = build_plans(CFG, store, date.today(), ranked_path=_no_ranked(tmp_path))
    assert len(plans) <= SEARCHES_PER_DAY


# ============ exit code / 可觀測性 =========================================

# 16. API 全失敗 → run 失敗(exit 1)
def test_all_api_failure_exits_nonzero(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _seed_alert(store)
    store.conn.close()

    def boom(o, d, dep, ret):
        raise SerpApiError("HTTP 401: Invalid API key")
    monkeypatch.setattr(fsc_mod, "search_google_flights", boom)
    monkeypatch.setattr(fsc_mod, "load_config", lambda p: CFG)
    monkeypatch.setattr(fsc_mod.time, "sleep", lambda s: None)
    rc = fsc_main(["x.yaml", str(tmp_path / "prices.db"), _no_ranked(tmp_path)])
    assert rc == 1


# 17. API 成功但零符合航班 → run 成功(exit 0)+ warning 計數
def test_api_ok_no_match_stays_green_with_warning(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.conn.close()
    monkeypatch.setattr(fsc_mod, "search_google_flights",
                        lambda o, d, dep, ret: {"best_flights": [],
                                                "other_flights": []})
    monkeypatch.setattr(fsc_mod, "load_config", lambda p: CFG)
    monkeypatch.setattr(fsc_mod.time, "sleep", lambda s: None)
    summary = fsc_run("x.yaml", str(tmp_path / "prices.db"),
                      ranked_path=_no_ranked(tmp_path))
    assert summary["api_errors"] == 0
    assert summary["api_ok"] == summary["planned"] == 3
    assert summary["api_ok_no_match"] == 3
    rc = fsc_main(["x.yaml", str(tmp_path / "prices.db"), _no_ranked(tmp_path)])
    assert rc == 0


# SerpApiError 部分失敗計入 errors 但不 crash(沿用 C′ 意圖)
def test_serpapi_error_counted_not_fatal(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _seed_alert(store)
    store.conn.close()
    seq = {"n": 0}

    def flaky(o, d, dep, ret):
        seq["n"] += 1
        if seq["n"] == 1:
            raise SerpApiError("HTTP 429")
        return {"best_flights": [], "other_flights": []}
    monkeypatch.setattr(fsc_mod, "search_google_flights", flaky)
    monkeypatch.setattr(fsc_mod, "load_config", lambda p: CFG)
    monkeypatch.setattr(fsc_mod.time, "sleep", lambda s: None)
    summary = fsc_run("x.yaml", str(tmp_path / "prices.db"),
                      ranked_path=_no_ranked(tmp_path))
    assert summary["api_errors"] == 1
    assert summary["api_ok"] == summary["planned"] - 1


# 18. summary 計數正確(輪替/驗證/各槽別)
def test_summary_counts_accurate(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _seed_alert(store, o="TPE", d="NRT", dep=_future(40), ret=_future(45))
    rp = _ranked_file(tmp_path, [("KHH", "FUK", _future(50), _future(55),
                                  12000, _iso(300))])
    store.conn.close()
    monkeypatch.setattr(fsc_mod, "search_google_flights",
                        lambda o, d, dep, ret: {"best_flights": [],
                                                "other_flights": []})
    monkeypatch.setattr(fsc_mod, "load_config", lambda p: CFG)
    monkeypatch.setattr(fsc_mod.time, "sleep", lambda s: None)
    summary = fsc_run("x.yaml", str(tmp_path / "prices.db"), ranked_path=rp)
    assert summary["rotation"] == 3
    assert summary["slot_alert"] >= 1
    assert summary["rotation"] + summary["verify"] == summary["planned"]
    assert summary["planned"] <= 6


# ============ 19/20:零真實 API + 無回歸(結構性,由全檔 mock 保證)========
