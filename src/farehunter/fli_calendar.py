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
2. 影子比價：最近 SHADOW_LOOKBACK_HOURS 小時內 SerpAPI／Scrape.do 查過的行程，
   用 fli 查同一組去回日期，記下兩邊價差——第二階段要不要讓 fli 取代 SerpAPI，
   就看這個數字（ADR 0003 的判斷標準，`report` 子命令直接算）。
整輪沒有任何一段日曆有價、也沒有任何一次比價得到回答時以 exit 1 結束，讓 workflow
變紅。8 條航線（含 TPE-NRT 這種天天有班的）同時全空，只會是 Google 封鎖、或 fli 的
解析悄悄失效——後者不會丟例外，所以「全空」也要算進去。

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
TIMEOUT_S = 45
CAL_RETRIES = 1               # 日曆時段回空／出錯時再打幾次
SHADOW_RETRIES = 2            # 影子比價出錯時再打幾次（實測 Google 內部錯誤約 4 成）
RETRY_WAIT_S = 12.0
SHADOW_LOOKBACK_HOURS = 24
SHADOW_MAX = 15
DEDUPE_DAYS = 3               # 這幾天內已比過的參考觀測不再重比
REQUEST_BUDGET = 200          # 單次執行的硬上限（正常約 60–90 次）

# ADR 0003 第二階段判斷標準（report 子命令據此給 PASS/FAIL）
CRITERION_CAL_OK = 0.95       # 日曆時段重試後有價的比例（從沒拿到過價的時段視為沒班表、不計）
CRITERION_SHADOW_WITHIN = 0.90
SHADOW_TOLERANCE = 0.02       # 與 SerpAPI 同行程價差 ±2% 以內
SHADOW_MAX_LAG_H = 6.0        # 只用參考觀測後 6 小時內完成的比價評估準確度


class FliError(RuntimeError):
    """沒有拿到可用的 Google 回應：連線錯誤、HTTP 錯誤、封鎖頁、Google 內部錯誤
    （回 HTTP 200 但沒有 wrb.fr 資料）或超出請求預算。訊息開頭是種類，方便 report 分類。"""


def resolve_airport(enum_cls, code: str):
    """把 IATA 代碼轉成 fli 的 Airport 成員，繞過 fli 0.9.0 的別名 bug。

    fli 用 Enum("Airport", {代碼: 機場名}) 建表，機場名重複的 48 個代碼會變成別名：
    Airport.OKA（那霸）其實是 Airport.NAH（印尼 Tahuna 的 Naha Airport），請求送出去
    就變成 NAH，KHH-OKA 永遠查不到（2026-10-25 實測）。fli 序列化只讀 `.name`，所以
    名稱不符時自建一個同類別、名稱正確的成員（值另取，避免 pydantic 依值換回 NAH）。
    """
    member = enum_cls[code]
    if member.name == code:
        return member
    fake = object.__new__(enum_cls)
    fake._name_ = code
    fake._value_ = f"{member.value} [{code}]"
    return fake


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

    @property
    def requests(self) -> int:
        return self.client.calls

    def calendar(self, origin: str, dest: str, start: date, end: date,
                 stay: int = STAY_DAYS) -> dict[str, float]:
        return _as_fli_error(self._calendar, origin, dest, start, end, stay)

    def cheapest(self, origin: str, dest: str, dep: date, ret: date):
        """同一組去回日期的最便宜直飛來回價 → (價格, 航空公司代碼)；查無航班回 None。"""
        return _as_fli_error(self._cheapest, origin, dest, dep, ret)

    def _calendar(self, origin, dest, start, end, stay):
        from fli.core import build_date_search_segments
        from fli.models import DateSearchFilters, MaxStops, PassengerInfo, SeatType
        from fli.search import SearchDates
        from fli.search._wire import parse_first_wrb_payload

        segments, trip = build_date_search_segments(
            resolve_airport(self.Airport, origin), resolve_airport(self.Airport, dest),
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

        class KeepOutbound(SearchFlights):
            # 來回總價在去程列上；search() 只回去回組合，所以把去程清單留下來
            outbound = None

            def _expand_multi_leg(self, flights, filters, **kw):
                if self.outbound is None:
                    self.outbound = flights
                return super()._expand_multi_leg(flights, filters, **kw)

        segments, trip = build_flight_segments(
            resolve_airport(self.Airport, origin), resolve_airport(self.Airport, dest),
            dep.isoformat(), ret.isoformat())
        filters = FlightSearchFilters(
            trip_type=trip, passenger_info=PassengerInfo(adults=1), flight_segments=segments,
            stops=MaxStops.NON_STOP, seat_type=SeatType.ECONOMY, sort_by=SortBy.CHEAPEST)
        search = KeepOutbound()
        search.client = self.client
        try:
            self.client.guard(lambda: search.search(
                filters, top_n=1, currency=self.currency, language=self.language))
        except FliError:
            if search.outbound is None:      # 回程那次失敗不要緊，去程已有來回總價
                raise
        if not search.outbound:
            if parse_first_wrb_payload(self.client.last_text) is None:
                raise FliError(f"google_error: {self.client.diag()}")
            return None
        priced = [f for f in search.outbound if f.price]
        if not priced:
            return None
        best = min(priced, key=lambda f: f.price)
        carriers = ",".join(dict.fromkeys(
            leg.airline.name.removeprefix("_") for leg in best.legs))
        return float(best.price), carriers


def _as_fli_error(fn, *args):
    """一條航線出事（未知機場代碼、fli 驗證錯誤…）只記成這一筆失敗，不拖垮整輪。"""
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
            with self._gate:                  # 也把 fli 的平行回程查詢排成單線
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
            if "unusual traffic" in low or "/sorry/" in self.last["path"]:
                raise FliError(f"blocked: captcha page HTTP {resp.status_code}")
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
                raise FliError(f"fli: {type(exc).__name__}: {str(exc)[:120]}; {self.diag()}") from exc

        def diag(self) -> str:
            if not self.last:
                return "no response"
            return f"HTTP {self.last['status']} {self.last['bytes']}B {self.last['path'][-40:]}"

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
                   sleep=None) -> list[dict]:
    sleep = sleep or time.sleep               # 呼叫時才取，測試換得掉
    records = []
    for origin, dest in routes:
        for idx, (start, end) in enumerate(windows(today, horizon)):
            rec = {"kind": "calendar", "route": f"{origin}-{dest}", "idx": idx,
                   "start": start.isoformat(),
                   "end": end.isoformat(), "stay": STAY_DAYS, "attempts": 0,
                   "status": "error", "error": None, "days": {}}
            for attempt in range(CAL_RETRIES + 1):
                if attempt:
                    sleep(RETRY_WAIT_S)
                rec["attempts"] += 1
                try:
                    days = backend.calendar(origin, dest, start, end, STAY_DAYS)
                except FliError as exc:
                    rec["status"], rec["error"] = "error", str(exc)
                    if str(exc).startswith("budget"):
                        break
                    continue
                rec["status"], rec["error"], rec["days"] = ("ok" if days else "empty"), None, days
                if days:
                    break
            records.append(rec)
    return records


# ---- 影子比價 -----------------------------------------------------------------

def _ref_key(r: dict) -> str:
    return f"{r['route']}|{r['depart']}|{r['return']}|{r['ref_provider']}|{r['ref_observed_at']}"


def shadow_targets(db_path: str, now: datetime, seen: set[str],
                   lookback_hours: float = SHADOW_LOOKBACK_HOURS,
                   limit: int = SHADOW_MAX) -> list[dict]:
    """最近 lookback 小時 SerpAPI／Scrape.do 查過、尚未比過的直飛行程。

    唯讀開啟、只讀現行 observations：這段時間的觀測出發日都還沒過，不可能已被封存，
    所以不需要 ATTACH（也就不會因封存檔問題拖垮觀察 job）。"""
    since = (now - timedelta(hours=lookback_hours)).isoformat(timespec="seconds")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
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


def run_shadow(backend, targets: list[dict], now: datetime, sleep=None) -> list[dict]:
    sleep = sleep or time.sleep
    records = []
    for t in targets:
        origin, dest = t["route"].split("-")
        rec = {"kind": "shadow", **t, "key": _ref_key(t), "attempts": 0, "status": "error",
               "error": None, "fli_price": None, "fli_carriers": None, "diff_pct": None,
               "lag_hours": None}
        for attempt in range(SHADOW_RETRIES + 1):
            if attempt:
                sleep(RETRY_WAIT_S)
            rec["attempts"] += 1
            try:
                got = backend.cheapest(origin, dest, date.fromisoformat(t["depart"]),
                                       date.fromisoformat(t["return"]))
            except FliError as exc:
                rec["error"] = str(exc)
                if str(exc).startswith("budget"):
                    break
                continue
            rec["error"] = None
            if got is None:
                rec["status"] = "none"
            else:
                rec["status"], (rec["fli_price"], rec["fli_carriers"]) = "ok", got
                rec["diff_pct"] = round((got[0] - t["ref_price"]) / t["ref_price"] * 100, 2)
            break
        ref_at = datetime.fromisoformat(t["ref_observed_at"].replace("Z", "+00:00"))
        rec["lag_hours"] = round((now - ref_at).total_seconds() / 3600, 2)
        records.append(rec)
    return records


# ---- 執行與輸出 ---------------------------------------------------------------

def load_seen(out_dir: Path, now: datetime, days: int = DEDUPE_DAYS) -> set[str]:
    seen = set()
    cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H%MZ")
    for path in sorted(out_dir.glob("*.jsonl.gz")):
        if path.name[:16] < cutoff:
            continue
        for rec in read_records(path):
            if rec.get("kind") == "shadow" and rec.get("key"):
                seen.add(rec["key"])
    return seen


def read_records(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_records(out_dir: Path, now: datetime, records: list[dict]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = now.strftime("%Y-%m-%dT%H%MZ")
    path, n = out_dir / f"{stem}.jsonl.gz", 1
    while path.exists():                      # 同一分鐘重跑：另開新檔，絕不覆寫
        n += 1
        path = out_dir / f"{stem}-{n}.jsonl.gz"
    # mtime=0：同樣內容產出同樣位元組，git 不會因時間戳多存一份
    with open(path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        for rec in records:
            gz.write((json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n").encode())
    return path


def run(config_path: str = "config.yaml", db_path: str = "prices.db",
        out_dir: str = "data/fli", now: datetime | None = None, backend=None,
        sleep=None) -> dict:
    from .runner import load_config
    now = now or datetime.now(timezone.utc)     # production 不傳 → 真實時鐘
    t0 = time.monotonic()                        # 只量經過時間；所有日期都由 now 導出
    cfg = load_config(config_path)
    routes = [(r["origin"], r["destination"]) for r in cfg["routes"]]
    backend = backend or FliBackend(currency=cfg.get("defaults", {}).get("currency", "twd").upper())
    out = Path(out_dir)

    calendar = sweep_calendar(backend, routes, now.date(), horizon_days(cfg), sleep=sleep)
    targets = shadow_targets(db_path, now, load_seen(out, now))
    shadow = run_shadow(backend, targets, now, sleep=sleep)

    cal_ok = sum(r["status"] == "ok" for r in calendar)
    sh_ok = sum(r["status"] in ("ok", "none") for r in shadow)
    summary = {
        "kind": "summary", "started_at": now.isoformat(timespec="seconds"),
        "finished_at": (now + timedelta(seconds=time.monotonic() - t0)).isoformat(timespec="seconds"),
        "fli_version": FLI_VERSION, "requests": backend.requests,
        "calendar_windows": len(calendar), "calendar_ok": cal_ok,
        "calendar_first_try_ok": sum(r["status"] == "ok" and r["attempts"] == 1 for r in calendar),
        "calendar_priced_days": sum(len(r["days"]) for r in calendar),
        "shadow_targets": len(shadow), "shadow_answered": sh_ok,
        "all_failed": backend.requests > 0 and cal_ok == 0 and sh_ok == 0,
    }
    path = write_records(out, now, calendar + shadow + [summary])
    summary["path"] = str(path)
    return summary


# ---- 觀察期報告（第二階段判斷用）-------------------------------------------------

def report(out_dir: str = "data/fli", days: int = 14, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H%MZ")
    cal, sh, runs = [], [], []
    for path in sorted(Path(out_dir).glob("*.jsonl.gz")):
        if path.name[:16] < cutoff:
            continue
        for rec in read_records(path):
            {"calendar": cal, "shadow": sh, "summary": runs}.get(rec.get("kind"), []).append(rec)
    # 某 (航線, 時段序號) 整段期間從沒拿到價 → 視為沒有班表，不算失敗；
    # 其他輪拿到過、這輪卻空或錯 → 才算漏報。
    ever_ok = {(r["route"], r.get("idx")) for r in cal if r["status"] == "ok"}
    scored = [r for r in cal if (r["route"], r.get("idx")) in ever_ok]
    fresh = [r for r in sh if r["status"] == "ok" and r["lag_hours"] is not None
             and r["lag_hours"] <= SHADOW_MAX_LAG_H]
    within = [r for r in fresh if abs(r["diff_pct"]) <= SHADOW_TOLERANCE * 100]
    errors = [r.get("error") or "" for r in cal + sh if r["status"] == "error"]
    res = {
        "runs": len(runs), "all_failed_runs": sum(bool(r.get("all_failed")) for r in runs),
        "requests": sum(r.get("requests", 0) for r in runs),
        "calendar_windows": len(cal), "calendar_scored": len(scored),
        "calendar_never_priced": sorted({f"{r['route']}#{r.get('idx')}" for r in cal} - {
            f"{k[0]}#{k[1]}" for k in ever_ok}),
        "calendar_ok_rate": _rate(sum(r["status"] == "ok" for r in scored), len(scored)),
        "calendar_first_try_rate": _rate(
            sum(r["status"] == "ok" and r["attempts"] == 1 for r in scored), len(scored)),
        "shadow_pairs": len(sh), "shadow_ok": sum(r["status"] == "ok" for r in sh),
        "shadow_fresh": len(fresh), "shadow_within_rate": _rate(len(within), len(fresh)),
        "shadow_median_abs_diff_pct": (round(statistics.median(abs(r["diff_pct"]) for r in fresh), 2)
                                       if fresh else None),
        "blocked": sum(e.startswith("blocked") for e in errors),
        "error_kinds": _kinds(errors),
    }
    res["criteria"] = {
        "calendar_ok_rate>=95%": _verdict(res["calendar_ok_rate"], CRITERION_CAL_OK),
        "shadow_within_2%>=90%": _verdict(res["shadow_within_rate"], CRITERION_SHADOW_WITHIN),
        "no_blocking": "PASS" if res["blocked"] == 0 else "FAIL",
    }
    return res


def _rate(n: int, d: int):
    return round(n / d, 4) if d else None


def _verdict(value, threshold) -> str:
    if value is None:
        return "NO DATA"
    return "PASS" if value >= threshold else "FAIL"


def _kinds(errors: list[str]) -> dict:
    kinds: dict[str, int] = {}
    for e in errors:
        k = e.split(":", 1)[0] or "unknown"
        kinds[k] = kinds.get(k, 0) + 1
    return kinds


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
    if summary["all_failed"]:
        print("✖ fli 本輪沒有任何一次請求成功（Google 封鎖或 fli 失效？），以 exit 1 結束")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
