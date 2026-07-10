"""Daily full-service fare snapshot runner. Usage: python -m farehunter.fsc_snapshot

C′（2026-07）：每日 SEARCHES_PER_DAY=3 次 SerpAPI 額度不變，槽位重分配——
有合格低價候選時為「2 輪替＋1 驗證」，無候選時完整退回「3 輪替」（與舊行為
一致）。先建 plans 再執行，結構上保證每 plan 恰一次 search、總數 ≤3。
"""
from __future__ import annotations

import logging
import time
from datetime import date as _date

from .runner import load_config
from .serpapi_flights import (search_google_flights, parse_full_service,
                              parse_cheapest_direct,
                              pick_routes_for_today, snapshot_dates,
                              horizon_for_slot, pick_verification_candidate,
                              SEARCHES_PER_DAY, SerpApiError)
from .storage import Store

log = logging.getLogger(__name__)


def build_plans(cfg: dict, store: Store, today: _date) -> list[dict]:
    """先建計畫、後執行。純 DB 讀取，零 API 呼叫。

    有合格候選 → per_day=2 輪替＋1 驗證槽；無候選 → per_day=3 輪替，
    與 C′ 之前的行為完全一致。plans 長度以 assert 鎖死在 SEARCHES_PER_DAY。
    """
    routes = cfg["routes"]
    thresholds = {(r["origin"], r["destination"]): r.get("absolute_threshold")
                  for r in routes}
    cand = pick_verification_candidate(store.conn, thresholds, today=today)
    rotation_n = SEARCHES_PER_DAY - 1 if cand else SEARCHES_PER_DAY
    plans: list[dict] = []
    for slot, route in enumerate(pick_routes_for_today(routes, today=today,
                                                       per_day=rotation_n)):
        weeks = horizon_for_slot(len(routes), today, slot, per_day=rotation_n)
        dep, ret = snapshot_dates(today, horizon_weeks=weeks)
        plans.append({"origin": route["origin"],
                      "destination": route["destination"],
                      "depart_date": dep, "return_date": ret,
                      "kind": "rotation"})
    if cand:
        plans.append({**cand, "kind": "verify"})
    assert len(plans) <= SEARCHES_PER_DAY, "SerpAPI 每日上限保護：plans 超額"
    return plans


def run(config_path: str = "config.yaml", db_path: str = "prices.db") -> dict:
    cfg = load_config(config_path)
    today = _date.today()
    store = Store(db_path)
    summary = {"searched": 0, "recorded": 0, "real": 0, "errors": 0,
               "verified": 0}
    try:
        plans = build_plans(cfg, store, today)
        for plan in plans:
            o, d = plan["origin"], plan["destination"]
            dep, ret = plan["depart_date"], plan["return_date"]
            summary["searched"] += 1
            try:
                payload = search_google_flights(o, d, dep, ret)
            except SerpApiError as exc:
                log.error("Snapshot failed %s→%s: %s", o, d, exc)
                summary["errors"] += 1
                continue
            if plan["kind"] == "verify":
                summary["verified"] += 1
                log.info("Verify probe %s→%s %s（reason=%s，警報價 %.0f）",
                         o, d, dep, plan.get("reason"), plan.get("price", 0))
            pi = payload.get("price_insights") or {}
            if pi.get("price_level"):
                rng = pi.get("typical_price_range") or [None, None]
                store.record_insight(o, d, dep, str(pi["price_level"]),
                                     rng[0], rng[1])
            # real overall-cheapest DIRECT with confirmed carrier → feeds the
            # monthly view with a genuine price instead of a cache estimate
            real = parse_cheapest_direct(payload, o, d, dep, ret)
            if real is not None:
                store.record(real)
                summary["real"] += 1
                log.info("Real probe %s→%s %s: %.0f TWD (%s)",
                         o, d, dep, real.price, real.carriers)
            # cheapest all-full-service DIRECT → 傳統航空 reference
            offer = parse_full_service(payload, o, d, dep, ret)
            if offer is None:
                log.info("No all-full-service itinerary %s→%s %s", o, d, dep)
            else:
                store.record(offer)
                summary["recorded"] += 1
                log.info("FSC snapshot %s→%s %s: %.0f TWD (%s)",
                         o, d, dep, offer.price, offer.carriers)
            time.sleep(1)
    finally:
        store.close()
    log.info("Snapshot summary: %s", summary)
    return summary


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = run(sys.argv[1] if len(sys.argv) > 1 else "config.yaml",
            sys.argv[2] if len(sys.argv) > 2 else "prices.db")
    print(f"快照完成: 查詢 {s['searched']} 次, 實價 {s['real']} 筆, "
          f"傳統航空 {s['recorded']} 筆, 驗證槽 {s['verified']} 次, "
          f"錯誤 {s['errors']} 次")
