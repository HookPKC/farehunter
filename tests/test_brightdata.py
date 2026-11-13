"""Bright Data 抓 Google Flights（ADR 0004）的測試。零真實 API：一律注入假的 fetch／session。"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from farehunter import brightdata_flights as bd                    # noqa: E402
import farehunter.runner as runner_mod                              # noqa: E402
from farehunter.models import Offer                                 # noqa: E402
from farehunter.storage import Store                                # noqa: E402

NOW = datetime(2026, 11, 15, 6, 17, tzinfo=timezone.utc)
DEP = (NOW.date() + timedelta(days=40)).isoformat()
RET = (NOW.date() + timedelta(days=45)).isoformat()


def _label(price, airline, nonstop=True, cur="New Taiwan dollars"):
    stop = "Nonstop flight" if nonstop else "1 stop flight"
    return (f'<li aria-label="From {price} {cur} round trip total. {stop} with {airline}. '
            f'Leaves Taoyuan at 6:50 AM."></li>')


def _page(*labels):
    return "<html><body>" + "".join(labels) + "</body></html>"


# ---- 解析 ------------------------------------------------------------------------

def test_parse_reads_price_stops_and_airlines():
    its = bd.parse(_page(_label("8,004", "Scoot"), _label("6,500", "Peach", nonstop=False),
                         _label("9,100", "Peach Aviation and Tigerair Taiwan"),
                         _label("7,000", "T&#39;way Air", cur="Taiwan dollars")))
    assert [(i.price, i.nonstop, i.airlines) for i in its] == [
        (8004.0, True, ("Scoot",)), (6500.0, False, ("Peach",)),
        (9100.0, True, ("Peach Aviation", "Tigerair Taiwan")), (7000.0, True, ("T'way Air",))]
    assert [i.carriers for i in its] == ["TR", "MM", "MM,IT", "TW"]


def test_unknown_airline_has_no_carrier_code():
    """對不上的不猜：寫進錯的代碼會讓「同航程」比對誤判。"""
    assert bd.parse(_page(_label("10,981", "Trinity Airways")))[0].carriers is None
    assert bd.parse(_page(_label("10,981", "Scoot and Trinity Airways")))[0].carriers is None


def test_parse_of_an_unrelated_page_is_empty():
    assert bd.parse("<html>captcha</html>") == []


def test_select_writes_the_cheapest_direct_and_the_wanted_airline():
    its = bd.parse(_page(_label("9,000", "Jetstar"), _label("7,391", "Scoot"),
                         _label("6,000", "Peach", nonstop=False),
                         _label("6,500", "Trinity Airways")))
    offers, matched = bd.select("TPE", "KIX", DEP, RET, its, want_carriers="GK")
    assert [(o.price, o.carriers) for o in offers] == [(7391.0, "TR"), (9000.0, "GK")]
    assert matched is offers[1]
    o = offers[0]
    assert (o.source, o.provider, o.stops, o.currency, o.fare_class, o.return_date) == (
        "google", "brightdata", 0, "TWD", "any", RET)
    # 想要的航空公司就是最便宜那家：只寫一筆
    offers, matched = bd.select("TPE", "KIX", DEP, RET, its, want_carriers="TR")
    assert len(offers) == 1 and matched is offers[0]
    # 想要的航空公司不在結果裡：照樣寫最便宜那筆，但沒有可當權威價的
    offers, matched = bd.select("TPE", "KIX", DEP, RET, its, want_carriers="IT")
    assert len(offers) == 1 and matched is None
    # 沒有任何直飛（或代碼都對不上）：什麼都不寫
    assert bd.select("TPE", "KIX", DEP, RET, bd.parse(_page(_label("1", "Peach", False))))[0] == []


# ---- 配速 ------------------------------------------------------------------------

def test_allowance_spreads_the_month_evenly():
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)                  # 11 月 720 小時
    assert bd.run_allowance(0, start) == 7                              # ceil(4750 / 720)
    assert bd.run_allowance(4750, start) == 0
    assert bd.run_allowance(5000, start) == 0
    # 前半個月都沒用到 → 後面每輪多用，但不超過單輪上限
    assert bd.run_allowance(0, datetime(2026, 11, 25, tzinfo=timezone.utc)) == bd.PER_RUN_MAX
    # 最後一小時：剩多少用多少，不會超過剩餘
    last = datetime(2026, 11, 30, 23, 30, tzinfo=timezone.utc)
    assert bd.run_allowance(4747, last) == 3
    # 12 月換年也算得對
    assert bd.run_allowance(0, datetime(2026, 12, 31, 22, tzinfo=timezone.utc)) == bd.PER_RUN_MAX


def test_month_total_ends_close_to_the_target():
    """整個月每小時跑一輪、照配速用，月底用量落在目標附近、絕不超過。"""
    used, t = 0, datetime(2026, 11, 1, tzinfo=timezone.utc)
    while t.month == 11:
        used += bd.run_allowance(used, t)
        t += timedelta(hours=1)
    target = int(bd.MONTHLY_FREE * bd.USE_FRACTION)
    assert target - 10 <= used <= target


# ---- fetch -----------------------------------------------------------------------

class _Resp:
    def __init__(self, status=200, text=""):
        self.status_code, self.text = status, text


class _Session:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp, exc, []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        if self.exc:
            raise self.exc
        return self.resp


def test_fetch_sends_the_tested_request(monkeypatch):
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "secret-key")
    s = _Session(_Resp(200, "<html>ok</html>"))
    assert bd.fetch("TPE", "NRT", DEP, RET, session=s) == "<html>ok</html>"
    url, kw = s.calls[0]
    assert url == bd.API_URL and kw["headers"]["Authorization"] == "Bearer secret-key"
    assert kw["json"]["zone"] == "mcp_unlocker" and kw["json"]["format"] == "raw"
    assert "curr=TWD" in kw["json"]["url"] and "hl=en" in kw["json"]["url"]
    assert kw["timeout"] == (bd.CONNECT_TIMEOUT_S, bd.READ_TIMEOUT_S) and bd.READ_TIMEOUT_S > 60
    monkeypatch.setenv("BRIGHTDATA_ZONE", "my_zone")
    bd.fetch("TPE", "NRT", DEP, RET, session=s)
    assert s.calls[1][1]["json"]["zone"] == "my_zone"


@pytest.mark.parametrize("session, match", [
    (_Session(_Resp(401, "bad token secret-key")), "HTTP 401"),
    (_Session(_Resp(200, "")), "empty body"),
    (_Session(exc=requests.ConnectTimeout("to https://x?k=secret-key")), "transport"),
])
def test_fetch_errors_never_carry_the_key(monkeypatch, session, match):
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "secret-key")
    with pytest.raises(bd.BrightDataError, match=match) as info:
        bd.fetch("TPE", "NRT", DEP, RET, session=session)
    assert "secret-key" not in str(info.value)


def test_the_key_is_masked_before_the_message_is_cut(monkeypatch):
    """金鑰剛好跨在 200 字的截斷點上：先截再遮會留下前半段。"""
    key = "BDKEY-a1b2c3d4e5f6g7h8i9j0"
    monkeypatch.setenv("BRIGHTDATA_API_KEY", key)
    s = _Session(_Resp(500, "x" * 180 + key + "y" * 50))
    with pytest.raises(bd.BrightDataError) as info:
        bd.fetch("TPE", "NRT", DEP, RET, session=s)
    assert key[:8] not in str(info.value) and len(str(info.value)) <= 200


def test_fetch_without_key_refuses():
    with pytest.raises(bd.BrightDataError, match="missing"):
        bd.fetch("TPE", "NRT", DEP, RET)
    assert bd.configured() is False


# ---- Verifier ----------------------------------------------------------------------

def _store(tmp_path):
    return Store(str(tmp_path / "prices.db"))


def _google_rows(store):
    return store.conn.execute(
        "SELECT origin, destination, depart_date, price, carriers FROM observations "
        "WHERE provider='brightdata' ORDER BY price").fetchall()


def test_check_records_offers_and_usage(tmp_path):
    st = _store(tmp_path)
    v = bd.Verifier(st, NOW, allowance=2,
                    fetcher=lambda *a: _page(_label("7,391", "Scoot"), _label("8,000", "Jetstar")))
    auth = v.check("TPE", "KIX", DEP, RET, "GK")
    assert (auth.price, auth.carriers) == (8000.0, "GK")
    assert [tuple(r) for r in _google_rows(st)] == [("TPE", "KIX", DEP, 7391.0, "TR"),
                                                    ("TPE", "KIX", DEP, 8000.0, "GK")]
    assert st.api_usage_since("brightdata", "2026-11-01") == 1
    assert v.left == 1 and v.stats["ok"] == 1 and v.stats["recorded"] == 2


def test_check_failure_is_soft_and_still_counted(tmp_path):
    st = _store(tmp_path)

    def boom(*a):
        raise bd.BrightDataError("HTTP 502")

    v = bd.Verifier(st, NOW, allowance=1, fetcher=boom)
    assert v.check("TPE", "KIX", DEP, RET, "GK") is None
    assert v.stats["errors"] == 1 and _google_rows(st) == []
    row = st.conn.execute("SELECT requests, ok FROM api_usage").fetchone()
    assert tuple(row) == (1, 0)                       # 送出就記一次（保守），但不算成功
    assert v.check("TPE", "KIX", DEP, RET, "GK") is None   # 額度用完：連送都不送
    assert st.api_usage_since("brightdata", "2026-11-01") == 1


def test_a_page_without_itineraries_counts_as_an_error(tmp_path):
    """拿到頁面卻解析不到：多半是 Google 改版，要看得見，不能當成功。"""
    st = _store(tmp_path)
    v = bd.Verifier(st, NOW, allowance=1, fetcher=lambda *a: "<html>new layout</html>")
    assert v.check("TPE", "KIX", DEP, RET) is None
    assert v.stats["errors"] == 1 and v.stats["ok"] == 0


def test_a_run_stops_using_bright_data_when_its_time_budget_is_spent(tmp_path):
    """等待放寬到 120 秒後，連續逾時會逼近 job 的 30 分鐘上限；整輪有總時間上限。"""
    st = _store(tmp_path)
    t = [0.0]

    def hang(*a):
        t[0] += bd.READ_TIMEOUT_S + bd.CONNECT_TIMEOUT_S
        raise bd.BrightDataTimeout("transport: ReadTimeout")

    v = bd.Verifier(st, NOW, allowance=15, fetcher=hang, clock=lambda: t[0])
    for _ in range(15):
        v.check("TPE", "NRT", DEP, RET)
    assert v.stats["requests"] == 5 and v.stats["timeouts"] == 5     # 5 × 130 秒就超過 600 秒
    v.sweep([_offer("TPE", "NRT", _day(30 + i), 7000 + i) for i in range(5)], [("TPE", "NRT")])
    assert v.stats["requests"] == 5 and v.stats["swept"] == 0


def test_a_sweep_stops_when_the_time_budget_runs_out_midway(tmp_path):
    """警報驗證已經花掉大半時間：巡檢查到超過上限就停，剩下的候選不算「巡檢過」。"""
    st = _store(tmp_path)
    t = [0.0]

    def slow(*a):
        t[0] += 250.0
        return _page(_label("7,000", "Jetstar"))

    v = bd.Verifier(st, NOW, allowance=10, fetcher=slow, clock=lambda: t[0])
    v.check("TPE", "NRT", DEP, RET)
    v.check("TPE", "NRT", DEP, RET)                              # 已花 500 秒
    v.sweep([_offer("TPE", "NRT", _day(30 + i), 7000 + i) for i in range(5)], [("TPE", "NRT")])
    assert v.stats["requests"] == 3 and v.stats["swept"] == 1    # 750 秒 ≥ 600 就停


def test_time_spent_outside_bright_data_does_not_count(tmp_path):
    """Verifier 在抓 Aviasales 之前就建立；抓價花的時間不能算進 Bright Data 的時間上限。"""
    st = _store(tmp_path)
    t = [0.0]
    v = bd.Verifier(st, NOW, allowance=2, fetcher=lambda *a: _page(_label("7,000", "Jetstar")),
                    clock=lambda: t[0])
    t[0] += 20 * 60                                  # 抓 Aviasales 花了 20 分鐘
    v.check("TPE", "NRT", DEP, RET)
    v.sweep([_offer("TPE", "NRT", _day(30), 7000)], [("TPE", "NRT")])
    assert v.stats["requests"] == 2 and v.stats["swept"] == 1


def test_timeouts_are_told_apart_from_other_failures(monkeypatch):
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "secret-key")
    with pytest.raises(bd.BrightDataTimeout, match="transport"):
        bd.fetch("TPE", "NRT", DEP, RET, session=_Session(exc=requests.ReadTimeout("slow")))
    with pytest.raises(bd.BrightDataError) as info:
        bd.fetch("TPE", "NRT", DEP, RET, session=_Session(exc=requests.ConnectionError("refused")))
    assert not isinstance(info.value, bd.BrightDataTimeout)


def test_slow_pages_are_measured(tmp_path):
    """2026-11 約 7% 的請求等了 60 秒還沒回來；放寬到 120 秒後要看得出救回多少。"""
    st = _store(tmp_path)
    t = [0.0]
    durations = iter([75.0, 10.0])

    def fetch(*a):
        t[0] += next(durations, 0.0)
        return _page(_label("7,000", "Jetstar"))

    v = bd.Verifier(st, NOW, allowance=3, fetcher=fetch, clock=lambda: t[0])
    v.check("TPE", "NRT", DEP, RET)
    v.check("TPE", "NRT", DEP, RET)
    assert v.stats["ok_over_60s"] == 1 and v.stats["slowest_s"] == 75

    def timeout(*a):
        t[0] += 120.0
        raise bd.BrightDataTimeout("transport: ReadTimeout")

    v.fetch = timeout
    v.check("TPE", "NRT", DEP, RET)
    assert v.stats["ok_over_60s"] == 1 and v.stats["slowest_s"] == 75   # 失敗的不算
    assert v.stats["timeouts"] == 1

    def slow_captcha(*a):                     # 拿到頁面、但解析不到：不算「救回來」
        t[0] += 90.0
        return "<html>captcha</html>"

    v2 = bd.Verifier(st, NOW, allowance=1, fetcher=slow_captcha, clock=lambda: t[0])
    v2.check("TPE", "NRT", DEP, RET)
    assert v2.stats["ok_over_60s"] == 0 and v2.stats["slowest_s"] == 90
    assert v2.stats["errors"] == 1 and v2.stats["timeouts"] == 0         # 失敗、但不是逾時
    # 逐日帳本：一週後用來評估等待上限值不值得（ADR 0004）
    row = st.conn.execute("SELECT requests, ok, timeouts, slow_ok, seconds FROM api_usage").fetchone()
    assert tuple(row) == (4, 2, 1, 1, 75.0 + 10.0 + 120.0 + 90.0)


def test_skipped_verifications_are_not_counted_as_checked(tmp_path):
    """額度或時間用完時沒有真的送出請求，不算進 alerts_checked（第二輪規格審查）。"""
    st = _store(tmp_path)
    t = [0.0]

    def hang(*a):
        t[0] += 130.0
        raise bd.BrightDataTimeout("transport: ReadTimeout")

    v = bd.Verifier(st, NOW, allowance=15, fetcher=hang, clock=lambda: t[0])
    for _ in range(8):
        v.verify_alert("KHH", "NRT", DEP, RET, "GK")
    assert v.stats["requests"] == 5 and v.stats["alerts_checked"] == 5
    v0 = bd.Verifier(st, NOW, allowance=0, fetcher=hang, clock=lambda: t[0])
    assert v0.verify_alert("KHH", "NRT", DEP, RET, "GK") is None
    assert v0.stats["alerts_checked"] == 0


def test_an_unexpected_error_is_also_soft(tmp_path):
    """解析程式的 bug 之類的意外，也只記錯誤、不往上丟（Bright Data 是加分項）。"""
    st = _store(tmp_path)

    def bug(*a):
        raise ValueError("unexpected")

    v = bd.Verifier(st, NOW, allowance=1, fetcher=bug)
    assert v.check("TPE", "KIX", DEP, RET, "GK") is None
    assert v.stats["errors"] == 1
    assert st.api_usage_since("brightdata", "2026-11-01") == 1


def test_freshness_survives_mixed_timestamp_formats(tmp_path):
    """observed_at 有舊格式（空格分隔）；直接比字串會把同一天稍晚的觀測當成更早。"""
    st = _store(tmp_path)
    st.conn.execute(
        "INSERT INTO observations (origin, destination, depart_date, return_date, price,"
        " currency, carriers, stops, duration, observed_at, fare_class, source, provider)"
        " VALUES ('TPE','NRT',?,?,8000,'TWD','GK',0,'',?,'any','google','serpapi')",
        (DEP, RET, (NOW - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")))
    st.conn.commit()
    since = (NOW - timedelta(hours=2)).isoformat(timespec="seconds")
    assert st.has_google_since("TPE", "NRT", DEP, RET, since)


def test_allowance_comes_from_this_months_ledger(tmp_path):
    st = _store(tmp_path)
    st.conn.execute("INSERT INTO api_usage (provider, day, requests, ok)"
                    " VALUES ('brightdata', '2026-10-31', 4000, 4000)")
    st.conn.commit()
    # 11/15 06:17 到月底約 378 小時：ceil(4750 / 378) = 13。上個月的 4,000 若被算進來會是 2
    assert bd.Verifier(st, NOW).left == 13
    st.conn.execute("INSERT INTO api_usage (provider, day, requests, ok)"
                    " VALUES ('brightdata', '2026-11-02', 4740, 4740)")
    st.conn.commit()
    assert bd.Verifier(st, NOW).left == 1             # 只剩 10 次：每輪 1 次


# ---- 巡檢 --------------------------------------------------------------------------

def _offer(o, d, dep, price, carriers="GK", ret_days=5, stops=0):
    ret = (datetime.fromisoformat(dep) + timedelta(days=ret_days)).date().isoformat()
    return Offer(o, d, dep, ret, price, "TWD", carriers, stops, "180", source="aviasales")


def _day(n):
    return (NOW.date() + timedelta(days=n)).isoformat()


def test_sweep_goes_route_by_route_cheapest_first_and_skips_fresh_ones(tmp_path):
    st = _store(tmp_path)
    asked = []

    def fetch(o, d, dep, ret):
        asked.append((o, d, dep))
        return _page(_label("9,999", "Jetstar"))

    # TPE-NRT 30 天後那趟 2 小時前已經有 Google 實價 → 跳過（26 小時前的那趟照查）
    for dep, hours in ((_day(30), 2), (_day(40), 26)):
        st.conn.execute(
            "INSERT INTO observations (origin, destination, depart_date, return_date, price,"
            " currency, carriers, stops, duration, observed_at, fare_class, source, provider)"
            " VALUES ('TPE','NRT',?,?,8000,'TWD','GK',0,'',?,'any','google','serpapi')",
            (dep, _offer("TPE", "NRT", dep, 1).return_date,
             (NOW - timedelta(hours=hours)).isoformat(timespec="seconds")))
    st.conn.commit()
    cands = [_offer("TPE", "NRT", _day(30), 7000), _offer("TPE", "NRT", _day(40), 7100),
             _offer("TPE", "NRT", _day(50), 7200), _offer("KHH", "KIX", _day(20), 9000),
             _offer("KHH", "KIX", _day(25), 8500),
             _offer("KHH", "KIX", _day(1), 100),            # 太近：不查
             _offer("KHH", "KIX", _day(60), 50, stops=1)]   # 轉機：不查
    v = bd.Verifier(st, NOW, allowance=3, fetcher=fetch)
    v.sweep(cands, [("TPE", "NRT"), ("KHH", "KIX")])
    assert asked == [("TPE", "NRT", _day(40)), ("KHH", "KIX", _day(25)),
                     ("TPE", "NRT", _day(50))]
    assert v.left == 0 and v.stats["swept"] == 3


def test_sweep_checks_fixed_dates_on_routes_without_cache_data(tmp_path):
    """KHH-NGO 這類快取沒資料的航線也要有 Google 實價。"""
    st = _store(tmp_path)
    asked = []
    v = bd.Verifier(st, NOW, allowance=2,
                    fetcher=lambda o, d, dep, ret: asked.append((o, d, dep, ret)) or "")
    v.sweep([], [("KHH", "NGO")])
    assert len(asked) == 2 and all(r[:2] == ("KHH", "NGO") for r in asked)
    dep, ret = asked[0][2], asked[0][3]
    assert (datetime.fromisoformat(ret) - datetime.fromisoformat(dep)).days == bd.STAY_NIGHTS
    assert dep >= _day(14)


def test_fixed_dates_cover_every_weekday():
    """KHH-NGO 只有週一、週三有直飛：清單若全落在同一個星期幾，碰上沒航班的那天就全部白查
    （2026-10-31 實跑：全是週二，3 次 0 筆）。"""
    for days in range(7):
        trips = bd.anchor_trips(NOW + timedelta(days=days))
        weekdays = {datetime.fromisoformat(dep).weekday() for dep, _ in trips}
        assert weekdays == set(range(7))
        assert min(dep for dep, _ in trips) >= (NOW.date() + timedelta(days=days + 14)).isoformat()


# ---- 冷門線：只挑有直飛的星期幾 ------------------------------------------------------

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)


def _weekdays(trips):
    return [datetime.fromisoformat(dep).weekday() for dep, _ in trips]


def test_fixed_dates_stick_to_weekdays_with_direct_flights():
    """KHH-NGO 冬季只有週一、週三有直飛：其他星期幾都落空過，就只查週一、週三。"""
    trips = bd.anchor_trips(NOW, prefer=frozenset({MON, WED}),
                            avoid=frozenset({TUE, THU, FRI, SAT, SUN}))
    assert len(trips) == 8 and set(_weekdays(trips)) == {MON, WED}
    first, last = (datetime.fromisoformat(trips[i][0]) for i in (0, -1))
    assert (last - first).days >= 35                  # 平均分散，不是擠在同一週
    assert all((datetime.fromisoformat(r) - datetime.fromisoformat(d)).days == bd.STAY_NIGHTS
               for d, r in trips)


def test_untried_weekdays_are_tried_first_and_misses_are_skipped():
    trips = bd.anchor_trips(NOW, prefer=frozenset({WED}), avoid=frozenset({TUE}))
    wds = _weekdays(trips)
    assert sorted(wds[:5]) == [MON, THU, FRI, SAT, SUN]   # 還沒試過的各試一天
    assert set(wds[5:]) == {WED} and TUE not in wds and len(trips) == 8


def test_fixed_dates_cover_the_whole_period_including_peak_season():
    """有飛的星期幾很多時（KHH-CTS），日子也要散到整段期間，不能擠在第一週。"""
    trips = bd.anchor_trips(NOW, prefer=frozenset({MON, TUE, WED, THU, FRI, SAT}),
                            avoid=frozenset({SUN}))
    deps = [datetime.fromisoformat(d).date() for d, _ in trips]
    assert len(deps) == 8 and SUN not in {d.weekday() for d in deps}
    assert (deps[-1] - deps[0]).days >= 50 and len({d.isocalendar()[1] for d in deps}) >= 6
    assert bd._spread(list(range(9)), 1) == [4]        # 只挑一個時取中間，不是最早那天


def test_no_fixed_dates_when_every_weekday_came_up_empty():
    assert bd.anchor_trips(NOW, avoid=frozenset(range(7))) == []


def _ret(dep):
    return (datetime.fromisoformat(dep) + timedelta(days=bd.STAY_NIGHTS)).date().isoformat()


def _check(st, o, d, dep, at, *, parsed=True):
    st.record_api_check("brightdata", o, d, dep, _ret(dep),
                        at.isoformat(timespec="seconds"), parsed=parsed)


def _obs(st, o, d, dep, at, provider="brightdata", stops=0):
    ret = _ret(dep)
    st.conn.execute(
        "INSERT INTO observations (origin, destination, depart_date, return_date, price,"
        " currency, carriers, stops, duration, observed_at, fare_class, source, provider)"
        " VALUES (?,?,?,?,8000,'TWD','IT',?,'',?,'any','google',?)",
        (o, d, dep, ret, stops, at.isoformat(timespec="seconds"), provider))
    st.conn.commit()
    return ret


def _next(wd, after=20):
    day = NOW.date() + timedelta(days=after)
    return (day + timedelta(days=(wd - day.weekday()) % 7)).isoformat()


def test_weekday_outcomes_follow_the_latest_result(tmp_path):
    st = _store(tmp_path)
    # 週三：10 天前 SerpAPI 查到直飛 → 有飛
    _obs(st, "KHH", "NGO", _next(WED), NOW - timedelta(days=10), provider="serpapi")
    # 週二：2 天前 Bright Data 拿到頁面卻沒寫入 → 落空
    _check(st, "KHH", "NGO", _next(TUE), NOW - timedelta(days=2))
    # 週一：5 天前落空，但 1 天前查到直飛 → 以最近一次為準，有飛
    _check(st, "KHH", "NGO", _next(MON), NOW - timedelta(days=5))
    _obs(st, "KHH", "NGO", _next(MON, after=40), NOW - timedelta(days=1))
    _check(st, "KHH", "NGO", _next(MON, after=40), NOW - timedelta(days=1))
    # 不算：31 天前的直飛、轉機的實價、別條航線、解析不到航班的頁面（驗證碼之類）
    _obs(st, "KHH", "NGO", _next(SAT), NOW - timedelta(days=31))
    _obs(st, "KHH", "NGO", _next(SUN), NOW - timedelta(days=1), stops=1)
    _obs(st, "KHH", "CTS", _next(FRI), NOW - timedelta(days=1))
    _check(st, "KHH", "NGO", _next(THU), NOW - timedelta(days=1), parsed=False)
    v = bd.Verifier(st, NOW, allowance=1, fetcher=lambda *a: "")
    known = v.weekday_outcomes("KHH", "NGO")
    assert (known.prefer, known.avoid) == (frozenset({MON, WED}), frozenset({TUE}))
    # 8 天前的落空已經過期
    assert bd.Verifier(st, NOW + timedelta(days=6), allowance=1,
                       fetcher=lambda *a: "").weekday_outcomes("KHH", "NGO").avoid == frozenset()


def test_a_recheck_updates_whether_the_page_was_readable(tmp_path):
    """同一趟先遇到驗證碼（看不出有沒有飛），25 小時後重查只有轉機 → 改算落空。"""
    st = _store(tmp_path)
    dep = _next(TUE)
    for h, page in ((0, "<html>captcha</html>"),
                    (25, _page(_label("15,000", "Tigerair Taiwan", nonstop=False)))):
        bd.Verifier(st, NOW + timedelta(hours=h), allowance=1,
                    fetcher=lambda *a, page=page: page).check("KHH", "NGO", dep, _ret(dep))
        avoid = bd.Verifier(st, NOW + timedelta(hours=h), allowance=0).weekday_outcomes(
            "KHH", "NGO").avoid
        assert avoid == (frozenset({TUE}) if h else frozenset())


def test_an_old_checks_table_gets_the_parsed_column(tmp_path):
    """正式 prices.db 的 api_checks 是 PR #9 建的、沒有 parsed：開檔時補上，舊列當成有解析到。"""
    db = tmp_path / "prices.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE api_checks (provider TEXT NOT NULL, origin TEXT NOT NULL,
        destination TEXT NOT NULL, depart_date TEXT NOT NULL, return_date TEXT NOT NULL,
        checked_at TEXT NOT NULL,
        PRIMARY KEY (provider, origin, destination, depart_date, return_date))""")
    con.execute("INSERT INTO api_checks VALUES ('brightdata','KHH','NGO',?,?,?)",
                (_next(TUE), _ret(_next(TUE)), (NOW - timedelta(days=1)).isoformat(timespec="seconds")))
    con.commit()
    con.close()
    st = Store(str(db))
    assert [tuple(r) for r in st.conn.execute("SELECT parsed FROM api_checks")] == [(1,)]
    assert bd.Verifier(st, NOW, allowance=0).weekday_outcomes("KHH", "NGO").avoid == {TUE}


def test_an_old_usage_table_gets_the_timing_columns(tmp_path):
    """正式 prices.db 的 api_usage 是 PR #9 建的、只有次數：開檔時補上耗時欄位，舊列記 0。"""
    db = tmp_path / "prices.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE api_usage (provider TEXT NOT NULL, day TEXT NOT NULL,
        requests INTEGER NOT NULL DEFAULT 0, ok INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (provider, day))""")
    con.execute("INSERT INTO api_usage VALUES ('brightdata', '2026-11-15', 3, 2)")
    con.commit()
    con.close()
    st = Store(str(db))
    st.record_api_usage("brightdata", "2026-11-15", ok=False, timeout=True, seconds=130.0)
    row = st.conn.execute("SELECT requests, ok, timeouts, slow_ok, seconds FROM api_usage").fetchone()
    assert tuple(row) == (4, 2, 1, 0, 130.0)
    assert st.api_usage_since("brightdata", "2026-11-01") == 4


def test_checks_through_the_real_path_are_classified_right(tmp_path):
    """走 check()：寫入直飛價的那趟算有飛（observed_at 跟 checked_at 必須是同一個時間戳）；
    只有轉機的算落空；解析不到航班的頁面看不出那天有沒有飛，不算落空。"""
    st = _store(tmp_path)
    pages = {MON: _page(_label("8,791", "Tigerair Taiwan")),
             TUE: _page(_label("15,000", "Tigerair Taiwan", nonstop=False)),
             WED: "<html>captcha</html>"}
    v = bd.Verifier(st, NOW, allowance=3,
                    fetcher=lambda o, d, dep, ret: pages[datetime.fromisoformat(dep).weekday()])
    for wd in (MON, TUE, WED):
        v.check("KHH", "NGO", _next(wd), _ret(_next(wd)))
    known = bd.Verifier(st, NOW + timedelta(hours=1), allowance=0).weekday_outcomes("KHH", "NGO")
    assert (known.prefer, known.avoid) == (frozenset({MON}), frozenset({TUE}))


def test_cold_route_sweep_skips_weekdays_without_direct_flights(tmp_path):
    """接起來跑：週二落空過 → 巡檢不查週二；週三有飛 → 會查到週三。"""
    st = _store(tmp_path)
    _obs(st, "KHH", "NGO", _next(WED), NOW - timedelta(days=3))
    _check(st, "KHH", "NGO", _next(TUE), NOW - timedelta(days=2))
    asked = []
    v = bd.Verifier(st, NOW, allowance=8,
                    fetcher=lambda o, d, dep, ret: asked.append(dep) or _page(_label("8,791", "Tigerair Taiwan")))
    v.sweep([], [("KHH", "NGO")])
    wds = [datetime.fromisoformat(dep).weekday() for dep in asked]
    assert len(asked) == 8 and TUE not in wds and WED in wds


def test_a_page_without_direct_flights_says_why(tmp_path, caplog):
    """沒寫入時要看得出是那天沒有直飛，還是航空公司名稱對不上（對照表要補）。"""
    st = _store(tmp_path)
    v = bd.Verifier(st, NOW, allowance=1, fetcher=lambda *a: _page(
        _label("7,000", "Brand New Air"), _label("9,000", "Jetstar", nonstop=False)))
    with caplog.at_level("INFO", logger="farehunter.brightdata_flights"):
        v.check("TPE", "NRT", DEP, RET)
    assert "2 個航班、直飛 1 個" in caplog.text and "Brand New Air" in caplog.text


def test_a_trip_without_direct_flights_is_not_rechecked_for_a_day(tmp_path):
    """拿到頁面但沒有直飛價時 observations 不會留下東西；沒有記「查過」的話每小時都會重查。"""
    st = _store(tmp_path)
    asked = []

    def connecting_only(o, d, dep, ret):
        asked.append(dep)
        return _page(_label("15,000", "Jetstar", nonstop=False))

    cands = [_offer("TPE", "NRT", _day(30), 7000), _offer("TPE", "NRT", _day(31), 7100)]
    v = bd.Verifier(st, NOW, allowance=1, fetcher=connecting_only)
    v.sweep(cands, [("TPE", "NRT")])
    assert v.stats["no_direct"] == 1 and v.stats["ok"] == 1 and _google_rows(st) == []
    bd.Verifier(st, NOW + timedelta(hours=1), allowance=1,
                fetcher=connecting_only).sweep(cands, [("TPE", "NRT")])
    assert asked == [_day(30), _day(31)]                # 一小時後換下一趟，不重查
    bd.Verifier(st, NOW + timedelta(hours=26), allowance=1,
                fetcher=connecting_only).sweep(cands, [("TPE", "NRT")])
    assert asked == [_day(30), _day(31), _day(30)]      # 超過 24 小時才再查
    assert st.conn.execute("SELECT COUNT(*) FROM api_checks").fetchone()[0] == 2  # 超過 24 小時照樣留著
    # 紀錄留 7 天（冷門線要記得哪些星期幾沒飛），超過的會被清掉，表不會一直長
    bd.Verifier(st, NOW + timedelta(days=8), allowance=1,
                fetcher=connecting_only).sweep(cands, [("TPE", "NRT")])
    assert [r[0] for r in st.conn.execute("SELECT depart_date FROM api_checks")] == [_day(30)]


def test_a_failed_request_is_retried_next_run(tmp_path):
    """傳輸錯誤沒拿到頁面：不算查過，下一輪照查。"""
    st = _store(tmp_path)
    asked = []

    def flaky(o, d, dep, ret):
        asked.append(dep)
        if len(asked) == 1:
            raise bd.BrightDataError("transport: ReadTimeout")
        return _page(_label("7,000", "Jetstar"))

    cands = [_offer("TPE", "NRT", _day(30), 7000), _offer("TPE", "NRT", _day(31), 7100)]
    for h in (0, 1):
        bd.Verifier(st, NOW + timedelta(hours=h), allowance=1,
                    fetcher=flaky).sweep(cands, [("TPE", "NRT")])
    assert asked == [_day(30), _day(30)]


def test_every_route_gets_its_turn_when_the_allowance_is_small(tmp_path):
    """11 月每輪約 7 次、航線 8 條：每輪都從第一條開始輪的話，最後一條（KHH-NGO）整個月
    都輪不到。起點每小時換一條。"""
    st = _store(tmp_path)
    routes = [("TPE", "NRT"), ("TPE", "KIX"), ("KHH", "NGO")]
    asked = []
    for h in range(3):
        bd.Verifier(st, NOW + timedelta(hours=h), allowance=1,
                    fetcher=lambda o, d, dep, ret: asked.append((o, d)) or "").sweep([], routes)
    assert sorted(asked) == sorted(routes)


def test_sweep_stops_at_the_deadline(tmp_path):
    st = _store(tmp_path)
    t = [0.0]

    def fetch(*a):
        t[0] += bd.SWEEP_DEADLINE_S / 2 + 1          # 每次花掉一半多的時限
        return _page(_label("7,000", "Jetstar"))

    v = bd.Verifier(st, NOW, allowance=10, fetcher=fetch, clock=lambda: t[0])
    v.sweep([_offer("TPE", "NRT", _day(30 + i), 7000 + i) for i in range(10)], [("TPE", "NRT")])
    assert v.stats["swept"] == 2 and v.left == 8


# ---- runner：警報前驗證 ---------------------------------------------------------------

def _run(tmp_path, monkeypatch, fetch, offers=None, threshold=8000, now=NOW, search=None):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "defaults:\n  currency: twd\n  months_ahead: 1\n  pause_seconds: 0\n"
        "routes:\n  - origin: KHH\n    destination: NRT\n"
        f"    absolute_threshold: {threshold}\n", encoding="utf-8")
    offers = offers or [Offer("KHH", "NRT", DEP, RET, 7761.0, "TWD", "GK", 0, "180")]
    monkeypatch.setattr(runner_mod, "TravelpayoutsClient",
                        type("C", (), {"__init__": lambda s, *a, **k: None,
                                       "search_month": search or (lambda s, *a, **k: {})}))
    monkeypatch.setattr(runner_mod, "parse_offers", lambda *a, **k: list(offers))
    sent = []
    monkeypatch.setattr(runner_mod, "notify",
                        lambda o, v, st=None, **k: sent.append((o, v, st)) or ["line"])
    monkeypatch.setattr(runner_mod, "channels_configured", lambda: True)
    monkeypatch.setattr(bd, "fetch", fetch)
    monkeypatch.setenv("FAREHUNTER_FORCE", "1")
    summary = runner_mod.run(str(cfg), str(tmp_path / "prices.db"), now=now)
    con = sqlite3.connect(tmp_path / "prices.db")
    alerts = con.execute("SELECT price, price_status FROM alerts").fetchall()
    con.close()
    return summary, sent, alerts


def test_without_a_key_the_runner_never_calls_bright_data(tmp_path, monkeypatch):
    def forbidden(*a):
        raise AssertionError("沒有金鑰不得連外")

    summary, sent, alerts = _run(tmp_path, monkeypatch, forbidden)
    assert "brightdata" not in summary and len(sent) == 1
    assert alerts == [(7761.0, "unverified")]


def test_alert_is_verified_with_the_google_price(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "k")
    summary, sent, alerts = _run(
        tmp_path, monkeypatch, lambda *a: _page(_label("7,650", "Jetstar"), _label("7,000", "Scoot")))
    assert alerts == [(7650.0, "verified")]
    offer, verdict, state = sent[0]
    assert state.state == "verified" and offer.price == 7650.0
    assert summary["brightdata"]["alerts_checked"] == 1


def test_no_alert_when_the_google_price_is_not_a_deal(tmp_path, monkeypatch):
    """快取說 7,761（低於門檻 8,000），Google 實價 9,200 → 不發。"""
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "k")
    summary, sent, alerts = _run(tmp_path, monkeypatch,
                                 lambda *a: _page(_label("9,200", "Jetstar")))
    assert sent == [] and alerts == []
    assert summary["alerts"] == 0


def test_other_airline_only_keeps_the_alert_unverified(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "k")
    summary, sent, alerts = _run(tmp_path, monkeypatch,
                                 lambda *a: _page(_label("7,000", "Tigerair Taiwan")))
    assert alerts == [(7761.0, "unverified")]
    con = sqlite3.connect(tmp_path / "prices.db")
    assert con.execute("SELECT carriers, price FROM observations WHERE provider='brightdata'"
                       ).fetchall()[0] == ("IT", 7000.0)        # 實價照樣記下來
    con.close()


def test_bright_data_failure_still_sends_the_alert(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "k")

    def down(*a):
        raise bd.BrightDataError("HTTP 503")

    summary, sent, alerts = _run(tmp_path, monkeypatch, down)
    assert alerts == [(7761.0, "unverified")] and len(sent) == 1
    assert summary["brightdata"]["errors"] >= 1


def _counting(page):
    """calls 只記警報那一趟（DEP／RET）被查了幾次；巡檢另外補查的固定日期不算。"""
    calls = []

    def fetch(o, d, dep, ret):
        if (dep, ret) == (DEP, RET):
            calls.append((o, d, dep, ret))
        return page

    return fetch, calls


def test_a_verified_alert_is_not_rechecked_every_hour(tmp_path, monkeypatch):
    """只有同一輪的實價才算 VERIFIED：一小時後驗證前的狀態又回到 UNVERIFIED、去重的桶子
    也不同。不擋的話同一則警報每小時都會再花一次額度（實際 DB 推估每月可達約 3,300 次）。"""
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "k")
    fetch, calls = _counting(_page(_label("7,650", "Jetstar")))
    _run(tmp_path, monkeypatch, fetch)
    summary, sent, alerts = _run(tmp_path, monkeypatch, fetch, now=NOW + timedelta(hours=1))
    assert len(calls) == 1 and sent == [] and alerts == [(7650.0, "verified")]
    _run(tmp_path, monkeypatch, fetch, now=NOW + timedelta(hours=25))
    assert len(calls) == 2                       # 超過 24 小時才重新驗證


def test_a_verified_trip_is_not_renotified_when_a_later_check_fails(tmp_path, monkeypatch):
    """實例：KHH-OKA 先發已驗證 7,315，隔天驗證逾時，又以未驗證 7,255 發給所有好友。
    已驗證過的行程，驗證落空時降級不重發（但照樣先驗證：實價可能已經大跌）。"""
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "k")
    calls = []

    def fetch(o, d, dep, ret):
        calls.append(dep)
        if len(calls) > 1:
            raise bd.BrightDataTimeout("transport: ReadTimeout")
        return _page(_label("7,650", "Jetstar"))

    _run(tmp_path, monkeypatch, fetch)
    summary, sent, alerts = _run(tmp_path, monkeypatch, fetch, now=NOW + timedelta(hours=25))
    assert sent == [] and alerts == [(7650.0, "verified")]
    assert summary["brightdata"]["alerts_checked"] == 1


def test_a_real_price_drop_after_a_verified_alert_is_still_sent(tmp_path, monkeypatch):
    """PR #16 規格審查的重現：已驗證 7,650，隔天快取還是 7,761，但 Google 跌到 6,500。
    降級判斷若放在驗證前，會拿快取價去比而擋掉；放在驗證後，這則新低價照發。"""
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "k")
    pages = iter([_page(_label("7,650", "Jetstar")), _page(_label("6,500", "Jetstar"))])
    fetch = lambda *a: next(pages, _page(_label("6,500", "Jetstar")))
    _run(tmp_path, monkeypatch, fetch)
    summary, sent, alerts = _run(tmp_path, monkeypatch, fetch, now=NOW + timedelta(hours=25))
    assert alerts == [(7650.0, "verified"), (6500.0, "verified")]


def test_a_downgrade_with_another_reason_is_not_resent(tmp_path, monkeypatch):
    """第二輪規格審查的重現：已驗證 new_low 8,000，隔天驗證逾時、改判 big_drop 8,500（更貴）。
    同一趟就是同一趟，不因為警報理由不同就再發一次。"""
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "k")
    st = Store(str(tmp_path / "prices.db"))
    for i in range(30):             # 這個出發日 30 筆 12,000 的歷史：樣本夠，8,500 也算大跌
        st.record(Offer("KHH", "NRT", DEP, RET, 12000.0, "TWD", "GK", 0, "180"),
                  observed_at=(NOW - timedelta(days=10, hours=i)).isoformat(timespec="seconds"))
    st.close()
    calls = []

    def fetch(o, d, dep, ret):
        calls.append(dep)
        if len(calls) > 1:
            raise bd.BrightDataTimeout("transport: ReadTimeout")
        return _page(_label("8,000", "Jetstar"))

    offers = [Offer("KHH", "NRT", DEP, RET, 8500.0, "TWD", "GK", 0, "180")]
    _, first, _ = _run(tmp_path, monkeypatch, fetch, offers=offers, threshold=1)  # 門檻 1：不觸發 absolute
    assert [(o.price, v.reason, s.state) for o, v, s in first] == [(8000.0, "new_low", "verified")]
    summary, sent, alerts = _run(tmp_path, monkeypatch, fetch, offers=offers, threshold=1,
                                 now=NOW + timedelta(hours=25))
    assert sent == [] and alerts == [(8000.0, "verified")]
    assert summary["brightdata"]["alerts_checked"] == 1      # 照樣先驗證（逾時）才擋


def test_without_bright_data_a_downgrade_is_not_resent(tmp_path, monkeypatch):
    """沒設金鑰時也一樣：同一趟已用 Google 實價通知過，快取價沒有再便宜一截就不重發。"""
    st = Store(str(tmp_path / "prices.db"))
    st.record_alert("KHH", "NRT", DEP, 7650.0, "absolute", return_date=RET,
                    carrier_signature="GK", price_source="google", price_status="verified")
    st.close()
    summary, sent, alerts = _run(tmp_path, monkeypatch, lambda *a: None)
    assert sent == [] and alerts == [(7650.0, "verified")]
    cheaper = [Offer("KHH", "NRT", DEP, RET, 6800.0, "TWD", "GK", 0, "180")]   # 便宜 11%
    summary, sent, alerts = _run(tmp_path, monkeypatch, lambda *a: None, offers=cheaper)
    assert len(sent) == 1 and alerts[-1] == (6800.0, "unverified")


def test_a_trip_google_says_is_not_cheap_is_not_rechecked_every_hour(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "k")
    fetch, calls = _counting(_page(_label("9,200", "Jetstar")))
    for h in (0, 1, 2):
        summary, sent, alerts = _run(tmp_path, monkeypatch, fetch,
                                     now=NOW + timedelta(hours=h))
    assert len(calls) == 1 and sent == [] and alerts == []


def test_a_bigger_drop_after_an_unmatched_check_is_sent_without_a_new_request(
        tmp_path, monkeypatch):
    """24 小時內查過、但沒有同航空公司的直飛價：快取再降一截照常發（不漏發），不重查。"""
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "k")
    fetch, calls = _counting(_page(_label("7,000", "Tigerair Taiwan")))
    _run(tmp_path, monkeypatch, fetch)
    cheaper = [Offer("KHH", "NRT", DEP, RET, 6900.0, "TWD", "GK", 0, "180")]
    summary, sent, alerts = _run(tmp_path, monkeypatch, fetch, offers=cheaper,
                                 now=NOW + timedelta(hours=1))
    assert len(calls) == 1 and len(sent) == 1
    assert alerts == [(7761.0, "unverified"), (6900.0, "unverified")]


def test_a_connecting_candidate_is_not_sent_to_bright_data(tmp_path, monkeypatch):
    """Bright Data 只寫直飛價：轉機的候選查了也對不上，白花一次。"""
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "k")
    fetch, calls = _counting(_page(_label("7,000", "Jetstar")))
    offers = [Offer("KHH", "NRT", DEP, RET, 7761.0, "TWD", "GK", 1, "480")]
    summary, sent, alerts = _run(tmp_path, monkeypatch, fetch, offers=offers)
    assert alerts == [(7761.0, "unverified")]
    assert summary["brightdata"]["alerts_checked"] == 0
    assert (DEP, RET) not in {c[2:] for c in calls}


def test_no_sweep_when_every_aviasales_search_failed(tmp_path, monkeypatch):
    """全部失敗的輪次 exit 1、不 commit prices.db：巡檢的帳本會消失、下一輪又照算額度。"""
    from farehunter.travelpayouts import TravelpayoutsError
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "k")
    fetch, calls = _counting(_page(_label("7,000", "Jetstar")))

    def down(self, *a, **k):
        raise TravelpayoutsError(401, "Unauthorized")

    summary, sent, alerts = _run(tmp_path, monkeypatch, fetch, search=down)
    assert runner_mod.all_searches_failed(summary)
    assert calls == [] and summary["brightdata"]["requests"] == 0


def test_an_unexpected_error_in_the_sweep_does_not_stop_the_run(tmp_path, monkeypatch):
    """巡檢出意外也要把整輪跑完（健康檢查、匯出照常）。"""
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "k")

    def broken(self, *a):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(bd.Verifier, "sweep", broken)
    summary, sent, alerts = _run(tmp_path, monkeypatch,
                                 lambda *a: _page(_label("7,650", "Jetstar")))
    assert alerts == [(7650.0, "verified")]        # 警報驗證在巡檢之前，照常
    assert summary["health"] is not None and "brightdata" in summary


def test_runner_sweeps_cheapest_trips_after_fetching(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "k")
    asked = []

    def fetch(o, d, dep, ret):
        asked.append(dep)
        return _page(_label("12,000", "Jetstar"))

    offers = [Offer("KHH", "NRT", (NOW.date() + timedelta(days=n)).isoformat(),
                    (NOW.date() + timedelta(days=n + 5)).isoformat(),
                    9000.0 + n, "TWD", "GK", 0, "180") for n in (30, 31, 32)]
    summary, sent, alerts = _run(tmp_path, monkeypatch, fetch, offers=offers)
    # 便宜的先查，沒有警報要驗證；快取只有 3 天，其餘用固定日期補到 ROUTE_MIN_TRIPS
    assert asked[:3] == [o.depart_date for o in offers]
    assert summary["brightdata"]["swept"] == bd.ROUTE_MIN_TRIPS == len(asked)
    assert summary["brightdata"]["recorded"] == bd.ROUTE_MIN_TRIPS
    con = sqlite3.connect(tmp_path / "prices.db")
    assert con.execute("SELECT SUM(requests) FROM api_usage").fetchone()[0] == bd.ROUTE_MIN_TRIPS
    con.close()


def test_the_runner_hands_the_line_budget_to_notify(tmp_path, monkeypatch):
    """LINE 配速要靠 runner 把 LineBudget 交給 notify；漏傳就等於沒有配速。"""
    got = []
    _run(tmp_path, monkeypatch, lambda *a: None)
    monkeypatch.setattr(runner_mod, "notify",
                        lambda o, v, st=None, **k: got.append(k) or ["line"])
    cheaper = [Offer("KHH", "NRT", DEP, RET, 5000.0, "TWD", "GK", 0, "180")]
    cfg = tmp_path / "config.yaml"
    monkeypatch.setattr(runner_mod, "parse_offers", lambda *a, **k: list(cheaper))
    runner_mod.run(str(cfg), str(tmp_path / "prices.db"), now=NOW)
    assert got and isinstance(got[0].get("line_budget"), runner_mod.LineBudget)


def test_a_thin_route_is_topped_up_with_fixed_dates(tmp_path):
    """冷門線的快取只有幾天：補固定日期到 ROUTE_MIN_TRIPS，快取裡的日期先查；
    快取日期夠多的航線不補（2026-11-11：五條高雄冷門線每天只被查 2–8 次）。"""
    st = _store(tmp_path)
    asked = []

    def fetch(o, d, dep, ret):
        asked.append((o, d, dep))
        return _page(_label("9,000", "Tigerair Taiwan"))

    thin = [_offer("KHH", "OKA", _day(30 + i), 6000 + i, carriers="IT") for i in range(2)]
    rich = [_offer("KHH", "NRT", _day(30 + i), 8000 + i) for i in range(bd.ROUTE_MIN_TRIPS + 3)]
    v = bd.Verifier(st, NOW, allowance=100, fetcher=fetch)
    v.sweep(thin + rich, [("KHH", "OKA"), ("KHH", "NRT")])
    oka = [dep for o, d, dep in asked if d == "OKA"]
    nrt = [dep for o, d, dep in asked if d == "NRT"]
    assert oka[:2] == [_day(30), _day(31)] and len(oka) == bd.ROUTE_MIN_TRIPS
    assert len(set(oka)) == len(oka)                         # 補的日期不重複
    assert sorted(nrt) == sorted(o.depart_date for o in rich)  # 夠多的航線只查快取裡的


# ---- 傳統航空（非廉航）的直飛最低價（2026-11-12）-----------------------------------
def _its(*rows):
    return bd.parse(_page(*[_label(p, a) for p, a in rows]))


def test_the_cheapest_full_service_nonstop_is_recorded_as_its_own_class():
    """頁面本來就列出所有航空公司：順便記下傳統航空的直飛最低價（fare_class='full'），
    給之後「不要廉航」的那組通知用；廉航的 'any' 列照舊。"""
    offers, matched = bd.select("KHH", "NRT", DEP, RET, _its(
        ("8,000", "Tigerair Taiwan"), ("12,729", "China Airlines"), ("13,400", "EVA Air")), "IT")
    assert [(o.price, o.carriers, o.fare_class) for o in offers] == [
        (8000.0, "IT", "any"), (12729.0, "CI", "full")]
    assert (matched.carriers, matched.fare_class) == ("IT", "any")


def test_no_full_service_row_when_only_low_cost_carriers_fly():
    offers, _ = bd.select("KHH", "NRT", DEP, RET, _its(("8,000", "Tigerair Taiwan"), ("8,500", "Jetstar")))
    assert [o.fare_class for o in offers] == ["any"]


def test_a_full_service_carrier_that_is_also_the_cheapest_gets_both_rows():
    """跟 Aviasales 的寫法一致：全場最低剛好是傳統航空時，'any' 與 'full' 各一筆。
    想驗證的那家也是它時，回傳的仍是 'any' 那筆。"""
    offers, matched = bd.select("KHH", "OKA", DEP, RET, _its(
        ("10,011", "Starlux Airlines"), ("10,500", "China Airlines")), "JX")
    assert [(o.price, o.carriers, o.fare_class) for o in offers] == [
        (10011.0, "JX", "any"), (10011.0, "JX", "full")]
    assert matched.fare_class == "any"


def test_full_rows_stay_out_of_the_any_class_readers(tmp_path):
    """full 列只給傳統航空用：路線統計、便宜日、看板主價讀的都是 'any'。"""
    st = _store(tmp_path)
    v = bd.Verifier(st, NOW, allowance=1, fetcher=lambda *a: _page(
        _label("8,000", "Tigerair Taiwan"), _label("12,729", "China Airlines")))
    v.check("KHH", "NRT", DEP, RET, "IT")
    rows = st.conn.execute("SELECT fare_class, carriers, price FROM observations ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("any", "IT", 8000.0), ("full", "CI", 12729.0)]
    assert st.route_stats("KHH", "NRT")["n"] == 1 and v.stats["recorded"] == 2
