"""fli 失敗項目重試（第六輪）。

第五輪：日曆 40 次全部 HTTP 200，但 9 個 61 天窗回空——KHH-OKA 五窗全空（Kiwi 卻查到
KHH-OKA 直飛 5,805）、KHH-KIX 中間兩窗空；即時查詢 7 次有 3 次 Google 回內部錯誤
（wrb.fr status 13）。成功的 4 次與日曆價完全一致。

這輪只重打那些失敗項目，每項最多 ATTEMPTS 次、間隔 RETRY_WAIT 秒，分清楚：
  回空／錯誤是「重試就好」（偶發）還是「每次都一樣」（真的沒有、或 fli 不支援）。
"""
from __future__ import annotations

import datetime as dt
import time

import probe_fli
from probe_fli import PIP_DEPS, _flight_case, _make_client, _row  # noqa: F401
from probe_flisweep import _calendar

PROVIDER = "fli-retry"
ATTEMPTS = 3
RETRY_WAIT = 12.0
probe_fli.MAX_CALLS = 50

# (route, 窗起, 窗迄)：第五輪回空的窗，加一個「可能真的沒班表」的對照（TPE-NRT 最後一窗）
CAL_ITEMS = [("KHH-OKA", 1, 61), ("KHH-OKA", 62, 122), ("KHH-KIX", 62, 122),
             ("KHH-KIX", 123, 183), ("TPE-NRT", 245, 305)]
# (route, 去, 回)：第五輪回 status 13 的即時查詢，加 KHH-OKA（日曆全空，看即時查詢有沒有）
FLIGHT_ITEMS = [("TPE-KIX", "2026-12-16", "2026-12-21"), ("KHH-KIX", "2026-10-29", "2026-11-03"),
                ("KHH-CTS", "2026-10-30", "2026-11-04"), ("KHH-OKA", "2026-12-07", "2026-12-12")]


def probe(env, today):
    try:
        import fli.search  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return [_row("import fli", "-", note=f"SKIPPED: {type(e).__name__}: {e}")]
    client = _make_client()
    rows = []
    for route, a, b in CAL_ITEMS:
        origin, dest = route.split("-")
        start, end = today + dt.timedelta(days=a), today + dt.timedelta(days=b)
        seq, days, before = [], {}, client.calls
        for k in range(ATTEMPTS):
            if k:
                time.sleep(RETRY_WAIT)
            try:
                days = _calendar(client, origin, dest, start, end)
                seq.append(f"o{len(days)}" if days else "0")
                if days:
                    break
            except Exception as e:  # noqa: BLE001
                seq.append(f"x:{type(e).__name__}")
        cheapest = min(days.items(), key=lambda kv: kv[1]) if days else None
        rows.append(_row(f"{route} calendar {start}..{end} retry", route, ok=bool(days),
                         calls=client.calls - before, dates=len(days),
                         min_price=cheapest[1] if cheapest else None, currency="TWD",
                         note=f"attempts {' '.join(seq)}"
                              + (f"; cheapest {cheapest[0]:%m-%d}={cheapest[1]:.0f}" if cheapest else "")
                              + f"; last {probe_fli._diag(client)}"[:200]))
    for route, dep, ret in FLIGHT_ITEMS:
        seq, last, before = [], None, client.calls
        for k in range(ATTEMPTS):
            if k:
                time.sleep(RETRY_WAIT)
            last = _flight_case(client, route, dep, ret, 0, "", "retry")
            ok = last["ok"] and last["min_price"]
            seq.append(f"o{last['min_price']:.0f}" if ok else "x")
            if ok:
                break
        last = dict(last)
        last.update(provider=PROVIDER, case=f"{route} flights {dep}->{ret} retry",
                    calls=client.calls - before,
                    note=(f"attempts {' '.join(seq)}; " + last["note"])[:300])
        rows.append(last)
    return rows
