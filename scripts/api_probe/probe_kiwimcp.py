"""FareHunter probe: Kiwi.com public MCP server (https://mcp.kiwi.com), keyless.

Not Tequila: this is Kiwi's own remote MCP endpoint for AI agents (Streamable HTTP,
JSON-RPC 2.0, no auth). Listed as "Authentication: None" on MCP directories (verified
2026-08-18) and used keyless by FlightScout (Sep 2026) and MikkoParkkola/trvl (2026).
Tool `search-flight` returns real Kiwi-sellable itineraries with price + booking link,
with +-N day flexibility on both departure and return (a small date grid per call).

Server schema drift seen in the wild:
  trvl (server v1.15.0+, stateless, no mcp-session-id): flyFrom, flyTo, departureDate
    (dd/mm/yyyy), returnDate, departureDateFlexRange/returnDateFlexRange (0..3),
    passengers {adults}, curr, sort, cabinClass M, locale
  FlightScout: adults, currency, departureDateFlexDays, ...
So we read `tools/list` (when the server is stateless) and pick the names it advertises.

Requests (4): initialize; tools/list (stateless) or notifications/initialized (if the
server hands out an mcp-session-id); search-flight KHH>NGO RT; search-flight TPE>NRT RT.
ToS: an official public agent endpoint -> lowest ToS risk of the three; still a free
service with no SLA, low volume only.
"""
from __future__ import annotations

import datetime as dt
import json
import time

PROVIDER = "kiwi-mcp"
PIP_DEPS: list[str] = []
URL = "https://mcp.kiwi.com"
PROTOCOL = "2025-06-18"
TIMEOUT_S = 55          # a Kiwi search takes ~10 s server-side
SLEEP_S = 3.0
CURRENCY = "TWD"
STAY_DAYS = 5
DEPART_DAYS = 43        # on 2026-10-25: 2026-12-07 -> 12-12 (the Google reference trip)
FLEX = 3
TOOL = "search-flight"
CASES = [("KHH-NGO", DEPART_DAYS), ("TPE-NRT", DEPART_DAYS)]
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
           "User-Agent": "farehunter-probe/0.1 (+https://github.com/HookPKC/farehunter)"}

_sleep = time.sleep


def _new_session():
    import requests
    return requests.Session()


def _row(case, route, ok=False, calls=0, note="", **kw):
    row = {"provider": PROVIDER, "case": case, "route": route, "ok": bool(ok),
           "results": None, "dates": None, "min_price": None, "currency": None,
           "carriers": None, "calls": int(calls), "note": str(note)[:300]}
    row.update(kw)
    row["note"] = str(row["note"])[:300]
    return row


def _err(exc):
    return f"{type(exc).__name__}: {str(exc)[:220]}"


class RpcError(RuntimeError):
    pass


def _decode(resp):
    """JSON-RPC message from a JSON or SSE (text/event-stream) body."""
    text = resp.text or ""
    if resp.status_code not in (200, 202):
        raise RpcError(f"HTTP {resp.status_code} body={text[:140]!r}")
    if not text.strip():
        return None
    msgs = []
    if text.lstrip().startswith(("{", "[")):
        msgs.append(json.loads(text))
    else:
        data = []
        for line in text.splitlines() + [""]:
            if line.startswith("data:"):
                data.append(line[5:].strip())
            elif not line.strip() and data:
                try:
                    msgs.append(json.loads("\n".join(data)))
                except ValueError:
                    pass
                data = []
    for m in msgs:
        if isinstance(m, dict) and ("result" in m or "error" in m):
            return m
    raise RpcError(f"no JSON-RPC message in body ({len(text)} bytes) {text[:100]!r}")


def _post(session, payload, sid=None):
    h = dict(HEADERS)
    if sid:
        h["mcp-session-id"] = sid
        h["mcp-protocol-version"] = PROTOCOL
    return session.post(URL, json=payload, headers=h, timeout=TIMEOUT_S)


def _result(msg):
    if not isinstance(msg, dict):
        raise RpcError("empty JSON-RPC reply")
    if msg.get("error"):
        e = msg["error"]
        raise RpcError(f"rpc error {e.get('code') if isinstance(e, dict) else ''}: "
                       f"{(e.get('message') if isinstance(e, dict) else e)!s:.160}")
    res = msg.get("result")
    if not isinstance(res, dict):
        raise RpcError("JSON-RPC result is not an object")
    return res


def _pick(props, *names):
    for n in names:
        if n in props:
            return n
    return None


def _build_args(props, origin, dest, dep, ret):
    """Map our query onto whatever argument names the tool schema advertises."""
    p = props or {}
    known = bool(p)
    fmt = lambda d: d.strftime("%d/%m/%Y")  # noqa: E731 - Kiwi wants day/month/year
    args = {(_pick(p, "flyFrom", "from", "origin") or "flyFrom"): origin,
            (_pick(p, "flyTo", "to", "destination") or "flyTo"): dest,
            (_pick(p, "departureDate", "dateFrom") or "departureDate"): fmt(dep),
            (_pick(p, "returnDate", "returnFrom") or "returnDate"): fmt(ret)}
    flex_key = _pick(p, "departureDateFlexRange", "departureDateFlexDays") or (
        None if known else "departureDateFlexRange")
    rflex_key = _pick(p, "returnDateFlexRange", "returnDateFlexDays") or (
        None if known else "returnDateFlexRange")
    for key in (flex_key, rflex_key):
        if key:
            hi = (p.get(key) or {}).get("maximum") if isinstance(p.get(key), dict) else None
            args[key] = min(FLEX, int(hi)) if isinstance(hi, (int, float)) else FLEX
    if "passengers" in p or not known:
        args["passengers"] = {"adults": 1}
    elif "adults" in p:
        args["adults"] = 1
    cur_key = _pick(p, "curr", "currency") or ("curr" if not known else None)
    cur, cur_note = CURRENCY, ""
    if cur_key:
        enum = (p.get(cur_key) or {}).get("enum") if isinstance(p.get(cur_key), dict) else None
        if isinstance(enum, list) and enum and CURRENCY not in enum:
            cur, cur_note = ("USD" if "USD" in enum else enum[0]), f" {CURRENCY} not in enum"
        args[cur_key] = cur
    if "sort" in p or not known:
        args["sort"] = "price"
    if "cabinClass" in p or not known:
        args["cabinClass"] = "M"
    if "locale" in p or not known:
        args["locale"] = "en"
    return args, cur, cur_note


def _legs(it):
    out = it.get("outbound") or it.get("sector") or {}
    back = it.get("inbound") or {}
    return (out if isinstance(out, dict) else {}), (back if isinstance(back, dict) else {})


def _segments(leg):
    segs = leg.get("segments") or leg.get("sectorSegments") or []
    return [s.get("segment", s) for s in segs if isinstance(s, dict)]


def _summarise(payload, cur):
    if isinstance(payload, list):
        items, currency, promised = payload, cur, None
    elif isinstance(payload, dict):
        items = payload.get("itineraries") or payload.get("results") or []
        currency, promised = payload.get("currency") or cur, payload.get("resultsCount")
    else:
        raise ValueError(f"tool payload is {type(payload).__name__}")
    if not isinstance(items, list):
        raise ValueError("itineraries is not a list")
    prices, dates, carriers, nonstop, it_prices = [], set(), [], [], []
    for it in items:
        if not isinstance(it, dict):
            continue
        price = it.get("price")
        price = price.get("amount") if isinstance(price, dict) else price
        price = float(price) if isinstance(price, (int, float)) and not isinstance(price, bool) else None
        out, back = _legs(it)
        so, sb = _segments(out), _segments(back)
        dep = str(out.get("departureTime") or (so[0].get("departureTime") if so else "") or "")[:10]
        if dep:
            dates.add(dep)
        codes = []
        for s in so + sb:
            c = s.get("carrier")
            c = c.get("code") if isinstance(c, dict) else c
            if isinstance(c, str) and c:
                codes.append(c)
                if c not in carriers:
                    carriers.append(c)
        if price is None:
            continue
        prices.append(price)
        if so and len(so) == 1 and (not back or len(sb) == 1):
            nonstop.append(price)
        if codes and set(codes) == {"IT"}:
            it_prices.append(price)
    return {"results": len(items), "dates": len(dates), "prices": prices, "currency": currency,
            "carriers": carriers, "nonstop": nonstop, "it": it_prices, "promised": promised}


def _payload(res):
    if res.get("isError"):
        txt = " ".join(str(c.get("text", "")) for c in res.get("content") or [] if isinstance(c, dict))
        raise RpcError(f"tool isError: {txt[:180]}")
    if isinstance(res.get("structuredContent"), (dict, list)):
        return res["structuredContent"]
    for c in res.get("content") or []:
        if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
            return json.loads(c["text"])
    raise RpcError("tool result has no text content")


def _case_row(session, sid, props, route, offset, today, schema_note, rid):
    o, d = route.split("-")
    dep = today + dt.timedelta(days=offset)
    ret = dep + dt.timedelta(days=STAY_DAYS)
    case = f"{route} RT {dep.isoformat()}+{STAY_DAYS}d flex+-{FLEX} search-flight"
    try:
        args, cur, cur_note = _build_args(props, o, d, dep, ret)
        resp = _post(session, {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                               "params": {"name": TOOL, "arguments": args}}, sid)
        s = _summarise(_payload(_result(_decode(resp))), cur)
        mn = min(s["prices"]) if s["prices"] else None
        note = (f"HTTP {resp.status_code}; {s['results']} itins"
                + (f" (resultsCount {s['promised']})" if s["promised"] is not None else "")
                + f", {s['dates']} dep dates; nonstop {len(s['nonstop'])}"
                + (f" min {min(s['nonstop']):.0f}" if s["nonstop"] else "")
                + f"; IT-only {len(s['it'])}" + (f" min {min(s['it']):.0f}" if s["it"] else "")
                + f"; args {','.join(sorted(args))}{cur_note}; {schema_note}; "
                "Kiwi price incl. taxes, may include self-transfer")
        return _row(case, route, ok=True, calls=1, results=s["results"], dates=s["dates"],
                    min_price=float(mn) if mn is not None else None,
                    currency=str(s["currency"])[:8] if s["currency"] else None,
                    carriers=",".join(s["carriers"][:8]) or None, note=note)
    except Exception as exc:  # noqa: BLE001
        return _row(case, route, calls=1, note=_err(exc))


def probe(env, today):
    try:
        session = _new_session()
    except Exception as exc:  # noqa: BLE001
        return [_row("setup", "-", note=f"setup failed: {_err(exc)}")]
    rows = []
    # 1. initialize
    try:
        resp = _post(session, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                                          "clientInfo": {"name": "farehunter-probe", "version": "0.1"}}})
        init = _result(_decode(resp))
        sid = (resp.headers.get("mcp-session-id") or "").strip() if getattr(resp, "headers", None) else ""
        info = init.get("serverInfo") or {}
        server = f"{info.get('name', '?')} {info.get('version', '?')} proto {init.get('protocolVersion', '?')}"
    except Exception as exc:  # noqa: BLE001
        return [_row("initialize", "-", calls=1, note=f"initialize failed: {_err(exc)}")]

    # 2. stateless -> tools/list to learn arg names; stateful -> the initialized notification
    _sleep(SLEEP_S)
    props, schema_note, ok2, note2 = {}, "", False, ""
    try:
        if sid:
            r2 = _post(session, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
            ok2 = r2.status_code in (200, 202, 204)
            schema_note = "stateful, schema not read (default arg names)"
            note2 = f"{server}; stateful session; notifications/initialized HTTP {r2.status_code}"
        else:
            r2 = _post(session, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            tools = _result(_decode(r2)).get("tools") or []
            names = [t.get("name") for t in tools if isinstance(t, dict)]
            tool = next((t for t in tools if isinstance(t, dict) and t.get("name") == TOOL), None)
            schema = (tool or {}).get("inputSchema") or {}
            props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
            req = schema.get("required") if isinstance(schema.get("required"), list) else []
            ok2 = tool is not None
            schema_note = "schema read" if props else "schema empty (default arg names)"
            note2 = (f"{server}; stateless; tools={names[:5]}; {TOOL} required={req[:6]}"
                     f" props={sorted(props)[:14]}")
    except Exception as exc:  # noqa: BLE001
        schema_note, note2 = "schema unavailable (default arg names)", f"{server}; step 2 failed: {_err(exc)}"
    rows.append(_row("handshake initialize + " + ("notifications/initialized" if sid else "tools/list"),
                     "-", ok=ok2, calls=2, note=note2))

    # 3-4. one round-trip search per case
    for i, (route, offset) in enumerate(CASES):
        _sleep(SLEEP_S)
        rows.append(_case_row(session, sid, props, route, offset, today, schema_note, 10 + i))
    return rows
