"""fli 正式規模壓測（第五輪）。

第四輪：fli 10 次請求全成功，日曆與即時查詢的參考價全部吻合。這輪用「真的上線會跑
的量」測：config.yaml 全部 8 條航線 × 未來約 300 天（5 個 61 天窗）= 40 次日曆請求，
再對每條航線日曆上最便宜的那天做一次即時查詢（每次 2 個請求），驗證日曆價準不準。
請求間隔沿用 probe_fli 的 SLEEP_S、不重試，所以成功率就是單次成功率。
"""
from __future__ import annotations

import datetime as dt
import time

import probe_fli
from probe_fli import PIP_DEPS, _flight_case, _make_client, _row  # noqa: F401

PROVIDER = "fli-sweep"
ROUTES = ["TPE-NRT", "TPE-KIX", "KHH-NRT", "KHH-KIX", "KHH-CTS", "KHH-FUK", "KHH-OKA", "KHH-NGO"]
WINDOWS = [(1, 61), (62, 122), (123, 183), (184, 244), (245, 305)]
STAY = probe_fli.STAY_DAYS
probe_fli.MAX_CALLS = 60          # 40 日曆 + 8×2 即時查詢


def _calendar(client, origin, dest, start, end):
    from fli.core import build_date_search_segments
    from fli.models import Airport, DateSearchFilters, MaxStops, PassengerInfo, SeatType
    from fli.search import SearchDates

    segments, trip = build_date_search_segments(
        getattr(Airport, origin), getattr(Airport, dest), start.isoformat(),
        trip_duration=STAY, is_round_trip=True)
    filters = DateSearchFilters(
        trip_type=trip, passenger_info=PassengerInfo(adults=1), flight_segments=segments,
        stops=MaxStops.NON_STOP, seat_type=SeatType.ECONOMY,
        from_date=start.isoformat(), to_date=end.isoformat(), duration=STAY)
    search = SearchDates()
    search.client = client
    priced = search.search(filters, currency=probe_fli.CURRENCY, language=probe_fli.LANGUAGE)
    by_date = {}
    for p in priced or []:
        d = p.date[0].date()
        by_date[d] = min(by_date.get(d, p.price), p.price)
    return by_date


def probe(env, today):
    try:
        import fli.search  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return [_row("import fli", "-", note=f"SKIPPED: {type(e).__name__}: {e}")]
    client = _make_client()
    rows, t0 = [], time.monotonic()
    anchor = today + dt.timedelta(days=43)        # 與 Kiwi 交叉比對用的同一天
    for route in ROUTES:
        origin, dest = route.split("-")
        marks, all_days, secs, err = "", {}, [], ""
        for a, b in WINDOWS:
            start, end = today + dt.timedelta(days=a), today + dt.timedelta(days=b)
            t = time.monotonic()
            try:
                days = _calendar(client, origin, dest, start, end)
                marks += "o" if days else "0"
                all_days.update(days)
            except Exception as e:  # noqa: BLE001
                marks += "x"
                err = err or f"{type(e).__name__}: {str(e)[:60]}"
            secs.append(time.monotonic() - t)
        cheapest = sorted(all_days.items(), key=lambda kv: (kv[1], kv[0]))
        near = [kv for kv in cheapest if kv[0] <= today + dt.timedelta(days=122)]
        top = ",".join(f"{d:%m-%d}={p:.0f}" for d, p in cheapest[:3])
        span = f"{min(all_days):%m-%d}..{max(all_days):%Y-%m-%d}" if all_days else "-"
        rows.append(_row(
            f"{route} calendar 5×61d RT{STAY}d nonstop", route, ok=bool(all_days),
            calls=len(WINDOWS), results=marks.count("o"), dates=len(all_days),
            min_price=cheapest[0][1] if cheapest else None, currency=probe_fli.CURRENCY,
            note=(f"windows {marks}; {len(all_days)} days {span}; cheap3 {top or '-'}; "
                  f"anchor {anchor:%m-%d}={all_days.get(anchor, '-')}; "
                  f"avg {sum(secs) / len(secs):.1f}s/req" + (f"; {err}" if err else ""))))
        # 即時查詢驗證日曆上 4 個月內最便宜那天
        if near:
            d, p = near[0]
            rows.append(_flight_case(client, route, d.isoformat(),
                                     (d + dt.timedelta(days=STAY)).isoformat(),
                                     p, "", f"calendar {d:%m-%d}={p:.0f}"))
    rows.append(_row("sweep total", "*", ok=True, calls=0,
                     note=f"{(time.monotonic() - t0) / 60:.1f} min, {client.calls} Google requests"))
    return rows
