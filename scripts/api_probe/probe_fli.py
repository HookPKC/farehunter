"""FareHunter probe: fli = PyPI `flights` 0.9.0 (import name `fli`, github.com/punitarani/fli).

API as read from the installed 0.9.0 source (not guessed):
  Date search ("cheapest dates" / price calendar):
    SearchDates().search(DateSearchFilters(trip_type=TripType.ROUND_TRIP, passenger_info=PassengerInfo(adults=1),
        flight_segments=build_date_search_segments(origin, dest, start, trip_duration=5, is_round_trip=True)[0],
        stops=MaxStops.NON_STOP, seat_type=SeatType.ECONOMY, from_date, to_date, duration=5),
        currency=, language=, country=) -> list[DatePrice(date=(out, ret), price, currency)] | None
    = ONE POST to www.google.com/_/FlightsFrontendUi/data/travel.frontend.flights.FlightsFrontendService/
      GetCalendarGraph?curr=&hl=  (body f.req=<json>). Round trip = fixed stay (`duration`, sent as [5, 5]);
      from/to bound the outbound date. Ranges > 61 days (MAX_DAYS_PER_SEARCH) are split into parallel 61-day
      chunks; docstring says <= 305 days ahead. Rows without a price are dropped by the library; the currency
      is decoded from a per-row protobuf token. Calendar rows carry no carrier.
  Flight search: SearchFlights().search(FlightSearchFilters(..., sort_by=SortBy.CHEAPEST), top_n, currency=...)
    = POST .../GetShoppingResults, then for a round trip top_n MORE calls (one per outbound, parallel threads)
      listing the return legs. The round-trip total sits on the outbound rows (fli's CLI: "Google Flights
      returns the full RT price on the outbound leg").
  HTTP: fli.search.client.Client (singleton via get_client()): curl_cffi.requests.Session with impersonate="chrome"
    per request, timeout = env FLI_TIMEOUT or 60 s, a global token bucket of 10 req/s, and tenacity
    @retry(stop_after_attempt(3), wait_exponential()) on get/post, so one search can silently be 3 requests.
    Errors: SearchHTTPError(status_code) / SearchTimeoutError / SearchConnectionError / SearchClientError.
  Hosts: only www.google.com (the opt-in Playwright booking-token helper also opens google.com; unused).
  No key, no third-party service.

What this module changes: each SearchDates/SearchFlights instance gets a _ProbeClient (fli Client subclass) whose
post() has NO retry (1 attempt = 1 counted call), a hard cap of MAX_CALLS, >= SLEEP_S between requests and a
record of the last exchange (status, final path, size, body), so a block/throttle page can be told apart from
"no flights". Flight searches use top_n=1 (1 outbound + 1 return-leg call): 4 x 1 + 3 x 2 = 10 calls.
The comparison cases use fixed dates on purpose (one-off check against earlier fast-flights/SerpAPI results);
the calendar windows are computed from `today`.
"""

import datetime as dt
import re
import threading
import time
from urllib.parse import urlparse

PROVIDER = "fli"
# fli 0.9.0 declares all its runtime deps (checked by importing in a clean venv); pins are for reproducibility.
# pydantic stays at 2.13.x: 2.14 needs typing_extensions>=4.16 but probe_fastflights pins 4.15.0, and the
# harness installs every module's PIP_DEPS in a single pip call.
PIP_DEPS = ["flights==0.9.0", "curl_cffi==0.16.3", "pydantic==2.13.5", "tenacity==9.2.1", "babel==2.18.0"]
TIMEOUT_S = 45
SLEEP_S = 3.0          # minimum gap between any two Google requests
MAX_CALLS = 12         # hard budget for the whole module
CURRENCY = "TWD"
LANGUAGE = "en-US"     # same hl/curr as probe_fastflights (no gl), so prices are comparable
STAY_DAYS = 5
# Calendar cases: (route, from offset, to offset, reference outbound date, reference RT price TWD).
CAL_CASES = [
    ("KHH-NGO", 14, 74, "2026-12-07", 14891),   # fast-flights + SerpAPI: 14,891 IT nonstop
    ("KHH-CTS", 14, 74, "2026-11-13", 11094),   # fast-flights: 11,094 SL
    ("TPE-NRT", 14, 74, "2026-11-29", 11426),   # control; fast-flights: 11,426, 22 options
    ("KHH-NGO", 75, 135, None, None),           # second window: how far ahead does it price?
]
# Flight cases: (route, depart, return, reference price, reference carrier, what we compare against).
FLIGHT_CASES = [
    ("KHH-NGO", "2026-12-07", "2026-12-12", 14891, "IT", "ff/SerpAPI 14891 IT"),
    ("TPE-NRT", "2026-11-30", "2026-12-08", 10088, "TR", "SerpAPI ~10088 TR, ff error"),
    ("KHH-CTS", "2026-11-13", "2026-11-18", 11094, "SL", "ff 11094 SL"),
]
WD = "MoTuWeThFrSaSu"


class BudgetExceeded(RuntimeError):
    """Raised instead of sending a request once MAX_CALLS requests were made."""


def _row(case, route, ok=False, calls=0, note="", **fields):
    row = {"provider": PROVIDER, "case": case, "route": route, "ok": ok, "results": None,
           "dates": None, "min_price": None, "currency": None, "carriers": None,
           "calls": calls, "note": note[:300]}
    row.update(fields)
    return row


def _wd(d):
    return WD[d.weekday() * 2:d.weekday() * 2 + 2]


def _snip(text, n=90):
    """Short single-line preview of a response body (HTML tags and the )]}' prefix stripped)."""
    t = (text or "").lstrip()
    if t.startswith(")]}'"):
        t = t[4:].lstrip().lstrip("0123456789")
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t).strip()[:n]


def _make_client():
    from fli.search.client import Client, _wrap_request_error
    from fli.search.exceptions import SearchHTTPError

    class _ProbeClient(Client):
        def __init__(self):
            super().__init__()
            self.calls, self.last, self.last_text = 0, {}, ""
            self._gate = threading.Lock()
            self._next_at = 0.0

        def get(self, url, **kw):
            return self._send("GET", url, kw)

        def post(self, url, **kw):
            return self._send("POST", url, kw)

        def _send(self, method, url, kw):
            with self._gate:  # also serialises fli's parallel workers
                if self.calls >= MAX_CALLS:
                    self.last, self.last_text = {}, ""
                    raise BudgetExceeded(f"probe budget of {MAX_CALLS} requests used up; not sent")
                wait = self._next_at - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self.calls += 1
                self.last, self.last_text = {"rpc": urlparse(url).path.rsplit("/", 1)[-1]}, ""
                self._rate_limiter.acquire()
                kw["timeout"] = TIMEOUT_S
                try:
                    session = self._session()
                    resp = (session.post if method == "POST" else session.get)(url, **kw)
                except Exception as e:
                    self.last["cause"] = f"{type(e).__name__}: {e}"
                    raise _wrap_request_error(method, url, e) from e
                finally:
                    self._next_at = time.monotonic() + SLEEP_S
            text = resp.text or ""
            content = getattr(resp, "content", None)
            self.last_text = text
            self.last.update(status=resp.status_code, path=urlparse(str(resp.url)).path,
                             nbytes=len(content) if isinstance(content, (bytes, bytearray)) else len(text))
            if resp.status_code >= 400:
                raise SearchHTTPError(f"Google Flights returned HTTP {resp.status_code}",
                                      status_code=resp.status_code)
            return resp

    return _ProbeClient()


def _diag(client, body=False):
    """Describe the last HTTP exchange: status, size, redirect target, block page, body preview, cause."""
    last = client.last
    if not last:
        return "no request sent"
    parts = []
    if "status" in last:
        status, text, path = last["status"], client.last_text, last.get("path") or ""
        parts.append(f"HTTP {status} {last['nbytes']}B")
        if "FlightsFrontendService" not in path:
            parts.append(f"final {path[:40]}")
        low = text[:6000].lower()
        if "unusual traffic" in low or "/sorry/" in path:
            parts.append("captcha/unusual-traffic page")
        elif "<html" in low and "consent" in low:
            parts.append("consent page")
        if body or status >= 400 or not text.lstrip().startswith(")]}'"):
            parts.append(f"body '{_snip(text)}'")
    if "cause" in last:
        parts.append(f"cause {last['cause'][:110]}")
    return "; ".join(parts)


def _fail(case, route, client, before, e):
    return _row(case, route, calls=client.calls - before,
                note=f"{type(e).__name__}: {str(e)[:110]}; {_diag(client)}")


def _calendar_case(client, today, route, a, b, ref_date, ref_price):
    case, before = f"{route} calendar", client.calls
    client.last, client.last_text = {}, ""
    try:
        from fli.core import build_date_search_segments
        from fli.models import Airport, DateSearchFilters, MaxStops, PassengerInfo, SeatType
        from fli.search import SearchDates
        from fli.search._wire import parse_first_wrb_payload

        origin, dest = route.split("-")
        start, end = today + dt.timedelta(days=a), today + dt.timedelta(days=b)
        case = f"{route} calendar RT stay={STAY_DAYS}d out {start}..{end} nonstop curr={CURRENCY}"
        segments, trip = build_date_search_segments(
            getattr(Airport, origin), getattr(Airport, dest), start.isoformat(),
            trip_duration=STAY_DAYS, is_round_trip=True)
        filters = DateSearchFilters(
            trip_type=trip, passenger_info=PassengerInfo(adults=1), flight_segments=segments,
            stops=MaxStops.NON_STOP, seat_type=SeatType.ECONOMY,
            from_date=start.isoformat(), to_date=end.isoformat(), duration=STAY_DAYS)
        search = SearchDates()
        search.client = client
        priced = search.search(filters, currency=CURRENCY, language=LANGUAGE)
        calls = client.calls - before
        payload = parse_first_wrb_payload(client.last_text)
        if priced is None and payload is None:
            return _row(case, route, calls=calls, note=f"no wrb.fr payload; {_diag(client, body=True)}")
        rows = payload[-1] if isinstance(payload, list) and payload and isinstance(payload[-1], list) else []
        by_date = {}
        for p in priced or []:
            d = p.date[0].date()
            by_date[d] = min(by_date.get(d, p.price), p.price)
        stays = sorted({(p.date[1] - p.date[0]).days for p in priced or [] if len(p.date) > 1})
        curs = sorted({p.currency for p in priced or [] if p.currency})
        cheapest = sorted(by_date.items(), key=lambda kv: (kv[1], kv[0]))
        top5 = ",".join(f"{d:%m-%d}{_wd(d)}={p:.0f}" for d, p in cheapest[:5])
        ref = ""
        if ref_date:
            rd = dt.date.fromisoformat(ref_date)
            if not start <= rd <= end:
                ref = f"ref {rd:%m-%d} outside range; "
            elif rd in by_date:
                ref = f"ref {rd:%m-%d}={by_date[rd]:.0f} vs {ref_price} ({by_date[rd] - ref_price:+.0f}); "
            else:
                ref = f"ref {rd:%m-%d} NOT priced (ref {ref_price}); "
        wd = ",".join(f"{WD[i * 2:i * 2 + 2]}{n}" for i in range(7)
                      if (n := sum(1 for d in by_date if d.weekday() == i)))
        span = f"{min(by_date):%m-%d}..{max(by_date):%m-%d}" if by_date else "-"
        note = (f"{len(by_date)}/{(end - start).days + 1} days priced ({len(rows)} rows), out {span}; "
                f"stay {stays or '-'}; cur {','.join(curs) or 'none(req ' + CURRENCY + ')'}; "
                f"cheap5 {top5 or '-'}; {ref}wd {wd or '-'}; {_diag(client, body=priced is None)}")
        return _row(case, route, ok=True, calls=calls, results=len(priced or []), dates=len(by_date),
                    min_price=cheapest[0][1] if cheapest else None,
                    currency=",".join(curs) or None, note=note)
    except Exception as e:
        return _fail(case, route, client, before, e)


def _code(leg):
    return leg.airline.name.removeprefix("_")


def _flight_case(client, route, dep, ret, ref_price, ref_carrier, ref_label):
    case = f"{route} flights RT {dep}->{ret} nonstop curr={CURRENCY} sort=CHEAPEST top_n=1"
    before = client.calls
    client.last, client.last_text = {}, ""
    try:
        from fli.core import build_flight_segments
        from fli.models import Airport, FlightSearchFilters, MaxStops, PassengerInfo, SeatType, SortBy
        from fli.search import SearchFlights
        from fli.search._wire import parse_first_wrb_payload

        class KeepOutbound(SearchFlights):
            """search() returns only (outbound, return) combos; keep the outbound list (RT totals) too."""
            outbound = None

            def _expand_multi_leg(self, flights, filters, **kw):
                if self.outbound is None:
                    self.outbound = flights
                return super()._expand_multi_leg(flights, filters, **kw)

        origin, dest = route.split("-")
        segments, trip = build_flight_segments(getattr(Airport, origin), getattr(Airport, dest), dep, ret)
        filters = FlightSearchFilters(
            trip_type=trip, passenger_info=PassengerInfo(adults=1), flight_segments=segments,
            stops=MaxStops.NON_STOP, seat_type=SeatType.ECONOMY, sort_by=SortBy.CHEAPEST)
        search = KeepOutbound()
        search.client = client
        combos, ret_err = None, None
        try:
            combos = search.search(filters, top_n=1, currency=CURRENCY, language=LANGUAGE)
        except Exception as e:
            if search.outbound is None:
                raise
            ret_err = f"{type(e).__name__}: {str(e)[:80]}; {_diag(client)}"
        calls = client.calls - before
        out = search.outbound
        if not out:
            if parse_first_wrb_payload(client.last_text) is None:
                return _row(case, route, calls=calls, note=f"no wrb.fr payload; {_diag(client, body=True)}")
            return _row(case, route, ok=True, calls=calls, results=0, dates=0,
                        note=f"0 itineraries ({ref_label}); {_diag(client, body=True)}")
        carriers = []
        rets = [c[-1] for c in combos or [] if isinstance(c, tuple)]
        for f in out + rets:
            for leg in f.legs:
                if _code(leg) not in carriers:
                    carriers.append(_code(leg))
        priced = [f for f in out if f.price is not None]
        best = min(priced, key=lambda f: f.price, default=None)
        curs = sorted({f.currency for f in out if f.currency})
        if best:
            legs = "+".join(_code(leg) + leg.flight_number for leg in best.legs)
            seen = "in" if ref_carrier in carriers else "NOT in"
            head = (f"cheapest {best.price:.0f} {legs} dep {best.legs[0].departure_datetime:%m-%d %H:%M}; "
                    f"vs {ref_label}: {best.price - ref_price:+.0f}, {ref_carrier} {seen} results")
        else:
            head = f"no priced row ({ref_label})"
        if ret_err:
            tail = f"return-leg call failed: {ret_err}"
        elif rets:
            rmin = min((r for r in rets if r.price is not None), key=lambda r: r.price, default=None)
            tail = f"return legs {len(rets)}" + (
                f", min row {rmin.price:.0f} {_code(rmin.legs[0])}{rmin.legs[0].flight_number}" if rmin else "")
        else:
            tail = "return legs 0"
        note = (f"{head}; {len(out)} outbound opts, max stops {max(f.stops for f in out)}; "
                f"cur {','.join(curs) or 'none'}; {tail}" + ("" if ret_err else f"; {_diag(client)}"))
        return _row(case, route, ok=True, calls=calls, results=len(out),
                    dates=len({f.legs[0].departure_datetime.date() for f in out if f.legs}),
                    min_price=best.price if best else None, currency=",".join(curs) or None,
                    carriers=",".join(carriers[:8]) or None, note=note)
    except Exception as e:
        return _fail(case, route, client, before, e)


def probe(env, today):
    try:
        import curl_cffi  # noqa: F401  (fli imports it lazily on the first request)
        import fli.core  # noqa: F401
        import fli.models  # noqa: F401
        import fli.search  # noqa: F401
    except Exception as e:
        return [_row("import fli", "-",
                     note=f"SKIPPED: fli (pip flights) import failed: {type(e).__name__}: {e}")]
    rows = []
    try:
        client = _make_client()
        for spec in CAL_CASES:
            rows.append(_calendar_case(client, today, *spec))
        for spec in FLIGHT_CASES:
            rows.append(_flight_case(client, *spec))
    except Exception as e:
        rows.append(_row("setup", "-", note=f"{type(e).__name__}: {e}"))
    return rows
