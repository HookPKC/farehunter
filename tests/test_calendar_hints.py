"""Picker 線索（calendar_hints、gflights2.refresh_hints、兩個巡檢的排序）的測試。零真實 API。"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from farehunter import brightdata_flights as bd                     # noqa: E402
from farehunter import calendar_hints as ch                         # noqa: E402
from farehunter import chocodata_flights as cd                      # noqa: E402
from farehunter import gflights2_flights as g2                      # noqa: E402
import farehunter.runner as runner_mod                              # noqa: E402
from farehunter.models import Offer                                 # noqa: E402
from farehunter.storage import Store                                # noqa: E402

NOW = datetime(2026, 11, 16, 6, 17, tzinfo=timezone.utc)            # 週一
AT = NOW.isoformat(timespec="seconds")


def _d(n):
    return (NOW.date() + timedelta(days=n)).isoformat()


def _store(tmp_path):
    return Store(str(tmp_path / "prices.db"))


def _hints(st, o, d, prices, fetched=AT):
    """prices：{幾天後出發: 價格}，一律住 5 晚。"""
    st.record_calendar_hints(o, d, [(_d(n), _d(n + 5), p) for n, p in prices.items()], fetched)


def _page(price="9,000", airline="Tigerair Taiwan", nonstop=True):
    stop = "Nonstop flight" if nonstop else "1 stop flight"
    return (f'<html><li aria-label="From {price} New Taiwan dollars round trip total. {stop} '
            f'with {airline}. Leaves Kaohsiung at 6:50 AM."></li></html>')


# ---- 挑日子 ------------------------------------------------------------------------

def test_pick_returns_the_cheapest_days_first(tmp_path):
    st = _store(tmp_path)
    _hints(st, "KHH", "OKA", {10: 9000, 11: 6000, 12: 7000, 13: 8000})
    assert ch.pick(st, NOW, "KHH", "OKA", limit=3) == [(_d(11), _d(16)), (_d(12), _d(17)), (_d(13), _d(18))]


def test_pick_skips_weekdays_without_direct_flights_and_prices_over_the_cap(tmp_path):
    st = _store(tmp_path)
    _hints(st, "KHH", "NGO", {10: 5000, 11: 6000, 12: 7000, 13: 20000})
    avoid = frozenset({date.fromisoformat(_d(10)).weekday()})
    assert ch.pick(st, NOW, "KHH", "NGO", avoid=avoid, cap=10000) == [(_d(11), _d(16)), (_d(12), _d(17))]


def test_pick_ignores_stale_hints_and_dates_too_close(tmp_path):
    st = _store(tmp_path)
    old = (NOW - timedelta(days=ch.STALE_DAYS + 1)).isoformat(timespec="seconds")
    _hints(st, "KHH", "OKA", {10: 5000}, fetched=old)
    _hints(st, "KHH", "FUK", {1: 5000, 9: 6000})
    assert ch.pick(st, NOW, "KHH", "OKA") == []
    assert ch.pick(st, NOW, "KHH", "FUK") == [(_d(9), _d(14))]


def test_windows_are_consecutive_and_cover_about_four_months():
    w = ch.windows(NOW.date())
    assert len(w) == ch.WINDOWS and w[0][0] == NOW.date() + timedelta(days=ch.LEAD_DAYS)
    assert w[1][0] == w[0][1] + timedelta(days=1)
    assert (w[-1][1] - w[0][0]).days + 1 == ch.WINDOWS * ch.WINDOW_DAYS


def test_cts_is_not_a_picker_route():
    """實測 KHH-CTS 4 天有 3 天是轉機價：不抓，繼續用固定日期。"""
    assert ("KHH", "CTS") not in ch.ROUTES


# ---- 抓取 ------------------------------------------------------------------------

class _Resp:
    def __init__(self, body, status=200):
        self.status_code, self._body, self.text = status, body, ""

    def json(self):
        return self._body


class _Session:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


def test_fetch_picker_keeps_only_well_formed_rows(monkeypatch):
    monkeypatch.setenv("RAPIDAPI_KEY", "secret-key")
    body = {"status": True, "data": [
        {"departure": _d(10), "return": _d(15), "price": 6249},
        {"departure": _d(11), "return": _d(17), "price": 7000},          # 不是 5 晚
        {"departure": _d(12), "return": _d(17), "price": "unavailable"},
        {"departure": _d(300), "return": _d(305), "price": 5000},        # 範圍外
        {"departure": "garbage", "return": _d(15), "price": 5000},
        {"departure": _d(13), "return": _d(18), "price": 0}]}
    s = _Session(_Resp(body))
    rows = g2.fetch_picker("KHH", "OKA", _d(3), _d(64), nights=5, session=s)
    assert rows == [(_d(10), _d(15), 6249.0)]
    url, kw = s.calls[0]
    assert url == g2.PICKER_URL and "secret-key" not in url and "secret-key" not in str(kw["params"])
    assert kw["params"]["trip_days"] == "5" and kw["params"]["departure_id"] == "KHH"
    with pytest.raises(g2.GFlights2Error) as e:
        g2.fetch_picker("KHH", "OKA", _d(3), _d(64), nights=5,
                        session=_Session(requests.ConnectionError("x secret-key")))
    assert "secret-key" not in str(e.value)


def _full_window(start, end, nights=5, price=8000.0):
    out, d = [], date.fromisoformat(start)
    while d <= date.fromisoformat(end):
        out.append((d.isoformat(), (d + timedelta(days=nights)).isoformat(), price))
        d += timedelta(days=1)
    return out


def test_refresh_fetches_one_due_window_per_run_and_then_rests(tmp_path):
    st = _store(tmp_path)
    calls = []

    def picker(o, d, start, end, *, nights):
        calls.append((o, d, start))
        return _full_window(start, end)

    seen = []
    for h in range(8):                       # 8 輪（跨 3 天，避開每天上限）
        now = NOW + timedelta(days=h // 3, hours=h % 3)
        seen.append(g2.refresh_hints(st, now, fetcher=picker))
    assert len(calls) == len(ch.ROUTES) * ch.WINDOWS            # 每段各抓一次
    assert {(o, d) for o, d, _ in calls} == set(ch.ROUTES)
    assert seen[-1]["skipped"] == "fresh" and seen[-1]["requests"] == 0
    assert st.api_usage_since(g2.PROVIDER, "2026-11-01") == len(calls)
    # 一週後才重抓
    again = g2.refresh_hints(st, NOW + timedelta(days=ch.REFRESH_DAYS + 3), fetcher=picker)
    assert again["requests"] == 1


def test_refresh_leaves_the_quota_for_alert_verification(tmp_path):
    st = _store(tmp_path)

    def forbidden(*a, **k):
        raise AssertionError("額度要留給警報驗證")

    for _ in range(g2.PICKER_DAILY_CEILING):
        st.record_api_usage(g2.PROVIDER, NOW.date().isoformat(), ok=True)
    assert g2.refresh_hints(st, NOW, fetcher=forbidden)["skipped"] == "quota"
    st2 = Store(str(tmp_path / "b.db"))
    for _ in range(g2.PICKER_WINDOW_CEILING):
        st2.record_api_usage(g2.PROVIDER, (NOW.date() - timedelta(days=5)).isoformat(), ok=True)
    assert g2.refresh_hints(st2, NOW, fetcher=forbidden)["skipped"] == "quota"
    assert g2.Verifier(st2, NOW).left == g2.PER_RUN_MAX          # 警報驗證還能用


def test_a_failed_refresh_is_counted_and_changes_nothing(tmp_path):
    st = _store(tmp_path)

    def boom(*a, **k):
        raise g2.GFlights2Error("HTTP 500")

    stats = g2.refresh_hints(st, NOW, fetcher=boom)
    assert stats["errors"] == 1 and st.api_usage_since(g2.PROVIDER, "2026-11-01") == 1
    assert st.conn.execute("SELECT COUNT(*) FROM calendar_hints").fetchone()[0] == 0
    assert g2.refresh_hints(st, NOW, fetcher=lambda *a, **k: [])["errors"] == 1   # 空回應也算失敗


# ---- 線索價絕不變成價格 ------------------------------------------------------------

def test_hints_never_reach_observations(tmp_path):
    st = _store(tmp_path)
    g2.refresh_hints(st, NOW, fetcher=lambda o, d, s, e, *, nights: _full_window(s, e, price=4321.0))
    assert st.conn.execute("SELECT COUNT(*) FROM calendar_hints").fetchone()[0] > 50
    assert st.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    assert st.conn.execute("SELECT COUNT(*) FROM observations_all").fetchone()[0] == 0


def test_a_hinted_day_without_a_direct_flight_leaves_no_price(tmp_path):
    """使用者的要求：只要直飛。線索日查不到直飛實價 → 不寫入任何價格、不會有警報可發。"""
    st = _store(tmp_path)
    _hints(st, "KHH", "FUK", {20: 5000})
    asked = []
    v = bd.Verifier(st, NOW, allowance=1, clock=lambda: 0.0,
                    fetcher=lambda o, d, dep, ret: asked.append((o, d, dep)) or _page(nonstop=False))
    v.sweep([], [("KHH", "FUK")])
    assert asked == [("KHH", "FUK", _d(20))]
    assert st.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    # 那個星期幾記成「沒有直飛」，下一輪同一個星期幾的線索不再挑
    _hints(st, "KHH", "FUK", {27: 4000, 28: 9000})
    later = NOW + timedelta(hours=1)
    avoid = bd.Verifier(st, later, allowance=0).weekday_outcomes("KHH", "FUK").avoid
    assert date.fromisoformat(_d(20)).weekday() in avoid
    assert ch.pick(st, later, "KHH", "FUK", avoid=avoid) == [(_d(28), _d(33))]


# ---- 巡檢的排序 --------------------------------------------------------------------

def test_bright_data_checks_hinted_days_before_cache_and_fixed_dates(tmp_path):
    st = _store(tmp_path)
    _hints(st, "KHH", "OKA", {30: 6000, 31: 5000, 32: 30000})
    asked = []
    v = bd.Verifier(st, NOW, allowance=3, clock=lambda: 0.0,
                    fetcher=lambda o, d, dep, ret: asked.append(dep) or _page())
    v.hint_caps = {("KHH", "OKA"): 6500 * ch.CAP_RATIO}
    cache = [Offer("KHH", "OKA", _d(40), _d(45), 5500.0, "TWD", "IT", 0, "100")]
    v.sweep(cache, [("KHH", "OKA")])
    assert asked == [_d(31), _d(30), _d(40)]          # 線索由低到高 → 快取；30,000 超過上限不排
    rows = st.conn.execute("SELECT price, source, provider FROM observations").fetchall()
    assert [tuple(r) for r in rows] == [(9000.0, "google", "brightdata")] * 3   # 寫的是查到的直飛實價


def test_routes_without_hints_behave_as_before(tmp_path):
    st = _store(tmp_path)
    _hints(st, "KHH", "OKA", {30: 6000})
    asked = []
    v = bd.Verifier(st, NOW, allowance=2, clock=lambda: 0.0,
                    fetcher=lambda o, d, dep, ret: asked.append((d, dep)) or _page())
    v.sweep([], [("KHH", "CTS")])
    want = [dep for dep, _ in bd.anchor_trips(NOW, 2)]
    assert asked == [("CTS", want[0]), ("CTS", want[1])]


def test_chocodata_also_starts_with_hinted_days_and_skips_what_bright_data_checked(tmp_path):
    st = _store(tmp_path)
    _hints(st, "KHH", "NGO", {30: 6000, 31: 5000})
    st.record(Offer("KHH", "NGO", _d(31), _d(36), 5000.0, "TWD", "IT", 0, "", source="google",
                    provider="brightdata"), observed_at=AT)                  # Bright Data 這輪查過最便宜的
    asked = []
    sw = cd.ColdSweeper(st, NOW, left=1,
                        fetcher=lambda o, d, dep, ret: asked.append((d, dep)) or _page())
    sw.queues = lambda c, _q=sw.queues: {r: q for r, q in _q(c).items() if r == ("KHH", "NGO")}
    sw.sweep([])
    assert asked == [("NGO", _d(30))] and sw.stats["hinted"] == 2


# ---- runner ------------------------------------------------------------------------

def _run(tmp_path, monkeypatch, picker, *, key=True):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "defaults:\n  currency: twd\n  months_ahead: 1\n  pause_seconds: 0\n"
        "routes:\n  - origin: KHH\n    destination: OKA\n    absolute_threshold: 6500\n",
        encoding="utf-8")
    monkeypatch.setattr(runner_mod, "TravelpayoutsClient",
                        type("C", (), {"__init__": lambda s, *a, **k: None,
                                       "search_month": lambda s, *a, **k: {}}))
    far = Offer("KHH", "OKA", _d(50), _d(55), 9999.0, "TWD", "IT", 0, "100")
    monkeypatch.setattr(runner_mod, "parse_offers", lambda *a, **k: [far])
    monkeypatch.setattr(runner_mod, "notify", lambda *a, **k: ["line"])
    monkeypatch.setattr(runner_mod, "channels_configured", lambda: True)
    for name in ("BRIGHTDATA_API_KEY", "CHOCODATA_API_KEY", "RAPIDAPI_KEY"):
        monkeypatch.delenv(name, raising=False)
    if key:
        monkeypatch.setenv("RAPIDAPI_KEY", "k")
    monkeypatch.setattr(g2, "fetch_picker", picker)
    monkeypatch.setattr(g2, "fetch", lambda *a: {"status": True, "data": {}})
    monkeypatch.setenv("FAREHUNTER_FORCE", "1")
    return runner_mod.run(str(cfg), str(tmp_path / "prices.db"), now=NOW)


def test_runner_refreshes_hints_once_per_run(tmp_path, monkeypatch):
    calls = []

    def picker(o, d, start, end, *, nights):
        calls.append((o, d))
        return _full_window(start, end)

    summary = _run(tmp_path, monkeypatch, picker)
    assert len(calls) == 1 and summary["picker"]["hints"] == ch.WINDOW_DAYS


def test_runner_without_the_key_never_fetches_hints(tmp_path, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("沒有金鑰不得連外")

    summary = _run(tmp_path, monkeypatch, forbidden, key=False)
    assert "picker" not in summary


def test_a_broken_picker_does_not_stop_the_run(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug")

    summary = _run(tmp_path, monkeypatch, boom)
    assert summary["health"] is not None and summary["picker"]["errors"] == 1
