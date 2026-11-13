"""連假日曆（holidays.py、export_web.build_holidays、看板的 JS 版本）的測試。"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from farehunter import holidays as H                                # noqa: E402
from farehunter.export_web import build_holidays                     # noqa: E402
from farehunter.models import Offer                                  # noqa: E402
from farehunter.storage import Store                                 # noqa: E402

# 人工小日曆：2030 年（不會跟真實資料混淆），含平日放假、落在週末的節日與一個補班日
DATA = {
    "years": [2030],
    "off": {"2030-02-01": "補假", "2030-02-02": "小年夜", "2030-02-03": "春節",
            "2030-02-04": "春節", "2030-02-05": "春節",
            "2030-04-05": "清明節",
            "2030-10-10": "國慶日",
            "2030-10-25": "臺灣光復暨金門古寧頭大捷紀念日"},
    "workdays": {"2030-02-09": "補班"},
}
CAL = H.Calendar(DATA)


def test_blocks_merge_weekends_with_holidays_and_name_them():
    got = [(b.start.isoformat(), b.end.isoformat(), b.days, b.name) for b in CAL.blocks()]
    # 2030-02-01 是週五：週五補假＋週六小年夜＋週日到週二春節＝ 5 天；名稱取「春節」而不是「補假」
    assert ("2030-02-01", "2030-02-05", 5, "春節") in got
    # 2030-04-05 週五 → 4/5–4/7 三天
    assert ("2030-04-05", "2030-04-07", 3, "清明節") in got
    # 2030-10-25 週五 → 光復節（名稱縮短）
    assert ("2030-10-25", "2030-10-27", 3, "光復節") in got
    # 2030-10-10 週四：只有一天，不算連假
    assert not any(b.start == date(2030, 10, 10) for b in CAL.blocks())


def test_plain_weekends_are_not_blocks():
    assert all(b.days >= H.MIN_BLOCK_DAYS for b in CAL.blocks())
    assert not any(b.start == date(2030, 3, 2) for b in CAL.blocks())   # 一般週末


def test_make_up_workday_counts_as_work():
    assert CAL.is_off(date(2030, 2, 9)) is False          # 週六補班
    assert CAL.is_off(date(2030, 2, 10)) is True          # 週日照放
    assert CAL.leave_days("2030-02-08", "2030-02-10") == 2  # 週五＋補班週六


def test_leave_days():
    assert CAL.leave_days("2030-02-01", "2030-02-05") == 0       # 整段連假
    assert CAL.leave_days("2030-01-30", "2030-02-06") == 3       # 1/30、1/31、2/6
    assert CAL.leave_days("2030-03-04", "2030-03-08") == 5       # 一般週一到週五
    assert CAL.leave_days("2030-03-08", "2030-03-11") == 2       # 週五＋週一


def test_uncovered_years_and_bad_input_give_none():
    assert CAL.leave_days("2030-12-30", "2031-01-02") is None
    assert CAL.leave_days("2030-03-04", None) is None
    assert CAL.leave_days("2030-03-08", "2030-03-04") is None
    assert CAL.leave_days("garbage", "2030-03-04") is None
    assert CAL.is_off(date(2031, 1, 1)) is None


def test_holiday_of_picks_the_block_with_most_overlap():
    assert CAL.holiday_of("2030-01-30", "2030-02-06").name == "春節"
    assert CAL.holiday_of("2030-03-04", "2030-03-08") is None


def test_missing_data_file_gives_an_empty_calendar(monkeypatch, tmp_path):
    monkeypatch.setattr(H, "DATA_PATH", tmp_path / "nope.json")
    H.default.cache_clear()
    try:
        c = H.default()
        assert c.blocks() == [] and c.leave_days("2027-02-01", "2027-02-05") is None
    finally:
        H.default.cache_clear()


def test_bundled_data_matches_known_2026_2027_holidays():
    """抽查官方公告：2026 春節 2/14–2/22 九天、2026-10-09 彈性補假、2027 春節 2/4–2/10。"""
    c = H.default()
    got = {(b.start.isoformat(), b.end.isoformat(), b.name) for b in c.blocks()}
    assert ("2026-02-14", "2026-02-22", "春節") in got
    assert ("2026-10-09", "2026-10-11", "國慶日") in got
    assert ("2027-02-04", "2027-02-10", "春節") in got
    assert {2026, 2027} <= c.years


# ---- export ------------------------------------------------------------------------

def _db(tmp_path, offers):
    st = Store(str(tmp_path / "prices.db"))
    for o, at in offers:
        st.record(o, observed_at=at)
    conn = sqlite3.connect(tmp_path / "prices.db")
    conn.row_factory = sqlite3.Row
    return conn


def _o(dep, ret, price, source="aviasales", o="KHH", d="OKA"):
    return Offer(o, d, dep, ret, price, "TWD", "IT", 0, "", source=source)


def test_build_holidays_lists_upcoming_blocks_with_the_cheapest_trip(tmp_path):
    now = datetime(2030, 1, 10, 4, tzinfo=timezone.utc)
    at = (now - timedelta(hours=1)).isoformat()
    conn = _db(tmp_path, [
        (_o("2030-02-01", "2030-02-05", 9000), at),     # 整段春節
        (_o("2030-02-02", "2030-02-06", 8500), at),     # 重疊 4 天（≥ 3）→ 算
        (_o("2030-01-29", "2030-02-02", 7000), at),     # 只重疊 2 天（< 3）→ 不算
        (_o("2030-04-04", "2030-04-08", 6000), at),     # 清明（重疊 3 天）
    ])
    h = build_holidays(conn, [("KHH", "OKA"), ("TPE", "NRT")], now, cal=CAL)
    assert h["years"] == [2030] and "2030-02-03" in h["off"] and h["workdays"] == ["2030-02-09"]
    by = {b["name"]: b for b in h["upcoming"]}
    cny = by["春節"]["routes"]
    assert [(r["origin"], r["price"], r["depart_date"], r["leave_days"]) for r in cny] == [
        ("KHH", 8500.0, "2030-02-02", 1)]                 # TPE-NRT 沒資料就不列
    assert by["清明節"]["routes"][0]["price"] == 6000.0
    assert by["清明節"]["routes"][0]["leave_days"] == 2   # 4/4 週四、4/8 週一


def test_build_holidays_prefers_fresh_prices_over_cheaper_old_ones(tmp_path):
    now = datetime(2030, 3, 20, 4, tzinfo=timezone.utc)
    fresh = (now - timedelta(days=2)).isoformat()
    old = (now - timedelta(days=40)).isoformat()
    conn = _db(tmp_path, [
        (_o("2030-04-04", "2030-04-08", 5000), old),     # 便宜但 40 天前
        (_o("2030-04-05", "2030-04-09", 7000), fresh),
        (_o("2030-04-05", "2030-04-08", 4000, d="NRT"), old),   # NRT 只有舊價 → 照用
    ])
    h = build_holidays(conn, [("KHH", "OKA"), ("KHH", "NRT")], now, cal=CAL)
    qm = {r["destination"]: r["price"] for r in
          next(b for b in h["upcoming"] if b["name"] == "清明節")["routes"]}
    assert qm == {"OKA": 7000.0, "NRT": 4000.0}
    order = [r["destination"] for r in
             next(b for b in h["upcoming"] if b["name"] == "清明節")["routes"]]
    assert order == ["OKA", "NRT"]          # 只有舊價的 NRT 雖然便宜也排後面


def test_build_holidays_skips_past_blocks_and_handles_no_data(tmp_path):
    now = datetime(2030, 3, 1, tzinfo=timezone.utc)
    conn = _db(tmp_path, [])
    h = build_holidays(conn, [("KHH", "OKA")], now, cal=CAL)
    assert "春節" not in [b["name"] for b in h["upcoming"]]
    assert all(b["routes"] == [] for b in h["upcoming"])
    empty = build_holidays(conn, [("KHH", "OKA")], now, cal=H.Calendar({}))
    assert empty == {"years": [], "off": [], "workdays": [], "upcoming": []}


# ---- 看板 JS 版本跟 Python 逐筆一致 --------------------------------------------------

def _js_block() -> str:
    html = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    a = html.index("// ==== HOLIDAYS-JS BEGIN")
    b = html.index("// ==== HOLIDAYS-JS END ====")
    return html[a:b]


@pytest.mark.skipif(shutil.which("node") is None, reason="沒有 node")
def test_dashboard_js_matches_python(tmp_path):
    cal = H.default()
    blocks = [b.as_dict() for b in cal.blocks()]
    payload = {"years": sorted(cal.years), "off": sorted(d.isoformat() for d in cal.off),
               "workdays": sorted(d.isoformat() for d in cal.workdays), "upcoming": blocks}
    trips = []
    d = date(2026, 1, 1)
    while d < date(2027, 12, 20):
        for n in (2, 4, 5, 9):
            trips.append((d.isoformat(), (d + timedelta(days=n)).isoformat()))
        d += timedelta(days=3)
    trips += [("2027-12-28", "2028-01-03"), ("2025-12-30", "2026-01-02")]   # 跨出資料範圍
    script = _js_block() + f"""
const c = holCal({json.dumps(payload, ensure_ascii=False)});
const trips = {json.dumps(trips)};
console.log(JSON.stringify(trips.map(([a,b]) => {{
  const h = holidayOf(c, a, b); return [leaveDays(c, a, b), h ? h.name : null];
}})));
"""
    f = tmp_path / "hol.js"
    f.write_text(script, encoding="utf-8")
    out = json.loads(subprocess.run(["node", str(f)], capture_output=True, text=True,
                                    check=True).stdout)

    def py_holiday(a, b):
        best, ov = None, 0
        for blk in cal.blocks():
            o = H.overlap_days(a, b, blk)
            if o >= (blk.days + 1) // 2 and o > ov:
                best, ov = blk, o
        return best.name if best else None

    want = [[cal.leave_days(a, b), py_holiday(a, b)] for a, b in trips]
    assert out == want
    assert len(trips) > 900
