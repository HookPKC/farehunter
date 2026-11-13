"""ChocoData 補冷門線（ADR 0005）的測試。零真實 API：一律注入假的 fetch／session。"""
from __future__ import annotations

import logging
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from farehunter import chocodata_flights as cd                      # noqa: E402
from farehunter import brightdata_flights as bd                     # noqa: E402
import farehunter.runner as runner_mod                              # noqa: E402
from farehunter.models import Offer                                 # noqa: E402
from farehunter.storage import Store                                # noqa: E402

NOW = datetime(2026, 11, 15, 6, 17, tzinfo=timezone.utc)
KEY = "asa_live_ab+c/d=1"


def _d(n):
    return (NOW.date() + timedelta(days=n)).isoformat()


def _label(price, airline, nonstop=True):
    stop = "Nonstop flight" if nonstop else "1 stop flight"
    return (f'<li aria-label="From {price} New Taiwan dollars round trip total. {stop} '
            f'with {airline}. Leaves Kaohsiung at 6:50 AM."></li>')


def _page(*labels):
    return "<html><body>" + "".join(labels) + "</body></html>"


def _store(tmp_path):
    return Store(str(tmp_path / "prices.db"))


class _Resp:
    def __init__(self, status=200, body=None, text=None):
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class _Session:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp, exc, []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        if self.exc:
            raise self.exc
        return self.resp


# ---- fetch：格式與金鑰遮罩 --------------------------------------------------------

def test_fetch_sends_the_bright_data_url_and_returns_the_html(monkeypatch):
    monkeypatch.setenv("CHOCODATA_API_KEY", KEY)
    s = _Session(_Resp(200, {"url": "x", "content_type": "html", "html": _page(_label("8,310", "Tigerair Taiwan"))}))
    page = cd.fetch("KHH", "NGO", _d(20), _d(25), session=s)
    assert "8,310" in page
    url, kw = s.calls[0]
    assert url == cd.API_URL
    assert kw["params"] == {"api_key": KEY, "url": bd.search_url("KHH", "NGO", _d(20), _d(25))}


def test_transport_errors_never_carry_the_url_or_key(monkeypatch):
    """requests 的例外原文含完整網址＝含金鑰；repo 公開，紀錄也公開。"""
    monkeypatch.setenv("CHOCODATA_API_KEY", KEY)
    exc = requests.ConnectionError(f"https://api.chocodata.com/x?api_key={KEY}&url=…")
    with pytest.raises(cd.ChocoDataError) as e:
        cd.fetch("KHH", "NGO", _d(20), _d(25), session=_Session(exc=exc))
    assert str(e.value) == "transport: ConnectionError"
    assert e.value.__cause__ is None


@pytest.mark.parametrize("echo", [KEY, "asa_live_ab%2Bc%2Fd%3D1", "asa_live_ab%2Bc%2Fd%3D1".replace("%2F", "/")])
def test_http_errors_are_redacted(monkeypatch, echo):
    monkeypatch.setenv("CHOCODATA_API_KEY", KEY)
    s = _Session(_Resp(500, {"error": "upstream", "message": f"failed for api_key={echo}"}))
    with pytest.raises(cd.ChocoDataError) as e:
        cd.fetch("KHH", "NGO", _d(20), _d(25), session=s)
    assert "HTTP 500" in str(e.value) and "ab+c" not in str(e.value) and "ab%2B" not in str(e.value)


@pytest.mark.parametrize("resp", [
    _Resp(402, {"error": "payment_required", "message": "Top up"}),
    _Resp(403, {"error": "insufficient_credits", "message": "Your balance is 0"}),
])
def test_out_of_credits_is_its_own_error(monkeypatch, resp):
    monkeypatch.setenv("CHOCODATA_API_KEY", KEY)
    with pytest.raises(cd.ChocoDataExhausted):
        cd.fetch("KHH", "NGO", _d(20), _d(25), session=_Session(resp))


def test_a_200_without_html_is_an_error(monkeypatch):
    monkeypatch.setenv("CHOCODATA_API_KEY", KEY)
    with pytest.raises(cd.ChocoDataError):
        cd.fetch("KHH", "NGO", _d(20), _d(25), session=_Session(_Resp(200, {"content_type": "json"})))


def test_configured_needs_the_key(monkeypatch):
    monkeypatch.delenv("CHOCODATA_API_KEY", raising=False)
    assert not cd.configured()
    monkeypatch.setenv("CHOCODATA_API_KEY", "  ")
    assert not cd.configured()


# ---- 額度 ------------------------------------------------------------------------

def _use(st, n, day):
    for _ in range(n):
        st.record_api_usage(cd.PROVIDER, day, ok=True)


def test_allowance_is_capped_per_run_per_day_and_in_total(tmp_path):
    st = _store(tmp_path)
    assert cd.allowance(st, NOW) == cd.PER_RUN_MAX
    _use(st, cd.DAILY_MAX - 1, NOW.date().isoformat())
    assert cd.allowance(st, NOW) == 1                       # 今天只剩 1 頁
    _use(st, 1, NOW.date().isoformat())
    assert cd.allowance(st, NOW) == 0
    (tmp_path / "b").mkdir()
    st2 = _store(tmp_path / "b")
    _use(st2, cd.TOTAL_MAX - 1, "2026-11-13")
    assert cd.allowance(st2, NOW) == 1                       # 一次性額度只剩 1 頁
    _use(st2, 1, "2026-11-14")
    assert cd.allowance(st2, NOW) == 0


def test_bright_data_usage_does_not_count(tmp_path):
    st = _store(tmp_path)
    for _ in range(40):
        st.record_api_usage(bd.PROVIDER, NOW.date().isoformat(), ok=True)
    assert cd.allowance(st, NOW) == cd.PER_RUN_MAX


# ---- 巡檢 ------------------------------------------------------------------------

def test_check_records_google_rows_under_its_own_provider(tmp_path):
    st = _store(tmp_path)
    sw = cd.ColdSweeper(st, NOW, left=1, fetcher=lambda *a: _page(
        _label("16,710", "Tigerair Taiwan"), _label("19,000", "China Airlines")))
    sw.check("KHH", "NGO", _d(20), _d(25))
    rows = st.conn.execute(
        "SELECT price, carriers, fare_class, source, provider FROM observations ORDER BY price").fetchall()
    assert [tuple(r) for r in rows] == [(16710.0, "IT", "any", "google", "chocodata"),
                                        (19000.0, "CI", "full", "google", "chocodata")]
    assert st.api_usage_since(cd.PROVIDER, "2026-11-01") == 1
    assert st.api_usage_since(bd.PROVIDER, "2026-11-01") == 0
    assert sw.stats["ok"] == 1 and sw.stats["recorded"] == 2


def test_only_cold_routes_are_swept(tmp_path):
    st = _store(tmp_path)
    asked = []
    sw = cd.ColdSweeper(st, NOW, left=2,
                        fetcher=lambda o, d, dep, ret: asked.append((o, d)) or _page(_label("9,000", "Tigerair Taiwan")))
    hot = [Offer("TPE", "NRT", _d(20), _d(25), 5000.0, "TWD", "MM", 0, "180"),
           Offer("KHH", "NRT", _d(21), _d(26), 5100.0, "TWD", "IT", 0, "180")]
    sw.sweep(hot)
    assert asked and all(r in cd.COLD_ROUTES for r in asked)
    assert len(asked) == 2 and sw.left == 0


def test_cache_dates_go_first_and_fresh_trips_are_skipped(tmp_path):
    st = _store(tmp_path)
    # Bright Data 這輪剛查過的那趟不重查
    st.record_api_check(bd.PROVIDER, "KHH", "NGO", _d(30), _d(35), NOW.isoformat(timespec="seconds"))
    asked = []
    sw = cd.ColdSweeper(st, NOW, left=2,
                        fetcher=lambda o, d, dep, ret: asked.append((o, d, dep)) or _page(_label("9,000", "Tigerair Taiwan")))
    cands = [Offer("KHH", "NGO", _d(30), _d(35), 9000.0, "TWD", "IT", 0, "180"),
             Offer("KHH", "NGO", _d(31), _d(36), 9100.0, "TWD", "IT", 0, "180")]
    sw.queues = lambda c, _q=sw.queues: {r: q for r, q in _q(c).items() if r == ("KHH", "NGO")}
    sw.sweep(cands)
    assert asked[0] == ("KHH", "NGO", _d(31))


def test_a_trip_with_a_recent_google_price_is_skipped(tmp_path):
    st = _store(tmp_path)
    st.record(Offer("KHH", "NGO", _d(31), _d(36), 9100.0, "TWD", "IT", 0, "", source="google",
                    provider="serpapi"), observed_at=(NOW - timedelta(hours=3)).isoformat())
    sw = cd.ColdSweeper(st, NOW, left=1, fetcher=lambda *a: _page(_label("9,000", "Tigerair Taiwan")))
    since = (NOW - timedelta(hours=24)).isoformat(timespec="seconds")
    assert sw._fresh("KHH", "NGO", _d(31), _d(36), since)
    assert not sw._fresh("KHH", "NGO", _d(32), _d(37), since)


def test_out_of_credits_stops_the_run(tmp_path):
    st = _store(tmp_path)
    calls = []

    def fetch(*a):
        calls.append(a)
        raise cd.ChocoDataExhausted("HTTP 402 payment_required: Top up")

    sw = cd.ColdSweeper(st, NOW, left=2, fetcher=fetch)
    sw.sweep([])
    assert len(calls) == 1 and sw.stats["exhausted"] and sw.left == 1


def test_errors_are_logged_without_raising(tmp_path, caplog):
    st = _store(tmp_path)

    def boom(*a):
        raise ValueError(f"oops {KEY}")

    sw = cd.ColdSweeper(st, NOW, left=1, fetcher=boom)
    with caplog.at_level(logging.WARNING):
        sw.check("KHH", "CTS", _d(20), _d(25))
    assert sw.stats["errors"] == 1 and st.api_usage_since(cd.PROVIDER, "2026-11-01") == 1
    assert KEY not in caplog.text and "ValueError" in caplog.text


def test_an_unparsable_page_is_an_error_but_counts_as_checked(tmp_path):
    st = _store(tmp_path)
    sw = cd.ColdSweeper(st, NOW, left=1, fetcher=lambda *a: "<html>captcha</html>")
    sw.check("KHH", "CTS", _d(20), _d(25))
    assert sw.stats["errors"] == 1
    assert st.api_checked_since(cd.PROVIDER, "KHH", "CTS", _d(20), _d(25),
                                (NOW - timedelta(hours=1)).isoformat())


# ---- runner 整合 ------------------------------------------------------------------

def _run(tmp_path, monkeypatch, fetch, offers=None):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "defaults:\n  currency: twd\n  months_ahead: 1\n  pause_seconds: 0\n"
        "routes:\n  - origin: KHH\n    destination: NGO\n    absolute_threshold: 1000\n",
        encoding="utf-8")
    offers = offers or [Offer("KHH", "NGO", _d(30), _d(35), 9000.0, "TWD", "IT", 0, "180")]
    monkeypatch.setattr(runner_mod, "TravelpayoutsClient",
                        type("C", (), {"__init__": lambda s, *a, **k: None,
                                       "search_month": lambda s, *a, **k: {}}))
    monkeypatch.setattr(runner_mod, "parse_offers", lambda *a, **k: list(offers))
    monkeypatch.setattr(runner_mod, "notify", lambda o, v, st=None, **k: ["line"])
    monkeypatch.setattr(runner_mod, "channels_configured", lambda: True)
    monkeypatch.setattr(cd, "fetch", fetch)
    monkeypatch.delenv("BRIGHTDATA_API_KEY", raising=False)
    monkeypatch.setenv("FAREHUNTER_FORCE", "1")
    return runner_mod.run(str(cfg), str(tmp_path / "prices.db"), now=NOW)


def test_without_a_key_the_runner_never_calls_chocodata(tmp_path, monkeypatch):
    def forbidden(*a):
        raise AssertionError("沒有金鑰不得連外")

    monkeypatch.delenv("CHOCODATA_API_KEY", raising=False)
    summary = _run(tmp_path, monkeypatch, forbidden)
    assert "chocodata" not in summary


def test_runner_sweeps_cold_routes_with_chocodata(tmp_path, monkeypatch):
    monkeypatch.setenv("CHOCODATA_API_KEY", "k")
    asked = []

    def fetch(o, d, dep, ret):
        asked.append((o, d, dep))
        return _page(_label("9,500", "Tigerair Taiwan"))

    monkeypatch.setattr(cd, "PER_RUN_MAX", 4)             # 四條線輪流各一趟
    summary = _run(tmp_path, monkeypatch, fetch)
    assert summary["chocodata"]["requests"] == cd.PER_RUN_MAX == len(asked)
    assert {(o, d) for o, d, _ in asked} == set(cd.COLD_ROUTES)
    assert ("KHH", "NGO", _d(30)) in asked                  # NGO 先查快取裡的那趟
    con = sqlite3.connect(tmp_path / "prices.db")
    n = con.execute("SELECT COUNT(*) FROM observations WHERE provider='chocodata'").fetchone()[0]
    con.close()
    assert n == cd.PER_RUN_MAX


def test_a_broken_sweeper_does_not_stop_the_run(tmp_path, monkeypatch):
    monkeypatch.setenv("CHOCODATA_API_KEY", "k")

    def broken(self, candidates):
        raise RuntimeError("bug")

    monkeypatch.setattr(cd.ColdSweeper, "sweep", broken)
    summary = _run(tmp_path, monkeypatch, lambda *a: _page())
    assert summary["health"] is not None and "chocodata" in summary
