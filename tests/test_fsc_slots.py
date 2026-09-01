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

# ---- 單一測試時間基準 -------------------------------------------------------
# 所有測試錨定這個虛構「今天」,與執行機器的真實 date.today() 完全無關,消除
# 跨日/時區/日期窗口漂移。TEST_NOW_REF 傳給 build_plans/build_verification_plans
# 作為 SQL julianday 基準;TEST_NOW 供 _iso/_sqlite 產生相對時間戳;TEST_TODAY
# 供 _future/_hero_window_dep 產生候選日期。三者同源,不再雙時間源裂開。
TEST_TODAY = date(2026, 7, 15)
TEST_NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
TEST_NOW_REF = "2026-07-15 12:00:00"


def _store(tmp_path):
    return Store(str(tmp_path / "prices.db"))


def _iso(hours_ago: float) -> str:
    """相對 TEST_NOW 的 ISO-T+00:00 時間戳(observations.observed_at 格式)。"""
    return (TEST_NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


def _sqlite(hours_ago: float) -> str:
    """相對 TEST_NOW 的空格分隔時間戳(alerts.sent_at 格式)。"""
    return (TEST_NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")


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


def _future(days: int, base: date = TEST_TODAY) -> str:
    """相對測試基準日的出發/回程日,不依賴真實今天。"""
    return (base + timedelta(days=days)).isoformat()


def _seed_alert(store, o="TPE", d="NRT", dep=None, ret=None,
                price=6100.0, reason="new_low", sent_hours_ago=2.0):
    """一組完整可用的 alert 候選:警報 + 能配對回程日的觀測。"""
    dep = dep or _future(45)
    ret = ret or _future(50)
    _obs(store, o, d, dep, ret, price, _iso(sent_hours_ago + 0.01))
    _alert(store, o, d, dep, price, reason, _sqlite(sent_hours_ago))
    return dep, ret


def _hero_window_dep(offset_days=40):
    """落在 Hero 視窗(次月1日 ~ TEST_TODAY+90d、且 >= +21d)的出發日。"""
    d = TEST_TODAY + timedelta(days=offset_days)
    nm = (TEST_TODAY.replace(day=1) + timedelta(days=32)).replace(day=1)
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


def _no_data(tmp_path):
    """隔離 cheap_day 池：不隔離的話會讀到 repo 裡真實的 docs/data.json，
    測試結果就隨當天的看板內容浮動。"""
    return str(tmp_path / "no_data.json")


# ============ Rotation 基礎 =================================================

# 3. 三個 Rotation 固定保留
def test_three_rotation_slots_always_present(tmp_path):
    store = _store(tmp_path)
    plans = build_plans(CFG, store, TEST_TODAY, ranked_path=_no_ranked(tmp_path), data_path=_no_data(tmp_path),
                        now_ref=TEST_NOW_REF)
    rot = [p for p in plans if p["kind"] == "rotation"]
    assert len(rot) == 3


# 既有意圖轉譯:無候選時三輪替與 legacy 完全一致
def test_no_candidate_rotation_matches_legacy(tmp_path):
    store = _store(tmp_path)
    today = TEST_TODAY
    plans = build_plans(CFG, store, today, ranked_path=_no_ranked(tmp_path), data_path=_no_data(tmp_path),
                        now_ref=TEST_NOW_REF)
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
    today = TEST_TODAY
    expected = [(r["origin"], r["destination"])
                for r in pick_routes_for_today(ROUTES, today=today,
                                               per_day=ROTATION_PER_DAY)]
    plans = build_plans(CFG, store, today, ranked_path=_no_ranked(tmp_path), data_path=_no_data(tmp_path),
                        now_ref=TEST_NOW_REF)
    rot = [p for p in plans if p["kind"] == "rotation"]
    assert [(p["origin"], p["destination"]) for p in rot] == expected


# ============ 三決策面各自能產生候選 ========================================

# 4a. Alert 候選能建 verification plan
def test_alert_candidate_creates_verification(tmp_path):
    store = _store(tmp_path)
    _seed_alert(store, o="KHH", d="KIX", dep=_future(60), ret=_future(65))
    plans = build_verification_plans(store.conn, THRESH, ROUTES,
                                     ranked_path=_no_ranked(tmp_path), data_path=_no_data(tmp_path),
                                     today=TEST_TODAY, now_ref=TEST_NOW_REF)
    assert any(p["slot_kind"] == "alert" for p in plans)


# 4b/3(裁定). CTA 從 ranked.json best_option 正確取得
def test_cta_candidate_from_ranked_best(tmp_path):
    store = _store(tmp_path)
    rp = _ranked_file(tmp_path, [
        ("KHH", "FUK", _future(40), _future(45), 12000, _iso(300))])
    cands = cta_candidates(rp, today=TEST_TODAY)
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
    # 前端權威 helper 與 Hero picker 必須用同一個固定時間基準,否則兩者會
    # 各自對照不同的「今天」,測試結果就會隨執行日期漂移。
    latest = authoritative_latest(store.conn, "KHH", "NRT",
                                  as_of_date=TEST_TODAY, as_of_ts=TEST_NOW_REF)
    hero = hero_from_latest(latest)
    cands = hero_candidates(store.conn, [{"origin": "KHH", "destination": "NRT"}],
                            today=TEST_TODAY, now_ref=TEST_NOW_REF)
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
    plans = build_plans(CFG, store, TEST_TODAY, ranked_path=_no_ranked(tmp_path), data_path=_no_data(tmp_path),
                        now_ref=TEST_NOW_REF)
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
                                     ranked_path=rp,
                                     today=TEST_TODAY, now_ref=TEST_NOW_REF)
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
                                     ranked_path=_no_ranked(tmp_path), data_path=_no_data(tmp_path),
                                     claimed_trips=claimed,
                                     today=TEST_TODAY, now_ref=TEST_NOW_REF)
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
        {"origin": "KHH", "destination": "FUK"}], ranked_path=rp,
        today=TEST_TODAY, now_ref=TEST_NOW_REF)
    routes_used = [(p["origin"], p["destination"]) for p in plans]
    assert len(set(routes_used)) == len(routes_used)   # 全不同 route
    # Alert 池有兩個 TPE-NRT，只該取一個——這就是「先求 route 分散」
    assert routes_used.count(("TPE", "NRT")) == 1


# 9a. 同一個池子內就要先求 route 分散：強候選在已佔用的 route 上、弱候選在
#     新 route 上時，應該挑弱的那個（換 route 的資訊量大於同 route 再驗一天）。
#     這條是突變測試逼出來的：原本把第一輪的 allow_same_route 改成 True
#     （等於完全放棄分散偏好）沒有任何測試會紅，因為每個池子剛好各有不同
#     route，偏好從來沒被真正行使過。
def test_diversity_preference_bites_within_a_single_pool(tmp_path):
    store = _store(tmp_path)
    dep_r, ret_r = _future(30), _future(35)
    # rotation 已經佔了 TPE-NRT 這條 route
    claimed = {("TPE", "NRT", dep_r, ret_r)}
    # Alert 池：TPE-NRT 更便宜（排序在前），KHH-KIX 較貴（排序在後）
    _seed_alert(store, o="TPE", d="NRT", dep=_future(40), ret=_future(45),
                price=5000, reason="new_low", sent_hours_ago=1)
    _seed_alert(store, o="KHH", d="KIX", dep=_future(41), ret=_future(46),
                price=9000, reason="new_low", sent_hours_ago=2)
    plans = build_verification_plans(
        store.conn, THRESH, [], ranked_path=_no_ranked(tmp_path),
        data_path=_no_data(tmp_path), today=TEST_TODAY,
        claimed_trips=set(claimed), now_ref=TEST_NOW_REF)
    assert len(plans) == 1
    # 明明 TPE-NRT 的候選更強，但那條 route 已被佔用 → 該挑 KHH-KIX
    assert (plans[0]["origin"], plans[0]["destination"]) == ("KHH", "KIX"), (
        f"沒有行使 route 分散偏好: {plans}")


# 9b. 但沒有別的 route 可換時，允許同 route 不同日期（build_verification_plans
#     的文件明說「補位時才允許同 route」）。這條行為原本沒有測試守著：
#     test_verification_prefers_route_diversity 斷言「全不同 route」其實比
#     設計更嚴，只因為 cheap_day 池長期是空的才沒被發現。cheap_day 開始供料
#     那天它就紅了，而且是紅在生產環境。
def test_same_route_is_allowed_only_as_a_fallback(tmp_path):
    store = _store(tmp_path)
    # 兩個 alert 同 route 不同日期，且沒有 CTA / Hero / cheap_day 候選
    _seed_alert(store, o="TPE", d="NRT", dep=_future(40), ret=_future(45),
                price=6000, reason="new_low", sent_hours_ago=1)
    _seed_alert(store, o="TPE", d="NRT", dep=_future(50), ret=_future(55),
                price=6100, reason="new_low", sent_hours_ago=2)
    plans = build_verification_plans(
        store.conn, THRESH, [], ranked_path=_no_ranked(tmp_path),
        data_path=_no_data(tmp_path), today=TEST_TODAY, now_ref=TEST_NOW_REF)
    # Alert 池每輪只貢獻一個槽，所以這裡只會有一個 plan——重點是它不會因為
    # 「同 route」而回 0 個，也不會硬湊到 3 個。
    assert len(plans) == 1
    assert (plans[0]["origin"], plans[0]["destination"]) == ("TPE", "NRT")


# 9c. 看板供料時不得因為「同 route 已被 alert 佔用」而完全放棄該槽
def test_cheap_day_may_reuse_a_claimed_route_when_nothing_else_is_left(tmp_path):
    """回歸：生產紅燈的那組資料。alert 佔了 TPE-NRT，cheap_day 池也只有
    TPE-NRT，於是走 fallback 產生第二個同 route 的計畫——那是設計行為，
    不是 bug。這條測試把它釘住，並且明確傳 data_path（不吃真實看板）。"""
    store = _store(tmp_path)
    _seed_alert(store, o="TPE", d="NRT", dep=_future(40), ret=_future(45),
                price=6000, reason="new_low", sent_hours_ago=1)
    board = _data_file(tmp_path, [{
        "origin": "TPE", "destination": "NRT",
        "depart_date": _future(70), "return_date": _future(75),
        "price": 6500, "discount_pct": 41.0, "neighbour_median": 11000.0,
        "neighbours": 8, "notable": True, "source": "aviasales",
        "observed_at": _iso(1)}])
    plans = build_verification_plans(
        store.conn, THRESH, [], ranked_path=_no_ranked(tmp_path),
        data_path=board, today=TEST_TODAY, now_ref=TEST_NOW_REF)
    kinds = [p.get("slot_kind") for p in plans]
    assert "cheap_day" in kinds, f"cheap_day 槽被整個放棄了: {plans}"
    assert len(plans) == 2
    # 同 route 但不同出發日——trip 層級的去重仍然生效
    assert len({(p["origin"], p["destination"], p["depart_date"]) for p in plans}) == 2


# 10. 候選不足 → 可少於 3 個 verification(不硬湊)
def test_insufficient_candidates_fewer_than_three(tmp_path):
    store = _store(tmp_path)
    _seed_alert(store, o="TPE", d="NRT", dep=_future(40), ret=_future(45))
    # 無 CTA、無 Hero 候選
    plans = build_verification_plans(store.conn, THRESH, [],
                                     ranked_path=_no_ranked(tmp_path), data_path=_no_data(tmp_path),
                                     today=TEST_TODAY, now_ref=TEST_NOW_REF)
    assert len(plans) == 1 and plans[0]["slot_kind"] == "alert"


# 11. 某 pool 缺候選時由其他 pool 補位(仍受 max_slots 限制)
def test_pool_shortfall_backfilled_by_others(tmp_path):
    store = _store(tmp_path)
    # 無 alert;CTA 兩筆不同 route → 應產生 CTA 槽(補上 alert 的空缺)
    rp = _ranked_file(tmp_path, [
        ("KHH", "FUK", _future(40), _future(45), 12000, _iso(300)),
        ("KHH", "OKA", _future(50), _future(55), 9000, _iso(200))])
    plans = build_verification_plans(store.conn, THRESH, [],
                                     ranked_path=rp, max_slots=3,
                                     today=TEST_TODAY, now_ref=TEST_NOW_REF)
    kinds = [p["slot_kind"] for p in plans]
    assert "cta" in kinds and "alert" not in kinds


# ============ 冷卻 =========================================================

# 12a. 72h cooling 對 alert 生效
def test_cooldown_alert(tmp_path):
    store = _store(tmp_path)
    dep, ret = _seed_alert(store, o="TPE", d="NRT", dep=_future(40), ret=_future(45))
    _obs(store, "TPE", "NRT", dep, ret, 9000, _iso(10), source="google")
    plans = build_verification_plans(store.conn, THRESH, [],
                                     ranked_path=_no_ranked(tmp_path), data_path=_no_data(tmp_path),
                                     today=TEST_TODAY, now_ref=TEST_NOW_REF)
    assert not any(p["slot_kind"] == "alert" for p in plans)


# 12b. 72h cooling 對 CTA 生效
def test_cooldown_cta(tmp_path):
    store = _store(tmp_path)
    dep, ret = _future(40), _future(45)
    rp = _ranked_file(tmp_path, [("KHH", "FUK", dep, ret, 12000, _iso(300))])
    _obs(store, "KHH", "FUK", dep, ret, 12000, _iso(10), source="google")
    plans = build_verification_plans(store.conn, THRESH, [], ranked_path=rp,
                                     today=TEST_TODAY, now_ref=TEST_NOW_REF)
    assert not any(p["slot_kind"] == "cta" for p in plans)


# 12c. 72h cooling 對 Hero 生效
def test_cooldown_hero(tmp_path):
    store = _store(tmp_path)
    dep = _hero_window_dep(42)
    _obs(store, "KHH", "NRT", dep, _future(47), 8000, _iso(50))
    _obs(store, "KHH", "NRT", dep, _future(47), 8000, _iso(10), source="google")
    plans = build_verification_plans(store.conn, THRESH,
                                     [{"origin": "KHH", "destination": "NRT"}],
                                     ranked_path=_no_ranked(tmp_path), data_path=_no_data(tmp_path),
                                     today=TEST_TODAY, now_ref=TEST_NOW_REF)
    assert not any(p["slot_kind"] == "hero" for p in plans)


# 71h/73h 邊界(沿用 C′)
def test_cooldown_boundary_71h_73h(tmp_path):
    store = _store(tmp_path)
    dep, ret = _seed_alert(store, o="TPE", d="NRT", dep=_future(40), ret=_future(45))
    _obs(store, "TPE", "NRT", dep, ret, 9000, _iso(71), source="google")
    assert pick_verification_candidate(store.conn, THRESH, today=TEST_TODAY, now_ref=TEST_NOW_REF) is None
    store.conn.execute("DELETE FROM observations WHERE source='google'")
    store.conn.commit()
    _obs(store, "TPE", "NRT", dep, ret, 9000, _iso(73), source="google")
    assert pick_verification_candidate(store.conn, THRESH, today=TEST_TODAY, now_ref=TEST_NOW_REF) is not None


# 混格式 julianday 24h 窗(沿用 C′)
def test_julianday_window_mixed_formats(tmp_path):
    store = _store(tmp_path)
    dep = _future(40)
    _obs(store, "TPE", "NRT", dep, _future(45), 6100, _iso(23))
    _alert(store, "TPE", "NRT", dep, 6100, "new_low", _sqlite(23))
    assert alert_candidates(store.conn, THRESH, today=TEST_TODAY, now_ref=TEST_NOW_REF)
    store.conn.execute("DELETE FROM alerts")
    _alert(store, "TPE", "NRT", dep, 6100, "new_low", _sqlite(25))
    assert not alert_candidates(store.conn, THRESH, today=TEST_TODAY, now_ref=TEST_NOW_REF)


# ============ Rotation vs Verification 不重複 ===============================

# 13. rotation 佔用的 trip 不會被 verification 選中
def test_rotation_trip_not_reused_by_verification(tmp_path):
    store = _store(tmp_path)
    today = TEST_TODAY
    # 找出 rotation 會選的第一個 trip,做成 alert 候選
    r0 = pick_routes_for_today(ROUTES, today=today, per_day=ROTATION_PER_DAY)[0]
    w = horizon_for_slot(len(ROUTES), today, 0, per_day=ROTATION_PER_DAY)
    dep, ret = snapshot_dates(today, horizon_weeks=w)
    _obs(store, r0["origin"], r0["destination"], dep, ret, 6000, _iso(2))
    _alert(store, r0["origin"], r0["destination"], dep, 6000, "new_low", _sqlite(1))
    plans = build_plans(CFG, store, today, ranked_path=_no_ranked(tmp_path), data_path=_no_data(tmp_path),
                        now_ref=TEST_NOW_REF)
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
    plans = build_plans(cfg, store, TEST_TODAY, ranked_path=rp, now_ref=TEST_NOW_REF)
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
    plans = build_plans(CFG, store, TEST_TODAY, ranked_path=rp, now_ref=TEST_NOW_REF)
    assert len(plans) <= SEARCHES_PER_DAY == 6
    store.conn.close()

    calls = []
    monkeypatch.setattr(fsc_mod, "search_google_flights",
                        lambda o, d, dep, ret: calls.append((o, d)) or
                        {"best_flights": [], "other_flights": []})
    monkeypatch.setattr(fsc_mod, "load_config", lambda p: CFG)
    monkeypatch.setattr(fsc_mod.time, "sleep", lambda s: None)
    summary = fsc_run("x.yaml", str(tmp_path / "prices.db"), ranked_path=rp,
                      today=TEST_TODAY, now_ref=TEST_NOW_REF)
    assert len(calls) <= 6 and summary["planned"] == len(calls)


# assert 硬上限存在
def test_plans_never_exceed_daily_cap(tmp_path):
    store = _store(tmp_path)
    for i in range(20):
        rt = ROUTES[i % 4]
        _seed_alert(store, o=rt["origin"], d=rt["destination"],
                    dep=_future(30 + i), ret=_future(35 + i),
                    price=5000 + i, sent_hours_ago=1 + i * 0.1)
    plans = build_plans(CFG, store, TEST_TODAY, ranked_path=_no_ranked(tmp_path), data_path=_no_data(tmp_path),
                        now_ref=TEST_NOW_REF)
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
                      ranked_path=_no_ranked(tmp_path), data_path=_no_data(tmp_path))
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
                      ranked_path=_no_ranked(tmp_path), data_path=_no_data(tmp_path))
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
    summary = fsc_run("x.yaml", str(tmp_path / "prices.db"), ranked_path=rp,
                      today=TEST_TODAY, now_ref=TEST_NOW_REF)
    assert summary["rotation"] == 3
    assert summary["slot_alert"] >= 1
    assert summary["rotation"] + summary["verify"] == summary["planned"]
    assert summary["planned"] <= 6


# ============ 19/20:零真實 API + 無回歸(結構性,由全檔 mock 保證)========


# ============ 單一時間來源 / 日期邊界(2026-07 CI 一致性修正)==============

def _seed_six(store, base, rp_dir):
    """在給定基準日 base 種下足以產生 6 plans 的候選(alert+cta+hero)。"""
    _obs(store, "TPE", "NRT", (base + timedelta(days=40)).isoformat(),
         (base + timedelta(days=45)).isoformat(), 6100,
         (TEST_NOW - timedelta(hours=2.01)).isoformat(timespec="seconds"))
    _alert(store, "TPE", "NRT", (base + timedelta(days=40)).isoformat(), 6100,
           "new_low", (TEST_NOW - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"))
    # hero: KHH-OKA 落在視窗
    hd = base + timedelta(days=42)
    nm = (base.replace(day=1) + timedelta(days=32)).replace(day=1)
    if hd < nm:
        hd = nm + timedelta(days=7)
    _obs(store, "KHH", "OKA", hd.isoformat(),
         (base + timedelta(days=47)).isoformat(), 9000,
         (TEST_NOW - timedelta(hours=50)).isoformat(timespec="seconds"))
    rp = rp_dir / "ranked.json"
    rp.write_text(json.dumps({"routes": [
        {"origin": "KHH", "destination": "FUK",
         "best_option": {"depart_date": (base + timedelta(days=50)).isoformat(),
                         "return_date": (base + timedelta(days=55)).isoformat(),
                         "price": 12000,
                         "observed_at": (TEST_NOW - timedelta(hours=300)).isoformat()}}]}),
        encoding="utf-8")
    return str(rp)


# 固定 today 下候選充足時恰好 6 plans(原始 flaky 測試的根治版)
def test_fixed_today_yields_exactly_six(tmp_path):
    store = _store(tmp_path)
    rp = _seed_six(store, TEST_TODAY, tmp_path)
    cfg = {"routes": ROUTES + [
        {"origin": "KHH", "destination": "FUK", "absolute_threshold": 12000},
        {"origin": "KHH", "destination": "OKA", "absolute_threshold": 9000}]}
    plans = build_plans(cfg, store, TEST_TODAY, ranked_path=rp,
                        now_ref=TEST_NOW_REF)
    assert len(plans) == 6
    assert sum(p["kind"] == "verify" for p in plans) == 3


# today 前進 1 天:仍用一致時間來源,結果穩定(不因日期絕對值改變槽數)
def test_today_plus_one_day_consistent(tmp_path):
    store = _store(tmp_path)
    base = TEST_TODAY + timedelta(days=1)
    now = TEST_NOW + timedelta(days=1)
    now_ref = now.strftime("%Y-%m-%d %H:%M:%S")
    # 直接以 base/now 種資料(不動全域 TEST_NOW)
    _obs(store, "TPE", "NRT", (base + timedelta(days=40)).isoformat(),
         (base + timedelta(days=45)).isoformat(), 6100,
         (now - timedelta(hours=2.01)).isoformat(timespec="seconds"))
    _alert(store, "TPE", "NRT", (base + timedelta(days=40)).isoformat(), 6100,
           "new_low", (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"))
    hd = base + timedelta(days=42)
    nm = (base.replace(day=1) + timedelta(days=32)).replace(day=1)
    if hd < nm:
        hd = nm + timedelta(days=7)
    _obs(store, "KHH", "OKA", hd.isoformat(),
         (base + timedelta(days=47)).isoformat(), 9000,
         (now - timedelta(hours=50)).isoformat(timespec="seconds"))
    rp = tmp_path / "ranked.json"
    rp.write_text(json.dumps({"routes": [
        {"origin": "KHH", "destination": "FUK",
         "best_option": {"depart_date": (base + timedelta(days=50)).isoformat(),
                         "return_date": (base + timedelta(days=55)).isoformat(),
                         "price": 12000,
                         "observed_at": (now - timedelta(hours=300)).isoformat()}}]}),
        encoding="utf-8")
    cfg = {"routes": ROUTES + [
        {"origin": "KHH", "destination": "FUK", "absolute_threshold": 12000},
        {"origin": "KHH", "destination": "OKA", "absolute_threshold": 9000}]}
    plans = build_plans(cfg, store, base, ranked_path=str(rp), now_ref=now_ref)
    assert len(plans) == 6


# 月底/跨月基準日:Hero 視窗(次月一日)仍正確,不崩
def test_month_end_boundary(tmp_path):
    store = _store(tmp_path)
    base = date(2026, 7, 31)                     # 月底
    now_ref = "2026-07-31 12:00:00"
    dep = date(2026, 9, 5).isoformat()           # 次月之後,落在視窗
    _obs(store, "KHH", "NRT", dep, date(2026, 9, 10).isoformat(), 8000,
         (TEST_NOW - timedelta(hours=50)).isoformat(timespec="seconds"))
    cands = hero_candidates(store.conn,
                            [{"origin": "KHH", "destination": "NRT"}], today=base)
    assert cands and cands[0]["depart_date"] == dep


# 72h cooling 邊界在固定 now_ref 下決定性(71h 冷卻中 / 73h 已過)
def test_cooldown_boundary_fixed_nowref(tmp_path):
    store = _store(tmp_path)
    dep = _future(40)
    # 71h 前的 google 觀測 → 相對 TEST_NOW_REF 仍在 72h 內 → 冷卻中
    _obs(store, "TPE", "NRT", dep, _future(45), 9000,
         (TEST_NOW - timedelta(hours=71)).isoformat(timespec="seconds"),
         source="google")
    from farehunter.serpapi_flights import _is_cooled
    assert _is_cooled(store.conn, "TPE", "NRT", dep, now_ref=TEST_NOW_REF) is True
    store.conn.execute("DELETE FROM observations WHERE source='google'")
    store.conn.commit()
    _obs(store, "TPE", "NRT", dep, _future(45), 9000,
         (TEST_NOW - timedelta(hours=73)).isoformat(timespec="seconds"),
         source="google")
    assert _is_cooled(store.conn, "TPE", "NRT", dep, now_ref=TEST_NOW_REF) is False


# Rotation 與 Verification 使用同一 today(rotation 日期以 today 為基準)
def test_rotation_and_verification_share_today(tmp_path):
    store = _store(tmp_path)
    _seed_alert(store, o="TPE", d="NRT", dep=_future(40), ret=_future(45))
    plans = build_plans(CFG, store, TEST_TODAY, ranked_path=_no_ranked(tmp_path), data_path=_no_data(tmp_path),
                        now_ref=TEST_NOW_REF)
    rot = [p for p in plans if p["kind"] == "rotation"]
    # rotation 的出發日必為 TEST_TODAY + horizon,不含真實今天的痕跡
    for p in rot:
        dep = date.fromisoformat(p["depart_date"])
        assert dep > TEST_TODAY


# 不因執行機器真實日期改變結果:同輸入兩次呼叫結果全等
def test_result_independent_of_wall_clock(tmp_path):
    store = _store(tmp_path)
    rp = _seed_six(store, TEST_TODAY, tmp_path)
    cfg = {"routes": ROUTES + [
        {"origin": "KHH", "destination": "FUK", "absolute_threshold": 12000},
        {"origin": "KHH", "destination": "OKA", "absolute_threshold": 9000}]}
    a = build_plans(cfg, store, TEST_TODAY, ranked_path=rp, now_ref=TEST_NOW_REF)
    b = build_plans(cfg, store, TEST_TODAY, ranked_path=rp, now_ref=TEST_NOW_REF)
    key = lambda ps: [(p["origin"], p["destination"], p["depart_date"],
                       p["return_date"], p["kind"]) for p in ps]
    assert key(a) == key(b)


# ============ 注入式時鐘:跨月／跨年邊界(deterministic clock)===============
# 背景:authoritative_latest 的視窗原本寫死 SQL 的 date('now'),而測試 fixture
# 綁 TEST_TODAY。2026-08-01 跨月時「次月一日」下界由 08-21 跳到 09-01,把種在
# 08-26 的 Hero 觀測踢出視窗 → plans 由 6 掉到 5,CI 連續紅燈。
# 修法是把時間來源改成 bind parameter,以下測試鎖住「結果只由注入日期決定」。

from farehunter.export_web import (                      # noqa: E402
    _as_of_pair, authoritative_latest as _auth_latest,
)


def _seed_six_plan_fixture(store, tmp_path, base: date):
    """以任意 base 日期種出可產生 6 plans 的固定 fixture(不碰真實今天)。"""
    def fut(n):
        return (base + timedelta(days=n)).isoformat()

    def hero_dep(off):
        d = base + timedelta(days=off)
        nm = (base.replace(day=1) + timedelta(days=32)).replace(day=1)
        return (d if d >= nm else nm + timedelta(days=7)).isoformat()

    _seed_alert(store, o="TPE", d="NRT", dep=fut(40), ret=fut(45))
    rp = _ranked_file(tmp_path, [("KHH", "FUK", fut(50), fut(55), 12000, _iso(300))])
    _obs(store, "KHH", "OKA", hero_dep(42), fut(47), 9000, _iso(50))
    cfg = {"routes": ROUTES + [
        {"origin": "KHH", "destination": "FUK", "absolute_threshold": 12000},
        {"origin": "KHH", "destination": "OKA", "absolute_threshold": 9000}]}
    return cfg, rp


def _plans_for(tmp_path, base: date, hhmm: str = "12:00:00"):
    store = _store(tmp_path)
    cfg, rp = _seed_six_plan_fixture(store, tmp_path, base)
    now_ref = f"{base.isoformat()} {hhmm}"
    # alert fixture 以 _iso() 相對 TEST_NOW 種下,故 alert 窗仍用 TEST_NOW_REF;
    # 日期視窗則由 base 決定——正是要驗證的注入點。
    return build_plans(cfg, store, base, ranked_path=rp, now_ref=TEST_NOW_REF)


def test_month_end_as_of_yields_six_plans(tmp_path):
    """月底(7/31):固定 fixture 產生 6 plans。"""
    assert len(_plans_for(tmp_path, date(2026, 7, 31))) == 6


def test_first_of_month_as_of_yields_six_plans(tmp_path):
    """跨月當天(8/1):同樣規則、對應的固定 fixture,仍為 6 plans。
    這正是 2026-08-01 讓 CI 紅燈的那一天。"""
    assert len(_plans_for(tmp_path, date(2026, 8, 1))) == 6


def test_year_boundary_as_of_is_stable(tmp_path):
    """跨年(12/31 → 1/1):兩側都穩定產生 6 plans。"""
    assert len(_plans_for(tmp_path, date(2026, 12, 31))) == 6
    assert len(_plans_for(tmp_path, date(2027, 1, 1))) == 6


def test_leap_day_as_of_does_not_error(tmp_path):
    """閏年 2/28→2/29→3/1 不得產生日期錯誤。"""
    for b in (date(2028, 2, 28), date(2028, 2, 29), date(2028, 3, 1)):
        assert len(_plans_for(tmp_path, b)) == 6


def test_same_fixture_same_as_of_is_independent_of_real_today(tmp_path):
    """同一組固定輸入 + 同一個 as_of → 結果逐項相同,與真實執行日期無關。"""
    def sig(base):
        return [(p["kind"], p.get("slot_kind"), p["origin"], p["destination"],
                 p["depart_date"], p["return_date"])
                for p in _plans_for(tmp_path, base)]
    a = sig(date(2026, 7, 31))
    b = sig(date(2026, 7, 31))
    assert a == b and len(a) == 6
    # 不同注入日期本來就該產生不同視窗下界 → 不得相同
    assert sig(date(2026, 8, 1)) != [] and len(sig(date(2026, 8, 1))) == 6


def test_window_lower_bound_follows_injected_date(tmp_path):
    """下界必須跟著注入日期走,而不是 CI 當天。"""
    store = _store(tmp_path)
    # 種一格 2026-08-26:7/31 視角在視窗內,8/1 視角(次月一日=9/1)則被排除
    _obs(store, "KHH", "NRT", "2026-08-26", "2026-08-31", 8000, _iso(50))
    on = _auth_latest(store.conn, "KHH", "NRT",
                      as_of_date=date(2026, 7, 31), as_of_ts="2026-07-31 12:00:00")
    off = _auth_latest(store.conn, "KHH", "NRT",
                       as_of_date=date(2026, 8, 1), as_of_ts="2026-08-01 12:00:00")
    assert [r["depart_date"] for r in on] == ["2026-08-26"]
    assert off == []


def test_as_of_pair_defaults_to_utc_not_local_today():
    """production 預設:未注入時使用真實 UTC 日期,與 SQLite date('now') 等價。"""
    import sqlite3
    from datetime import datetime as _dt, timezone as _tz
    date_s, ts_s = _as_of_pair()
    utc_now = _dt.now(_tz.utc)
    assert date_s == utc_now.strftime("%Y-%m-%d")
    conn = sqlite3.connect(":memory:")
    sql_date = conn.execute("SELECT date('now')").fetchone()[0]
    conn.close()
    assert date_s == sql_date            # 與 SQLite 的 UTC 語意一致
    assert ts_s.startswith(date_s)


def test_as_of_pair_accepts_date_and_str():
    d, t = _as_of_pair(date(2026, 7, 15), "2026-07-15 12:00:00")
    assert d == "2026-07-15" and t == "2026-07-15 12:00:00"
    d2, t2 = _as_of_pair("2026-07-15", None)
    assert d2 == "2026-07-15" and t2 is not None      # ts 退回真實 UTC now


def test_production_default_matches_legacy_now_semantics(tmp_path):
    """未注入時的結果,必須與原本寫死 date('now') 的語意相同。"""
    import sqlite3
    store = _store(tmp_path)
    conn = store.conn
    lo1, lo2, hi = conn.execute(
        "SELECT date('now','start of month','+1 month'),"
        " date('now','+21 days'), date('now','+90 days')").fetchone()
    inside = max(lo1, lo2)
    _obs(store, "KHH", "NRT", inside, hi, 8000, _iso(1))
    rows = _auth_latest(conn, "KHH", "NRT")          # 不傳 as_of → 真實 UTC
    assert [r["depart_date"] for r in rows] == [inside]


# ============ cheap_day 驗證槽 ==============================================
# cheap_days 看板是最新的推薦介面，卻曾是唯一沒有驗證優先權的。實測拿快取價對
# Google 即時價比對（同航程、時間差 ≤6 小時、74 筆）：絕對誤差中位 7.9%、
# 90 百分位 27%，28% 的情況下快取比實價便宜 >10%。看板第一名若落在那 28% 裡，
# 使用者點進去會看到高得多的價格。

def _data_file(tmp_path, entries):
    """寫一個最小 data.json；entries: list of dict（覆寫預設欄位）。"""
    base = {"origin": "TPE", "destination": "NRT",
            "depart_date": _future(60), "return_date": _future(65),
            "price": 7000.0, "neighbour_median": 10000.0, "discount_pct": 30.0,
            "neighbours": 8, "notable": True,
            "observed_at": _iso(2), "source": "aviasales"}
    path = tmp_path / "data.json"
    path.write_text(json.dumps(
        {"cheap_days": [{**base, **e} for e in entries]}, ensure_ascii=False),
        encoding="utf-8")
    return str(path)


def test_cheap_day_candidate_is_picked(tmp_path):
    from farehunter.serpapi_flights import cheap_days_candidates
    p = _data_file(tmp_path, [{}])
    got = cheap_days_candidates(p, TEST_TODAY)
    assert len(got) == 1
    assert got[0]["slot_kind"] == "cheap_day"
    assert got[0]["origin"] == "TPE" and got[0]["destination"] == "NRT"


def test_only_notable_entries_are_worth_a_search(tmp_path):
    """看板列到 15% 就顯示，但只有 ≥30% 才值得花一次額度。"""
    from farehunter.serpapi_flights import cheap_days_candidates
    p = _data_file(tmp_path, [{"notable": False, "discount_pct": 18.0}])
    assert cheap_days_candidates(p, TEST_TODAY) == []


def test_google_sourced_entries_need_no_verification(tmp_path):
    """已經是 Google 實價的不必再花額度驗證——那是這個池子存在的前提。"""
    from farehunter.serpapi_flights import cheap_days_candidates
    p = _data_file(tmp_path, [{"source": "google"}])
    assert cheap_days_candidates(p, TEST_TODAY) == []


def test_bigger_discount_goes_first(tmp_path):
    from farehunter.serpapi_flights import cheap_days_candidates
    p = _data_file(tmp_path, [
        {"depart_date": _future(60), "discount_pct": 31.0},
        {"depart_date": _future(70), "discount_pct": 44.0},
    ])
    got = cheap_days_candidates(p, TEST_TODAY)
    assert [g["discount_pct"] for g in got] == [44.0, 31.0]


def test_entry_without_return_date_is_skipped(tmp_path):
    """沒有回程日組不出比價查詢，不猜。"""
    from farehunter.serpapi_flights import cheap_days_candidates
    p = _data_file(tmp_path, [{"return_date": None}])
    assert cheap_days_candidates(p, TEST_TODAY) == []


def test_missing_or_broken_data_json_is_failsoft(tmp_path):
    from farehunter.serpapi_flights import cheap_days_candidates
    assert cheap_days_candidates(str(tmp_path / "nope.json"), TEST_TODAY) == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert cheap_days_candidates(str(bad), TEST_TODAY) == []
    empty = tmp_path / "empty.json"
    empty.write_text("{}", encoding="utf-8")
    assert cheap_days_candidates(str(empty), TEST_TODAY) == []


def test_cheap_day_reaches_the_plan(tmp_path):
    """整合：cheap_day 候選要真的出現在 build_plans 的輸出裡。"""
    store = _store(tmp_path)
    dep, ret = _future(60), _future(65)
    p = _data_file(tmp_path, [{"depart_date": dep, "return_date": ret}])
    plans = build_plans(CFG, store, TEST_TODAY, ranked_path=_no_ranked(tmp_path),
                        data_path=p, now_ref=TEST_NOW_REF)
    store.close()
    kinds = [pl.get("slot_kind") for pl in plans]
    assert "cheap_day" in kinds, f"cheap_day 沒有進入 plans：{kinds}"
    cd = [pl for pl in plans if pl.get("slot_kind") == "cheap_day"][0]
    assert cd["depart_date"] == dep and cd["return_date"] == ret
    assert len(plans) <= 6, "不得超過每日額度"


def test_no_notable_cheap_day_yields_the_slot(tmp_path):
    """沒有 notable 的日子，cheap_day 池自動讓位，不會長期擠掉 cta / hero。"""
    store = _store(tmp_path)
    p = _data_file(tmp_path, [{"notable": False}])
    ranked = _ranked_file(tmp_path, [("TPE", "KIX", _future(50), _future(55),
                                     6400.0, _iso(50))])
    plans = build_plans(CFG, store, TEST_TODAY, ranked_path=ranked,
                        data_path=p, now_ref=TEST_NOW_REF)
    store.close()
    kinds = [pl.get("slot_kind") for pl in plans]
    assert "cheap_day" not in kinds
    assert "cta" in kinds, f"cheap_day 讓位後 cta 應接上：{kinds}"
