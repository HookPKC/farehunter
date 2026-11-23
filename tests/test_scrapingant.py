"""ScrapingAnt 補冷門線（ADR 0005）的測試。零真實 API：一律注入假的 fetch／session／usage。"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from farehunter import brightdata_flights as bd                     # noqa: E402
from farehunter import chocodata_flights as cd                      # noqa: E402
from farehunter import scrapingant_flights as sa                    # noqa: E402
import farehunter.runner as runner_mod                              # noqa: E402
from farehunter.models import Offer                                 # noqa: E402
from farehunter.storage import Store                                # noqa: E402

NOW = datetime(2026, 11, 22, 6, 17, tzinfo=timezone.utc)
AT = NOW.isoformat(timespec="seconds")
KEY = "sa-live-ab+c/d=1"


def _d(n):
    return (NOW.date() + timedelta(days=n)).isoformat()


def _page(price="9,500", airline="Tigerair Taiwan", nonstop=True):
    stop = "Nonstop flight" if nonstop else "1 stop flight"
    return (f'<html><li aria-label="From {price} New Taiwan dollars round trip total. {stop} '
            f'with {airline}. Leaves Kaohsiung at 6:50 AM."></li></html>')


def _store(tmp_path):
    return Store(str(tmp_path / "prices.db"))


class _Resp:
    def __init__(self, status=200, text="", body=None):
        self.status_code, self.text, self._body = status, text, body

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


# ---- fetch：金鑰只在 header、錯誤遮罩 --------------------------------------------

def test_fetch_puts_the_key_in_a_header_and_returns_the_html(monkeypatch):
    monkeypatch.setenv("SCRAPINGANT_API_KEY", KEY)
    s = _Session(_Resp(200, _page()))
    assert sa.fetch("KHH", "NGO", _d(30), _d(35), session=s) == _page()
    (url, kw), = s.calls
    assert url == sa.API_URL and kw["headers"] == {"x-api-key": KEY}
    assert kw["params"] == {"url": bd.search_url("KHH", "NGO", _d(30), _d(35)), "browser": "true",
                            "proxy_country": "US"}     # 固定出口：避開歐洲 IP 的 Google 同意頁
    assert KEY not in url and KEY not in str(kw["params"])


def test_transport_errors_carry_only_the_exception_type(monkeypatch):
    monkeypatch.setenv("SCRAPINGANT_API_KEY", KEY)
    s = _Session(exc=requests.ConnectionError(f"failed url ...x-api-key={KEY}"))
    with pytest.raises(sa.ScrapingAntError) as e:
        sa.fetch("KHH", "NGO", _d(30), _d(35), session=s)
    assert str(e.value) == "transport: ConnectionError"


def test_http_errors_are_redacted_and_out_of_credits_is_its_own_error(monkeypatch):
    monkeypatch.setenv("SCRAPINGANT_API_KEY", KEY)
    with pytest.raises(sa.ScrapingAntError) as e:
        sa.fetch("KHH", "NGO", _d(30), _d(35), session=_Session(_Resp(500, f"bad key {KEY}")))
    assert KEY not in str(e.value) and "***" in str(e.value)
    with pytest.raises(sa.ScrapingAntExhausted):
        sa.fetch("KHH", "NGO", _d(30), _d(35),
                 session=_Session(_Resp(403, '{"detail":"API credits limit exceeded"}')))
    assert issubclass(sa.ScrapingAntExhausted, cd.ChocoDataExhausted)    # 巡檢會把它當成「本輪停」


def test_an_empty_200_is_an_error(monkeypatch):
    monkeypatch.setenv("SCRAPINGANT_API_KEY", KEY)
    with pytest.raises(sa.ScrapingAntError):
        sa.fetch("KHH", "NGO", _d(30), _d(35), session=_Session(_Resp(200, "")))


def test_remaining_reads_credits_and_the_period_end(monkeypatch):
    monkeypatch.setenv("SCRAPINGANT_API_KEY", KEY)
    s = _Session(_Resp(200, body={"plan_name": "Free", "plan_total_credits": 10000,
                                  "remained_credits": 9970, "start_date": "2026-11-22T01:15:13",
                                  "end_date": "2026-12-22T01:15:13"}))
    credits, end = sa.remaining(session=s)
    assert credits == 9970 and end == datetime(2026, 12, 22, 1, 15, 13, tzinfo=timezone.utc)
    assert s.calls[0][1]["headers"] == {"x-api-key": KEY}
    assert sa.remaining(session=_Session(_Resp(401, body={}))) is None
    assert sa.remaining(session=_Session(exc=requests.Timeout())) is None


# ---- 額度：照剩餘點數配速到重置日 ------------------------------------------------

def _use(st, n, day=None):
    for _ in range(n):
        st.record_api_usage(sa.PROVIDER, day or NOW.date().isoformat(), ok=True)


def test_allowance_spreads_the_remaining_pages_until_the_reset(tmp_path):
    st = _store(tmp_path)
    end = NOW + timedelta(days=30)
    # 9,970 點、留 500 → 947 頁、30 天 → 今天 31 頁；每輪最多 2
    assert sa.allowance(st, NOW, (9970, end)) == sa.PER_RUN_MAX
    _use(st, 30)
    assert sa.allowance(st, NOW, (9670, end)) == 1          # 今天已用 30：(917＋30)÷30＝31 → 再 1 頁
    _use(st, 1)
    assert sa.allowance(st, NOW, (9660, end)) == 0          # 今天的份用完
    assert sa.allowance(st, NOW, (sa.RESERVE_CREDITS + 5, end)) == 0      # 只剩緩衝
    (tmp_path / "x").mkdir()
    st2 = Store(str(tmp_path / "x" / "p.db"))
    assert sa.allowance(st2, NOW, (800, NOW + timedelta(hours=3))) == 2   # 快重置了：剩下的可以用掉


def test_allowance_falls_back_to_fixed_caps_without_usage(tmp_path):
    st = _store(tmp_path)
    assert sa.allowance(st, NOW, None) == sa.PER_RUN_MAX
    _use(st, sa.FALLBACK_DAILY_MAX)
    assert sa.allowance(st, NOW, None) == 0
    (tmp_path / "y").mkdir()
    st2 = Store(str(tmp_path / "y" / "p.db"))
    _use(st2, sa.WINDOW_MAX, (NOW.date() - timedelta(days=3)).isoformat())
    assert sa.allowance(st2, NOW, None) == 0


# ---- 巡檢 ------------------------------------------------------------------------

def test_sweeper_writes_google_rows_under_its_own_provider(tmp_path):
    st = _store(tmp_path)
    asked = []
    sw = sa.Sweeper(st, NOW, left=1, fetcher=lambda o, d, dep, ret: asked.append((o, d, dep)) or _page())
    sw.tiger_hints = {("KHH", "CTS"): [(_d(30), _d(35))]}
    sw.queues = lambda c, _q=sw.queues: {r: q for r, q in _q(c).items() if r == ("KHH", "CTS")}
    sw.sweep([])
    assert asked == [("KHH", "CTS", _d(30))]                        # 虎航線索先查
    rows = st.conn.execute("SELECT price, source, provider FROM observations").fetchall()
    assert [tuple(r) for r in rows] == [(9500.0, "google", "scrapingant")]
    assert st.api_usage_since(sa.PROVIDER, NOW.date().isoformat()) == 1
    assert st.api_usage_since(cd.PROVIDER, NOW.date().isoformat()) == 0


def test_trips_any_sweeper_checked_today_are_skipped_both_ways(tmp_path):
    st = _store(tmp_path)
    st.record_api_check(cd.PROVIDER, "KHH", "CTS", _d(30), _d(35), AT, parsed=True)    # ChocoData 查過
    asked = []
    sw = sa.Sweeper(st, NOW, left=1, fetcher=lambda o, d, dep, ret: asked.append(dep) or _page())
    sw.tiger_hints = {("KHH", "CTS"): [(_d(30), _d(35)), (_d(31), _d(36))]}
    sw.queues = lambda c, _q=sw.queues: {r: q for r, q in _q(c).items() if r == ("KHH", "CTS")}
    sw.sweep([])
    assert asked == [_d(31)]
    asked2 = []
    choco = cd.ColdSweeper(st, NOW, left=1, fetcher=lambda o, d, dep, ret: asked2.append(dep) or _page())
    choco.tiger_hints = {("KHH", "CTS"): [(_d(31), _d(36)), (_d(32), _d(37))]}
    choco.queues = lambda c, _q=choco.queues: {r: q for r, q in _q(c).items() if r == ("KHH", "CTS")}
    choco.sweep([])
    assert asked2 == [_d(32)]                                       # ScrapingAnt 剛查過的 31 號跳過


def test_another_sweepers_unparsable_page_does_not_block_but_its_own_does(tmp_path):
    """2026-11-22：ScrapingAnt 31 頁有 14 頁解析不到，原本會連帶讓 ChocoData 24 小時內不查那幾趟。"""
    st = _store(tmp_path)
    st.record_api_check(sa.PROVIDER, "KHH", "CTS", _d(30), _d(35), AT, parsed=False)   # ScrapingAnt 解析不到
    st.record_api_check(cd.PROVIDER, "KHH", "CTS", _d(31), _d(36), AT, parsed=False)   # ChocoData 自己解析不到
    asked = []
    choco = cd.ColdSweeper(st, NOW, left=1, fetcher=lambda o, d, dep, ret: asked.append(dep) or _page())
    choco.tiger_hints = {("KHH", "CTS"): [(_d(31), _d(36)), (_d(30), _d(35))]}
    choco.queues = lambda c, _q=choco.queues: {r: q for r, q in _q(c).items() if r == ("KHH", "CTS")}
    choco.sweep([])
    assert asked == [_d(30)]                                        # 31 號自己查過；30 號別家沒解析到 → 照查


def test_an_unparsable_page_logs_a_hint_not_the_page(tmp_path, caplog):
    import logging
    st = _store(tmp_path)
    page = "<html><head><title>Before you continue to Google</title></head><body>consent</body></html>"
    sw = sa.Sweeper(st, NOW, left=1, fetcher=lambda *a: page)
    sw.tiger_hints = {("KHH", "CTS"): [(_d(30), _d(35))]}
    sw.queues = lambda c, _q=sw.queues: {r: q for r, q in _q(c).items() if r == ("KHH", "CTS")}
    with caplog.at_level(logging.WARNING):
        sw.sweep([])
    assert "標題「Before you continue to Google」" in caplog.text and "同意頁" in caplog.text
    assert cd.page_hint(None) == "非文字（NoneType）"


def test_out_of_credits_stops_the_run(tmp_path):
    st = _store(tmp_path)
    calls = []

    def fetch(o, d, dep, ret):
        calls.append(dep)
        raise sa.ScrapingAntExhausted("HTTP 403: credits limit exceeded")

    sw = sa.Sweeper(st, NOW, left=2, fetcher=fetch)
    sw.sweep([])
    assert len(calls) == 1 and sw.stats["exhausted"] is True


# ---- runner ------------------------------------------------------------------------

def _run(tmp_path, monkeypatch, fetch, usage=(9970, NOW + timedelta(days=30))):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "defaults:\n  currency: twd\n  months_ahead: 1\n  pause_seconds: 0\n"
        "routes:\n  - origin: KHH\n    destination: NGO\n    absolute_threshold: 1000\n",
        encoding="utf-8")
    offers = [Offer("KHH", "NGO", _d(30), _d(35), 9000.0, "TWD", "IT", 0, "180")]
    monkeypatch.setattr(runner_mod, "TravelpayoutsClient",
                        type("C", (), {"__init__": lambda s, *a, **k: None,
                                       "search_month": lambda s, *a, **k: {}}))
    monkeypatch.setattr(runner_mod, "parse_offers", lambda *a, **k: list(offers))
    monkeypatch.setattr(runner_mod, "notify", lambda o, v, st=None, **k: ["line"])
    monkeypatch.setattr(runner_mod, "channels_configured", lambda: True)
    monkeypatch.setattr(sa, "fetch", fetch)
    monkeypatch.setattr(sa.Sweeper, "usage_fetcher", staticmethod(lambda: usage))
    monkeypatch.setenv("FAREHUNTER_FORCE", "1")
    return runner_mod.run(str(cfg), str(tmp_path / "prices.db"), now=NOW)


def test_without_a_key_the_runner_never_calls_scrapingant(tmp_path, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("沒有金鑰不得連外")

    monkeypatch.setattr(sa.Sweeper, "usage_fetcher", staticmethod(forbidden))
    summary = _run(tmp_path, monkeypatch, forbidden)
    assert "scrapingant" not in summary


def test_runner_sweeps_cold_routes_with_scrapingant(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRAPINGANT_API_KEY", "k")
    asked = []

    def fetch(o, d, dep, ret):
        asked.append((o, d, dep))
        return _page()

    monkeypatch.setattr(sa, "PER_RUN_MAX", 4)              # 四條線輪流各一趟
    summary = _run(tmp_path, monkeypatch, fetch)
    assert summary["scrapingant"]["requests"] == sa.PER_RUN_MAX == len(asked)
    assert {(o, d) for o, d, _ in asked} == set(cd.COLD_ROUTES)
    assert ("KHH", "NGO", _d(30)) in asked                  # NGO 先查快取裡的那趟
    con = sqlite3.connect(tmp_path / "prices.db")
    n = con.execute("SELECT COUNT(*) FROM observations WHERE provider='scrapingant'").fetchone()[0]
    con.close()
    assert n == sa.PER_RUN_MAX
    assert sa.PROVIDER in runner_mod.SWEEP_PROVIDERS                # 查到的實價也能觸發巡檢警報


def test_a_broken_scrapingant_does_not_stop_the_run(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRAPINGANT_API_KEY", "k")

    def boom():
        raise RuntimeError("bug")

    monkeypatch.setattr(sa.Sweeper, "_allowance", lambda self: boom())
    summary = _run(tmp_path, monkeypatch, lambda *a: _page())
    assert summary["health"] is not None and "scrapingant" not in summary


def test_usage_panel_shows_the_last_30_days(tmp_path):
    from farehunter.export_web import export
    db = tmp_path / "p.db"
    st = Store(str(db))
    st.close()
    assert export(str(db), str(tmp_path / "d.json"), now=NOW)["usage"]["scrapingant"] is None
    st = Store(str(db))
    _use(st, 3, (NOW.date() - timedelta(days=31)).isoformat())          # 滾出視窗
    _use(st, 5, (NOW.date() - timedelta(days=2)).isoformat())
    _use(st, 2)
    st.close()
    a = export(str(db), str(tmp_path / "d.json"), now=NOW)["usage"]["scrapingant"]
    assert (a["used"], a["ok"], a["limit"], a["today"]) == (7, 7, sa.WINDOW_MAX, 2)
