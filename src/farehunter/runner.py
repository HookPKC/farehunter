"""Runner: load config, iterate months per route, store, evaluate, notify."""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from .travelpayouts import TravelpayoutsClient, parse_offers, TravelpayoutsError
from .storage import Store
from .analyzer import evaluate
from .notify import notify, channels_configured

log = logging.getLogger(__name__)

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
        web_export_path: str = WEB_EXPORT_PATH) -> dict:
    skip, age = guard_decision(web_export_path)
    if skip:
        log.info("資料齡 %.0f 分鐘 < %d 分鐘，跳過本輪（防重複 guard；"
                 "手動 Run 勾選 force 或設 FAREHUNTER_FORCE=1 可強制執行）",
                 age, GUARD_MINUTES)
        _emit_skip_output()
        return {"searched": 0, "recorded": 0, "alerts": 0, "errors": 0,
                "skipped": True}
    cfg = load_config(config_path)
    defaults = cfg.get("defaults", {})
    client = TravelpayoutsClient()
    store = Store(db_path)
    summary = {"searched": 0, "recorded": 0, "alerts": 0, "errors": 0}
    today_iso = date.today().isoformat()

    try:
        for route in cfg["routes"]:
            origin, dest = route["origin"], route["destination"]
            merged = {**defaults, **route}
            months = upcoming_months(merged.get("months_ahead", 6))
            stats = store.route_stats(origin, dest)   # stats BEFORE this run

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
                    continue

                for offer in offers:
                    if offer.depart_date < today_iso:
                        continue                      # stale cache entry
                    store.record(offer)
                    summary["recorded"] += 1

                    verdict = evaluate(
                        offer, stats,
                        absolute_threshold=merged.get("absolute_threshold"),
                        drop_pct=merged.get("drop_pct", 25.0),
                        min_history=merged.get("min_history", 30),
                    )
                    if verdict.is_deal and not store.recently_alerted(
                            origin, dest, offer.depart_date, offer.price):
                        sent = notify(offer, verdict)
                        if not sent and channels_configured():
                            log.error("通知發送失敗，保留至下一輪重試: %s→%s %s",
                                      origin, dest, offer.depart_date)
                            continue
                        store.record_alert(origin, dest, offer.depart_date,
                                           offer.price, verdict.reason)
                        summary["alerts"] += 1

                time.sleep(merged.get("pause_seconds", 0.6))
    finally:
        store.close()

    log.info("Run summary: %s", summary)
    return summary
