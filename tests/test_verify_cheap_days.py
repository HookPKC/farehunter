"""verify_airlines 的 cheap_day 主池：把 Scrape.do 額度花在看板推薦上。

背景：SearchApi 日曆於 2026-08 移除後，原本的 carriers='' 候選池永久見底
（實測 DB 內該類列全是七月、0 筆在 14 天內），這支程式等於空轉。改指向看板的
「特別便宜」日——那是首頁最顯眼、卻唯一沒有實價背書的推薦。
"""
import sys, json, logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from farehunter.verify_airlines import pick_candidates, _gap_note, _too_old
from farehunter.storage import Store

TODAY = date(2026, 9, 1)
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
NOW_REF = "2026-09-01 12:00:00"


def _board(tmp_path, *entries):
    p = tmp_path / "data.json"
    p.write_text(json.dumps({"cheap_days": list(entries)}), encoding="utf-8")
    return str(p)


def _entry(o="TPE", d="NRT", dep="2026-11-20", ret="2026-11-25", price=9000,
           discount=35.0, notable=True, source="aviasales", hours_ago=2):
    at = (NOW - timedelta(hours=hours_ago)).isoformat()
    return {"origin": o, "destination": d, "depart_date": dep, "return_date": ret,
            "price": price, "discount_pct": discount, "notable": notable,
            "source": source, "observed_at": at}


def _pick(store, tmp_path, *entries, **kw):
    return pick_candidates(store, data_path=_board(tmp_path, *entries),
                           today=TODAY, now=NOW, now_ref=NOW_REF, **kw)


def _store(tmp_path):
    return Store(str(tmp_path / "p.db"))


def _real(store, o, d, dep, ret, price, carriers, hours_ago):
    """直接寫入 observations 以控制 observed_at。走 store.record 的話時間戳
    會被蓋成真實時鐘，冷卻測試就沒有固定的時間基準可比。"""
    at = (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    store.conn.execute(
        "INSERT INTO observations (origin,destination,depart_date,return_date,"
        "price,currency,carriers,stops,duration,observed_at,fare_class,source,"
        "provider) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (o, d, dep, ret, price, "TWD", carriers, 0, "180", at, "any",
         "google", "serpapi"))
    store.conn.commit()


def test_board_entry_becomes_the_primary_candidate(tmp_path):
    st = _store(tmp_path)
    got = _pick(st, tmp_path, _entry())
    assert len(got) == 1
    assert got[0]["kind"] == "cheap_day"
    assert (got[0]["origin"], got[0]["depart_date"]) == ("TPE", "2026-11-20")
    assert got[0]["return_date"] == "2026-11-25"   # 沒有回程日就組不出比價查詢
    st.close()


def test_biggest_discount_goes_first(tmp_path):
    """額度有限，先證實或推翻落差最大的那一筆。"""
    st = _store(tmp_path)
    got = _pick(st, tmp_path,
                _entry(d="NRT", discount=31.0),
                _entry(d="KIX", discount=48.0),
                _entry(d="CTS", discount=39.0))
    assert [c["destination"] for c in got] == ["KIX", "CTS", "NRT"]
    st.close()


def test_one_per_route_spreads_the_budget(tmp_path):
    """同航線兩個便宜日只查一次——3 次額度攤在不同航線上資訊量最大。"""
    st = _store(tmp_path)
    got = _pick(st, tmp_path,
                _entry(d="NRT", dep="2026-11-20", discount=48.0),
                _entry(d="NRT", dep="2026-11-21", discount=45.0),
                _entry(d="KIX", dep="2026-11-20", discount=40.0))
    assert [(c["destination"], c["depart_date"]) for c in got] == [
        ("NRT", "2026-11-20"), ("KIX", "2026-11-20")]
    st.close()


def test_daily_limit_is_respected(tmp_path):
    st = _store(tmp_path)
    got = _pick(st, tmp_path, *[_entry(d=x, discount=40.0 - i)
                                for i, x in enumerate(["NRT", "KIX", "CTS", "FUK"])])
    assert len(got) == 3           # VERIFICATIONS_PER_DAY
    st.close()


def test_trip_already_priced_by_fsc_is_skipped(tmp_path):
    """關鍵去重：fsc_snapshot 的 cheap_day 槽也吃同一個池子。

    scrape.do 與 serpapi 都寫 source='google'，所以 72 小時冷卻自動讓
    「誰先跑誰認領」，兩邊不會花兩次額度查同一個行程。
    """
    st = _store(tmp_path)
    _real(st, "TPE", "NRT", "2026-11-20", "2026-11-25", 11800, "BR", 5)
    assert _pick(st, tmp_path, _entry()) == []
    # 冷卻期外（>72h）就重新可查
    (tmp_path / "sub").mkdir()
    st2 = _store(tmp_path / "sub")
    _real(st2, "TPE", "NRT", "2026-11-20", "2026-11-25", 11800, "BR", 80)
    assert len(_pick(st2, tmp_path, _entry())) == 1
    st.close(); st2.close()


def test_not_notable_is_ignored(tmp_path):
    """只有夠大的落差才值得花一次額度（cheap_days_candidates 的條件）。"""
    st = _store(tmp_path)
    assert _pick(st, tmp_path, _entry(notable=False)) == []
    st.close()


def test_already_real_price_is_ignored(tmp_path):
    """看板那筆本身已是 google 實價 → 不必再驗。"""
    st = _store(tmp_path)
    assert _pick(st, tmp_path, _entry(source="google")) == []
    st.close()


def test_stale_board_is_skipped_with_a_warning(tmp_path, caplog):
    """第二道防線：cheap_days 只收 24h 內觀測，但那是 export_web 跑的時候。

    若 export 停擺，data.json 會凍結在舊推薦上——那正是我們剛花一個 commit
    修掉的浪費模式，所以這裡要擋，而且要出聲（這個是真的異常，值得 warning）。
    """
    st = _store(tmp_path)
    with caplog.at_level(logging.INFO):
        assert _pick(st, tmp_path, _entry(hours_ago=24 * 30)) == []
    assert any(r.levelname == "WARNING" and "export_web" in r.getMessage()
               for r in caplog.records)
    st.close()


def test_board_takes_precedence_over_unverified_pool(tmp_path):
    """順序就是價值判斷：看板先，補航班資訊後。"""
    st = _store(tmp_path)
    _real(st, "KHH", "KIX", "2026-11-20", "2026-11-25", 7000, "", 1)
    got = _pick(st, tmp_path, _entry(o="TPE", d="NRT"), limit=2)
    assert [c["kind"] for c in got] == ["cheap_day", "unverified"]
    st.close()


def test_full_board_leaves_no_room_for_fallback(tmp_path):
    st = _store(tmp_path)
    _real(st, "KHH", "KIX", "2026-11-20", "2026-11-25", 7000, "", 1)
    got = _pick(st, tmp_path, *[_entry(d=x, discount=40.0 - i)
                                for i, x in enumerate(["NRT", "CTS", "FUK"])])
    assert [c["kind"] for c in got] == ["cheap_day"] * 3
    st.close()


def test_never_exceeds_the_daily_budget(tmp_path):
    """真正的不變量：絕不回超過 limit 筆，否則就是超花付費額度。

    這條要獨立存在，因為「提早 return / limit-len(out) / SQL LIMIT」三道界線
    彼此互相遮蔽——突變測試證實單獨拔掉任何一道都不會有測試變紅。程式端因此
    也加了 assert（見 pick_candidates 末尾），這個測試釘住兩者。
    """
    st = _store(tmp_path)
    # 兩池同時滿載：看板 4 筆、另有 3 條航線的 carriers='' 觀測
    for i, (o, d) in enumerate([("KHH", "KIX"), ("KHH", "CTS"), ("KHH", "OKA")]):
        _real(st, o, d, "2026-11-20", "2026-11-25", 7000 + i, "", 1)
    board = [_entry(o="TPE", d=x, discount=40.0 - i)
             for i, x in enumerate(["NRT", "KIX", "FUK", "NGO"])]
    for lim in (1, 2, 3, 5):
        got = _pick(st, tmp_path, *board, limit=lim)
        assert len(got) <= lim, f"limit={lim} 卻回 {len(got)} 筆"
    st.close()


def test_past_departure_is_not_verified(tmp_path):
    """_valid_horizon：只查明天以後的出發日。"""
    st = _store(tmp_path)
    assert _pick(st, tmp_path, _entry(dep="2026-08-20", ret="2026-08-25")) == []
    st.close()


# ---- 落差說明：這支程式的產出重點 ---------------------------------------

def test_gap_note_reports_signed_difference():
    """使用者問的是「看板還準嗎」，答案就是這個百分比。"""
    assert "+31.1%" in _gap_note({"price": 9000, "kind": "cheap_day"}, 11800)
    assert "-5.6%" in _gap_note({"price": 9000, "kind": "cheap_day"}, 8500)
    assert "看板估價" in _gap_note({"price": 9000, "kind": "cheap_day"}, 8500)
    assert "原觀測" in _gap_note({"price": 9000, "kind": "unverified"}, 8500)


def test_gap_note_never_crashes_on_bad_estimate():
    """data.json 的 price 可能是 None——報告不能因此炸掉整輪。"""
    for bad in (None, "", "abc", 0):
        assert "無" in _gap_note({"price": bad, "kind": "cheap_day"}, 9000)


def test_too_old_treats_unparseable_as_old():
    """不猜：時間戳看不懂就當它過舊，寧可閒置也不浪費額度。"""
    assert _too_old("", 14, NOW)
    assert _too_old("not-a-date", 14, NOW)
    assert _too_old(None, 14, NOW)
    # 無時區的字串視為 UTC，不應誤判
    assert not _too_old("2026-09-01T00:00:00", 14, NOW)
    assert _too_old("2026-08-01T00:00:00", 14, NOW)
