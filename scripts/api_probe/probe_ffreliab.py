"""fast-flights 穩定度測試（第三輪）。

前兩輪：價格與 SerpAPI 完全一致（KHH-NGO 12/07 14,891 IT），但 10 次只成功 4 次，
其中 TPE-NRT 11/30 這種一定有航班的行程也回 errorHasStatus。這輪要分清楚
「真的沒航班」「Google 限流／偶發錯誤」「我們的查詢寫法有問題」，並找出最穩的寫法。

1. repeat  ：6 組已知有航班的行程 × 基準寫法 × 3 次（輪流打，間隔 SPACING 秒）
2. variants：每組行程各試一次 any-stops（程式內篩直飛）／hl=zh-TW／沿用 cookie 的
             長連線 client／單程去程
3. retry   ：基準寫法失敗的，等 RETRY_WAIT 秒重打一次，看救不救得回來
4. burst   ：同一組行程 0.5 秒間隔連打 BURST 次，看會不會被限流
每個 (行程, 寫法) 彙整成一列；note 裡逐次列出結果碼：
  OK<價> 成功 | NF Google errorHasStatus | BLK 同意頁/驗證碼 | EMPTY 解析到 0 筆 | E:<例外>
"""
from __future__ import annotations

import datetime as dt
import time

from probe_fastflights import PIP_DEPS, TIMEOUT_S, _blocked, _parse  # noqa: F401

PROVIDER = "fast-flights-reliability"
CURRENCY = "TWD"
SPACING = 4.0
RETRY_WAIT = 10.0
BURST = 8

# 已知有直飛的行程（prices.db 的 SerpAPI/Scrape.do 觀測或前兩輪 fast-flights 成功）
ITINS = [
    ("KHH-NGO", "2026-12-07", "2026-12-12", "r2 OK 14891 IT"),
    ("KHH-CTS", "2026-11-13", "2026-11-18", "r2 OK 11094 SL"),
    ("TPE-NRT", "2026-11-29", "2026-12-04", "r1 OK 11426"),
    ("TPE-NRT", "2026-11-30", "2026-12-08", "r2 NF; serpapi TR 10088"),
    ("TPE-NRT", "2026-11-14", "2026-11-18", "scrapedo TR 9108"),
    ("TPE-NRT", "2027-04-23", "2027-04-28", "serpapi IT 10188"),
]


def _client():
    import primp
    return primp.Client(impersonate="chrome_145", impersonate_os="macos",
                        referer=True, cookie_store=True, timeout=TIMEOUT_S)


def _query(route, dep, ret, *, stops=0, lang="en-US", one_way=False):
    from fast_flights import FlightQuery, Passengers, create_query
    o, d = route.split("-")
    legs = [FlightQuery(date=dep, from_airport=o, to_airport=d, max_stops=stops)]
    if not one_way:
        legs.append(FlightQuery(date=ret, from_airport=d, to_airport=o, max_stops=stops))
    return create_query(flights=legs, trip="one-way" if one_way else "round-trip",
                        seat="economy", passengers=Passengers(adults=1),
                        language=lang, currency=CURRENCY)


def _attempt(client, q, nonstop_only=False):
    """打一次，回 (結果碼, 最低價或 None, 診斷字串)。"""
    from fast_flights.fetcher import URL
    from selectolax.lexbor import LexborHTMLParser
    try:
        res = client.get(URL, params=q.params())
        html = res.text
        blocked = _blocked(res.status_code, str(res.url), html)
        if blocked:
            return "BLK", None, f"{blocked} HTTP{res.status_code}"
        node = LexborHTMLParser(html).css_first(r"script.ds\:1")
        if node is None:
            return "E:NoDs1", None, f"{len(html)}B"
        data = node.text().split("data:", 1)[1].rsplit(",", 1)[0]
        if data.endswith("errorHasStatus: true"):
            return "NF", None, f"tail={data[-60:]!r} {len(html)}B"
        stock, top, err = _parse(html)
        items = list(stock) + list(top)
        if nonstop_only:
            items = [f for f in items if f.flights and len(f.flights) == 1]
        prices = sorted(f.price for f in items if isinstance(f.price, (int, float)) and f.price > 0)
        if not prices:
            return "EMPTY", None, err or ""
        return f"OK{prices[0]}", prices[0], ""
    except Exception as e:  # noqa: BLE001
        return f"E:{type(e).__name__}", None, str(e)[:60]


def _row(case, route, codes, prices, calls, extra=""):
    ok_n = sum(c.startswith("OK") for c in codes)
    return {"provider": PROVIDER, "case": case, "route": route,
            "ok": ok_n > 0, "results": ok_n, "dates": None,
            "min_price": float(min(prices)) if prices else None,
            "currency": CURRENCY, "carriers": None, "calls": calls,
            "note": (f"{ok_n}/{len(codes)} ok: " + " ".join(codes)
                     + (f" | {extra}" if extra else ""))[:300]}


def probe(env, today):
    try:
        import fast_flights  # noqa: F401
        import primp  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return [{"provider": PROVIDER, "case": "import", "route": "-", "ok": False,
                 "results": None, "dates": None, "min_price": None, "currency": None,
                 "carriers": None, "calls": 0,
                 "note": f"SKIPPED: import failed: {type(e).__name__}: {e}"[:300]}]

    rows, first = [], [True]

    def spaced(seconds=SPACING):
        if not first[0]:
            time.sleep(seconds)
        first[0] = False

    # 1. repeat：輪流打，避免同一組連打造成的偏差
    results = {i: [] for i in range(len(ITINS))}
    diags = {i: [] for i in range(len(ITINS))}
    for _ in range(3):
        for i, (route, dep, ret, _ref) in enumerate(ITINS):
            spaced()
            code, price, diag = _attempt(_client(), _query(route, dep, ret))
            results[i].append((code, price))
            if diag:
                diags[i].append(diag)

    # 3. retry：基準寫法失敗的各重打一次
    retried = {}
    for i, (route, dep, ret, _ref) in enumerate(ITINS):
        if any(not c.startswith("OK") for c, _ in results[i]):
            time.sleep(RETRY_WAIT)
            retried[i] = _attempt(_client(), _query(route, dep, ret))[:2]

    for i, (route, dep, ret, ref) in enumerate(ITINS):
        codes = [c for c, _ in results[i]]
        prices = [p for _, p in results[i] if p]
        extra = f"[{ref}]"
        if i in retried:
            extra += f" retry10s={retried[i][0]}"
        if diags[i]:
            extra += f" diag={diags[i][0]}"
        rows.append(_row(f"repeat x3 baseline {dep}/{ret}", route, codes, prices,
                         len(codes) + (1 if i in retried else 0), extra))

    # 2. variants：每組各一次
    persistent = _client()
    variants = [
        ("any-stops→filter nonstop", lambda r, d, t: (_client(), _query(r, d, t, stops=None), True)),
        ("hl=zh-TW", lambda r, d, t: (_client(), _query(r, d, t, lang="zh-TW"), False)),
        ("persistent client+cookies", lambda r, d, t: (persistent, _query(r, d, t), False)),
        ("one-way outbound", lambda r, d, t: (_client(), _query(r, d, t, one_way=True), False)),
    ]
    for label, build in variants:
        codes, prices = [], []
        for route, dep, ret, _ref in ITINS:
            spaced()
            client, q, nonstop_only = build(route, dep, ret)
            code, price, _diag = _attempt(client, q, nonstop_only=nonstop_only)
            codes.append(f"{route[-3:]}{dep[5:]}={code}")
            if price:
                prices.append(price)
        rows.append(_row(f"variant {label} (6 itineraries)", "*", codes, prices, len(codes)))

    # 4. burst：同一組 0.5 秒連打
    route, dep, ret, _ref = ITINS[2]
    codes, prices = [], []
    for k in range(BURST):
        if k:
            time.sleep(0.5)
        code, price, _diag = _attempt(_client(), _query(route, dep, ret))
        codes.append(code)
        if price:
            prices.append(price)
    rows.append(_row(f"burst x{BURST} @0.5s {dep}/{ret}", route, codes, prices, BURST))
    return rows
