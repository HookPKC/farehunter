"""Telegram 每日行情摘要（daily_digest）的測試。零真實 API。"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from farehunter import daily_digest as dd                           # noqa: E402
from farehunter import notify as N                                  # noqa: E402
import farehunter.runner as runner_mod                              # noqa: E402
from farehunter.models import Offer                                 # noqa: E402
from farehunter.storage import Store                                # noqa: E402

NOW = datetime(2026, 11, 22, 0, 17, tzinfo=timezone.utc)            # 台灣 11/22（日）08:17
AT = NOW.isoformat(timespec="seconds")
ROUTES = [{"origin": "KHH", "destination": "OKA", "absolute_threshold": 5000},
          {"origin": "TPE", "destination": "NRT", "absolute_threshold": 7000},
          {"origin": "KHH", "destination": "CTS", "absolute_threshold": 12000}]


def _d(n):
    return (NOW.date() + timedelta(days=n)).isoformat()


def _store(tmp_path):
    return Store(str(tmp_path / "prices.db"))


def _google(st, o, d, dep, nights, price, carriers="IT", hours_ago=1, source="google", stops=0):
    st.record(Offer(o, d, _d(dep), _d(dep + nights), price, "TWD", carriers, stops, "",
                    source=source, provider="brightdata" if source == "google" else "travelpayouts"),
              observed_at=(NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds"))


def _telegram(monkeypatch, sent, ok=True):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:tg")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(N, "send_telegram", lambda text, **k: sent.append(text) or ok)

    def no_line(*a, **k):
        raise AssertionError("每日摘要不得用 LINE")
    monkeypatch.setattr(N, "send_line", no_line)


# ---- 內容 --------------------------------------------------------------------------

def test_digest_lists_each_route_cheapest_google_direct_price_with_week_trend(tmp_path):
    st = _store(tmp_path)
    _google(st, "KHH", "OKA", 30, 5, 4798)
    _google(st, "KHH", "OKA", 31, 5, 5500)                             # 較貴的不列
    _google(st, "KHH", "OKA", 33, 5, 5215, hours_ago=24 * 8)           # 上週
    _google(st, "TPE", "NRT", 40, 3, 8218, carriers="MM")
    st.conn.execute("INSERT INTO alerts (origin, destination, depart_date, price, reason, sent_at)"
                    " VALUES ('KHH','FUK',?,6781,'absolute','2026-11-21 03:19:09')", (_d(5),))
    st.record_line_send("2026-11-21T03:17:47+00:00", "owner", 42)
    st.conn.commit()
    text = dd.build(st, ROUTES, NOW)
    lines = text.splitlines()
    assert lines[0] == "☀️ FareHunter 每日行情 11/22（日）"
    m30, m35 = dd._md(_d(30)), dd._md(_d(35))
    assert lines[2] == f"✅ KHH-OKA 4,798　{m30}–{m35} 台灣虎航（上週 5,215，↓8%）"
    assert lines[3].startswith("TPE-NRT 8,218") and "樂桃" in lines[3] and "上週" not in lines[3]
    assert lines[4] == "KHH-CTS：近 48 小時沒有直飛實價"
    assert lines[5] == "✅ 低於門檻｜昨天警報 1 則｜LINE 本月約剩 42 則"


def test_only_recent_google_direct_round_trips_in_the_stay_range_count(tmp_path):
    st = _store(tmp_path)
    _google(st, "KHH", "OKA", 30, 5, 3000, source="travelpayouts")    # 快取價（要寫「約」）不列
    _google(st, "KHH", "OKA", 30, 5, 3100, stops=1)                   # 轉機
    _google(st, "KHH", "OKA", 30, 14, 3200)                           # 住 14 晚
    _google(st, "KHH", "OKA", 1, 3, 3300)                             # 明天出發，太近
    _google(st, "KHH", "OKA", 30, 5, 3400, hours_ago=dd.LOOKBACK_HOURS + 1)   # 太舊
    _google(st, "KHH", "OKA", 30, 5, 6000)
    rows = {x["route"]: x for x in dd.route_lows(st, ROUTES, NOW)}
    assert rows["KHH-OKA"]["price"] == 6000


def test_routes_below_threshold_come_first_then_by_price_to_threshold(tmp_path):
    st = _store(tmp_path)
    _google(st, "KHH", "OKA", 30, 5, 5900)                             # 1.18 倍
    _google(st, "TPE", "NRT", 30, 5, 6900)                             # 0.99 倍 → 最前
    _google(st, "KHH", "CTS", 30, 5, 12600)                            # 1.05 倍
    assert [x["route"] for x in dd.route_lows(st, ROUTES, NOW)] == ["TPE-NRT", "KHH-CTS", "KHH-OKA"]


# ---- 什麼時候發 --------------------------------------------------------------------

def test_sent_once_in_the_morning_window_only(tmp_path, monkeypatch):
    st, sent = _store(tmp_path), []
    _telegram(monkeypatch, sent)
    early = NOW - timedelta(hours=1)                                   # 台灣 07:17
    assert dd.maybe_send(st, ROUTES, early) == {"sent": False, "skipped": "not_due"}
    assert dd.maybe_send(st, ROUTES, NOW)["sent"] is True
    assert dd.maybe_send(st, ROUTES, NOW + timedelta(hours=1))["skipped"] == "not_due"   # 今天發過
    assert len(sent) == 1
    noon = NOW + timedelta(days=1, hours=4)                            # 隔天台灣 12:17：過了中午不補
    assert dd.maybe_send(st, ROUTES, noon)["skipped"] == "not_due"
    assert dd.maybe_send(st, ROUTES, NOW + timedelta(days=1))["sent"] is True
    assert len(sent) == 2


def test_a_failed_send_is_retried_next_round(tmp_path, monkeypatch):
    st, sent = _store(tmp_path), []
    _telegram(monkeypatch, sent, ok=False)
    assert dd.maybe_send(st, ROUTES, NOW) == {"sent": False, "skipped": "send_failed"}
    assert not st.digest_sent("2026-11-22")
    _telegram(monkeypatch, sent, ok=True)
    assert dd.maybe_send(st, ROUTES, NOW + timedelta(hours=1))["sent"] is True
    assert st.digest_sent("2026-11-22")


def test_without_telegram_nothing_is_sent(tmp_path, monkeypatch):
    st = _store(tmp_path)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    def boom(*a, **k):
        raise AssertionError("沒設 Telegram 不得送出")
    monkeypatch.setattr(N, "send_telegram", boom)
    monkeypatch.setattr(N, "send_line", boom)
    assert dd.maybe_send(st, ROUTES, NOW) == {"sent": False, "skipped": "no_telegram"}


# ---- runner ------------------------------------------------------------------------

def _run(tmp_path, monkeypatch, now):
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
    monkeypatch.setattr(runner_mod, "channels_configured", lambda: True)
    for name in ("BRIGHTDATA_API_KEY", "CHOCODATA_API_KEY", "RAPIDAPI_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FAREHUNTER_FORCE", "1")
    return runner_mod.run(str(cfg), str(tmp_path / "prices.db"), now=now)


def test_the_morning_run_sends_the_digest(tmp_path, monkeypatch):
    sent = []
    _telegram(monkeypatch, sent)
    summary = _run(tmp_path, monkeypatch, NOW)
    assert summary["digest"] == {"sent": True, "skipped": ""}
    assert len(sent) == 1 and sent[0].startswith("☀️ FareHunter 每日行情")


def test_a_broken_digest_does_not_stop_the_run(tmp_path, monkeypatch):
    _telegram(monkeypatch, [])

    def boom(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(dd, "build", boom)
    summary = _run(tmp_path, monkeypatch, NOW)
    assert summary["health"] is not None and "digest" not in summary
