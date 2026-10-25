"""FareHunter free-API probe: Travelpayouts / Aviasales Data API.

Question: can ANY free Data API variant return more KHH-NGO / KHH-CTS fares
than production's request (v3 prices_for_dates, no market, one_way=false,
direct=true, per YYYY-MM month)?

Cases (all calls go to api.travelpayouts.com, token only in X-Access-Token):
  A  production request, KHH-NGO / KHH-CTS / TPE-NRT x 6 months  -> 1 row/route
  B  market sweep (12 markets) for KHH-NGO / KHH-CTS x 3 months
     (months 2..4 of the production window; 6 months would cost 144 calls
     alone and blow the 150-call budget)                         -> 1 row/(route,market)
  C  KHH-NGO variants x 6 months: direct=false, one_way=true, both;
     plus "departure_at omitted" (undated) for the 3 routes      -> 1 row/variant
  D  other Data API endpoints, KHH-NGO + TPE-NRT control         -> 1 row/endpoint
     (control queries TPE-TYO: v1/v2 endpoints key on CITY codes, and NGO is
     already Nagoya's city code)

Row notes carry: status (DATA/EMPTY/PARTIAL/ERR), errors, per-month
results/dates ("mo r/d 11:3/2"), fpB=<hash of items in the B months> so a B
market row can be matched to the A baseline (same hash = same data, i.e.
that market IS the default or the param was ignored), nonstop / round-trip
counts, departure span, past-date count and found_at range when present.

Interface: see INTERFACE.md. probe(env, today) never raises.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import time
from collections import Counter
from urllib.parse import quote

PROVIDER = "travelpayouts"
ENV_NAME = "TRAVELPAYOUTS_TOKEN"
SIGNUP_URL = "https://www.travelpayouts.com/ (free partner account -> API token)"
PIP_DEPS: list[str] = []          # stdlib + requests only

HOST = "https://api.travelpayouts.com"
CALL_BUDGET = 150                 # hard cap on HTTP requests per probe() run
PAUSE_S = 0.6                     # between calls (same host)
TIMEOUT_S = 30                    # per request (<= 60 per interface)
RETRY_429_WAIT_S = 3.0
MAX_TRANSPORT_ERRORS = 5          # cumulative (prod uses 3); 5 x 30s timeout caps the stall
NOTE_MAX = 300
DEADLINE_S = 480                  # wall clock for the whole probe: the shared job
                                  # has timeout-minutes 25 and prints rows only at the end

ROUTES_A = [("KHH", "NGO"), ("KHH", "CTS"), ("TPE", "NRT")]
ROUTES_B = [("KHH", "NGO"), ("KHH", "CTS")]
MARKETS = ["ru", "us", "jp", "kr", "hk", "tw", "sg", "th", "ph", "kz", "de", "gb"]
# D: (route label, origin, destination-as-queried)
ROUTES_D = [("KHH-NGO", "KHH", "NGO"), ("TPE-NRT", "TPE", "TYO")]

PFD = "/aviasales/v3/prices_for_dates"

_sleep = time.sleep               # monkeypatchable in tests


def _make_session():
    """Lazy import so a missing/broken `requests` is caught inside probe()."""
    import requests  # noqa: PLC0415
    return requests.Session()


# ---------------------------------------------------------------- planning
def upcoming_months(n: int, today: _dt.date) -> list[str]:
    """Current month plus the next n-1 (same rule as runner.upcoming_months)."""
    out, y, m = [], today.year, today.month
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def _pfd_params(o: str, d: str, month: str | None, *, one_way: bool = False,
                direct: bool = True, market: str | None = None) -> dict:
    """Exactly TravelpayoutsClient.search_month's params (incl. `cy`)."""
    p: dict = {"origin": o, "destination": d}
    if month:
        p["departure_at"] = month
    p.update({
        "one_way": "true" if one_way else "false",
        "direct": "true" if direct else "false",
        "unique": "false",
        "sorting": "price",
        "currency": "twd",
        "cy": "twd",
        "limit": 1000,
        "page": 1,
    })
    if market:
        p["market"] = market
    return p


def _case(label: str, route: str, reqs: list, fp_tags=None,
          per_req: bool = False) -> dict:
    return {"case": label, "route": route,
            "reqs": [{"path": p, "params": prm, "tag": t} for p, prm, t in reqs],
            "fp_tags": list(fp_tags) if fp_tags else None, "per_req": per_req}


def build_plan(today: _dt.date) -> list[dict]:
    months = upcoming_months(6, today)
    b_months = months[1:4]
    span6 = f"{months[0]}..{months[-1]}"
    span3 = f"{b_months[0]}..{b_months[-1]}"
    typ_dep = today + _dt.timedelta(days=45)      # typical trip: ~30-60d out
    typ_ret = typ_dep + _dt.timedelta(days=5)
    typ_month = typ_dep.strftime("%Y-%m")
    cases: list[dict] = []

    # A. baseline = production request
    for o, d in ROUTES_A:
        cases.append(_case(
            f"A prices_for_dates baseline (prod params) {span6}", f"{o}-{d}",
            [(PFD, _pfd_params(o, d, m), m) for m in months],
            fp_tags=b_months, per_req=True))

    # B. market sweep
    for o, d in ROUTES_B:
        for mk in MARKETS:
            cases.append(_case(
                f"B prices_for_dates market={mk} {span3}", f"{o}-{d}",
                [(PFD, _pfd_params(o, d, m, market=mk), m) for m in b_months],
                fp_tags=b_months, per_req=True))

    # C. request-shape variants (default market)
    o, d = "KHH", "NGO"
    for label, kw in [("direct=false", {"direct": False}),
                      ("one_way=true", {"one_way": True}),
                      ("one_way=true direct=false",
                       {"one_way": True, "direct": False})]:
        cases.append(_case(
            f"C prices_for_dates {label} {span6}", f"{o}-{d}",
            [(PFD, _pfd_params(o, d, m, **kw), m) for m in months],
            per_req=True))
    for o2, d2 in ROUTES_A:   # KHH-NGO + KHH-CTS + TPE-NRT as control
        cases.append(_case(
            "C prices_for_dates departure_at omitted (undated, prod params)",
            f"{o2}-{d2}", [(PFD, _pfd_params(o2, d2, None), "undated")]))

    # D. other free Data API endpoints
    for route, o, d in ROUTES_D:
        sfx = "" if d == route[-3:] else f" [queried {o}-{d}]"
        base = {"origin": o, "destination": d, "currency": "twd"}
        latest = {**base, "period_type": "year", "one_way": "false",
                  "sorting": "price", "limit": 1000, "page": 1}
        d_reqs = [
            ("D v3/grouped_prices group_by=departure_at departure_at="
             f"{typ_month}", "/aviasales/v3/grouped_prices",
             {**base, "departure_at": typ_month, "group_by": "departure_at"}),
            ("D v3/grouped_prices group_by=month (no date)",
             "/aviasales/v3/grouped_prices", {**base, "group_by": "month"}),
            ("D v3/get_latest_prices period_type=year one_way=false "
             "show_to_affiliates=false", "/aviasales/v3/get_latest_prices",
             {**latest, "show_to_affiliates": "false"}),
            ("D v3/get_latest_prices period_type=year one_way=false "
             "show_to_affiliates=true", "/aviasales/v3/get_latest_prices",
             {**latest, "show_to_affiliates": "true"}),
            ("D v2/prices/latest period_type=year one_way=false "
             "show_to_affiliates=false", "/v2/prices/latest",
             {**latest, "show_to_affiliates": "false"}),
            (f"D v2/prices/month-matrix month={typ_month}-01 "
             "show_to_affiliates=false", "/v2/prices/month-matrix",
             {**base, "month": f"{typ_month}-01", "show_to_affiliates": "false"}),
            (f"D v2/prices/week-matrix {typ_dep.isoformat()}/"
             f"{typ_ret.isoformat()} show_to_affiliates=false",
             "/v2/prices/week-matrix",
             {**base, "depart_date": typ_dep.isoformat(),
              "return_date": typ_ret.isoformat(), "show_to_affiliates": "false"}),
            (f"D v1/prices/calendar depart_date={typ_month} "
             "calendar_type=departure_date", "/v1/prices/calendar",
             {**base, "depart_date": typ_month, "calendar_type": "departure_date"}),
            ("D v1/prices/cheap (any date)", "/v1/prices/cheap",
             {**base, "page": 1}),
            ("D v1/prices/direct (any date)", "/v1/prices/direct", dict(base)),
            ("D v1/prices/monthly", "/v1/prices/monthly", dict(base)),
            ("D v3/search_by_price_range value 1..9999999 one_way=false "
             "direct=false", "/aviasales/v3/search_by_price_range",
             {**base, "value_min": 1, "value_max": 9999999, "one_way": "false",
              "direct": "false", "limit": 1000, "page": 1}),
        ]
        for label, path, params in d_reqs:
            cases.append(_case(label + sfx, route, [(path, params, "single")]))
    return cases


def planned_calls(today: _dt.date) -> int:
    """Max HTTP requests excluding 429 retries (retries fill the slack up to
    CALL_BUDGET and are also hard-capped at runtime)."""
    return sum(len(c["reqs"]) for c in build_plan(today))


# ---------------------------------------------------------------- HTTP
def _snip(text, n: int = 90) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:n]


def _secrets_of(token: str) -> list[str]:
    return (list(dict.fromkeys([token, quote(token, safe="")]))
            if token and len(token) >= 4 else [])


def _clean(text, secrets: list[str], n: int = 90) -> str:
    """Strip secrets BEFORE truncating (a half-cut token would survive)."""
    s = str(text or "")
    for sec in secrets:
        s = s.replace(sec, "***")
    return _snip(s, n)


def _body_err(resp, clean) -> str:
    try:
        j = resp.json()
        if isinstance(j, dict):
            for k in ("error", "message", "errors", "detail"):
                if j.get(k):
                    return clean(j[k])
        return clean(json.dumps(j))
    except Exception:  # noqa: BLE001
        return clean(getattr(resp, "text", ""))


class _Http:
    def __init__(self, session, token: str, retry_slack: int):
        self.session = session
        self.token = token
        self.retry_slack = max(0, retry_slack)
        self.calls = 0
        self.transport_errors = 0
        self._called = False
        self._secrets = _secrets_of(token)
        self.deadline = time.monotonic() + DEADLINE_S

    def _clean(self, text, n: int = 90) -> str:
        return _clean(text, self._secrets, n)

    def get(self, path: str, params: dict):
        """-> (payload | None, error | None). Never raises."""
        if self.transport_errors >= MAX_TRANSPORT_ERRORS:
            return None, f"not called: circuit open ({MAX_TRANSPORT_ERRORS} transport errors)"
        retried = False
        while True:
            if self.calls >= CALL_BUDGET:
                return None, "not called: call budget exhausted"
            if time.monotonic() > self.deadline:
                return None, f"not called: probe deadline {DEADLINE_S}s reached"
            if self._called:
                _sleep(PAUSE_S)
            self._called = True
            self.calls += 1
            try:
                resp = self.session.get(
                    HOST + path, params=params, timeout=TIMEOUT_S,
                    headers={"X-Access-Token": self.token,
                             "Accept-Encoding": "gzip, deflate"})
            except Exception as exc:  # noqa: BLE001  (timeouts, DNS, resets...)
                self.transport_errors += 1
                return None, f"transport {type(exc).__name__}: {self._clean(exc, 120)}"
            code = getattr(resp, "status_code", None)
            if code == 429 and not retried and self.retry_slack > 0:
                self.retry_slack -= 1
                retried = True
                _sleep(RETRY_429_WAIT_S)
                continue
            if code != 200:
                return None, f"HTTP {code}: {_body_err(resp, self._clean)}"
            try:
                payload = resp.json()
            except Exception:  # noqa: BLE001
                return None, f"HTTP 200 non-JSON: {self._clean(getattr(resp, 'text', ''), 60)}"
            if not isinstance(payload, dict):
                return None, f"HTTP 200 unexpected JSON {type(payload).__name__}"
            if payload.get("success") is False:
                return None, ("success=false: "
                              f"{self._clean(payload.get('error') or payload.get('message'))}")
            return payload, None


# ---------------------------------------------------------------- parsing
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_KEYDATE_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")


def _num(x):
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        try:
            return float(x)
        except ValueError:
            return None
    return None


def _date10(x):
    s = str(x or "")
    return s[:10] if _DATE_RE.match(s) else None


def _is_fare(d: dict) -> bool:
    return _num(d.get("price", d.get("value"))) is not None


def _extract(data, key_hint=None, tr_hint=None, depth=0, out=None) -> list:
    """Collect fare dicts anywhere under `data`.

    v3 prices_for_dates / v2 latest: list of fares. v3 grouped_prices and
    v1 calendar/monthly: {"2026-12-01": fare}. v1 cheap/direct:
    {"NGO": {"0": fare, "1": fare}} (inner key = number of transfers)."""
    if out is None:
        out = []
    if depth > 6:
        return out
    if isinstance(data, list):
        for x in data:
            _extract(x, key_hint, tr_hint, depth + 1, out)
    elif isinstance(data, dict):
        if _is_fare(data):
            out.append((data, key_hint, tr_hint))
        else:
            for k, v in data.items():
                ks = str(k)
                kh = ks if _KEYDATE_RE.match(ks) else key_hint
                th = int(ks) if ks.isdigit() and len(ks) <= 2 else tr_hint
                _extract(v, kh, th, depth + 1, out)
    return out


def _norm(it: dict, key_hint, tr_hint):
    price = _num(it.get("price", it.get("value")))
    if price is None:
        return None
    dep = _date10(it.get("departure_at") or it.get("depart_date"))
    if not dep and key_hint and len(key_hint) == 10:
        dep = key_hint
    tr = it.get("transfers", it.get("number_of_changes"))
    tr = int(tr) if _num(tr) is not None else tr_hint
    rtr = it.get("return_transfers")
    rtr = int(rtr) if _num(rtr) is not None else None
    return {
        "dep": dep,
        "ret": _date10(it.get("return_at") or it.get("return_date")),
        "price": price,
        "airline": str(it.get("airline") or it.get("main_airline") or ""),
        "tr": tr,
        "rtr": rtr,
        "found": _date10(it.get("found_at")),
    }


def _fp(fares: list) -> str:
    if not fares:
        return "-"
    key = sorted((f["dep"] or "", f["ret"] or "", f["price"], f["airline"],
                  f["tr"] if f["tr"] is not None else -1) for f in fares)
    return hashlib.sha1(json.dumps(key).encode()).hexdigest()[:6]


# ---------------------------------------------------------------- rows
def _row(case: str, route: str, ok: bool = False, results=None, dates=None,
         min_price=None, currency=None, carriers=None, calls: int = 0,
         note: str = "") -> dict:
    return {"provider": PROVIDER, "case": case, "route": route, "ok": ok,
            "results": results, "dates": dates, "min_price": min_price,
            "currency": currency, "carriers": carriers, "calls": calls,
            "note": note[:NOTE_MAX]}


def _execute(case: dict, http: _Http, today: _dt.date) -> dict:
    calls_before = http.calls
    tagged: list = []          # (fare, tag)
    per: list = []             # (tag, fares | None)
    errs: list = []
    n_ok = 0
    currency = None
    no_data_key = False
    for req in case["reqs"]:
        payload, err = http.get(req["path"], req["params"])
        if err:
            errs.append(err)
            per.append((req["tag"], None))
            continue
        n_ok += 1
        if "data" not in payload:
            no_data_key = True
        fares = [f for f in (_norm(*t) for t in _extract(payload.get("data")))
                 if f]
        if currency is None and isinstance(payload.get("currency"), str):
            currency = payload["currency"].upper()
        tagged.extend((f, req["tag"]) for f in fares)
        per.append((req["tag"], fares))

    fares = [f for f, _ in tagged]
    total = len(case["reqs"])
    ok = n_ok == total
    dep_dates = {f["dep"] for f in fares if f["dep"]}
    prices = [f["price"] for f in fares]
    carriers = [a for a, _ in sorted(Counter(f["airline"] for f in fares
                                             if f["airline"]).items(),
                                     key=lambda kv: (-kv[1], kv[0]))][:8]

    if n_ok == 0:
        status = "ERR"
    elif not ok:
        status = "PARTIAL"
    else:
        status = "DATA" if fares else "EMPTY"
    parts = [status]
    if errs:
        uniq = list(dict.fromkeys(errs))
        parts.append(f"ERR x{len(errs)}: " + " | ".join(uniq[:2]))
    if no_data_key:
        parts.append("no 'data' key in payload")
    if case["per_req"]:
        cells = []
        for tag, fs in per:
            mm = tag[5:] if len(tag) == 7 else tag
            cells.append(f"{mm}:ERR" if fs is None else
                         f"{mm}:{len(fs)}/{len({f['dep'] for f in fs if f['dep']})}")
        parts.append("mo r/d " + " ".join(cells))
    elif fares:
        hist = Counter(f["dep"][2:7] for f in fares if f["dep"])
        parts.append("dm " + " ".join(f"{k}:{v}" for k, v in sorted(hist.items())[:10]))
    if case["fp_tags"]:
        fp_ok = all(fs is not None for tag, fs in per if tag in case["fp_tags"])
        fp_fares = [f for f, tag in tagged if tag in case["fp_tags"]]
        parts.append("fpB=" + (_fp(fp_fares) if fp_ok else "n/a"))
    if fares:
        known = [f for f in fares if f["tr"] is not None]
        if known:
            ns = sum(1 for f in known if f["tr"] == 0 and (f["rtr"] or 0) == 0)
            parts.append(f"nonstop={ns}/{len(fares)}")
        parts.append(f"rt={sum(1 for f in fares if f['ret'])}/{len(fares)}")
        if dep_dates:
            parts.append(f"span={min(dep_dates)}..{max(dep_dates)}")
        past = sum(1 for f in fares if f["dep"] and f["dep"] < today.isoformat())
        if past:
            parts.append(f"past={past}")
        found = sorted(f["found"] for f in fares if f["found"])
        if found:
            parts.append(f"found={found[0]}..{found[-1]}")
    return _row(case["case"], case["route"], ok=ok,
                results=len(fares) if n_ok else None,
                dates=len(dep_dates) if n_ok else None,
                min_price=min(prices) if prices else None,
                currency=(currency or "TWD") if n_ok else None,
                carriers=",".join(carriers) if carriers else None,
                calls=http.calls - calls_before,
                note="; ".join(parts))


def _scrub(value, secrets: list[str]):
    if isinstance(value, str):
        for s in secrets:
            value = value.replace(s, "***")
    return value


def _scrub_rows(rows: list[dict], token: str) -> list[dict]:
    """Defence in depth; error texts are already cleaned at the source."""
    secrets = _secrets_of(token)
    return [{k: _scrub(v, secrets) for k, v in r.items()} for r in rows]


# ---------------------------------------------------------------- entry
def probe(env: dict, today: _dt.date) -> list[dict]:
    token = ""
    rows: list[dict] = []
    try:
        token = str((env or {}).get(ENV_NAME) or "").strip()
        if not token:
            return [_row("all", "*",
                         note=f"SKIPPED: {ENV_NAME} not set (needs user signup at {SIGNUP_URL})")]
        if isinstance(today, _dt.datetime):
            today = today.date()
        plan = build_plan(today)
        planned = sum(len(c["reqs"]) for c in plan)
        try:
            session = _make_session()
        except Exception as exc:  # noqa: BLE001
            return _scrub_rows([_row("all", "*", note=(
                f"ERR: cannot create HTTP session ({type(exc).__name__}: "
                f"{_clean(exc, _secrets_of(token), 150)})"))], token)
        http = _Http(session, token, retry_slack=CALL_BUDGET - planned)
        for case in plan:
            before = http.calls
            try:
                rows.append(_execute(case, http, today))
            except Exception as exc:  # noqa: BLE001  (probe bug: keep going)
                rows.append(_row(case["case"], case["route"],
                                 calls=http.calls - before,
                                 note=f"ERR probe bug {type(exc).__name__}: {_clean(exc, _secrets_of(token), 200)}"))
    except Exception as exc:  # noqa: BLE001
        rows.append(_row("all", "*", note=(
            f"ERR probe crashed {type(exc).__name__}: {_clean(exc, _secrets_of(token), 200)}")))
    return _scrub_rows(rows, token)
