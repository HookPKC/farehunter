"""虎航官網日曆（tigerair_calendar、tigerair_fares 表、兩個巡檢的排序）的測試。零真實 API。

conftest 已經把 tigerair_calendar.fetch 換成一律失敗；這裡要測抓取結果時一律明確傳 fetcher。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from farehunter import brightdata_flights as bd                     # noqa: E402
from farehunter import chocodata_flights as cd                      # noqa: E402
from farehunter import tigerair_calendar as tc                      # noqa: E402
import farehunter.runner as runner_mod                              # noqa: E402
from farehunter.models import Offer                                 # noqa: E402
from farehunter.storage import Store                                # noqa: E402

NOW = datetime(2026, 11, 21, 6, 17, tzinfo=timezone.utc)
AT = NOW.isoformat(timespec="seconds")
ROUTES = [("KHH", "OKA"), ("TPE", "NRT")]


def _d(n):
    return (NOW.date() + timedelta(days=n)).isoformat()


def _row(o, d, n, price, up="2026-11-21 13:00:00", cur="TWD"):
    return {"origin": o, "destination": d, "pricingCurrency": cur, "pricingDate": _d(n),
            "pricingAmount": f"{price:.3f}", "updatedAt": up}


def _store(tmp_path):
    return Store(str(tmp_path / "prices.db"))


def _page(price="9,000", airline="Tigerair Taiwan", nonstop=True):
    stop = "Nonstop flight" if nonstop else "1 stop flight"
    return (f'<html><li aria-label="From {price} New Taiwan dollars round trip total. {stop} '
            f'with {airline}. Leaves Kaohsiung at 6:50 AM."></li></html>')


# ---- 解析 --------------------------------------------------------------------------

def test_parse_keeps_our_directions_and_skips_malformed_rows():
    rows = [
        _row("KHH", "OKA", 10, 999),
        _row("OKA", "KHH", 15, 1299),                     # 回程方向也要
        _row("KHH", "BKK", 10, 1999),                     # 不是我們的線
        _row("KHH", "OKA", 11, 0),                        # 沒價
        _row("KHH", "OKA", 12, 999, cur="JPY"),           # 不是台幣
        {"origin": "KHH", "destination": "OKA", "pricingDate": "bad", "pricingAmount": "1"},
        {"origin": "KHH", "destination": "OKA", "pricingDate": _d(13)},           # 缺價格
        "not a dict",
    ]
    got = tc.parse(rows, {("KHH", "OKA"), ("OKA", "KHH")})
    assert got == {("KHH", "OKA", _d(10)): (999.0, "2026-11-21 13:00:00"),
                   ("OKA", "KHH", _d(15)): (1299.0, "2026-11-21 13:00:00")}


def test_parse_keeps_the_most_recently_updated_duplicate():
    rows = [_row("KHH", "OKA", 10, 1999, up="2026-11-20 08:00:00"),
            _row("KHH", "OKA", 10, 999, up="2026-11-21 08:00:00"),
            _row("KHH", "OKA", 10, 2999, up="2026-11-19 08:00:00")]
    assert tc.parse(rows, {("KHH", "OKA")}) == {("KHH", "OKA", _d(10)): (999.0, "2026-11-21 08:00:00")}


# ---- 收集：只記變化 ----------------------------------------------------------------

def _fetcher(by_station):
    return lambda station: by_station[station]


def test_collect_writes_only_new_or_changed_prices(tmp_path):
    st = _store(tmp_path)
    data = {"KHH": [_row("KHH", "OKA", 10, 999), _row("OKA", "KHH", 15, 1299)],
            "TPE": [_row("TPE", "NRT", 20, 2999)]}
    legs, stats = tc.collect(st, NOW, ROUTES, fetcher=_fetcher(data))
    assert legs == {("KHH", "OKA", _d(10)): 999.0, ("OKA", "KHH", _d(15)): 1299.0,
                    ("TPE", "NRT", _d(20)): 2999.0}
    assert stats == {"requests": 2, "ok": 2, "legs": 3, "written": 3, "errors": 0}

    later = NOW + timedelta(hours=1)
    _, again = tc.collect(st, later, ROUTES, fetcher=_fetcher(data))
    assert again["written"] == 0                                          # 沒變就不寫

    data["KHH"][0] = _row("KHH", "OKA", 10, 1999)                         # 特賣結束
    _, changed = tc.collect(st, later + timedelta(hours=1), ROUTES, fetcher=_fetcher(data))
    assert changed["written"] == 1
    hist = st.conn.execute("""SELECT price FROM tigerair_fares WHERE origin='KHH'
                              AND destination='OKA' ORDER BY fetched_at""").fetchall()
    assert [r[0] for r in hist] == [999.0, 1999.0]
    assert st.tigerair_latest()[("KHH", "OKA", _d(10))] == 1999.0


def test_one_station_failing_keeps_the_other_and_both_failing_writes_nothing(tmp_path):
    st = _store(tmp_path)

    def half(station):
        if station == "TPE":
            raise tc.TigerairError("HTTP 503")
        return [_row("KHH", "OKA", 10, 999)]

    legs, stats = tc.collect(st, NOW, ROUTES, fetcher=half)
    assert legs == {("KHH", "OKA", _d(10)): 999.0}
    assert (stats["ok"], stats["errors"], stats["written"]) == (1, 1, 1)

    def broken(station):
        raise ValueError("bug")

    legs, stats = tc.collect(st, NOW, ROUTES, fetcher=broken)
    assert legs == {} and (stats["errors"], stats["written"]) == (2, 0)


def test_old_departures_are_pruned(tmp_path):
    st = _store(tmp_path)
    st.conn.execute("INSERT INTO tigerair_fares VALUES ('KHH','OKA',?,999,NULL,?)",
                    (_d(-tc.KEEP_PAST_DAYS - 1), AT))
    st.conn.execute("INSERT INTO tigerair_fares VALUES ('KHH','OKA',?,999,NULL,?)",
                    (_d(-tc.KEEP_PAST_DAYS), AT))
    tc.collect(st, NOW, ROUTES, fetcher=lambda s: [_row("KHH", "OKA", 10, 999)])
    left = [r[0] for r in st.conn.execute("SELECT depart_date FROM tigerair_fares ORDER BY 1")]
    assert left == [_d(-tc.KEEP_PAST_DAYS), _d(10)]


def test_official_prices_never_reach_observations(tmp_path):
    """官網價是單程未稅，不是直飛來回實價：不進價格資料、看板、警報、統計。"""
    st = _store(tmp_path)
    tc.collect(st, NOW, ROUTES, fetcher=lambda s: [_row("KHH", "OKA", 10, 999),
                                                   _row("OKA", "KHH", 15, 999)])
    assert st.conn.execute("SELECT COUNT(*) FROM tigerair_fares").fetchone()[0] == 2
    assert st.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    assert st.conn.execute("SELECT COUNT(*) FROM observations_all").fetchone()[0] == 0


# ---- 挑線索 ------------------------------------------------------------------------

def _legs(out, back, o="KHH", d="OKA"):
    """out／back：{幾天後: 單程價}。"""
    legs = {(o, d, _d(n)): p for n, p in out.items()}
    legs.update({(d, o, _d(n)): p for n, p in back.items()})
    return legs


def test_pairs_pick_the_cheapest_combination_per_departure_day():
    legs = _legs({10: 999, 11: 3000, 12: 1500}, {13: 3000, 15: 999, 16: 999, 17: 1200})
    got = tc.pairs(legs, "KHH", "OKA", earliest=_d(2), limit=3)
    # 同價時取接近 5 晚的：10 號 → 15 號（5 晚）而不是 16 號；12 號 → 16 號（4 晚）而不是 15 號（3 晚）；
    # 11 號 → 16 號（5 晚）。依加總排序：1,998、2,499、3,999
    assert got == [(_d(10), _d(15)), (_d(12), _d(16)), (_d(11), _d(16))]


def test_pairs_respect_the_stay_range_lead_time_and_cap():
    legs = _legs({1: 500, 10: 999, 20: 999, 30: 4000}, {11: 500, 21: 500, 30: 999, 35: 500})
    cap = 999 + 999 + tc.FEE_FLOOR
    got = tc.pairs(legs, "KHH", "OKA", earliest=_d(2), cap=cap)
    # 1 號太近；10 號只配得到 11 號（1 晚）或 21 號（11 晚），都不在 2–10 晚；
    # 20 號住 10 晚、30 號回：999＋999＋稅費下限剛好等於上限 → 挑；30 號：4,000＋500＋1,000 > 上限
    assert got == [(_d(20), _d(30))]
    assert tc.pairs(legs, "KHH", "OKA", earliest=_d(2), cap=cap - 1) == []      # 稅費下限有算進去


def test_hints_cover_only_routes_with_a_usable_pair():
    legs = _legs({10: 999}, {15: 999})
    got = tc.hints(legs, ROUTES, NOW, {}, limit=4)
    assert got == {("KHH", "OKA"): [(_d(10), _d(15))]}


def test_put_first_moves_existing_trips_forward_keeping_their_airline():
    q = [(_d(40), _d(45), "IT"), (_d(10), _d(15), "GK"), (_d(50), _d(55), None)]
    n = tc.put_first(q, [(_d(10), _d(15)), (_d(20), _d(25)), (_d(10), _d(15))])
    assert n == 2
    assert q == [(_d(10), _d(15), "GK"), (_d(20), _d(25), None),
                 (_d(40), _d(45), "IT"), (_d(50), _d(55), None)]


# ---- 巡檢的排序 --------------------------------------------------------------------

def test_bright_data_checks_tigerair_days_first_and_writes_only_google_prices(tmp_path):
    st = _store(tmp_path)
    asked = []
    v = bd.Verifier(st, NOW, allowance=2, clock=lambda: 0.0,
                    fetcher=lambda o, d, dep, ret: asked.append((d, dep, ret)) or _page())
    v.tiger_hints = {("TPE", "NRT"): [(_d(30), _d(35))]}
    cache = [Offer("TPE", "NRT", _d(40), _d(45), 5500.0, "TWD", "IT", 0, "100")]
    v.sweep(cache, [("TPE", "NRT")])
    assert asked == [("NRT", _d(30), _d(35)), ("NRT", _d(40), _d(45))]
    assert v.stats["tiger_hinted"] == 1
    rows = st.conn.execute("SELECT price, source FROM observations").fetchall()
    assert [tuple(r) for r in rows] == [(9000.0, "google")] * 2         # 寫的是查到的直飛實價


def test_a_tigerair_day_without_a_direct_flight_leaves_no_price(tmp_path):
    """只要直飛：線索日查不到直飛實價 → 什麼都不寫。"""
    st = _store(tmp_path)
    v = bd.Verifier(st, NOW, allowance=1, clock=lambda: 0.0,
                    fetcher=lambda o, d, dep, ret: _page(nonstop=False))
    v.tiger_hints = {("KHH", "OKA"): [(_d(30), _d(35))]}
    v.sweep([], [("KHH", "OKA")])
    assert st.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0


def test_bright_data_takes_only_per_route_tigerair_days(tmp_path):
    st = _store(tmp_path)
    asked = []
    v = bd.Verifier(st, NOW, allowance=1, clock=lambda: 0.0,
                    fetcher=lambda o, d, dep, ret: asked.append(dep) or _page())
    v.tiger_hints = {("TPE", "NRT"): [(_d(30 + i), _d(35 + i)) for i in range(10)]}
    v.sweep([], [("TPE", "NRT")])
    assert v.stats["tiger_hinted"] == tc.PER_ROUTE and asked == [_d(30)]


def test_chocodata_starts_with_tigerair_days_on_cold_routes(tmp_path):
    st = _store(tmp_path)
    asked = []
    sw = cd.ColdSweeper(st, NOW, left=1,
                        fetcher=lambda o, d, dep, ret: asked.append((d, dep)) or _page())
    sw.tiger_hints = {("KHH", "CTS"): [(_d(30), _d(35))]}
    sw.queues = lambda c, _q=sw.queues: {r: q for r, q in _q(c).items() if r == ("KHH", "CTS")}
    sw.sweep([])
    assert asked == [("CTS", _d(30))] and sw.stats["tiger_hinted"] == 1


# ---- runner ------------------------------------------------------------------------

def _run(tmp_path, monkeypatch, tiger_fetch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "defaults:\n  currency: twd\n  months_ahead: 1\n  pause_seconds: 0\n"
        "routes:\n  - origin: KHH\n    destination: OKA\n    absolute_threshold: 5000\n",
        encoding="utf-8")
    monkeypatch.setattr(runner_mod, "TravelpayoutsClient",
                        type("C", (), {"__init__": lambda s, *a, **k: None,
                                       "search_month": lambda s, *a, **k: {}}))
    far = Offer("KHH", "OKA", _d(50), _d(55), 9999.0, "TWD", "IT", 0, "100")
    monkeypatch.setattr(runner_mod, "parse_offers", lambda *a, **k: [far])
    monkeypatch.setattr(runner_mod, "notify", lambda *a, **k: ["line"])
    monkeypatch.setattr(runner_mod, "channels_configured", lambda: True)
    for name in ("CHOCODATA_API_KEY", "RAPIDAPI_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "k")
    asked = []
    monkeypatch.setattr(bd, "fetch", lambda o, d, dep, ret, **k: asked.append((dep, ret)) or _page())
    monkeypatch.setattr(tc, "fetch", tiger_fetch)
    monkeypatch.setenv("FAREHUNTER_FORCE", "1")
    summary = runner_mod.run(str(cfg), str(tmp_path / "prices.db"), now=NOW)
    return summary, asked


def test_runner_collects_and_hands_the_cheapest_tigerair_days_to_the_sweep(tmp_path, monkeypatch):
    def fake(station):
        return [_row("KHH", "OKA", 20, 999), _row("OKA", "KHH", 25, 999),
                _row("KHH", "OKA", 21, 9999), _row("OKA", "KHH", 26, 9999)] if station == "KHH" else []

    summary, asked = _run(tmp_path, monkeypatch, fake)
    assert summary["tigerair"]["written"] == 4 and summary["tigerair"]["hint_routes"] == 1
    assert asked[0] == (_d(20), _d(25))                                  # 虎航線索最先查
    assert (_d(21), _d(26)) not in asked                                 # 19,998＋稅費 > 上限，不挑
    st = Store(str(tmp_path / "prices.db"))
    assert st.conn.execute("SELECT COUNT(*) FROM observations WHERE price < 2000").fetchone()[0] == 0


def test_a_broken_tigerair_does_not_stop_the_run(tmp_path, monkeypatch):
    def boom(station):
        raise RuntimeError("bug")

    summary, asked = _run(tmp_path, monkeypatch, boom)
    assert summary["health"] is not None
    assert summary["tigerair"]["errors"] == 2 and summary["tigerair"]["hint_routes"] == 0
    assert asked                                                         # 巡檢照舊
