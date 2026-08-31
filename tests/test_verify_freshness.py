"""verify_airlines 候選新鮮度界線的測試。"""
import sys, logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from farehunter.verify_airlines import pick_candidates, CANDIDATE_MAX_AGE_DAYS
from farehunter.storage import Store

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
    assert len(pick_candidates(st))==1
    st.close()


def test_stale_candidate_is_skipped(tmp_path):
    """核心回歸：SearchApi 額度用盡後，候選池被凍結在數十天前的觀測。

    沒有這個界線，verify_airlines 會每天花 3 次 Scrape.do（每月 90 次＝
    免費層的 90%）去驗證早就不存在的價格。
    """
    st=Store(str(tmp_path/"p.db")); _obs(st,_fut(60),12000.0,days_ago=49)
    assert pick_candidates(st)==[]
    st.close()


def test_boundary(tmp_path):
    st=Store(str(tmp_path/"p.db"))
    _obs(st,_fut(60),12000.0,days_ago=CANDIDATE_MAX_AGE_DAYS-1)
    _obs(st,_fut(61),12500.0,days_ago=CANDIDATE_MAX_AGE_DAYS+1)
    got=pick_candidates(st, limit=5)
    assert [g["depart_date"] for g in got]==[_fut(60)]
    st.close()


def test_stale_pool_logs_a_warning(tmp_path, caplog):
    """「沒有新鮮目標」與「壞掉了」要能分辨——前者記 warning 並指出可能原因。"""
    st=Store(str(tmp_path/"p.db")); _obs(st,_fut(60),12000.0,days_ago=49)
    with caplog.at_level(logging.INFO):
        assert pick_candidates(st)==[]
    assert any("無新鮮候選" in r.getMessage() for r in caplog.records)
    assert any("是否停止供料" in r.getMessage() for r in caplog.records)
    st.close()


def test_empty_pool_is_info_not_warning(tmp_path, caplog):
    """所有觀測都已有航空公司 → 正常狀態，不該記 warning。"""
    st=Store(str(tmp_path/"p.db")); _obs(st,_fut(60),12000.0,days_ago=1,carriers="CI")
    with caplog.at_level(logging.INFO):
        assert pick_candidates(st)==[]
    assert not any(r.levelname=="WARNING" for r in caplog.records)
    st.close()
