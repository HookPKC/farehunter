"""verify_airlines 候選新鮮度界線的測試。

注意 `_no_data`：`pick_candidates` 的主池讀 `docs/data.json`，若不隔離，
這些測試會依 repo 裡的真實看板內容而時綠時紅（曾實際發生）。
"""
import sys, json, logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from farehunter.verify_airlines import pick_candidates, CANDIDATE_MAX_AGE_DAYS
from farehunter.storage import Store


def _no_data(tmp_path):
    """空的 data.json → cheap_day 主池為空，只測 fallback 池。"""
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({"cheap_days": []}), encoding="utf-8")
    return str(p)


def _obs(store, dep, price, days_ago, carriers="", ret="2027-01-05"):
    at=(datetime.now(timezone.utc)-timedelta(days=days_ago)).isoformat(timespec="seconds")
    store.conn.execute(
        "INSERT INTO observations (origin,destination,depart_date,return_date,price,"
        "currency,carriers,stops,duration,observed_at,fare_class,source,provider)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("TPE","NRT",dep,ret,price,"TWD",carriers,0,"180",at,"any","google","searchapi"))
    store.conn.commit()

def _fut(days):
    return (datetime.now(timezone.utc)+timedelta(days=days)).date().isoformat()


def test_fresh_candidate_is_picked(tmp_path):
    st=Store(str(tmp_path/"p.db")); _obs(st,_fut(60),12000.0,days_ago=1)
    got = pick_candidates(st, data_path=_no_data(tmp_path))
    assert len(got)==1 and got[0]["kind"]=="unverified"
    st.close()


def test_stale_candidate_is_skipped(tmp_path):
    """核心回歸：SearchApi 額度用盡後，候選池被凍結在數十天前的觀測。

    沒有這個界線，verify_airlines 會每天花 3 次 Scrape.do（每月 90 次＝
    免費層的 90%）去驗證早就不存在的價格。
    """
    st=Store(str(tmp_path/"p.db")); _obs(st,_fut(60),12000.0,days_ago=49)
    assert pick_candidates(st, data_path=_no_data(tmp_path))==[]
    st.close()


def test_boundary(tmp_path):
    st=Store(str(tmp_path/"p.db"))
    _obs(st,_fut(60),12000.0,days_ago=CANDIDATE_MAX_AGE_DAYS-1)
    _obs(st,_fut(61),12500.0,days_ago=CANDIDATE_MAX_AGE_DAYS+1)
    got=pick_candidates(st, limit=5, data_path=_no_data(tmp_path))
    assert [g["depart_date"] for g in got]==[_fut(60)]
    st.close()


def test_stale_pool_is_info_not_warning(tmp_path, caplog):
    """SearchApi 日曆已移除，池底那批舊觀測不會再更新——這是預期狀態。

    每天為此發一次 warning 只會讓真正的警報被忽略，所以降為 info，
    但訊息仍必須說出「有幾筆、為什麼跳過」，維運者才能自行判讀。
    """
    st=Store(str(tmp_path/"p.db")); _obs(st,_fut(60),12000.0,days_ago=49)
    with caplog.at_level(logging.INFO):
        assert pick_candidates(st, data_path=_no_data(tmp_path))==[]
    assert not any(r.levelname=="WARNING" for r in caplog.records)
    assert any("SearchApi" in r.getMessage() and "1 筆" in r.getMessage()
               for r in caplog.records)
    st.close()


def test_empty_pool_is_info_not_warning(tmp_path, caplog):
    """所有觀測都已有航空公司 → 正常狀態，不該記 warning。"""
    st=Store(str(tmp_path/"p.db")); _obs(st,_fut(60),12000.0,days_ago=1,carriers="CI")
    with caplog.at_level(logging.INFO):
        assert pick_candidates(st, data_path=_no_data(tmp_path))==[]
    assert not any(r.levelname=="WARNING" for r in caplog.records)
    st.close()
