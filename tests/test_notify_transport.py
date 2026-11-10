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
              "LINE_CHANNEL_ACCESS_TOKEN", "LINE_USER_ID", "LINE_BROADCAST"):
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


def test_both_channels_use_the_short_timeout(monkeypatch):
    """管道掛掉時不要每則卡約 1 分鐘：連線 5 秒、讀取 20 秒。"""
    _env(monkeypatch)
    seen = []

    def post(url, **kw):
        seen.append(kw["timeout"])
        return _Resp()

    N.send_telegram("hi", session=_Session(post))
    N.send_line("hi", session=_Session(post))
    assert seen == [(5, 20), (5, 20)]


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


# ---- LINE 廣播給所有好友（LINE_BROADCAST=1） ---------------------------------------

class _JsonResp:
    def __init__(self, data, status=200):
        self.status_code, self._data, self.text = status, data, json.dumps(data)

    def json(self):
        return self._data


class _LineApi:
    """假的 LINE API：記下 POST 到哪裡、帶什麼；額度可設定，或讓查額度失敗。"""
    def __init__(self, limit=200, used=0, unlimited=False, quota_error=None):
        self.limit, self.used, self.unlimited, self.quota_error = limit, used, unlimited, quota_error
        self.posts = []

    def get(self, url, **kw):
        if self.quota_error:
            raise self.quota_error(f"refused; headers={kw.get('headers')}")
        if url.endswith("/quota"):
            return _JsonResp({"type": "none"} if self.unlimited
                             else {"type": "limited", "value": self.limit})
        return _JsonResp({"totalUsage": self.used})

    def post(self, url, **kw):
        self.posts.append((url, kw["json"]))
        return _Resp()


def _broadcast_env(monkeypatch, user=True):
    _env(monkeypatch, telegram=False, line=True)
    monkeypatch.setenv("LINE_BROADCAST", "1")
    if not user:
        monkeypatch.delenv("LINE_USER_ID")


def test_broadcast_reaches_every_friend_when_quota_is_ample(monkeypatch):
    _broadcast_env(monkeypatch)
    api = _LineApi(limit=200, used=100)
    assert N.send_line("便宜機票", session=api) is True
    (url, body), = api.posts
    assert url.endswith("/message/broadcast") and "to" not in body
    assert body["messages"][0]["text"] == "便宜機票"


def test_low_quota_falls_back_to_the_owner_only(monkeypatch, caplog):
    """5 個人每月約 230 則，會超過免費的 200 則；快用完時至少擁有者還收得到。"""
    _broadcast_env(monkeypatch)
    api = _LineApi(limit=200, used=200 - N.LINE_OWNER_RESERVE + 1)
    with caplog.at_level(logging.WARNING, logger="farehunter.notify"):
        assert N.send_line("便宜機票", session=api) is True
    (url, body), = api.posts
    assert url.endswith("/message/push") and body["to"] == "U42"
    assert "只發給擁有者" in caplog.text
    # 剛好剩保留的則數時照樣廣播
    api = _LineApi(limit=200, used=200 - N.LINE_OWNER_RESERVE)
    N.send_line("便宜機票", session=api)
    assert api.posts[0][0].endswith("/message/broadcast")


def test_unlimited_or_unknown_quota_still_broadcasts(monkeypatch, caplog):
    _broadcast_env(monkeypatch)
    api = _LineApi(unlimited=True)
    N.send_line("hi", session=api)
    assert api.posts[0][0].endswith("/message/broadcast")
    api = _LineApi(quota_error=requests.Timeout)
    with caplog.at_level(logging.ERROR, logger="farehunter.notify"):
        assert N.send_line("hi", session=api) is True
    assert api.posts[0][0].endswith("/message/broadcast")
    assert LINE_TOKEN not in caplog.text


def test_broadcast_without_an_owner_id_skips_the_quota_check(monkeypatch):
    _broadcast_env(monkeypatch, user=False)
    api = _LineApi(quota_error=AssertionError)        # 不該去查額度
    assert N.send_line("hi", session=api) is True
    assert api.posts[0][0].endswith("/message/broadcast")


def test_without_the_switch_it_still_pushes_to_the_owner_only(monkeypatch):
    _env(monkeypatch, telegram=False, line=True)
    api = _LineApi(quota_error=AssertionError)
    assert N.send_line("hi", session=api) is True
    (url, body), = api.posts
    assert url.endswith("/message/push") and body["to"] == "U42"


def test_workflows_broadcast_alerts_but_not_the_test_message():
    """警報與便宜日推播給所有好友；手動測試通知只發給擁有者，免得吵到朋友。"""
    wf = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    for name in ("monitor.yml", "verify-airlines.yml"):
        assert 'LINE_BROADCAST: "1"' in (wf / name).read_text(encoding="utf-8")
    assert "LINE_BROADCAST" not in (wf / "test-notify.yml").read_text(encoding="utf-8")


# ---- LINE 配速與半夜靜音（2026-11-10）--------------------------------------------
DAY = datetime(2026, 11, 10, 3, 0, tzinfo=timezone.utc)     # 台灣 11/10 11:00，本月含今天還剩 21 天


class _PacedApi(_LineApi):
    """額外回應好友統計；insight=None 代表 LINE 不提供（好友太少時就是這樣）。"""
    def __init__(self, insight=None, **kw):
        super().__init__(**kw)
        self.insight = insight

    def get(self, url, **kw):
        if url == N.LINE_INSIGHT_API:
            return _JsonResp(self.insight or {"status": "unready"})
        return super().get(url, **kw)


def _budget(tmp_path, now=DAY, earlier=()):
    st = Store(str(tmp_path / "prices.db"))
    for at, mode in earlier:
        st.record_line_send(at, mode)
    return st, N.LineBudget(st, now)


def _mode(api):
    (url, body), = api.posts
    return "broadcast" if url.endswith("/broadcast") else "owner"


def test_the_first_broadcast_of_the_day_goes_out_while_above_the_reserve(tmp_path, monkeypatch):
    """月底剩 40 則：照進度早就不夠每天一則，但今天第一則還是發給朋友。"""
    _broadcast_env(monkeypatch)
    st, b = _budget(tmp_path)
    api = _PacedApi(limit=200, used=160)
    assert N.send_line("便宜機票", session=api, budget=b) is True
    assert _mode(api) == "broadcast"
    assert st.line_sends_since("broadcast", "2026-11-10T00:00:00+00:00") == 1


def test_a_second_broadcast_needs_room_for_one_a_day_until_month_end(tmp_path, monkeypatch):
    """今天已廣播過：剩 100 則、還有 21 天，廣播完得留 30＋5×20＝130 → 只發給擁有者。"""
    _broadcast_env(monkeypatch)
    st, b = _budget(tmp_path, earlier=[("2026-11-10T01:00:00+00:00", "broadcast")])
    api = _PacedApi(limit=200, used=100)
    assert N.send_line("便宜機票", session=api, budget=b) is True
    assert _mode(api) == "owner"
    assert st.line_sends_since("owner", "2026-11-10T00:00:00+00:00") == 1
    roomy = _PacedApi(limit=200, used=60)                      # 剩 140：135 ≥ 130 → 可以
    assert N.send_line("便宜機票", session=roomy, budget=b) is True
    assert _mode(roomy) == "broadcast"


def test_the_day_is_counted_in_taiwan_time(tmp_path, monkeypatch):
    """台灣 23:30 的廣播是前一天的；台灣 00:30 那則又是當天第一則。"""
    _broadcast_env(monkeypatch)
    now = datetime(2026, 11, 10, 16, 30, tzinfo=timezone.utc)   # 台灣 11/11 00:30
    st, b = _budget(tmp_path, now=now, earlier=[("2026-11-10T15:30:00+00:00", "broadcast")])
    assert b.broadcasts_today() == 0 and b.days_left() == 20
    api = _PacedApi(limit=200, used=150)
    assert N.send_line("便宜機票", session=api, budget=b) is True
    assert _mode(api) == "broadcast"


def test_recipients_come_from_line_statistics_with_a_fallback(tmp_path, monkeypatch):
    """12 位好友時一次廣播用掉 12 則；同樣的剩餘額度，5 人可以、12 人就不行。"""
    _broadcast_env(monkeypatch)
    st, b = _budget(tmp_path, earlier=[("2026-11-10T01:00:00+00:00", "broadcast")])
    # 剩 140：5 人時 135 ≥ 130 可以；12 人時 128 < 30＋12×20 → 不行
    many = _PacedApi(limit=200, used=60, insight={"status": "ready", "targetedReaches": 12})
    assert N.send_line("便宜機票", session=many, budget=b) is True
    assert _mode(many) == "owner"
    monkeypatch.setenv("LINE_RECIPIENTS", "12")                # 讀不到統計時用設定值
    assert N._line_recipients(_PacedApi(), {}, "t", DAY) == 12
    monkeypatch.delenv("LINE_RECIPIENTS")
    assert N._line_recipients(_PacedApi(), {}, "t", DAY) == N.DEFAULT_RECIPIENTS


def test_a_failed_send_is_not_counted(tmp_path, monkeypatch):
    _broadcast_env(monkeypatch)
    st, b = _budget(tmp_path)

    class _Down(_PacedApi):
        def post(self, url, **kw):
            return type("R", (), {"status_code": 500, "text": "boom"})()

    assert N.send_line("便宜機票", session=_Down(), budget=b) is False
    assert b.broadcasts_today() == 0


def test_night_messages_are_delivered_silently(tmp_path, monkeypatch):
    """台灣 23:00–07:00 靜音送達（訊息照常到、手機不響）；白天照常。LINE 與 Telegram 都一樣。"""
    _env(monkeypatch, telegram=True, line=True)
    night = datetime(2026, 11, 10, 16, 0, tzinfo=timezone.utc)   # 台灣 00:00
    assert N.quiet_hours(night) and not N.quiet_hours(DAY)
    assert N.quiet_hours(datetime(2026, 11, 10, 22, 59, tzinfo=timezone.utc))      # 06:59
    assert not N.quiet_hours(datetime(2026, 11, 10, 23, 0, tzinfo=timezone.utc))   # 07:00
    assert N.quiet_hours(datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc))       # 23:00
    st = Store(str(tmp_path / "prices.db"))
    for now, quiet in ((night, True), (DAY, False)):
        posts = []

        def post(url, **kw):
            posts.append((url, kw["json"]))
            return _Resp()

        monkeypatch.setattr(N.requests, "post", post)
        N.notify(_offer(), Verdict(True, "absolute", "x"), line_budget=N.LineBudget(st, now))
        line = [b for u, b in posts if "api.line.me" in u]
        tg = [b for u, b in posts if "telegram" in u]
        assert line[0]["notificationDisabled"] is quiet
        assert tg[0]["disable_notification"] is quiet
