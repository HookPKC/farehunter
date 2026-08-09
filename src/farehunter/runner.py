"""Runner: load config, iterate months per route, store, evaluate, notify."""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from .travelpayouts import TravelpayoutsClient, parse_offers, TravelpayoutsError
from .storage import Store
from .analyzer import evaluate
from .notify import notify, channels_configured
from . import health
from . import price_state

log = logging.getLogger(__name__)


def _resolve_state(store, offer, now):
    """把 Aviasales candidate 解析成價格狀態。零 API：只讀既有 observations。

    candidate 的 observed_at 即本輪抓取時刻（store.record 亦以此刻寫入），
    因此以 now 代表；reference 取同航程、不晚於 now 的最新 google 觀測。
    """
    cand = price_state.PriceObservation(
        origin=offer.origin, destination=offer.destination,
        depart_date=offer.depart_date, return_date=offer.return_date,
        price=offer.price, currency=offer.currency, carriers=offer.carriers,
        stops=offer.stops, fare_class=offer.fare_class,
        source=offer.source, observed_at=now)
    row = store.latest_itinerary_google(
        origin=offer.origin, destination=offer.destination,
        depart_date=offer.depart_date, return_date=offer.return_date,
        stops=offer.stops, fare_class=offer.fare_class,
        currency=offer.currency, not_after=now.isoformat(timespec="seconds"))
    ref = None
    if row is not None:
        obs = row["observed_at"]
        ts = datetime.fromisoformat(obs.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ref = price_state.PriceObservation(
            origin=row["origin"], destination=row["destination"],
            depart_date=row["depart_date"], return_date=row["return_date"],
            price=row["price"], currency=row["currency"], carriers=row["carriers"],
            stops=row["stops"], fare_class=row["fare_class"],
            source=row["source"], observed_at=ts)
    return price_state.resolve_alert_price(cand, now, reference=ref)

# ---- 防重複 guard（fail-open）----------------------------------------------
# 目的：GitHub schedule、手動 Run、外部排程器（cron-job.org）任意組合觸發時，
# 若資料仍新鮮就跳過本輪，避免重複抓價與 commit 噪音。
# 鐵律：guard 只能「跳過」，絕不能「擋路」——任何讀取/解析錯誤一律照常執行，
# 寧可重複，不可讓 guard 自己成為新的停擺原因（PLAYBOOK 1-6 後續強化）。
GUARD_MINUTES = 55
WEB_EXPORT_PATH = "docs/data.json"


def guard_decision(export_path: str = WEB_EXPORT_PATH,
                   force: bool | None = None) -> tuple[bool, float | None]:
    """回傳 (是否跳過, 資料齡分鐘)。資料齡不可知時回 (False, None)＝照常執行。

    force=None 時讀環境變數 FAREHUNTER_FORCE（'1'/'true' 視為強制執行）。
    """
    if force is None:
        force = os.environ.get("FAREHUNTER_FORCE", "").lower() not in ("", "0", "false")
    try:
        with open(export_path, encoding="utf-8") as fh:
            generated_at = json.load(fh)["generated_at"]
        ts = datetime.fromisoformat(str(generated_at))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds() / 60
    except Exception as exc:  # noqa: BLE001 — fail-open：guard 絕不擋路
        log.warning("guard 無法判讀資料齡（%s），照常執行", exc)
        return False, None
    if force:
        return False, age
    return (0 <= age < GUARD_MINUTES), age


def _emit_skip_output() -> None:
    """寫入 GITHUB_OUTPUT 讓 workflow 後續步驟（export/commit）一併跳過。

    本地執行無 GITHUB_OUTPUT 時靜默略過。若不通知 workflow，export_web 會用
    「現在」重寫 generated_at——沒抓新價卻重置新鮮度時鐘，違反全站誠實語意。
    """
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    try:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write("skip=true\n")
    except OSError as exc:
        log.warning("無法寫入 GITHUB_OUTPUT（%s）", exc)


def load_config(path: str = "config.yaml") -> dict:
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not cfg or "routes" not in cfg:
        raise ValueError(f"{path} must define a 'routes' list")
    return cfg


def upcoming_months(n: int, today: date | None = None) -> list[str]:
    """Current month plus the next n-1 months, as YYYY-MM strings."""
    today = today or date.today()
    out = []
    y, m = today.year, today.month
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def run(config_path: str = "config.yaml", db_path: str = "prices.db",
        web_export_path: str = WEB_EXPORT_PATH,
        now: datetime | None = None) -> dict:
    skip, age = guard_decision(web_export_path)
    if skip:
        log.info("資料齡 %.0f 分鐘 < %d 分鐘，跳過本輪（防重複 guard；"
                 "手動 Run 勾選 force 或設 FAREHUNTER_FORCE=1 可強制執行）",
                 age, GUARD_MINUTES)
        _emit_skip_output()
        return {"searched": 0, "recorded": 0, "alerts": 0, "errors": 0,
                "empty": 0, "zero_record_routes": [], "skipped": True}
    now = now or datetime.now(timezone.utc)   # production 不傳 → 真實時鐘
    cfg = load_config(config_path)
    defaults = cfg.get("defaults", {})
    client = TravelpayoutsClient()
    store = Store(db_path)
    # empty / zero_record_routes 是零結果可觀測性的核心計數（見 health.py 的
    # 存在理由）：empty 數「API 成功但解析後零筆」的月份次數，
    # zero_record_routes 列出整輪一筆都沒寫進 DB 的航線。兩者都不是錯誤——
    # 薄航線偶爾回空是正常的——但必須可數、可見，否則一條航線可以連續 9 天
    # 全空而 workflow 從頭到尾綠燈。
    summary = {"searched": 0, "recorded": 0, "alerts": 0, "errors": 0,
               "empty": 0, "zero_record_routes": []}
    today_iso = date.today().isoformat()

    try:
        for route in cfg["routes"]:
            origin, dest = route["origin"], route["destination"]
            merged = {**defaults, **route}
            months = upcoming_months(merged.get("months_ahead", 6))
            # 兩組基準，都取自本輪寫入之前的狀態：
            #   route_stats     整條航線 → new_low（史上最低是極值事件）
            #   stats_by_date   單一出發日 → big_drop（反常便宜是相對事件）
            stats = store.route_stats(origin, dest)
            stats_by_date = store.route_stats_by_date(origin, dest)
            route_recorded = 0
            route_empty = 0

            for month in months:
                summary["searched"] += 1
                try:
                    payload = client.search_month(
                        origin, dest, month,
                        currency=merged.get("currency", "twd"),
                        market=merged.get("market"),
                        direct=merged.get("non_stop", False),
                        one_way=merged.get("one_way", False),
                    )
                except TravelpayoutsError as exc:
                    log.error("Search failed %s→%s %s: %s", origin, dest, month, exc)
                    summary["errors"] += 1
                    continue

                offers = parse_offers(payload, origin, dest,
                    max_stops=0 if merged.get("non_stop") else None)
                if not offers:
                    log.info("No cached fares %s→%s %s", origin, dest, month)
                    summary["empty"] += 1
                    route_empty += 1
                    continue

                for offer in offers:
                    if offer.depart_date < today_iso:
                        continue                      # stale cache entry
                    store.record(offer)
                    summary["recorded"] += 1
                    route_recorded += 1

                    # ---- evaluate 之前先解析價格狀態（零 API、只讀既有 DB）----
                    # 系統可能已有同航程的 Google 觀測；VERIFIED 時用權威價去
                    # 判定 deal，不符合就自然不產生 Alert（無 suppression）。
                    pstate = _resolve_state(store, offer, now)
                    eval_offer = offer
                    if pstate.state == price_state.VERIFIED:
                        eval_offer = replace(offer, price=pstate.selected_price)

                    verdict = evaluate(
                        eval_offer, stats,
                        date_stats=stats_by_date.get(offer.depart_date),
                        absolute_threshold=merged.get("absolute_threshold"),
                        drop_pct=merged.get("drop_pct", 25.0),
                        min_history=merged.get("min_history", 30),
                    )
                    if not pstate.eligible_for_alert:
                        continue          # candidate 過舊：不發主動通知
                    csig = price_state.carrier_signature(offer.carriers)
                    if verdict.is_deal and not store.recently_alerted(
                            origin, dest, offer.depart_date,
                            pstate.selected_price,
                            return_date=offer.return_date,
                            carrier_signature=csig,
                            price_status=pstate.state,
                            reference_price=pstate.reference_price):
                        sent = notify(eval_offer, verdict, pstate)
                        if not sent and channels_configured():
                            log.error("通知發送失敗，保留至下一輪重試: %s→%s %s",
                                      origin, dest, offer.depart_date)
                            continue
                        store.record_alert(
                            origin, dest, offer.depart_date,
                            pstate.selected_price, verdict.reason,
                            return_date=offer.return_date,
                            carrier_signature=csig,
                            price_source=pstate.selected_source,
                            price_status=pstate.state,
                            reference_price=pstate.reference_price,
                            reference_observed_at=(
                                pstate.reference_observed_at.isoformat(
                                    timespec="seconds")
                                if pstate.reference_observed_at else None))
                        summary["alerts"] += 1

                time.sleep(merged.get("pause_seconds", 0.6))

            if route_recorded == 0:
                # 本輪這條航線一筆都沒寫進 DB。單次不代表故障（薄航線正常會
                # 有空輪），但必須被數到並升級成 warning——這是 KHH→NGO 能
                # 連續 9 天無人察覺的直接原因。
                summary["zero_record_routes"].append(
                    health.route_key(origin, dest))
                log.warning("航線本輪零記錄 %s→%s（%d/%d 個月回空）",
                            origin, dest, route_empty, len(months))

        if summary["zero_record_routes"]:
            log.error("本輪完全沒有抓到資料的航線 %d/%d 條：%s",
                      len(summary["zero_record_routes"]), len(cfg["routes"]),
                      ", ".join(summary["zero_record_routes"]))

        # 累積視角：本輪零記錄只看當下，觀測齡才能抓到「已經斷線多天」。
        # 用 config 的航線清單而非 DB DISTINCT，否則從未成功抓過的航線
        # 永遠不會出現在健康報告裡。
        health_block = health.build_health_safe(
            store.conn,
            [(r["origin"], r["destination"]) for r in cfg["routes"]],
            now)
        health.log_health(health_block)
        summary["health"] = None if health_block is None else {
            "counts": health_block["counts"],
            "degraded": health_block["degraded"]}
    finally:
        store.close()

    log.info("Run summary: %s", summary)
    return summary
