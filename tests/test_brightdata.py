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


def test_allowance_comes_from_this_months_ledger(tmp_path):
    st = _store(tmp_path)
    st.conn.execute("INSERT INTO api_usage VALUES ('brightdata', '2026-10-31', 4000, 4000)")
    st.conn.commit()
    # 11/15 06:17 到月底約 378 小時：ceil(4750 / 378) = 13。上個月的 4,000 若被算進來會是 2
    assert bd.Verifier(st, NOW).left == 13
    st.conn.execute("INSERT INTO api_usage VALUES ('brightdata', '2026-11-02', 4740, 4740)")
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
    # 超過 24 小時的紀錄（31 天後那趟，25 小時前查的）會被清掉，表不會一直長
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

def _run(tmp_path, monkeypatch, fetch, offers=None, threshold=8000):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "defaults:\n  currency: twd\n  months_ahead: 1\n  pause_seconds: 0\n"
        "routes:\n  - origin: KHH\n    destination: NRT\n"
        f"    absolute_threshold: {threshold}\n", encoding="utf-8")
    offers = offers or [Offer("KHH", "NRT", DEP, RET, 7761.0, "TWD", "GK", 0, "180")]
    monkeypatch.setattr(runner_mod, "TravelpayoutsClient",
                        type("C", (), {"__init__": lambda s, *a, **k: None,
                                       "search_month": lambda s, *a, **k: {}}))
    monkeypatch.setattr(runner_mod, "parse_offers", lambda *a, **k: list(offers))
    sent = []
    monkeypatch.setattr(runner_mod, "notify",
                        lambda o, v, st=None: sent.append((o, v, st)) or ["line"])
    monkeypatch.setattr(runner_mod, "channels_configured", lambda: True)
    monkeypatch.setattr(bd, "fetch", fetch)
    monkeypatch.setenv("FAREHUNTER_FORCE", "1")
    summary = runner_mod.run(str(cfg), str(tmp_path / "prices.db"), now=NOW)
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
    assert asked == [o.depart_date for o in offers]          # 便宜的先查，沒有警報要驗證
    assert summary["brightdata"]["swept"] == 3 and summary["brightdata"]["recorded"] == 3
    con = sqlite3.connect(tmp_path / "prices.db")
    assert con.execute("SELECT SUM(requests) FROM api_usage").fetchone()[0] == 3
    con.close()
