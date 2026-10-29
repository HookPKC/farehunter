"""fli（Google Flights 內部 API）觀察期：只記錄、不影響任何現有行為。

決策與實測數據見 docs/adr/0003-fli-google-calendar.md。一句話：fli 一次請求就能拿到
一條航線 61 天的直飛來回價，2026-10-25 九輪實測裡日曆價與即時查詢、與 SerpAPI
全部一致；但它是非官方介面，Google 偶發內部錯誤要靠重試，所以先觀察 2 週再決定。

**刻意不寫 prices.db。** observations 的讀者不分來源：route_stats（new_low 基準）、
export 的統計與健康檢查都會把新資料算進去；寫成 source='google' 還會讓 SerpAPI／
Scrape.do 的 72 小時冷卻以為「已經有實價」而跳過付費驗證。觀察期的資料因此只寫進
data/fli/<UTC 時間>.jsonl.gz——每次執行一個新檔，之後不再修改，git 不會累積大型二進位。

每次執行做兩件事：
1. 日曆：config.yaml 的每條航線 × 未來約 300 天（61 天一個時段）的直飛來回價；
   回空或出錯的時段等 RETRY_WAIT_S 秒重打一次（實測：偶發回空重試一次就補回）。
   上一輪在 MIN_SWEEP_GAP_MIN 分鐘內才跑過就跳過（FSC 與 Verify 剛好接連跑完時）。
2. 影子比價：最近 SHADOW_LOOKBACK_HOURS 小時內 SerpAPI／Scrape.do 查過的行程，
   用 fli 查同一組去回日期，記下兩邊價差。`report` 子命令依 ADR 0003 的標準判斷
   要不要進第二階段。

整輪共用一個 Breaker：被封鎖（第一次就停，繼續打只會被擋更久）、請求預算用完、
連續連線錯誤、或超過 DEADLINE_S，剩下的請求一律不發、記成 skipped，但已拿到的資料
照樣寫出。以下任一情況 exit 1 讓 workflow 變紅：
- 整輪沒有任何一段日曆有價、也沒有任何一次比價拿到價格——8 條航線（含 TPE-NRT 這種
  天天有班的）同時全空，只會是封鎖或 fli 的解析悄悄失效，後者不會丟例外；
- 出現封鎖頁；
- prices.db 讀不到（影子比價沒做成）。

用法：
  python -m farehunter.fli_calendar run [config.yaml] [prices.db] [data/fli]
  python -m farehunter.fli_calendar report [data/fli] [天數]
"""
from __future__ import annotations

import gzip
import json
import logging
import sqlite3
import statistics
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

FLI_VERSION = "0.9.0"         # 與 requirements-fli.txt 同步；用到 fli 的私有模組，升版要重測
STAY_DAYS = 5                 # 日曆的固定停留天數（fli 的日曆只支援固定天數）
WINDOW_DAYS = 61              # fli 單次日曆請求的上限（MAX_DAYS_PER_SEARCH）
MAX_AHEAD_DAYS = 300          # fli 文件：最遠 305 天
SPACING_S = 3.0               # 任兩次 Google 請求的最小間隔
TIMEOUT_S = 20                # 實測日曆回應 2–8 秒；掛著不回應的故障不要每次都等 45 秒
CAL_RETRIES = 1               # 日曆時段回空／出錯時再打幾次
SHADOW_RETRIES = 2            # 影子比價出錯時再打幾次（實測 Google 內部錯誤約 4 成）
RETRY_WAIT_S = 12.0
SHADOW_LOOKBACK_HOURS = 24
SHADOW_MAX = 15
DEDUPE_DAYS = 3               # 這幾天內已比過的參考觀測不再重比
REQUEST_BUDGET = 200          # 單次執行的硬上限（正常約 55–70 次）
DEADLINE_S = 10 * 60          # 正常約 5–6 分鐘；超過就收手寫檔（job timeout 前一定寫得出來）
MAX_TRANSPORT_ERRORS = 5      # 連續這麼多次連線錯誤就停（同 runner.MAX_TRANSPORT_ERRORS 的理由）
MIN_SWEEP_GAP_MIN = 30        # FSC 與 Verify 接連跑完時，第二輪只做影子比價

# ADR 0003 第二階段判斷標準（report 子命令據此給 PASS/FAIL/NO DATA）
CRITERION_CAL_OK = 0.95       # 日曆時段重試後有價的比例（從沒拿到過價的時段視為沒班表、不計）
CRITERION_WITHIN = 0.90       # 與參考價 ±SHADOW_TOLERANCE 以內的比例（即時查詢、日曆各一項）
#: 回應不超過這麼多字元就整段記進錯誤訊息（Google 內部錯誤約 95 字元；正常回應數十 KB）
DIAG_BODY_MAX = 300
SHADOW_TOLERANCE = 0.02
SHADOW_MAX_LAG_H = 6.0        # 只用參考觀測後 6 小時內完成的比較評估準確度

ERROR_KINDS = ("transport", "http", "blocked", "google_error", "budget", "currency",
               "fli", "skipped")


class FliError(RuntimeError):
    """沒有拿到可用的 Google 回應。訊息開頭是種類（ERROR_KINDS），report 據此分類：
    transport 連線／逾時、http 4xx/5xx、blocked 封鎖頁或 429/403、google_error 回 200
    但沒有 wrb.fr 資料（Google 內部錯誤）、budget 請求預算用完、currency 幣別不符、
    fli 其他（含解析失敗、未知機場代碼）。"""


def resolve_airport(enum_cls, code: str):
    """把 IATA 代碼轉成 fli 的 Airport 成員，繞過 fli 0.9.0 的別名 bug。

    fli 用 Enum("Airport", {代碼: 機場名}) 建表，機場名重複的 48 個代碼會變成別名：
    Airport.OKA（那霸）其實是 Airport.NAH（印尼 Tahuna 的 Naha Airport），請求送出去
    就變成 NAH，KHH-OKA 永遠查不到（2026-10-25 實測）。fli 序列化只讀 `.name`，所以
    名稱不符時自建一個同類別、名稱正確的成員（值另取，避免 pydantic 依值換回 NAH）。
    回應端也要處理，見 FliBackend._airport。
    """
    member = enum_cls[code]
    if member.name == code:
        return member
    fake = object.__new__(enum_cls)
    fake._name_ = code
    fake._value_ = f"{member.value} [{code}]"
    return fake


class Breaker:
    """整輪共用的停止條件。觸發後剩下的請求都不發（記成 skipped），已拿到的照樣寫出。"""

    def __init__(self, deadline_s: float = DEADLINE_S, clock=time.monotonic):
        self.clock, self.deadline_s = clock, deadline_s
        self.until = clock() + deadline_s
        self.reason: str | None = None
        self.transport_streak = 0

    def check(self) -> str | None:
        if self.reason is None and self.clock() >= self.until:
            self.reason = f"deadline: {self.deadline_s:.0f}s"
        return self.reason

    def failed(self, exc: Exception) -> None:
        msg = str(exc)
        if msg.startswith(("blocked", "budget")):
            self.reason = self.reason or msg
        if msg.startswith("transport"):
            self.transport_streak += 1
            if self.transport_streak >= MAX_TRANSPORT_ERRORS:
                self.reason = self.reason or f"transport: {MAX_TRANSPORT_ERRORS} in a row"
        else:
            self.transport_streak = 0

    def answered(self) -> None:
        self.transport_streak = 0


# ---- fli 後端（只在真的要打 Google 時才 import fli）-----------------------------

class FliBackend:
    """fli 的薄包裝：固定間隔、硬預算、不藏重試（fli 內建 tenacity 會讓一次查詢
    默默變成 3 次請求，重試由上層的 CAL_RETRIES／SHADOW_RETRIES 明確控制）。"""

    def __init__(self, currency: str = "TWD", language: str = "en-US",
                 budget: int = REQUEST_BUDGET):
        from fli.models import Airport
        self.Airport = Airport
        self.currency, self.language = currency, language
        self.client = _make_client(budget)
        self._airports: dict[str, object] = {}

    @property
    def requests(self) -> int:
        return self.client.calls

    def calendar(self, origin: str, dest: str, start: date, end: date,
                 stay: int = STAY_DAYS) -> dict[str, float]:
        return _as_fli_error(self._calendar, origin, dest, start, end, stay)

    def cheapest(self, origin: str, dest: str, dep: date, ret: date):
        """同一組去回日期的最便宜直飛來回價 → (價格, 航空公司代碼)；查無航班回 None。"""
        return _as_fli_error(self._cheapest, origin, dest, dep, ret)

    def _airport(self, code: str):
        """resolve_airport 只修了請求端；fli 解析回應時用另一張表
        （fli.search._decoders._AIRPORT_BY_CODE = {m.name: m for m in Airport}，
        Enum 迭代會跳過別名，所以沒有 OKA），抵達 OKA 的航班列會被整列丟掉。
        把自建的成員也登記進那張表。"""
        if code not in self._airports:
            member = resolve_airport(self.Airport, code)
            if member.name != self.Airport[code].name:
                from fli.search import _decoders
                _decoders._AIRPORT_BY_CODE.setdefault(code, member)
            self._airports[code] = member
        return self._airports[code]

    def _check_currency(self, got) -> None:
        got = {str(c).upper() for c in got if c}
        if got and got != {self.currency}:
            raise FliError(f"currency: asked {self.currency}, got {sorted(got)}")

    def _calendar(self, origin, dest, start, end, stay):
        from fli.core import build_date_search_segments
        from fli.models import DateSearchFilters, MaxStops, PassengerInfo, SeatType
        from fli.search import SearchDates
        from fli.search._wire import parse_first_wrb_payload

        segments, trip = build_date_search_segments(
            self._airport(origin), self._airport(dest),
            start.isoformat(), trip_duration=stay, is_round_trip=True)
        filters = DateSearchFilters(
            trip_type=trip, passenger_info=PassengerInfo(adults=1), flight_segments=segments,
            stops=MaxStops.NON_STOP, seat_type=SeatType.ECONOMY,
            from_date=start.isoformat(), to_date=end.isoformat(), duration=stay)
        search = SearchDates()
        search.client = self.client
        priced = self.client.guard(lambda: search.search(
            filters, currency=self.currency, language=self.language))
        if priced is None and parse_first_wrb_payload(self.client.last_text) is None:
            raise FliError(f"google_error: {self.client.diag()}")
        self._check_currency(getattr(p, "currency", None) for p in priced or [])
        days: dict[str, float] = {}
        for p in priced or []:
            d = p.date[0].date().isoformat()
            days[d] = min(days.get(d, p.price), float(p.price))
        return days

    def _cheapest(self, origin, dest, dep, ret):
        from fli.core import build_flight_segments
        from fli.models import FlightSearchFilters, MaxStops, PassengerInfo, SeatType, SortBy
        from fli.search import SearchFlights
        from fli.search._wire import parse_first_wrb_payload

        class OutboundOnly(SearchFlights):
            # 來回總價就在去程列上（fli 自己的 MCP server 也這樣用）。不展開回程：
            # 每次比價省一個請求，也不會讓回程那次的錯誤（包括封鎖）被悄悄吞掉。
            outbound = None

            def _expand_multi_leg(self, flights, filters, **kw):
                self.outbound = flights
                return []

        segments, trip = build_flight_segments(
            self._airport(origin), self._airport(dest), dep.isoformat(), ret.isoformat())
        filters = FlightSearchFilters(
            trip_type=trip, passenger_info=PassengerInfo(adults=1), flight_segments=segments,
            stops=MaxStops.NON_STOP, seat_type=SeatType.ECONOMY, sort_by=SortBy.CHEAPEST)
        search = OutboundOnly()
        search.client = self.client
        self.client.guard(lambda: search.search(
            filters, top_n=1, currency=self.currency, language=self.language))
        if not search.outbound:
            if parse_first_wrb_payload(self.client.last_text) is None:
                raise FliError(f"google_error: {self.client.diag()}")
            return None
        priced = [f for f in search.outbound if f.price]
        if not priced:
            return None
        best = min(priced, key=lambda f: f.price)
        self._check_currency([getattr(best, "currency", None)])
        carriers = ",".join(dict.fromkeys(
            leg.airline.name.removeprefix("_") for leg in best.legs))
        return float(best.price), carriers


def _as_fli_error(fn, *args):
    """一條航線出事（未知機場代碼、fli 驗證或解析錯誤…）只記成這一筆失敗，不拖垮整輪。"""
    try:
        return fn(*args)
    except FliError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FliError(f"fli: {type(exc).__name__}: {str(exc)[:160]}") from exc


def _make_client(budget: int):
    import threading
    from urllib.parse import urlparse

    from fli.search.client import Client

    class _Client(Client):
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
            with self._gate:                  # 也把 fli 可能的平行請求排成單線
                if self.calls >= budget:
                    raise FliError(f"budget: {budget} requests used")
                wait = self._next_at - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self.calls += 1
                self.last, self.last_text = {}, ""
                kw["timeout"] = TIMEOUT_S
                try:
                    session = self._session()
                    resp = (session.post if method == "POST" else session.get)(url, **kw)
                except Exception as exc:  # noqa: BLE001 — 一律轉成 FliError
                    raise FliError(f"transport: {type(exc).__name__}: {exc}"[:200]) from exc
                finally:
                    self._next_at = time.monotonic() + SPACING_S
            self.last_text = resp.text or ""
            self.last = {"status": resp.status_code, "path": urlparse(str(resp.url)).path,
                         "bytes": len(self.last_text)}
            low = self.last_text[:6000].lower()
            if ("unusual traffic" in low or "/sorry/" in self.last["path"]
                    or resp.status_code in (403, 429)):
                raise FliError(f"blocked: HTTP {resp.status_code} {self.last['path'][-30:]}")
            if resp.status_code >= 400:
                raise FliError(f"http: {resp.status_code}")
            return resp

        def guard(self, fn):
            """fli 會把底層錯誤包成自己的例外；統一成 FliError，訊息帶上最後一次交換。"""
            try:
                return fn()
            except FliError:
                raise
            except Exception as exc:  # noqa: BLE001
                cause = exc.__cause__ or exc.__context__
                if isinstance(cause, FliError):  # fli 包住了我們自己丟的錯誤
                    raise cause from exc
                raise FliError(f"fli: {type(exc).__name__}: {str(exc)[:120]}; {self.diag()}") from exc

        def diag(self) -> str:
            if not self.last:
                return "no response"
            out = f"HTTP {self.last['status']} {self.last['bytes']}B {self.last['path'][-40:]}"
            # Google 內部錯誤的回應很短（約 95 bytes），整段留下才分得出是哪一種錯誤。
            # 2026-10-26～29 即時查詢 58 次裡 54 次這樣失敗，當時只記了長度，查不出原因。
            if self.last["bytes"] <= DIAG_BODY_MAX:
                out += " body=" + " ".join(self.last_text.split())
            return out

    return _Client()


# ---- 日曆掃描 -----------------------------------------------------------------

def horizon_days(cfg: dict) -> int:
    months = int(cfg.get("defaults", {}).get("months_ahead", 10))
    return min(MAX_AHEAD_DAYS, months * 31)


def windows(today: date, horizon: int) -> list[tuple[date, date]]:
    """從明天起、每 WINDOW_DAYS 天一段，最後一段截在 horizon。fli 的日期驗證用真實
    時鐘，所以永遠不含今天。"""
    out, a = [], 1
    while a <= horizon:
        b = min(a + WINDOW_DAYS - 1, horizon)
        out.append((today + timedelta(days=a), today + timedelta(days=b)))
        a = b + 1
    return out


def sweep_calendar(backend, routes: list[tuple[str, str]], today: date, horizon: int,
                   breaker: Breaker | None = None, sleep=None, out: list | None = None) -> list[dict]:
    """逐筆 append 到 out：中途出非預期例外時，已完成的時段仍在 out 裡、照樣寫得出去。"""
    sleep = sleep or time.sleep               # 呼叫時才取，測試換得掉
    breaker = breaker or Breaker()
    out = [] if out is None else out
    for origin, dest in routes:
        for idx, (start, end) in enumerate(windows(today, horizon)):
            rec = {"kind": "calendar", "route": f"{origin}-{dest}", "idx": idx,
                   "start": start.isoformat(), "end": end.isoformat(), "stay": STAY_DAYS,
                   "attempts": 0, "status": "error", "errors": [], "days": {}}
            out.append(rec)
            answered = False
            for attempt in range(CAL_RETRIES + 1):
                stop = breaker.check()
                if stop:
                    rec["errors"].append(f"skipped: {stop}")
                    break
                if attempt:
                    sleep(RETRY_WAIT_S)
                rec["attempts"] += 1
                try:
                    days = backend.calendar(origin, dest, start, end, STAY_DAYS)
                except FliError as exc:
                    rec["errors"].append(str(exc))
                    breaker.failed(exc)
                    continue
                breaker.answered()
                answered, rec["days"] = True, days
                if days:
                    break
            rec["status"] = "ok" if rec["days"] else ("empty" if answered else "error")
    return out


# ---- 影子比價 -----------------------------------------------------------------

def _ref_key(r: dict) -> str:
    return f"{r['route']}|{r['depart']}|{r['return']}|{r['ref_provider']}|{r['ref_observed_at']}"


def shadow_targets(db_path: str, now: datetime, seen: set[str],
                   lookback_hours: float = SHADOW_LOOKBACK_HOURS,
                   limit: int = SHADOW_MAX) -> list[dict]:
    """最近 lookback 小時 SerpAPI／Scrape.do 查過、尚未比過的直飛行程。

    唯讀開啟（檔案不存在時直接報錯，絕不建檔）、只讀現行 observations：這段時間的觀測
    出發日都還沒過，不可能已被封存，所以不需要 ATTACH。"""
    since = (now - timedelta(hours=lookback_hours)).isoformat(timespec="seconds")
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            """SELECT origin, destination, depart_date, return_date, MIN(price),
                      provider, observed_at, carriers
                 FROM observations
                WHERE source='google' AND fare_class='any' AND stops=0
                  AND return_date IS NOT NULL AND observed_at >= ? AND depart_date > ?
                GROUP BY origin, destination, depart_date, return_date, provider, observed_at
                ORDER BY observed_at DESC""",
            (since, now.date().isoformat())).fetchall()
    finally:
        conn.close()
    out = []
    for o, d, dep, ret, price, provider, obs_at, carriers in rows:
        t = {"route": f"{o}-{d}", "depart": dep, "return": ret, "ref_price": float(price),
             "ref_provider": provider, "ref_observed_at": obs_at, "ref_carriers": carriers}
        if _ref_key(t) in seen:
            continue
        seen.add(_ref_key(t))
        out.append(t)
        if len(out) >= limit:
            break
    return out


def run_shadow(backend, targets: list[dict], now: datetime, breaker: Breaker | None = None,
               sleep=None, elapsed=None, out: list | None = None) -> list[dict]:
    """elapsed()：從 now 起算經過的秒數，讓 lag 量的是「實際比價的時刻」而不是開跑時刻。"""
    sleep = sleep or time.sleep
    breaker = breaker or Breaker()
    elapsed = elapsed or (lambda: 0.0)
    out = [] if out is None else out
    for t in targets:
        origin, dest = t["route"].split("-")
        rec = {"kind": "shadow", **t, "key": _ref_key(t), "attempts": 0, "status": "error",
               "errors": [], "fli_price": None, "fli_carriers": None, "diff_pct": None,
               "lag_hours": None}
        out.append(rec)
        for attempt in range(SHADOW_RETRIES + 1):
            stop = breaker.check()
            if stop:
                rec["errors"].append(f"skipped: {stop}")
                break
            if attempt:
                sleep(RETRY_WAIT_S)
            rec["attempts"] += 1
            try:
                got = backend.cheapest(origin, dest, date.fromisoformat(t["depart"]),
                                       date.fromisoformat(t["return"]))
            except FliError as exc:
                rec["errors"].append(str(exc))
                breaker.failed(exc)
                continue
            breaker.answered()
            if got is None:
                rec["status"] = "none"
            else:
                rec["status"], (rec["fli_price"], rec["fli_carriers"]) = "ok", got
                rec["diff_pct"] = round((got[0] - t["ref_price"]) / t["ref_price"] * 100, 2)
            break
        ref_at = datetime.fromisoformat(t["ref_observed_at"].replace("Z", "+00:00"))
        when = now + timedelta(seconds=elapsed())
        rec["lag_hours"] = round((when - ref_at).total_seconds() / 3600, 2)
    return out


# ---- 執行與輸出 ---------------------------------------------------------------

def _stamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H%MZ")


def _stamp_time(name: str) -> datetime | None:
    try:
        return datetime.strptime(name[:16], "%Y-%m-%dT%H%MZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_ts(value) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def read_records(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _files(out_dir: Path, since: datetime) -> list[Path]:
    cutoff = _stamp(since)
    return [p for p in sorted(out_dir.glob("*.jsonl.gz")) if p.name[:16] >= cutoff]


def _safe_records(path: Path) -> list[dict] | None:
    """壞掉的單一檔案不能拖垮整輪或整份報告。"""
    try:
        return read_records(path)
    except (OSError, EOFError, ValueError) as exc:
        log.warning("略過無法讀取的 %s（%s）", path.name, exc)
        return None


def load_seen(out_dir: Path, now: datetime, days: int = DEDUPE_DAYS) -> set[str]:
    seen = set()
    for path in _files(out_dir, now - timedelta(days=days)):
        for rec in _safe_records(path) or []:
            if rec.get("kind") == "shadow" and rec.get("key"):
                seen.add(rec["key"])
    return seen


def minutes_since_last_run(out_dir: Path, now: datetime) -> float | None:
    stamps = [t for p in out_dir.glob("*.jsonl.gz") if (t := _stamp_time(p.name))]
    if not stamps:
        return None
    return (now - max(stamps)).total_seconds() / 60


def write_records(out_dir: Path, now: datetime, records: list[dict]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = _stamp(now)
    path, n = out_dir / f"{stem}.jsonl.gz", 1
    while path.exists():                      # 同一分鐘重跑：另開新檔，絕不覆寫
        n += 1
        path = out_dir / f"{stem}-{n}.jsonl.gz"
    # mtime=0：同樣內容產出同樣位元組，git 不會因時間戳多存一份
    with open(path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        for rec in records:
            gz.write((json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n").encode())
    return path


def _kind(err: str) -> str:
    k = err.split(":", 1)[0]
    return k if k in ERROR_KINDS else "unknown"


def _kinds(errors) -> dict:
    kinds: dict[str, int] = {}
    for e in errors:
        kinds[_kind(e)] = kinds.get(_kind(e), 0) + 1
    return kinds


def _shadow_line(r: dict) -> str:
    head = f"{r['route']} {r['depart']}/{r['return']} {r['status']}"
    if r["status"] == "ok":
        return (head + f" {r['diff_pct']:+.1f}% (fli {r['fli_price']:.0f} vs "
                f"{r['ref_provider']} {r['ref_price']:.0f}, lag {r['lag_hours']}h)")
    if r["status"] == "error" and r["errors"]:
        return head + f" [{_kind(r['errors'][-1])}]"
    return head


def run(config_path: str = "config.yaml", db_path: str = "prices.db",
        out_dir: str = "data/fli", now: datetime | None = None, backend=None,
        sleep=None, breaker: Breaker | None = None) -> dict:
    from .runner import load_config
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)  # 不傳 → 真實時鐘
    t0 = time.monotonic()                     # 只量經過時間；所有日期都由 now 導出
    elapsed = lambda: time.monotonic() - t0   # noqa: E731
    cfg = load_config(config_path)
    routes = [(r["origin"], r["destination"]) for r in cfg["routes"]]
    out = Path(out_dir)

    problems: list[str] = []
    # 先做不花 Google 請求的部分：prices.db 有問題時不該先燒掉 5 分鐘的日曆請求
    try:
        targets = shadow_targets(db_path, now, load_seen(out, now))
    except sqlite3.Error as exc:
        targets = []
        problems.append(f"shadow_targets: {type(exc).__name__}: {exc}")
    gap = minutes_since_last_run(out, now)
    skip_sweep = gap is not None and 0 <= gap < MIN_SWEEP_GAP_MIN

    backend = backend or FliBackend(currency=cfg.get("defaults", {}).get("currency", "twd").upper())
    breaker = breaker or Breaker()
    calendar: list[dict] = []
    shadow: list[dict] = []
    try:
        if not skip_sweep:
            sweep_calendar(backend, routes, now.date(), horizon_days(cfg), breaker, sleep, calendar)
        run_shadow(backend, targets, now, breaker, sleep, elapsed, shadow)
    except Exception as exc:                  # 非預期錯誤：先寫出已拿到的部分，再往上拋
        problems.append(f"crash: {type(exc).__name__}: {exc}"[:300])
        raise
    finally:
        all_errors = [e for r in calendar + shadow for e in r["errors"]]
        cal_ok = sum(r["status"] == "ok" for r in calendar)
        sh_ok = sum(r["status"] == "ok" for r in shadow)
        summary = {
            "kind": "summary", "started_at": now.isoformat(timespec="seconds"),
            "finished_at": (now + timedelta(seconds=elapsed())).isoformat(timespec="seconds"),
            "fli_version": FLI_VERSION, "requests": backend.requests,
            "calendar_skipped": (f"last run {gap:.0f} min ago" if skip_sweep else None),
            "calendar_windows": len(calendar), "calendar_ok": cal_ok,
            "calendar_first_try_ok": sum(r["status"] == "ok" and r["attempts"] == 1
                                         for r in calendar),
            "calendar_priced_days": sum(len(r["days"]) for r in calendar),
            "shadow_targets": len(shadow), "shadow_ok": sh_ok,
            "shadow_none": sum(r["status"] == "none" for r in shadow),
            # 逐筆列在摘要裡：workflow log 直接看得到每組比價，不必下載 .gz
            "shadow": [_shadow_line(r) for r in shadow],
            "stopped": breaker.reason, "errors": _kinds(all_errors),
            "blocked": sum(e.startswith("blocked") for e in all_errors),
            "problems": problems,
            # 有做事卻一筆價格都沒拿到。「none」（查無航班）不算：解析悄悄失效時就是這樣
            "all_failed": bool(calendar or shadow) and cal_ok == 0 and sh_ok == 0,
        }
        path = write_records(out, now, calendar + shadow + [summary])
        summary["path"] = str(path)
    return summary


def failed(summary: dict) -> bool:
    """workflow 要不要變紅。見模組 docstring。"""
    return bool(summary["all_failed"] or summary["blocked"] or summary["problems"])


# ---- 觀察期報告（第二階段判斷用）-------------------------------------------------

def report(out_dir: str = "data/fli", days: int = 14, now: datetime | None = None) -> dict:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cal, sh, runs, cal_vs_ref, unreadable = [], [], [], [], 0
    cal_runs: list[tuple[datetime, dict]] = []   # (這輪開始時刻, {航線: {出發日: 價}})
    for path in _files(Path(out_dir), now - timedelta(days=days)):
        recs = _safe_records(path)
        if recs is None:
            unreadable += 1
            continue
        file_cal = [r for r in recs if r.get("kind") == "calendar"]
        file_sh = [r for r in recs if r.get("kind") == "shadow"]
        cal += file_cal
        sh += file_sh
        runs += [r for r in recs if r.get("kind") == "summary"]
        days_by_route: dict[str, dict] = {}
        for r in file_cal:
            days_by_route.setdefault(r["route"], {}).update(r.get("days") or {})
        run_at = _stamp_time(path.name)
        if days_by_route and run_at is not None:
            cal_runs.append((run_at, days_by_route))

    # 第二階段要用的是日曆價：同航線、同出發日、住 STAY_DAYS 晚的日曆價拿來跟參考價比
    # （不多花任何請求）。日曆取參考觀測前後 SHADOW_MAX_LAG_H 小時內、時間最接近的
    # 那一輪，不限同一輪：FSC 與 Verify 30 分鐘內先後跑完時，第二輪 fli 不重掃日曆，
    # 原本那輪的比價就沒有日曆價可對（2026-10-26 12:49 那輪 5 組全部落空）。
    for r in sh:
        stay = (date.fromisoformat(r["return"]) - date.fromisoformat(r["depart"])).days
        ref_at = _parse_ts(r.get("ref_observed_at"))
        if stay != STAY_DAYS or ref_at is None:
            continue
        near = [(abs((run_at - ref_at).total_seconds()) / 3600, price)
                for run_at, by_route in cal_runs
                if (price := by_route.get(r["route"], {}).get(r["depart"]))]
        near = [x for x in near if x[0] <= SHADOW_MAX_LAG_H]
        if near:
            price = min(near)[1]
            cal_vs_ref.append({"ref_provider": r.get("ref_provider"),
                               "diff_pct": round((price - r["ref_price"]) / r["ref_price"] * 100, 2)})

    # 某 (航線, 時段序號) 整段期間每一次都是 Google 明確回「沒有航班」（empty）→
    # 視為沒有班表，不計。只要有一次是錯誤（google_error、逾時、被 Breaker 跳過），
    # 那一組就照算：錯誤不是「沒有班表」的證據。原本的規則是「從沒拿到價就不計」，
    # 2026-10-26 main 第一輪 KHH-NGO 5 段全是 google_error，整組被剔除，報告寫
    # error_rate 0、PASS——一條航線整整兩週都失敗也照樣 PASS。
    statuses: dict[tuple, set] = {}
    for r in cal:
        statuses.setdefault((r["route"], r.get("idx")), set()).add(r["status"])
    no_schedule = {k for k, st in statuses.items() if st == {"empty"}}
    scored = [r for r in cal if (r["route"], r.get("idx")) not in no_schedule]
    by_route: dict[str, list[int]] = {}
    for r in scored:
        tally = by_route.setdefault(r["route"], [0, 0])
        tally[0] += r["status"] == "ok"
        tally[1] += 1
    fresh = [r for r in sh if r["status"] == "ok" and r["lag_hours"] is not None
             and r["lag_hours"] <= SHADOW_MAX_LAG_H]
    tol = SHADOW_TOLERANCE * 100
    errors = [e for r in cal + sh for e in r.get("errors") or []]
    res = {
        "runs": len(runs), "unreadable_files": unreadable,
        "red_runs": sum(failed(r) for r in runs if "problems" in r),
        "requests": sum(r.get("requests", 0) for r in runs),
        "calendar_windows": len(cal), "calendar_scored": len(scored),
        "calendar_no_schedule": sorted(f"{k[0]}#{k[1]}" for k in no_schedule),
        # 一條航線整段失敗時，總比例可能還過得了門檻；逐航線列出才看得到
        "calendar_by_route": {k: f"{ok}/{n}" for k, (ok, n) in sorted(by_route.items())},
        "calendar_ok_rate": _rate(sum(r["status"] == "ok" for r in scored), len(scored)),
        "calendar_first_try_rate": _rate(
            sum(r["status"] == "ok" and r["attempts"] == 1 for r in scored), len(scored)),
        "calendar_error_rate": _rate(sum(r["status"] == "error" for r in scored), len(scored)),
        "shadow_pairs": len(sh), "shadow_ok": sum(r["status"] == "ok" for r in sh),
        "shadow_fresh": len(fresh),
        "shadow_within_rate": _rate(sum(abs(r["diff_pct"]) <= tol for r in fresh), len(fresh)),
        # 參考價有 SerpAPI 與 Scrape.do 兩個來源；分開列，才分得出是 fli 不準還是某個
        # 參考來源本身跟 Google 不一致（2026-10-28／29 KHH-NRT 兩組，fli 與 SerpAPI 都是
        # 12,858，只有 Scrape.do 低約 7%）
        "shadow_within_by_provider": _within_by(fresh, "ref_provider", tol),
        "shadow_median_abs_diff_pct": (round(statistics.median(abs(r["diff_pct"]) for r in fresh), 2)
                                       if fresh else None),
        "calendar_vs_ref_pairs": len(cal_vs_ref),
        "calendar_vs_ref_within_rate": _rate(sum(abs(d["diff_pct"]) <= tol for d in cal_vs_ref),
                                             len(cal_vs_ref)),
        "calendar_vs_ref_by_provider": _within_by(cal_vs_ref, "ref_provider", tol),
        "blocked": sum(e.startswith("blocked") for e in errors),
        "error_kinds": _kinds(errors),
    }
    res["criteria"] = {
        "calendar_ok_rate>=95%": _verdict(res["calendar_ok_rate"], CRITERION_CAL_OK),
        "shadow_within_2%>=90%": _verdict(res["shadow_within_rate"], CRITERION_WITHIN),
        "calendar_vs_ref_within_2%>=90%": _verdict(res["calendar_vs_ref_within_rate"],
                                                    CRITERION_WITHIN),
        "no_blocking": ("NO DATA" if not runs else "PASS" if res["blocked"] == 0 else "FAIL"),
    }
    return res


def _within_by(recs: list[dict], field: str, tol: float) -> dict[str, str]:
    """依 field 分組，列出「價差在 ±tol% 內的組數／總組數」。"""
    groups: dict[str, list[int]] = {}
    for r in recs:
        tally = groups.setdefault(r.get(field) or "?", [0, 0])
        tally[0] += abs(r["diff_pct"]) <= tol
        tally[1] += 1
    return {k: f"{ok}/{n}" for k, (ok, n) in sorted(groups.items())}


def _rate(n: int, d: int):
    return round(n / d, 4) if d else None


def _verdict(value, threshold) -> str:
    if value is None:
        return "NO DATA"
    return "PASS" if value >= threshold else "FAIL"


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cmd = argv[1] if len(argv) > 1 else "run"
    if cmd == "report":
        res = report(argv[2] if len(argv) > 2 else "data/fli",
                     int(argv[3]) if len(argv) > 3 else 14)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    if cmd != "run":
        print(__doc__)
        return 2
    args = argv[2:] + [None] * 3
    summary = run(args[0] or "config.yaml", args[1] or "prices.db", args[2] or "data/fli")
    print(json.dumps(summary, ensure_ascii=False))
    if failed(summary):
        print("✖ fli 觀察本輪異常（全部沒拿到價／出現封鎖頁／prices.db 讀不到），以 exit 1 結束；"
              "這輪的紀錄已寫出。現有監控不受影響，見 RUNBOOK §3c。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
