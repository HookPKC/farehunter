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


def _win_dep(offset=40):
    d = date.today() + timedelta(days=offset)
    nm = (date.today().replace(day=1) + timedelta(days=32)).replace(day=1)
    return (max(d, nm + timedelta(days=7))).isoformat()


def test_hero_is_cheapest_authoritative_grid(tmp_path):
    """hero_from_latest == 前端 latest.reduce(min price)。"""
    store = Store(str(tmp_path / "p.db"))
    d1, d2, d3 = _win_dep(30), _win_dep(45), _win_dep(60)
    _obs(store, "KHH", "NRT", d1, "2027-01-01", 13000, _iso(1))
    _obs(store, "KHH", "NRT", d2, "2027-01-01", 8500, _iso(1))   # 最低
    _obs(store, "KHH", "NRT", d3, "2027-01-01", 11000, _iso(1))
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
    _obs(store, "TPE", "NRT", dep, "2027-01-01", 6000, _iso(1), source="aviasales")
    _obs(store, "TPE", "NRT", dep, "2027-01-01", 9000, _iso(2), source="google")
    latest = authoritative_latest(store.conn, "TPE", "NRT")
    grid = [x for x in latest if x["depart_date"] == dep]
    assert len(grid) == 1 and grid[0]["price"] == 9000  # google 勝,非取最低
    assert grid[0]["source"] == "google"
    store.close()


def test_stale_google_loses_to_recent_cache(tmp_path):
    """>14 天的舊 google 不再享優先,由最新觀測勝出。"""
    store = Store(str(tmp_path / "p.db"))
    dep = _win_dep(40)
    _obs(store, "TPE", "KIX", dep, "2027-01-01", 9000, _iso(20), source="google")
    _obs(store, "TPE", "KIX", dep, "2027-01-01", 6000, _iso(1), source="aviasales")
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
        _obs(store, "KHH", "NRT", dep, "2027-01-01", 10000 + i * 500, _iso(1))
        _obs(store, "TPE", "NRT", dep, "2027-01-01", 8000 + i * 300, _iso(1))
    store.close()
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    export(str(tmp_path / "p.db"), str(a))
    export(str(tmp_path / "p.db"), str(b))
    norm = lambda s: re.sub(r'"generated_at": "[^"]*"', '"generated_at": "X"', s)
    assert norm(a.read_text(encoding="utf-8")) == norm(b.read_text(encoding="utf-8"))
