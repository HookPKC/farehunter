"""google-flights2 第二驗證來源（ADR 0005）的測試。零真實 API：一律注入假的 fetch／session。"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from farehunter import brightdata_flights as bd                     # noqa: E402
from farehunter import gflights2_flights as g2                      # noqa: E402
import farehunter.runner as runner_mod                              # noqa: E402
from farehunter.models import Offer                                 # noqa: E402
from farehunter.storage import Store                                # noqa: E402

NOW = datetime(2026, 11, 15, 6, 17, tzinfo=timezone.utc)
DEP = (NOW.date() + timedelta(days=40)).isoformat()
RET = (NOW.date() + timedelta(days=45)).isoformat()
KEY = "rapid-key-123"


def _it(price, *legs):
    """legs：(航空公司名稱, 班號)。回應裡 stops 一律是 0（實測：連轉機也是 0）。"""
    return {"price": price, "stops": 0,
            "flights": [{"airline": a, "flight_number": n} for a, n in legs]}


def _body(top=(), other=()):
    return {"status": True, "message": "Success",
            "data": {"itineraries": {"topFlights": list(top), "otherFlights": list(other)},
                     "priceHistory": {"summary": {"current": 1}}}}


# 2026-11-14 實測 KHH-NRT 11/30–12/07 的形狀
REAL = _body(top=[_it(11114, ("Jetstar", "GK 18")), _it(13087, ("Thai AirAsia", "FD 234")),
                  _it(14468, ("China Airlines", "CI 126"))],
             other=[_it(9000, ("Hong Kong Express", "UO 131"), ("Hong Kong Express", "UO 650")),
                    _it("unavailable", ("Vietnam Airlines", "VN 587"), ("Vietnam Airlines", "VN 310")),
                    _it(13100, ("United", "UA 838"))])


def test_parse_ignores_the_stops_field_and_unavailable_prices():
    its = g2.parse(REAL)
    assert [(i.price, i.nonstop, i.carriers) for i in its] == [
        (11114.0, True, "GK"), (13087.0, True, "FD"), (14468.0, True, "CI"),
        (9000.0, False, "UO"), (13100.0, True, "UA")]


def test_parse_of_garbage_is_empty():
    assert g2.parse({}) == [] and g2.parse({"data": None}) == [] and g2.parse("x") == []


def test_an_unreadable_flight_number_gives_no_carrier():
    it, = g2.parse(_body(top=[_it(8000, ("Mystery Air", ""))]))
    assert it.carriers is None


class _Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        if self._body is None:
            raise ValueError
        return self._body


class _Session:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp, exc, []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        if self.exc:
            raise self.exc
        return self.resp


def test_fetch_sends_the_key_in_a_header_not_the_url(monkeypatch):
    monkeypatch.setenv("RAPIDAPI_KEY", KEY)
    s = _Session(_Resp(200, REAL))
    assert g2.fetch("KHH", "NRT", DEP, RET, session=s) is REAL
    url, kw = s.calls[0]
    assert KEY not in url and KEY not in str(kw["params"])
    assert kw["headers"]["X-RapidAPI-Key"] == KEY
    assert kw["params"]["departure_id"] == "KHH" and kw["params"]["return_date"] == RET
    assert kw["params"]["currency"] == "TWD"


def test_fetch_errors_never_carry_the_key(monkeypatch):
    monkeypatch.setenv("RAPIDAPI_KEY", KEY)
    with pytest.raises(g2.GFlights2Timeout) as e:
        g2.fetch("KHH", "NRT", DEP, RET, session=_Session(exc=requests.ReadTimeout(f"key={KEY}")))
    assert str(e.value) == "transport: ReadTimeout" and e.value.__cause__ is None
    with pytest.raises(g2.GFlights2Error) as e:
        g2.fetch("KHH", "NRT", DEP, RET, session=_Session(_Resp(429, None, f"quota for {KEY}")))
    assert "429" in str(e.value) and KEY not in str(e.value)
    with pytest.raises(g2.GFlights2Error):
        g2.fetch("KHH", "NRT", DEP, RET, session=_Session(_Resp(200, {
            "status": False, "message": [{"departure_id": "Departure ID is required."}]})))


def test_allowance_is_capped_per_run_per_day_and_per_30_days(tmp_path):
    st = Store(str(tmp_path / "prices.db"))
    assert g2.Verifier(st, NOW).left == g2.PER_RUN_MAX
    for _ in range(g2.DAILY_MAX - 1):
        st.record_api_usage(g2.PROVIDER, NOW.date().isoformat(), ok=True)
    assert g2.Verifier(st, NOW).left == 1
    st.record_api_usage(g2.PROVIDER, NOW.date().isoformat(), ok=True)
    assert g2.Verifier(st, NOW).left == 0
    assert g2.Verifier(st, NOW + timedelta(days=1)).left == g2.PER_RUN_MAX
    for _ in range(g2.WINDOW_MAX):
        st.record_api_usage(g2.PROVIDER, (NOW.date() - timedelta(days=10)).isoformat(), ok=True)
    assert g2.Verifier(st, NOW + timedelta(days=1)).left == 0          # 近 30 天用滿
    assert g2.Verifier(st, NOW + timedelta(days=25)).left == g2.PER_RUN_MAX   # 滾出視窗
    for _ in range(50):                                                 # Bright Data 的用量不算
        st.record_api_usage(bd.PROVIDER, (NOW + timedelta(days=25)).date().isoformat(), ok=True)
    assert g2.Verifier(st, NOW + timedelta(days=25)).left == g2.PER_RUN_MAX


def test_check_records_google_rows_under_its_own_provider(tmp_path):
    st = Store(str(tmp_path / "prices.db"))
    v = g2.Verifier(st, NOW, fetcher=lambda *a: REAL)
    auth = v.check("KHH", "NRT", DEP, RET, "FD")
    assert (auth.price, auth.carriers, auth.provider, auth.source) == (13087.0, "FD", "gflights2", "google")
    rows = st.conn.execute("SELECT price, carriers, fare_class, provider FROM observations "
                           "ORDER BY fare_class, price").fetchall()
    assert [tuple(r) for r in rows] == [(11114.0, "GK", "any", "gflights2"),
                                        (13087.0, "FD", "any", "gflights2"),
                                        (13100.0, "UA", "full", "gflights2")]   # 聯合航空也是傳統航空
    assert st.api_usage_since("gflights2", "2026-11-01") == 1
    assert st.api_usage_since("brightdata", "2026-11-01") == 0
    assert v.checked_recently("KHH", "NRT", DEP, RET, "FD")
    assert not bd.Verifier(st, NOW, allowance=0).checked_recently("KHH", "NRT", DEP, RET, "FD")


# ---- runner：Bright Data 驗證不到時才問第二來源 ------------------------------------

def _label(price, airline):
    return (f'<li aria-label="From {price} New Taiwan dollars round trip total. Nonstop flight '
            f'with {airline}. Leaves Kaohsiung at 6:50 AM."></li>')


def _run(tmp_path, monkeypatch, *, bd_fetch=None, g2_fetch=None, offers=None, now=NOW):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "defaults:\n  currency: twd\n  months_ahead: 1\n  pause_seconds: 0\n"
        "routes:\n  - origin: KHH\n    destination: NRT\n    absolute_threshold: 8000\n",
        encoding="utf-8")
    offers = offers or [Offer("KHH", "NRT", DEP, RET, 7761.0, "TWD", "GK", 0, "180")]
    monkeypatch.setattr(runner_mod, "TravelpayoutsClient",
                        type("C", (), {"__init__": lambda s, *a, **k: None,
                                       "search_month": lambda s, *a, **k: {}}))
    monkeypatch.setattr(runner_mod, "parse_offers", lambda *a, **k: list(offers))
    sent = []
    monkeypatch.setattr(runner_mod, "notify",
                        lambda o, v, st=None, **k: sent.append((o.price, st.state)) or ["line"])
    monkeypatch.setattr(runner_mod, "channels_configured", lambda: True)
    monkeypatch.delenv("CHOCODATA_API_KEY", raising=False)
    for name, mod, f in (("BRIGHTDATA_API_KEY", bd, bd_fetch), ("RAPIDAPI_KEY", g2, g2_fetch)):
        if f is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, "k")
            monkeypatch.setattr(mod, "fetch", f)
    monkeypatch.setattr(bd.Verifier, "sweep", lambda self, c, r: None)
    monkeypatch.setenv("FAREHUNTER_FORCE", "1")
    summary = runner_mod.run(str(cfg), str(tmp_path / "prices.db"), now=now)
    return summary, sent


def _counting(result):
    calls = []

    def f(*a):
        calls.append(a)
        if isinstance(result, Exception):
            raise result
        return result
    return f, calls


def test_without_a_key_the_runner_never_calls_it(tmp_path, monkeypatch):
    summary, sent = _run(tmp_path, monkeypatch)
    assert "gflights2" not in summary and sent == [(7761.0, "unverified")]


def test_it_is_not_asked_when_bright_data_already_verified(tmp_path, monkeypatch):
    g, calls = _counting(REAL)
    summary, sent = _run(tmp_path, monkeypatch, g2_fetch=g,
                         bd_fetch=lambda *a: "<html>" + _label("7,650", "Jetstar") + "</html>")
    assert calls == [] and sent == [(7650.0, "verified")]
    assert summary["gflights2"]["requests"] == 0


def test_it_verifies_what_bright_data_could_not(tmp_path, monkeypatch):
    """Bright Data 的頁面上沒有捷星 → 原本會以未驗證發出；第二來源查到捷星 7,700 → 已驗證。"""
    g, calls = _counting(_body(top=[_it(7700, ("Jetstar", "GK 18"))]))
    summary, sent = _run(tmp_path, monkeypatch, g2_fetch=g,
                         bd_fetch=lambda *a: "<html>" + _label("9,500", "Tigerair Taiwan") + "</html>")
    assert len(calls) == 1 and sent == [(7700.0, "verified")]
    assert summary["gflights2"]["alerts_checked"] == 1


def test_it_stops_an_alert_whose_real_price_is_not_cheap(tmp_path, monkeypatch):
    g, calls = _counting(REAL)                    # 捷星實價 11,114：不便宜
    b, _ = _counting(bd.BrightDataTimeout("transport: ReadTimeout"))
    summary, sent = _run(tmp_path, monkeypatch, g2_fetch=g, bd_fetch=b)
    assert len(calls) == 1 and sent == []
    con = sqlite3.connect(tmp_path / "prices.db")
    assert con.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0
    con.close()


def test_it_works_alone_and_is_not_asked_again_within_a_day(tmp_path, monkeypatch):
    g, calls = _counting(_body(top=[_it(9000, ("Tigerair Taiwan", "IT 280"))]))   # 沒有捷星
    summary, sent = _run(tmp_path, monkeypatch, g2_fetch=g)
    assert len(calls) == 1 and sent == [(7761.0, "unverified")]
    _run(tmp_path, monkeypatch, g2_fetch=g, now=NOW + timedelta(hours=1))
    assert len(calls) == 1


def test_a_failing_second_source_leaves_the_alert_as_it_was(tmp_path, monkeypatch):
    g, calls = _counting(g2.GFlights2Error("HTTP 429: quota"))
    summary, sent = _run(tmp_path, monkeypatch, g2_fetch=g)
    assert sent == [(7761.0, "unverified")] and summary["gflights2"]["errors"] == 1
