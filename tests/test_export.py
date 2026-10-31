import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from farehunter.storage import Store
from farehunter.models import Offer
from farehunter.export_web import export


def test_export_shapes_and_future_filter(tmp_path):
    import datetime as dt
    nm = dt.date.today().replace(day=1) + dt.timedelta(days=40)   # 次月
    f1, f2 = nm.replace(day=22).isoformat(), nm.replace(day=27).isoformat()
    db = tmp_path / "t.db"
    store = Store(str(db))
    # one past departure (must be excluded from calendar) + two near-term future
    for dep, price in [("2020-01-01", 5000), (f1, 9000), (f2, 8000)]:
        store.record(Offer("TPE", "NRT", dep, None, price, "TWD", "CI", 0, "PT3H"))
    store.record_alert("TPE", "NRT", f2, 8000, "absolute")
    store.close()

    out = tmp_path / "docs" / "data.json"
    payload = export(str(db), str(out))

    assert out.exists()
    assert payload == json.loads(out.read_text(encoding="utf-8"))
    assert payload["totals"] == {"observations": 3, "routes": 1, "alerts_24h": 1}

    r = payload["routes"][0]
    assert r["stats"]["n"] == 3 and r["stats"]["min"] == 5000
    cal_dates = [x["depart_date"] for x in r["latest"]]
    assert cal_dates == [f1, f2]                           # past date excluded, near-term kept
    assert len(r["history"]) == 1                          # all observed today
    assert payload["alerts"][0]["reason"] == "absolute"


def test_alerts_drop_departed_trips_and_keep_one_row_per_trip(tmp_path):
    """2026-10-31 的看板：列著 10-29、10-30 出發的警報，同一行程也會重複出現。"""
    import datetime as dt
    now = dt.datetime(2026, 10, 30, 17, 0, tzinfo=dt.timezone.utc)   # 台灣已是 10-31 01:00
    db = tmp_path / "t.db"
    Store(str(db)).close()
    import sqlite3
    con = sqlite3.connect(db)
    rows = [  # origin, dest, depart, return, price, reason, sent_at(UTC), source, status
        ("KHH", "KIX", "2026-10-30", "2026-11-05", 7500, "absolute", "2026-10-27 04:17:49", "aviasales", "unverified"),
        ("KHH", "NRT", "2026-11-23", "2026-11-28", 12990, "big_drop", "2026-10-23 01:17:44", "aviasales", "unverified"),
        ("KHH", "NRT", "2026-11-23", "2026-11-28", 12713, "big_drop", "2026-10-30 03:17:47", "aviasales", "unverified"),
        ("KHH", "NRT", "2026-11-23", "2026-11-29", 12818, "big_drop", "2026-10-25 04:17:42", "aviasales", "unverified"),
        ("TPE", "NRT", "2026-10-31", "2026-11-04", 8000, "absolute", "2026-10-29 02:00:00", "google", "verified"),
        ("TPE", "KIX", "2026-11-05", None, 6588, "big_drop", "2026-08-01 08:00:00", None, None),
    ]
    for r in rows:
        con.execute("INSERT INTO alerts (origin, destination, depart_date, return_date, price, reason,"
                    " sent_at, price_source, price_status) VALUES (?,?,?,?,?,?,?,?,?)", r)
    con.commit()
    con.close()
    alerts = export(str(db), str(tmp_path / "data.json"), now=now)["alerts"]
    trips = [(a["origin"], a["destination"], a["depart_date"], a["return_date"]) for a in alerts]
    assert ("KHH", "KIX", "2026-10-30", "2026-11-05") not in trips        # 已出發
    assert ("TPE", "NRT", "2026-10-31", "2026-11-04") in trips            # 台灣今天出發，照列
    assert trips.count(("KHH", "NRT", "2026-11-23", "2026-11-28")) == 1   # 同一行程只留一筆
    assert ("KHH", "NRT", "2026-11-23", "2026-11-29") in trips            # 回程日不同＝不同行程
    nrt = next(a for a in alerts if a["return_date"] == "2026-11-28")
    assert nrt["price"] == 12713 and nrt["sent_at"] == "2026-10-30 03:17:47"   # 留最新的
    assert nrt["price_source"] == "aviasales" and nrt["price_status"] == "unverified"
    assert [a["sent_at"] for a in alerts] == sorted((a["sent_at"] for a in alerts), reverse=True)


def test_chips_skip_current_month_and_too_soon(tmp_path):
    """日期籤跳過當月與 21 天內的太趕日期；監控資料仍保留（僅顯示過濾）。"""
    import datetime as dt
    from farehunter.storage import Store
    from farehunter.models import Offer
    from farehunter.export_web import export
    store = Store(str(tmp_path / "t.db"))
    today = dt.date.today()
    soon = today + dt.timedelta(days=5)          # 太趕
    nextm = (today.replace(day=1) + dt.timedelta(days=32)).replace(day=10)  # 次月
    far = today + dt.timedelta(days=25)          # >21 天但可能仍當月
    for d0 in (soon, nextm, far):
        r0 = d0 + dt.timedelta(days=5)
        store.record(Offer("TPE", "NRT", d0.isoformat(), r0.isoformat(),
                           9000, "TWD", "IT", 0, "", source="google"))
    store.close()
    payload = export(str(tmp_path / "t.db"), str(tmp_path / "d.json"))
    chips = {c["depart_date"] for c in payload["routes"][0]["latest"]}
    assert soon.isoformat() not in chips                     # 太趕被排除
    assert all(c >= (today.replace(day=1) + dt.timedelta(days=32)).replace(day=1).isoformat()
               for c in chips)                                # 皆為次月(含)以後
