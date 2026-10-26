"""LINE／Telegram 連線失敗（逾時、斷線）不能讓整輪掛掉。

修正前 send_telegram／send_line 只看 status_code，`requests` 的連線例外會一路
冒出去：monitor 在迴圈中途死掉、Export／Commit 都不跑（整小時的價格沒進 repo，
本輪已推過的通知也沒記到，下一輪會重推）；Verify Airlines 的推播步驟跑在
Commit 之前，同樣讓整輪驗證結果丟掉。兩個呼叫端本來就把「回 False」當成
「沒送出、下一輪重試」，所以這裡只要把例外變成 False。
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from farehunter import cheap_day_notify as C                     # noqa: E402
from farehunter import notify as N                               # noqa: E402
import farehunter.runner as runner_mod                           # noqa: E402
from farehunter.analyzer import Verdict                          # noqa: E402
from farehunter.models import Offer                              # noqa: E402
from farehunter.storage import Store                             # noqa: E402

TG_TOKEN = "123456:tg-secret-token"
LINE_TOKEN = "line-secret-token"


def _env(monkeypatch, telegram=True, line=True):
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
              "LINE_CHANNEL_ACCESS_TOKEN", "LINE_USER_ID"):
        monkeypatch.delenv(k, raising=False)
    if telegram:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TG_TOKEN)
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    if line:
        monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", LINE_TOKEN)
        monkeypatch.setenv("LINE_USER_ID", "U42")


class _Resp:
    status_code = 200
    text = "ok"


def _raising(exc_type, calls=None):
    """假的 requests.post：記下打了哪裡，然後丟例外——訊息照 requests 的樣子帶完整 URL。"""
    def post(url, **kw):
        if calls is not None:
            calls.append(url)
        raise exc_type(f"HTTPSConnectionPool: Max retries exceeded with url: {url}")
    return post


class _Session:
    def __init__(self, post):
        self.post = post


# ---- 單一管道 ---------------------------------------------------------------

def test_telegram_timeout_returns_false_and_hides_token(monkeypatch, caplog):
    """Telegram 的 token 就在 URL 路徑裡，requests 的例外訊息會原樣帶出來。"""
    _env(monkeypatch)
    with caplog.at_level(logging.ERROR, logger="farehunter.notify"):
        ok = N.send_telegram("hi", session=_Session(_raising(requests.Timeout)))
    assert ok is False
    assert "Telegram" in caplog.text and "Timeout" in caplog.text
    assert TG_TOKEN not in caplog.text


def test_line_connection_error_returns_false_and_hides_token(monkeypatch, caplog):
    _env(monkeypatch)

    def post(url, **kw):
        raise requests.ConnectionError(f"refused; headers={kw.get('headers')}")

    with caplog.at_level(logging.ERROR, logger="farehunter.notify"):
        ok = N.send_line("hi", session=_Session(post))
    assert ok is False
    assert "LINE" in caplog.text and "ConnectionError" in caplog.text
    assert LINE_TOKEN not in caplog.text


def test_non_200_still_returns_false(monkeypatch):
    """原本的 HTTP 錯誤路徑不變。"""
    _env(monkeypatch)
    bad = type("R", (), {"status_code": 401, "text": "unauthorized"})()
    assert N.send_telegram("hi", session=_Session(lambda url, **kw: bad)) is False
    assert N.send_line("hi", session=_Session(lambda url, **kw: bad)) is False


def test_success_still_returns_true(monkeypatch):
    _env(monkeypatch)
    assert N.send_telegram("hi", session=_Session(lambda url, **kw: _Resp())) is True
    assert N.send_line("hi", session=_Session(lambda url, **kw: _Resp())) is True


# ---- notify()：一個管道壞了不能拖累另一個 -----------------------------------

DEP = (datetime.now(timezone.utc).date() + timedelta(days=40)).isoformat()
RET = (datetime.now(timezone.utc).date() + timedelta(days=45)).isoformat()


def _offer():
    return Offer(origin="KHH", destination="NRT", depart_date=DEP,
                 return_date=RET, price=7761.0, currency="TWD", carriers="GK",
                 stops=0, duration="180", fare_class="any", source="aviasales")


VERDICT = Verdict(is_deal=True, reason="absolute", detail="7,761 <= 門檻 8,000")


def test_notify_survives_both_channels_failing(monkeypatch):
    _env(monkeypatch)
    calls = []
    monkeypatch.setattr(N.requests, "post", _raising(requests.Timeout, calls))
    assert N.notify(_offer(), VERDICT) == []
    assert len(calls) == 2                       # Telegram 失敗後照樣試 LINE


def test_notify_reports_the_channel_that_worked(monkeypatch):
    _env(monkeypatch)

    def post(url, **kw):
        if "telegram" in url:
            raise requests.ReadTimeout("read timed out")
        return _Resp()

    monkeypatch.setattr(N.requests, "post", post)
    assert N.notify(_offer(), VERDICT) == ["line"]


# ---- 呼叫端：整輪跑完，沒送出的留到下一輪 -----------------------------------

def _monitor(tmp_path, monkeypatch):
    p = tmp_path / "config.yaml"
    p.write_text(
        "defaults:\n  currency: twd\n  months_ahead: 1\n  pause_seconds: 0\n"
        "routes:\n  - origin: KHH\n    destination: NRT\n"
        "    absolute_threshold: 8000\n", encoding="utf-8")
    monkeypatch.setattr(runner_mod, "TravelpayoutsClient",
                        type("C", (), {"__init__": lambda s, *a, **k: None,
                                       "search_month": lambda s, *a, **k: {}}))
    monkeypatch.setattr(runner_mod, "parse_offers", lambda *a, **k: [_offer()])
    # 同一個測試裡連跑兩輪模擬「下一輪」；不開 force 的話第二輪會被 55 分鐘 guard 跳過
    monkeypatch.setenv("FAREHUNTER_FORCE", "1")
    return runner_mod.run(str(p), str(tmp_path / "prices.db"))


def _alerts(db):
    st = Store(str(db))
    n = st.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    st.close()
    return n


def test_monitor_run_finishes_and_retries_next_round(tmp_path, monkeypatch, caplog):
    _env(monkeypatch)
    monkeypatch.setattr(N.requests, "post", _raising(requests.ConnectTimeout))
    with caplog.at_level(logging.ERROR):
        summary = _monitor(tmp_path, monkeypatch)
    assert summary["alerts"] == 0
    assert _alerts(tmp_path / "prices.db") == 0  # 沒記成已通知 → 下一輪會再送
    assert "通知發送失敗" in caplog.text

    # 下一輪管道恢復：同一則照常送出並記錄
    monkeypatch.setattr(N.requests, "post", lambda url, **kw: _Resp())
    assert _monitor(tmp_path, monkeypatch)["alerts"] == 1
    assert _alerts(tmp_path / "prices.db") == 1


def test_cheap_day_run_counts_failure_instead_of_crashing(tmp_path, monkeypatch):
    _env(monkeypatch)
    monkeypatch.setattr(N.requests, "post", _raising(requests.Timeout))
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    item = {"origin": "TPE", "destination": "NRT", "depart_date": "2026-11-20",
            "return_date": "2026-11-25", "price": 7200.0,
            "neighbour_median": 12000.0, "neighbours": 8, "discount_pct": 40.0,
            "notable": True, "source": "google", "carriers": "BR",
            "observed_at": (now - timedelta(minutes=5)).isoformat()}
    board = tmp_path / "data.json"
    board.write_text(json.dumps({"cheap_days": [item]}), encoding="utf-8")
    db = tmp_path / "p.db"
    s = C.run(str(db), str(board), now=now)
    assert s["candidates"] == 1, "測試資料要先能合格，否則測不到推播路徑"
    assert s["failed"] == 1 and s["sent"] == 0
    st = Store(str(db))
    assert st.conn.execute("SELECT COUNT(*) FROM cheap_day_notices").fetchone()[0] == 0
    st.close()
