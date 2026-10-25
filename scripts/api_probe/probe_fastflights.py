"""FareHunter probe: fast-flights 3.1.0 (unofficial Google Flights scraper, protobuf `tfs` URL).

API as read from the installed 3.1.0 source:
  create_query(flights=[FlightQuery(date, from_airport, to_airport, max_stops=...)],
               trip="round-trip", seat="economy", passengers=Passengers(adults=1),
               language=..., currency=...) -> Query
  get_flights(q, proxy=None, integration=None) == parser.parse(fetch_flights_html(q))
  fetch_flights_html: primp.Client(impersonate="chrome_145", impersonate_os="macos")
      GET https://www.google.com/travel/flights?tfs=<b64 protobuf>&hl=..&curr=..  (no timeout)
  Result: list of Flights(type, price:int, airlines:[names], flights:[SingleFlight], carbon).
3.x has no `fetch_mode` and no hosted fallback (v2's "fallback" went through a hosted
Playwright service). The only alternatives are paid integrations: BrightData (proxies the Google
URL via api.brightdata.com) and SearchApi (searchapi.io google_flights engine). Not used here.
No calendar / price-graph call exists, and the stock parser returns no low/typical/high level.

We pass our own FetchIntegration that builds the same primp client but adds a timeout and keeps
the HTTP status/final URL, so a consent or captcha page can be told apart from "0 flights".
The stock parser reads only payload[3] ("other flights"). We run the same parser on payload[2]
("top flights") too and report both counts.
"""

import datetime as dt
import json
import time

PROVIDER = "fast-flights"
# fast_flights 3.1.0 imports typing_extensions but does not declare it as a dependency.
PIP_DEPS = ["fast-flights==3.1.0", "primp==2.0.1", "typing_extensions==4.15.0"]
TIMEOUT_S = 45
SLEEP_S = 1.0
CURRENCY = "TWD"
STAY_DAYS = 5
# 第二輪：直接用 SerpAPI 最近實測過的同一組行程比價（prices.db 的 google 觀測），
# 第一輪挑的日期剛好沒有直飛（KHH-NGO 台灣虎航是週一班，11-29 是週日）。
# (route, depart, return, max_stops, SerpAPI 參考價)
CASES = [
    ("KHH-NGO", "2026-12-07", "2026-12-12", 0, "serpapi 10-23: 14891 IT"),
    ("KHH-CTS", "2026-11-13", "2026-11-18", 0, "serpapi 10-20: 11097 SL"),
    ("TPE-NRT", "2026-11-30", "2026-12-08", 0, "serpapi 10-24: 10088 TR"),
    ("KHH-NGO", "2027-01-08", "2027-01-13", 0, "round-1 TypeError repro"),
    ("KHH-NGO", "2027-01-04", "2027-01-09", 0, "Monday in Jan"),
]


def _row(case, route, ok=False, calls=0, note="", **fields):
    row = {"provider": PROVIDER, "case": case, "route": route, "ok": ok, "results": None,
           "dates": None, "min_price": None, "currency": None, "carriers": None,
           "calls": calls, "note": note[:300]}
    row.update(fields)
    return row


def _make_fetcher():
    import primp
    from fast_flights.fetcher import URL
    from fast_flights.integrations import FetchIntegration

    class DirectFetch(FetchIntegration):
        status = None
        final_url = ""

        def fetch_html(self, q, /):
            self.status, self.final_url = None, ""
            client = primp.Client(impersonate="chrome_145", impersonate_os="macos",
                                  referer=True, cookie_store=True, timeout=TIMEOUT_S)
            res = client.get(URL, params=q.params())
            self.status, self.final_url = res.status_code, str(res.url)
            return res.text

    return DirectFetch()


def _blocked(status, url, html):
    low = html.lower()
    if "consent.google" in url or "before you continue" in low:
        return "consent page / blocked"
    if "/sorry/" in url or "unusual traffic" in low:
        return "captcha (sorry page) / blocked"
    if status != 200:
        return "non-200 status"
    return None


def _parse(html):
    """Return (stock, top, top_err): stock = fast_flights' parse(html), top = same parser on payload[2]."""
    from fast_flights.parser import parse_js
    from selectolax.lexbor import LexborHTMLParser  # what fast_flights.parser.parse uses

    node = LexborHTMLParser(html).css_first(r"script.ds\:1")
    if node is None:
        raise LookupError("no <script class='ds:1'> in page (layout change or bot page)")
    js = node.text()
    try:
        stock = parse_js(js)
    except Exception as e:  # 3.1.0 parse_js 在 payload[3] 為 None 時會 TypeError
        stock, top_err = [], f"stock:{type(e).__name__}"
    else:
        top_err = None
    top = []
    try:
        payload = json.loads(js.split("data:", 1)[1].rsplit(",", 1)[0])
        if payload[2] and payload[2][0]:
            alt = list(payload)
            alt[3] = payload[2]
            top = parse_js("data:" + json.dumps(alt) + ", x")
    except Exception as e:
        top_err = (top_err + "," if top_err else "") + f"top:{type(e).__name__}"
    return stock, top, top_err


def _summary(items, code_by_name):
    prices = [float(f.price) for f in items if isinstance(f.price, (int, float)) and f.price > 0]
    dates = {f.flights[0].departure.date for f in items if f.flights}
    carriers = []
    for f in items:
        for name in f.airlines or []:
            code = code_by_name.get(name, name)
            if code not in carriers:
                carriers.append(code)
    stops = max((len(f.flights) - 1 for f in items if f.flights), default=None)
    return {"results": len(items), "dates": len(dates), "min_price": min(prices, default=None),
            "carriers": ",".join(carriers[:8]) or None}, stops


def _run_case(fetcher, today, route, dep, ret, max_stops, ref):
    from fast_flights import FlightQuery, FlightsNotFound, Passengers, create_query, fetch_flights_html

    origin, dest = route.split("-")
    dep, ret = dt.date.fromisoformat(dep), dt.date.fromisoformat(ret)
    stops_label = "nonstop" if max_stops == 0 else "any-stops"
    case = f"{route} RT {dep.isoformat()}/{ret.isoformat()} {stops_label} [{ref}]"
    calls = 0
    try:
        q = create_query(
            flights=[FlightQuery(date=dep.isoformat(), from_airport=origin, to_airport=dest, max_stops=max_stops),
                     FlightQuery(date=ret.isoformat(), from_airport=dest, to_airport=origin, max_stops=max_stops)],
            trip="round-trip", seat="economy", passengers=Passengers(adults=1),
            language="en-US", currency=CURRENCY)
        calls = 1
        html = fetch_flights_html(q, fetch_integration=fetcher)
        blocked = _blocked(fetcher.status, fetcher.final_url, html)
        if blocked:
            return _row(case, route, calls=calls, note=f"{blocked}; HTTP {fetcher.status}; {len(html)} bytes")
        stock, top, top_err = _parse(html)
        meta = getattr(stock, "metadata", None) or getattr(top, "metadata", None)
        code_by_name = {a.name: a.code for a in meta.airlines} if meta else {}
        fields, stops = _summary(list(stock) + list(top), code_by_name)
        cheapest = sorted((f.price, ",".join(f.airlines or [])) for f in list(stock) + list(top)
                          if isinstance(f.price, (int, float)) and f.price > 0)[:3]
        note = (f"HTTP 200; stock={len(stock)} top={len(top)}"
                + (f" ({top_err})" if top_err else "")
                + f"; max stops={stops}; cheapest3={cheapest}")
        return _row(case, route, ok=True, calls=calls, currency=CURRENCY, note=note, **fields)
    except FlightsNotFound as e:
        return _row(case, route, calls=calls, note=f"FlightsNotFound (Google errorHasStatus): {e}")
    except Exception as e:
        return _row(case, route, calls=calls, note=f"{type(e).__name__}: {e}")


def probe(env, today):
    try:
        import fast_flights  # noqa: F401
    except Exception as e:
        return [_row("import fast_flights", "-", note=f"SKIPPED: fast_flights import failed: {type(e).__name__}: {e}")]
    rows = []
    try:
        fetcher = _make_fetcher()
        for i, (route, dep, ret, max_stops, ref) in enumerate(CASES):
            if i:
                time.sleep(SLEEP_S)
            rows.append(_run_case(fetcher, today, route, dep, ret, max_stops, ref))
    except Exception as e:
        rows.append(_row("setup", "-", note=f"{type(e).__name__}: {e}"))
    return rows
