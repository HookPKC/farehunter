"""export_web 的顯示規則驗收（實價優先、月份最低、carrier 回退）。

檔名沿用歷史（原本也含 SearchApi 日曆解析測試）。SearchApi 的一次性試用額度
於 2026-08 用盡且不續費，gcal_sweep / longrange_sweep / searchapi_calendar
已移除，相關的兩個測試隨之刪除；此檔其餘測試與資料來源無關，全部保留。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from farehunter.storage import Store
from farehunter.models import Offer
from farehunter.export_web import export

def test_chip_prefers_fresh_google_price(tmp_path):
    import datetime as dt
    dep = ((dt.date.today().replace(day=1) + dt.timedelta(days=40)).replace(day=22)).isoformat()
    ret = ((dt.date.today().replace(day=1) + dt.timedelta(days=40)).replace(day=27)).isoformat()
    db = tmp_path / "t.db"
    store = Store(str(db))
    store.record(Offer("TPE", "NRT", dep, ret, 9999, "TWD",
                       "IT", 0, "190", source="aviasales"))
    store.record(Offer("TPE", "NRT", dep, ret, 9200, "TWD",
                       "", 0, "", source="google"))
    # aviasales 之後又寫入一筆（模擬每小時監控）——google 價 14 天內仍應優先
    store.record(Offer("TPE", "NRT", dep, ret, 9950, "TWD",
                       "MM", 0, "190", source="aviasales"))
    store.close()
    payload = export(str(db), str(tmp_path / "d.json"))
    chip = payload["routes"][0]["latest"][0]
    assert chip["source"] == "google"
    assert chip["price"] == 9200
    assert chip["ref_carriers"] == "MM"     # 最近一筆快取所見航空作為參考


def test_route_insight_upsert_and_export(tmp_path):
    db = tmp_path / "t.db"
    store = Store(str(db))
    store.record(Offer("TPE", "NRT", "2099-08-01", "2099-08-06", 9200, "TWD",
                       "IT", 0, "190"))
    store.record_insight("TPE", "NRT", "2099-08-01", "high", 12000, 19000)
    store.record_insight("TPE", "NRT", "2099-08-07", "low", 12000, 19000)  # 覆蓋
    store.close()
    payload = export(str(db), str(tmp_path / "d.json"))
    ins = payload["routes"][0]["insight"]
    assert ins["price_level"] == "low" and ins["depart_date"] == "2099-08-07"
    assert ins["typical_low"] == 12000


def test_ref_carriers_falls_back_to_route_common(tmp_path):
    import datetime as dt
    nm = dt.date.today().replace(day=1) + dt.timedelta(days=40)
    d_far = nm.replace(day=27).isoformat()      # 同視窗內但與目標日差 >3 天
    d_tgt = nm.replace(day=22).isoformat()
    db = tmp_path / "t.db"
    store = Store(str(db))
    # 快取只有較遠日期（>3 天差），但航線常見航空是 IT
    store.record(Offer("KHH", "CTS", d_far, (nm.replace(day=28)).isoformat(), 12000,
                       "TWD", "IT", 0, "250", source="aviasales"))
    store.record(Offer("KHH", "CTS", d_tgt, (nm.replace(day=20)).isoformat(), 10765,
                       "TWD", "", 0, "", source="google"))
    store.close()
    payload = export(str(db), str(tmp_path / "d.json"))
    chips = {c["depart_date"]: c for c in payload["routes"][0]["latest"]}
    assert chips[d_tgt]["ref_carriers"] == "IT"   # 航線常見航空退階


def test_monthly_low_picks_cheapest_per_month(tmp_path):
    db = tmp_path / "t.db"
    store = Store(str(db))
    import datetime as dt
    base = dt.date.today().replace(day=1)
    def d(month_offset, day, price, carriers="", source="aviasales"):
        m = base.month - 1 + month_offset
        y = base.year + m // 12
        dep = dt.date(y, m % 12 + 1, day)
        ret = dep + dt.timedelta(days=5)
        store.record(Offer("TPE", "NRT", dep.isoformat(), ret.isoformat(),
                           price, "TWD", carriers, 0, "", source=source))
    d(1, 10, 12000); d(1, 20, 9500, "IT")      # 次月最低 9500
    d(3, 5, 6800, "", "google"); d(3, 15, 7200) # 第3月最低 6800（google）
    store.close()
    payload = export(str(db), str(tmp_path / "d.json"))
    monthly = payload["routes"][0]["monthly"]
    prices = {m["ym"][-2:]: m["price"] for m in monthly}
    assert min(m["price"] for m in monthly) == 6800   # 全期最低
    # 每月只留一筆、取當月最低
    assert len([m for m in monthly]) == len({m["ym"] for m in monthly})
    cheapest = min(monthly, key=lambda m: m["price"])
    assert cheapest["source"] == "google"


def test_monthly_real_price_beats_cheaper_cache_same_date(tmp_path):
    """同一出發日：新鮮 google 實價應覆蓋更便宜但過時的快取價。"""
    db = tmp_path / "t.db"
    store = Store(str(db))
    import datetime as dt
    dep = (dt.date.today().replace(day=1) + dt.timedelta(days=40))
    ret = dep + dt.timedelta(days=5)
    # 快取價較低但只是快取；google 實價較高卻是現況真相
    store.record(Offer("KHH", "KIX", dep.isoformat(), ret.isoformat(),
                       7322, "TWD", "MM", 0, "", source="aviasales"))
    store.record(Offer("KHH", "KIX", dep.isoformat(), ret.isoformat(),
                       9135, "TWD", "", 0, "", source="google"))
    store.close()
    payload = export(str(db), str(tmp_path / "d.json"))
    monthly = payload["routes"][0]["monthly"]
    cell = [x for x in monthly if x["depart_date"] == dep.isoformat()][0]
    assert cell["price"] == 9135 and cell["source"] == "google"   # 實價勝出
    assert cell["return_date"] == ret.isoformat()                  # 帶回程日
