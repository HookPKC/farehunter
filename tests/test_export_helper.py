"""export_web 權威 latest helper 的重構驗收:
- authoritative_latest / hero_from_latest 與前端 reduce 規則一致
- helper 抽取後 export 輸出語意不變(逐格結構一致)
全程本地 SQLite,零真實 API。"""
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from farehunter.export_web import authoritative_latest, hero_from_latest
from farehunter.storage import Store


def _iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")


def _obs(store, o, d, dep, ret, price, observed_at, source="aviasales", stops=0):
    store.conn.execute(
        "INSERT INTO observations (origin,destination,depart_date,return_date,"
        "price,currency,carriers,stops,duration,observed_at,fare_class,source,"
        "provider) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (o, d, dep, ret, price, "TWD", "CI", stops, 180, observed_at,
         "any", source, "test"))
    store.conn.commit()


def _ret(dep):
    """出發後 5 晚回程。回程日原本寫死 2027-01-01，停留天數會跟著今天漂移；看板只列
    2–10 晚的行程後（models.MIN/MAX_STAY_NIGHTS），寫死的日期就成了定時炸彈。"""
    return (date.fromisoformat(dep) + timedelta(days=5)).isoformat()


def _win_dep(offset=40):
    d = date.today() + timedelta(days=offset)
    nm = (date.today().replace(day=1) + timedelta(days=32)).replace(day=1)
    return (max(d, nm + timedelta(days=7))).isoformat()


def test_hero_is_cheapest_authoritative_grid(tmp_path):
    """hero_from_latest == 前端 latest.reduce(min price)。"""
    store = Store(str(tmp_path / "p.db"))
    d1, d2, d3 = _win_dep(30), _win_dep(45), _win_dep(60)
    _obs(store, "KHH", "NRT", d1, _ret(d1), 13000, _iso(1))
    _obs(store, "KHH", "NRT", d2, _ret(d2), 8500, _iso(1))   # 最低
    _obs(store, "KHH", "NRT", d3, _ret(d3), 11000, _iso(1))
    latest = authoritative_latest(store.conn, "KHH", "NRT")
    hero = hero_from_latest(latest)
    assert hero["price"] == 8500 and hero["depart_date"] == d2
    # 模擬前端 reduce,結果必須相同
    reduce_hero = min(latest, key=lambda x: x["price"])
    assert reduce_hero["depart_date"] == hero["depart_date"]
    store.close()


def test_fresh_google_wins_per_date(tmp_path):
    """同 depart_date:14 天內 fresh google 無條件勝出(即使價格較高)。"""
    store = Store(str(tmp_path / "p.db"))
    dep = _win_dep(40)
    _obs(store, "TPE", "NRT", dep, _ret(dep), 6000, _iso(1), source="aviasales")
    _obs(store, "TPE", "NRT", dep, _ret(dep), 9000, _iso(2), source="google")
    latest = authoritative_latest(store.conn, "TPE", "NRT")
    grid = [x for x in latest if x["depart_date"] == dep]
    assert len(grid) == 1 and grid[0]["price"] == 9000  # google 勝,非取最低
    assert grid[0]["source"] == "google"
    store.close()


def test_stale_google_loses_to_recent_cache(tmp_path):
    """>14 天的舊 google 不再享優先,由最新觀測勝出。"""
    store = Store(str(tmp_path / "p.db"))
    dep = _win_dep(40)
    _obs(store, "TPE", "KIX", dep, _ret(dep), 9000, _iso(20), source="google")
    _obs(store, "TPE", "KIX", dep, _ret(dep), 6000, _iso(1), source="aviasales")
    grid = [x for x in authoritative_latest(store.conn, "TPE", "KIX")
            if x["depart_date"] == dep]
    assert len(grid) == 1 and grid[0]["source"] == "aviasales"
    store.close()


def test_hero_from_empty_is_none():
    assert hero_from_latest([]) is None


def test_export_bitwise_identical_after_refactor(tmp_path):
    """helper 抽取後,export 輸出(排除 generated_at 時間戳)語意不變。
    以同一 DB 連跑兩次 export,逐格比對 latest 結構穩定。"""
    import json, re
    from farehunter.export_web import export
    store = Store(str(tmp_path / "p.db"))
    for i, dep in enumerate([_win_dep(30), _win_dep(45), _win_dep(60)]):
        _obs(store, "KHH", "NRT", dep, _ret(dep), 10000 + i * 500, _iso(1))
        _obs(store, "TPE", "NRT", dep, _ret(dep), 8000 + i * 300, _iso(1))
    store.close()
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    export(str(tmp_path / "p.db"), str(a))
    export(str(tmp_path / "p.db"), str(b))
    # 兩個欄位取的是 export 當下的牆鐘：generated_at 與 health.checked_at。
    # 只正規化前者的話，兩次 export 若跨過一個秒界就會假失敗——而 monitor.yml
    # 是「測試沒過就不抓價」，這種偶發紅燈會直接漏掉一次價格監控。
    def norm(s: str) -> str:
        return re.sub(r'"(generated_at|checked_at)": "[^"]*"', r'"\1": "X"', s)
    assert norm(a.read_text(encoding="utf-8")) == norm(b.read_text(encoding="utf-8"))


def test_export_populates_cheap_days_end_to_end(tmp_path):
    """整合回歸：模組測試全綠但 export 的呼叫端壞掉，網站會靜靜地什麼都不顯示。

    實際發生過——build_cheap_days 的參數從 today 改成 now 之後，export_web 仍傳
    today=，被那裡的 try/except 吞成一則 warning 加空清單。288 個單元測試全過，
    但看板是空的。這個測試跨過那個接縫。
    """
    import json
    from datetime import date as _date
    from farehunter.export_web import export

    store = Store(str(tmp_path / "p.db"))
    base = _date.today() + timedelta(days=60)
    fresh = _iso(0)
    for i in range(21):
        dep = (base + timedelta(days=i)).isoformat()
        # 中間那天明顯比周圍便宜；其餘持平
        price = 6000 if i == 10 else 10000
        _obs(store, "TPE", "NRT", dep, (base + timedelta(days=i + 5)).isoformat(),
             price, fresh)
    store.close()

    out = tmp_path / "data.json"
    payload = export(str(tmp_path / "p.db"), str(out))
    cheap = payload["cheap_days"]
    assert cheap, "export 沒有產出 cheap_days——呼叫端可能又壞了"
    assert cheap[0]["depart_date"] == (base + timedelta(days=10)).isoformat()
    assert cheap[0]["discount_pct"] == 40.0
    assert cheap[0]["observed_at"] and cheap[0]["return_date"]
    # 寫進檔案的內容也要有（前端讀的是檔案，不是回傳值）
    assert json.loads(out.read_text(encoding="utf-8"))["cheap_days"] == cheap


def test_usage_block_reports_each_service_without_guessing(tmp_path):
    """看板的用量面板：各服務本月用量由 export 算好；讀不到的項目是 None，不塞假數字。"""
    import json
    from datetime import datetime, timezone
    from farehunter.storage import Store
    from farehunter.export_web import export
    now = datetime(2026, 11, 15, 6, 0, tzinfo=timezone.utc)
    db = tmp_path / "p.db"
    st = Store(str(db))
    st.record_api_usage("brightdata", "2026-10-31", ok=True)              # 上個月的不算
    st.record_api_usage("brightdata", "2026-11-02", ok=True)
    st.record_api_usage("brightdata", "2026-11-03", ok=False, timeout=True)
    st.record_line_send("2026-11-14T15:30:00+00:00", "broadcast", 120)    # 台灣 11/14 23:30
    st.record_line_send("2026-11-14T17:00:00+00:00", "broadcast", 115)    # 台灣 11/15 01:00（今天）
    st.record_line_send("2026-11-15T02:00:00+00:00", "owner", 114)
    st.close()
    u = export(str(db), str(tmp_path / "data.json"), now=now)["usage"]
    assert u["brightdata"] == {"used": 2, "ok": 1, "timeouts": 1, "limit": 5000, "target": 4750}
    assert u["line"] == {"broadcasts": 2, "owner_only": 1, "broadcasts_today": 1,
                         "quota_left": 114, "quota_left_at": "2026-11-15T02:00:00+00:00"}
    assert u["scrapedo"]["credits_used"] == 0 and u["serpapi"] is None     # 沒有 quota.json
    (tmp_path / "quota.json").write_text(json.dumps(
        {"total_searches_left": 205, "searches_per_month": 250, "next_reset_estimate": "2026-12-03"}))
    u = export(str(db), str(tmp_path / "data.json"), now=now)["usage"]
    assert (u["serpapi"]["left"], u["serpapi"]["limit"]) == (205, 250)


def test_usage_survives_a_database_older_than_the_send_log(tmp_path):
    """export 直接開檔、不經 Store 的 migration：還沒有 quota_left 欄的舊檔也要匯得出來。"""
    import sqlite3
    from datetime import datetime, timezone
    from farehunter.storage import Store
    from farehunter.export_web import export
    db = tmp_path / "p.db"
    Store(str(db)).close()
    con = sqlite3.connect(db)
    con.execute("DROP TABLE line_sends")
    con.execute("CREATE TABLE line_sends (sent_at TEXT NOT NULL, mode TEXT NOT NULL)")
    con.execute("INSERT INTO line_sends VALUES ('2026-11-15T01:00:00+00:00', 'broadcast')")
    con.commit(); con.close()
    u = export(str(db), str(tmp_path / "data.json"),
               now=datetime(2026, 11, 15, 6, 0, tzinfo=timezone.utc))["usage"]
    assert u["line"]["broadcasts"] == 1 and u["line"]["quota_left"] is None
    st = Store(str(db))                                                   # 開檔時補上欄位
    st.record_line_send("2026-11-15T02:00:00+00:00", "owner", 99)
    st.close()


def test_full_service_low_uses_the_latest_price_of_each_trip(tmp_path):
    """看板的「傳統航空最低」：每一趟取最新一筆再挑最便宜；舊的低價、超過 7 天的、
    住超過 10 晚的都不算。"""
    from farehunter.export_web import export
    store = Store(str(tmp_path / "p.db"))
    d1, d2, d3 = _win_dep(30), _win_dep(45), _win_dep(60)

    def full(dep, ret, price, days_ago):
        store.conn.execute(
            "INSERT INTO observations (origin,destination,depart_date,return_date,price,currency,"
            "carriers,stops,duration,observed_at,fare_class,source,provider) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("KHH", "NRT", dep, ret, price, "TWD", "CI", 0, 180, _iso(days_ago), "full", "google", "test"))
    _obs(store, "KHH", "NRT", d1, _ret(d1), 8000, _iso(1))            # 廉航的 'any' 價
    full(d1, _ret(d1), 11000, 3); full(d1, _ret(d1), 13000, 1)        # 同一趟：後來漲到 13,000
    full(d2, _ret(d2), 12500, 2)
    full(d3, _ret(d3), 9000, 20)                                      # 超過 7 天
    far = (date.fromisoformat(d2) + timedelta(days=30)).isoformat()
    full(d2, far, 9500, 1)                                            # 住 30 晚
    store.conn.commit(); store.close()
    route = export(str(tmp_path / "p.db"), str(tmp_path / "d.json"))["routes"][0]
    assert (route["fsc_latest"]["price"], route["fsc_latest"]["depart_date"]) == (12500, d2)
    chip = next(c for c in route["latest"] if c["depart_date"] == d1)
    assert chip["full_price"] == 13000
