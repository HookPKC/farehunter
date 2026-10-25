"""fli 的 OKA 機場 bug 驗證（第七輪）。

fli 0.9.0 用 Enum("Airport", {代碼: 機場名}) 建機場表，機場名重複的 48 個代碼會變成別名：
Airport.OKA（那霸）其實是 Airport.NAH（印尼 Tahuna 的 Naha Airport），所以 KHH-OKA
查詢送出去的是 NAH，日曆與即時查詢永遠是 0。繞法：自建一個 _name_="OKA" 的 Airport
成員（序列化只讀 .name）。離線已確認請求內容從 NAH 變成 OKA；這裡打真的 Google 確認。
"""
from __future__ import annotations

import datetime as dt
import time

import probe_fli
from probe_fli import PIP_DEPS, _diag, _make_client, _row  # noqa: F401

PROVIDER = "fli-oka"


def _oka():
    from fli.models import Airport
    m = object.__new__(Airport)
    m._name_, m._value_ = "OKA", "Naha Airport (Okinawa)"
    return m


def probe(env, today):
    try:
        from fli.core import build_date_search_segments, build_flight_segments
        from fli.models import (Airport, DateSearchFilters, FlightSearchFilters, MaxStops,
                                PassengerInfo, SeatType, SortBy)
        from fli.search import SearchDates, SearchFlights
    except Exception as e:  # noqa: BLE001
        return [_row("import fli", "-", note=f"SKIPPED: {type(e).__name__}: {e}")]
    client, rows, oka = _make_client(), [], _oka()
    start, end = today + dt.timedelta(days=1), today + dt.timedelta(days=61)
    try:
        seg, trip = build_date_search_segments(Airport.KHH, oka, start.isoformat(),
                                               trip_duration=5, is_round_trip=True)
        s = SearchDates()
        s.client = client
        priced = s.search(DateSearchFilters(
            trip_type=trip, passenger_info=PassengerInfo(adults=1), flight_segments=seg,
            stops=MaxStops.NON_STOP, seat_type=SeatType.ECONOMY, from_date=start.isoformat(),
            to_date=end.isoformat(), duration=5), currency="TWD", language="en-US") or []
        by = {}
        for p in priced:
            by[p.date[0].date()] = min(by.get(p.date[0].date(), p.price), p.price)
        top = sorted(by.items(), key=lambda kv: kv[1])[:3]
        rows.append(_row(f"KHH-OKA calendar {start}..{end} (OKA workaround)", "KHH-OKA",
                         ok=bool(by), calls=client.calls, dates=len(by),
                         min_price=top[0][1] if top else None, currency="TWD",
                         note=f"{len(by)} days; cheap3 {','.join(f'{d:%m-%d}={p:.0f}' for d, p in top)}; "
                              f"{_diag(client)}"))
    except Exception as e:  # noqa: BLE001
        rows.append(_row("KHH-OKA calendar (OKA workaround)", "KHH-OKA", calls=client.calls,
                         note=f"{type(e).__name__}: {e}; {_diag(client)}"))
    before = client.calls
    dep = today + dt.timedelta(days=43)
    try:
        seg, trip = build_flight_segments(Airport.KHH, oka, dep.isoformat(),
                                          (dep + dt.timedelta(days=5)).isoformat())
        f = SearchFlights()
        f.client = client
        out = []
        orig = f._expand_multi_leg

        def keep(flights, filters, **kw):
            out.extend(flights)
            return orig(flights, filters, **kw)
        f._expand_multi_leg = keep
        try:
            f.search(FlightSearchFilters(trip_type=trip, passenger_info=PassengerInfo(adults=1),
                                         flight_segments=seg, stops=MaxStops.NON_STOP,
                                         seat_type=SeatType.ECONOMY, sort_by=SortBy.CHEAPEST),
                     top_n=1, currency="TWD", language="en-US")
        except Exception:  # noqa: BLE001 — 回程那次失敗也保留去程（含來回總價）
            pass
        best = min((x for x in out if x.price), key=lambda x: x.price, default=None)
        legs = "+".join(l.airline.name.removeprefix("_") + l.flight_number for l in best.legs) if best else "-"
        rows.append(_row(f"KHH-OKA flights {dep}+5d (OKA workaround)", "KHH-OKA", ok=bool(best),
                         calls=client.calls - before, results=len(out),
                         min_price=best.price if best else None, currency="TWD",
                         note=f"cheapest {best.price if best else '-'} {legs}; Kiwi 12-07±3 nonstop 5805; "
                              f"{_diag(client)}"))
    except Exception as e:  # noqa: BLE001
        rows.append(_row("KHH-OKA flights (OKA workaround)", "KHH-OKA", calls=client.calls - before,
                         note=f"{type(e).__name__}: {e}; {_diag(client)}"))
    return rows
