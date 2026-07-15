"""Daily full-service fare snapshot runner. Usage: python -m farehunter.fsc_snapshot

每日 SerpAPI 上限 SEARCHES_PER_DAY(6):固定 3 個 Rotation Slot + 最多 3 個
Verification Slot(Alert / CTA / Hero 三決策面,彈性 fallback)。先建 plans、
後執行,結構保證每 plan 恰一次 search、總數 ≤6。

exit code 規則:planned>0 且 api_errors==planned(全數 API 失敗)→ 非零退出
讓 GitHub Actions 紅燈寄信;HTTP 成功但無符合航班(api_ok_no_match)不算失敗,
僅在 summary 輸出 warning。
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import date as _date

from .runner import load_config
from .serpapi_flights import (search_google_flights, parse_full_service,
                              parse_cheapest_direct,
                              pick_routes_for_today, snapshot_dates,
                              horizon_for_slot, build_verification_plans,
                              SEARCHES_PER_DAY, SerpApiError)
from .storage import Store

log = logging.getLogger(__name__)

ROTATION_SLOTS = 3   # 固定保留,與 verification 完全解耦


def build_plans(cfg: dict, store: Store, today: _date,
                ranked_path: str = "docs/ranked.json") -> list[dict]:
    """先建計畫、後執行。純 DB/JSON 讀取,零 API。

    3 個 rotation 先加入 claimed_trips;verification 最多 3 個且不得與
    rotation 或彼此重複 trip(見 build_verification_plans)。plans 總長以
    assert 鎖死 ≤ SEARCHES_PER_DAY。
    """
    routes = cfg["routes"]
    thresholds = {(r["origin"], r["destination"]): r.get("absolute_threshold")
                  for r in routes}
    plans: list[dict] = []
    claimed_trips: set = set()
    for slot, route in enumerate(pick_routes_for_today(routes, today=today,
                                                       per_day=ROTATION_SLOTS)):
        weeks = horizon_for_slot(len(routes), today, slot, per_day=ROTATION_SLOTS)
        dep, ret = snapshot_dates(today, horizon_weeks=weeks)
        plans.append({"origin": route["origin"], "destination": route["destination"],
                      "depart_date": dep, "return_date": ret, "kind": "rotation"})
        claimed_trips.add((route["origin"], route["destination"], dep, ret))

    verify_budget = SEARCHES_PER_DAY - len(plans)
    for v in build_verification_plans(store.conn, thresholds, routes,
                                      ranked_path=ranked_path, today=today,
                                      claimed_trips=claimed_trips,
                                      max_slots=verify_budget):
        plans.append({**v, "kind": "verify"})

    assert len(plans) <= SEARCHES_PER_DAY, "SerpAPI 每日上限保護:plans 超額"
    return plans


def run(config_path: str = "config.yaml", db_path: str = "prices.db",
        ranked_path: str = "docs/ranked.json") -> dict:
    cfg = load_config(config_path)
    today = _date.today()
    store = Store(db_path)
    summary = {"planned": 0, "api_ok": 0, "api_errors": 0, "api_ok_no_match": 0,
               "recorded": 0, "real": 0, "insights": 0,
               "rotation": 0, "verify": 0,
               "slot_alert": 0, "slot_cta": 0, "slot_hero": 0}
    try:
        plans = build_plans(cfg, store, today, ranked_path=ranked_path)
        summary["planned"] = len(plans)
        for plan in plans:
            o, d = plan["origin"], plan["destination"]
            dep, ret = plan["depart_date"], plan["return_date"]
            try:
                payload = search_google_flights(o, d, dep, ret)
            except SerpApiError as exc:
                log.error("Snapshot failed %s→%s: %s", o, d, exc)
                summary["api_errors"] += 1
                continue
            summary["api_ok"] += 1
            if plan["kind"] == "rotation":
                summary["rotation"] += 1
            else:
                summary["verify"] += 1
                summary[f"slot_{plan.get('slot_kind', 'alert')}"] += 1
                log.info("Verify probe [%s] %s→%s %s~%s",
                         plan.get("slot_kind"), o, d, dep, ret)
            pi = payload.get("price_insights") or {}
            if pi.get("price_level"):
                rng = pi.get("typical_price_range") or [None, None]
                store.record_insight(o, d, dep, str(pi["price_level"]),
                                     rng[0], rng[1])
                summary["insights"] += 1
            real = parse_cheapest_direct(payload, o, d, dep, ret)
            if real is not None:
                store.record(real); summary["real"] += 1
                log.info("Real probe %s→%s %s: %.0f TWD (%s)",
                         o, d, dep, real.price, real.carriers)
            offer = parse_full_service(payload, o, d, dep, ret)
            if offer is None:
                if real is None:
                    summary["api_ok_no_match"] += 1
                    log.warning("API OK but no matching flight %s→%s %s", o, d, dep)
                else:
                    log.info("No all-full-service itinerary %s→%s %s", o, d, dep)
            else:
                store.record(offer); summary["recorded"] += 1
                log.info("FSC snapshot %s→%s %s: %.0f TWD (%s)",
                         o, d, dep, offer.price, offer.carriers)
            time.sleep(1)
    finally:
        store.close()
    log.info("Snapshot summary: %s", summary)
    if summary["api_ok_no_match"]:
        log.warning("%d plan(s) returned HTTP OK but zero matching flights",
                    summary["api_ok_no_match"])
    return summary


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = run(argv[0] if len(argv) > 0 else "config.yaml",
            argv[1] if len(argv) > 1 else "prices.db",
            argv[2] if len(argv) > 2 else "docs/ranked.json")
    print(f"快照完成: 計畫 {s['planned']}(輪替 {s['rotation']}/驗證 {s['verify']}"
          f":alert {s['slot_alert']}/cta {s['slot_cta']}/hero {s['slot_hero']})"
          f", API 成功 {s['api_ok']}/錯誤 {s['api_errors']}/成功無航班 "
          f"{s['api_ok_no_match']}, 實價 {s['real']} 傳統 {s['recorded']} "
          f"insights {s['insights']}")
    # 全數 API 失敗 → 非零退出,讓 workflow 紅燈;成功但零航班不算失敗
    if s["planned"] > 0 and s["api_errors"] == s["planned"]:
        print("ERROR: 所有 API 查詢均失敗,快照零產出", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
