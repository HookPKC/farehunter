"""Travelpayouts 連線層錯誤（逾時、斷線、回應本體壞掉）的處理。

2026-10-18 11:17 UTC（run 39122615061）：一次 requests.ReadTimeout 從
search_month 原樣拋出，runner 只接 TravelpayoutsError，於是整輪 run() 中止——
Run monitor 變紅，export / commit / 心跳全部跳過，那一小時 8 條航線已經抓到的
觀測跟著 runner 一起丟掉。

鎖定三件事：
1. 連線層例外一律轉成 TravelpayoutsError(TRANSPORT_ERROR)，讓 runner 照月份跳過。
2. 熔斷：同一輪累計 MAX_TRANSPORT_ERRORS 次就不再呼叫 API。否則「掛著不回應」
   的故障會讓 80 次搜尋各等 60 秒，超過 job 的 30 分鐘上限，還一路佔住
   farehunter concurrency 群組。
3. 整輪沒有任何一次搜尋成功 → all_searches_failed() 為真（run.py 據此 exit 1）。
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

import farehunter.runner as runner_mod
from farehunter.travelpayouts import (TRANSPORT_ERROR, TravelpayoutsClient,
                                      TravelpayoutsError)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "prices_for_dates.json").read_text()
)
NOW = datetime(2026, 8, 7, 1, 36, 0, tzinfo=timezone.utc)


# ---- client：例外轉換 --------------------------------------------------------

class RaisingSession:
    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def get(self, *a, **kw):
        self.calls += 1
        raise self.exc


class BadJsonResponse:
    status_code = 200

    def json(self):
        raise requests.exceptions.JSONDecodeError("Expecting value", "", 0)


class BadJsonSession:
    def get(self, *a, **kw):
        return BadJsonResponse()


@pytest.mark.parametrize("exc", [
    requests.exceptions.ReadTimeout("Read timed out. (read timeout=60)"),
    requests.exceptions.ConnectionError("Connection reset by peer"),
])
def test_transport_exception_becomes_travelpayouts_error(exc):
    session = RaisingSession(exc)
    client = TravelpayoutsClient(token="t", session=session)

    with pytest.raises(TravelpayoutsError) as info:
        client.search_month("KHH", "NGO", "2026-11")

    assert info.value.status_code == TRANSPORT_ERROR
    assert type(exc).__name__ in str(info.value)   # log 要看得出是哪一種
    assert session.calls == 1                       # 不重試：下一小時會補回


def test_non_json_200_becomes_travelpayouts_error():
    client = TravelpayoutsClient(token="t", session=BadJsonSession())

    with pytest.raises(TravelpayoutsError) as info:
        client.search_month("KHH", "NGO", "2026-11")

    assert info.value.status_code == TRANSPORT_ERROR


# ---- runner：照月份跳過、熔斷、全滅判定 ---------------------------------------

def _config(tmp_path, months=3, routes=("TPE-NRT",)):
    lines = ["defaults:", "  currency: twd", f"  months_ahead: {months}",
             "  pause_seconds: 0", "routes:"]
    for r in routes:
        o, d = r.split("-")
        lines += [f"  - origin: {o}", f"    destination: {d}"]
    cfg = tmp_path / "config.yaml"
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(cfg)


def _client_factory(fail_calls):
    """回傳 (client 類別, 呼叫紀錄)。fail_calls 是要丟連線錯誤的呼叫序號集合
    （從 1 起算）；"all" 表示每次都丟。"""
    calls = []

    class Client:
        def __init__(self, *a, **kw):
            pass

        def search_month(self, origin, dest, month, **kw):
            calls.append((origin, dest, month))
            if fail_calls == "all" or len(calls) in fail_calls:
                raise TravelpayoutsError(TRANSPORT_ERROR,
                                         "ReadTimeout: Read timed out.")
            return FIXTURE

    return Client, calls


def test_one_timeout_skips_only_that_month(tmp_path, monkeypatch):
    Client, calls = _client_factory({2})
    monkeypatch.setattr(runner_mod, "TravelpayoutsClient", Client)

    summary = runner_mod.run(_config(tmp_path, months=3),
                             str(tmp_path / "p.db"), now=NOW)

    assert len(calls) == 3                 # 後面的月份照抓
    assert summary["searched"] == 3
    assert summary["errors"] == 1
    assert summary["recorded"] > 0         # 其他月份的觀測都寫進去了
    assert not runner_mod.all_searches_failed(summary)


def test_breaker_stops_calling_after_max_transport_errors(tmp_path, monkeypatch):
    Client, calls = _client_factory("all")
    monkeypatch.setattr(runner_mod, "TravelpayoutsClient", Client)

    summary = runner_mod.run(
        _config(tmp_path, months=10, routes=("TPE-NRT", "KHH-NGO")),
        str(tmp_path / "p.db"), now=NOW)

    assert len(calls) == runner_mod.MAX_TRANSPORT_ERRORS
    assert summary["searched"] == 20       # 跳過的月份仍算「該搜而沒搜到」
    assert summary["errors"] == 20
    assert summary["zero_record_routes"] == ["TPE-NRT", "KHH-NGO"]
    assert runner_mod.all_searches_failed(summary)


def test_breaker_counts_cumulative_not_consecutive(tmp_path, monkeypatch):
    # 時好時壞也要熔斷：每次逾時都是 60 秒，零星的也會累積到 job 上限
    Client, calls = _client_factory({1, 3, 5})
    monkeypatch.setattr(runner_mod, "TravelpayoutsClient", Client)

    summary = runner_mod.run(_config(tmp_path, months=10),
                             str(tmp_path / "p.db"), now=NOW)

    assert len(calls) == 5
    assert summary["errors"] == 3 + 5      # 3 次逾時 + 熔斷後跳過的 5 個月
    assert summary["recorded"] > 0
    assert not runner_mod.all_searches_failed(summary)


def test_http_errors_do_not_trip_breaker(tmp_path, monkeypatch):
    # HTTP 錯誤是秒回，不會拖時間，熔斷只管連線層錯誤
    calls = []

    class Client:
        def __init__(self, *a, **kw):
            pass

        def search_month(self, *a, **kw):
            calls.append(a)
            raise TravelpayoutsError(401, "Unauthorized")

    monkeypatch.setattr(runner_mod, "TravelpayoutsClient", Client)

    summary = runner_mod.run(_config(tmp_path, months=5),
                             str(tmp_path / "p.db"), now=NOW)

    assert len(calls) == 5
    assert summary["errors"] == 5
    # token 過期這類全面 HTTP 錯誤，以前是綠燈＋「錯誤 80 次」沒人發現
    assert runner_mod.all_searches_failed(summary)


def test_all_searches_failed_ignores_guard_skip_and_empty_results():
    skipped = {"searched": 0, "recorded": 0, "alerts": 0, "errors": 0,
               "empty": 0, "zero_record_routes": [], "skipped": True}
    assert not runner_mod.all_searches_failed(skipped)

    all_empty = {"searched": 10, "recorded": 0, "alerts": 0, "errors": 0,
                 "empty": 10, "zero_record_routes": ["KHH-NGO"]}
    assert not runner_mod.all_searches_failed(all_empty)   # 薄航線回空不是故障
