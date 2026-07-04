import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from farehunter.storage import Store
from farehunter.models import Offer
from farehunter.export_web import export


def test_export_shapes_and_future_filter(tmp_path):
    db = tmp_path / "t.db"
    store = Store(str(db))
    # one past departure (must be excluded from calendar) + two future
    for dep, price in [("2020-01-01", 5000), ("2099-01-01", 9000), ("2099-02-01", 8000)]:
        store.record(Offer("TPE", "NRT", dep, None, price, "TWD", "CI", 0, "PT3H"))
    store.record_alert("TPE", "NRT", "2099-02-01", 8000, "absolute")
    store.close()

    out = tmp_path / "docs" / "data.json"
    payload = export(str(db), str(out))

    assert out.exists()
    assert payload == json.loads(out.read_text(encoding="utf-8"))
    assert payload["totals"] == {"observations": 3, "routes": 1, "alerts_24h": 1}

    r = payload["routes"][0]
    assert r["stats"]["n"] == 3 and r["stats"]["min"] == 5000
    cal_dates = [x["depart_date"] for x in r["latest"]]
    assert cal_dates == ["2099-01-01", "2099-02-01"]      # past date excluded
    assert len(r["history"]) == 1                          # all observed today
    assert payload["alerts"][0]["reason"] == "absolute"


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
