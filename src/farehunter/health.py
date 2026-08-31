"""Route health:把「某條航線已經悄悄停止產出觀測」升級成人看得到的訊號。

零 API：只讀既有 observations。時間基準一律由呼叫端注入——不使用 SQLite 的
date('now') / datetime('now')，沿用 967029c 建立的注入時鐘慣例，讓測試不必
綁真實日期。

存在理由（2026-08-07 唯讀盤點）：KHH→NGO 自 2026-07-29 起連續 9 天零觀測，
其間 monitor 每小時全綠、每輪照常 commit，卻沒有任何一處把「一條 config 裡
的航線完全沒抓到東西」變成可見訊號——runner 的 empty 路徑只寫 log.info，
summary 也只在拋出 TravelpayoutsError 時才計 errors。同一個盲點讓 SearchApi
額度耗盡（gcal/longrange 全數 429、寫 0 筆）潛伏了 10 天。

刻意不新增資料表：偵測完全建立在既有 observations.observed_at 之上，因此本
commit 可獨立 revert，不留 schema 殘跡。

刻意不改 exit code：monitor.yml 的 commit 步驟預設 `if: success()`，讓 Run
monitor 這步變紅會連帶擋掉 prices.db 的 commit，把「一條航線沒資料」升級成
「全部航線都不寫入」。可見性因此改走 docs/data.json 的 health 區塊——五個
writer 都 `git add docs/data.json`，所以每輪 commit 的 diff 都會顯示狀態變化。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# 門檻。monitor 每小時一班，健康航線的觀測齡正常在 1 小時內；薄航線
# （KHH-CTS 全期 463 列，對比 KHH-NRT 42033 列）每日 1–47 筆之間跳動，
# 所以 stale 取 24 小時避免把正常抖動當故障，dead 取 72 小時代表
# 「連最寬鬆的 route card primary 48 小時視窗都已經救不回來」。
STALE_AFTER_HOURS = 24.0
DEAD_AFTER_HOURS = 72.0

OK = "ok"
STALE = "stale"
DEAD = "dead"
NEVER = "never_observed"

DEGRADED = (STALE, DEAD, NEVER)


def route_key(origin: str, destination: str) -> str:
    return f"{origin}-{destination}"


def parse_ts(value) -> datetime | None:
    """把 observations.observed_at 解析成 aware datetime。無法解析回 None。

    fail-open：健康檢查絕不能自己成為新的停擺原因，所以解析失敗一律回 None
    （呈現為 never_observed），不拋例外。
    """
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def last_observed_at(conn, origin: str, destination: str) -> str | None:
    """該航線最新一筆觀測的 observed_at（不分來源、不分 fare_class）。

    刻意不濾 source：問的是「這條航線還有沒有任何管道在供料」，
    不是「主價夠不夠新鮮」（後者是 current_price 的職責）。
    """
    try:
        row = conn.execute(
            "SELECT MAX(observed_at) FROM observations "
            "WHERE origin=? AND destination=?",
            (origin, destination)).fetchone()
    except Exception as exc:                      # noqa: BLE001 — fail-open
        log.warning("route health 查詢失敗 %s→%s（%s）", origin, destination, exc)
        return None
    return row[0] if row else None


def classify(age_hours: float | None,
             stale_after_hours: float = STALE_AFTER_HOURS,
             dead_after_hours: float = DEAD_AFTER_HOURS) -> str:
    """依觀測齡分級。age_hours 為 None（從未觀測／無法解析）→ never_observed。

    未來時間戳（負齡，時鐘偏移）視為 ok：健康檢查不負責偵測時鐘問題。
    """
    if age_hours is None:
        return NEVER
    if age_hours >= dead_after_hours:
        return DEAD
    if age_hours >= stale_after_hours:
        return STALE
    return OK


def build_health(conn, routes, now: datetime,
                 stale_after_hours: float = STALE_AFTER_HOURS,
                 dead_after_hours: float = DEAD_AFTER_HOURS) -> dict:
    """為每條 routes 內的航線算觀測齡與狀態。

    routes：(origin, destination) 的可迭代物。刻意由呼叫端傳入而非從
    observations DISTINCT 推導——真相是 config.yaml 列了哪些航線該被監控，
    從觀測反推會讓「從未成功抓過的航線」永遠不出現在健康報告裡。
    """
    entries = []
    for origin, destination in routes:
        last = last_observed_at(conn, origin, destination)
        ts = parse_ts(last)
        age = None if ts is None else round((now - ts).total_seconds() / 3600, 1)
        entries.append({
            "route": route_key(origin, destination),
            "origin": origin,
            "destination": destination,
            "last_observed_at": last,
            "age_hours": age,
            "status": classify(age, stale_after_hours, dead_after_hours),
        })
    counts = {OK: 0, STALE: 0, DEAD: 0, NEVER: 0}
    for e in entries:
        counts[e["status"]] += 1
    return {
        "checked_at": now.isoformat(timespec="seconds"),
        "thresholds": {"stale_after_hours": stale_after_hours,
                       "dead_after_hours": dead_after_hours},
        "counts": counts,
        "degraded": [e["route"] for e in entries if e["status"] in DEGRADED],
        "routes": entries,
    }


def build_health_safe(conn, routes, now: datetime, **kwargs) -> dict | None:
    """fail-open 包裝：健康檢查絕不能自己成為新的停擺原因。回傳 None 表不可用。

    與 runner 的 guard 同一條鐵律（「只能跳過，絕不能擋路」）：monitor.yml 的
    commit 步驟預設 `if: success()`，所以任何從 export/runner 拋出的例外都會
    連帶擋掉 prices.db 的 commit——等於為了回報「一條航線斷線」而讓八條航線
    全部不寫入。寧可失去這一輪的健康報告。

    刻意回 None 而非「全部健康」的空殼：後者會被讀成「沒有異常」，
    違反全站「絕不 silent fallback」的語意約束。消費端必須顯式處理 None。
    """
    try:
        return build_health(conn, routes, now, **kwargs)
    except Exception as exc:            # noqa: BLE001 — fail-open
        log.warning("route health 計算失敗，本輪略過健康報告（%s）", exc)
        return None


def log_health(health: dict | None, logger: logging.Logger | None = None) -> None:
    """把健康狀態寫進 job log：dead/never → error，stale → warning。

    同樣 fail-open：格式化錯誤不得擋掉整輪資料寫入。
    """
    logger = logger or log
    if not health:
        return
    try:
        _log_health(health, logger)
    except Exception as exc:            # noqa: BLE001 — fail-open
        logger.warning("route health 記錄失敗（%s）", exc)


def _log_health(health: dict, logger: logging.Logger) -> None:
    for e in health["routes"]:
        if e["status"] == DEAD:
            logger.error("航線斷線 %s：最後觀測 %s（%.1f 小時前，門檻 %.0f）",
                         e["route"], e["last_observed_at"], e["age_hours"],
                         health["thresholds"]["dead_after_hours"])
        elif e["status"] == NEVER:
            logger.error("航線從未有可解析的觀測 %s（config 有列，DB 無資料）",
                         e["route"])
        elif e["status"] == STALE:
            logger.warning("航線觀測過舊 %s：最後觀測 %s（%.1f 小時前，門檻 %.0f）",
                           e["route"], e["last_observed_at"], e["age_hours"],
                           health["thresholds"]["stale_after_hours"])
    if health["degraded"]:
        logger.error("Route health 異常 %d/%d 條：%s",
                     len(health["degraded"]), len(health["routes"]),
                     ", ".join(health["degraded"]))
    else:
        logger.info("Route health 全部 %d 條航線觀測新鮮", len(health["routes"]))


# ---- sweep 的結束碼：讓「全軍覆沒」不再靜默 ----------------------------------

#: 各支 sweep 用來表示「有寫進 DB」的欄位名（不同 sweep 命名不同）。
_RECORDED_KEYS = ("recorded", "dates_covered", "verified")


def sweep_failed_entirely(summary: dict) -> bool:
    """True 表示這一輪 sweep 每一次查詢都失敗、而且什麼都沒記錄。

    存在的理由是一次實際發生的靜默失敗：SearchApi 的月額度用完之後，
    gcal_sweep 的 16 次查詢全部回 HTTP 429，summary 是
    {'searched': 16, 'recorded': 0, 'errors': 16}——但進入點無條件 exit 0，
    於是 workflow 每週照樣綠燈、照樣 commit，而 Google 即時價（唯一的基準
    真相來源）整整五週沒有進帳，沒有任何地方看得出來。

    判斷刻意收得很緊，只認「全軍覆沒」：
      - searched == 0        → 沒查就沒失敗（例如當週沒有掃描窗）
      - errors < searched    → 部分成功，屬正常波動
      - 有任何記錄           → 有進帳就不算壞掉
    薄航線查得到但解析後回空是 errors=0 / recorded=0，不會被誤判——那是
    正常現象，PLAYBOOK 也明訂它該被數但不該當成故障。
    """
    searched = int(summary.get("searched") or 0)
    if searched <= 0:
        return False
    if int(summary.get("errors") or 0) < searched:
        return False
    return not any(int(summary.get(k) or 0) > 0 for k in _RECORDED_KEYS)


def sweep_exit_code(summary: dict, name: str = "sweep",
                    logger: logging.Logger | None = None) -> int:
    """回傳給 SystemExit 用的結束碼；全軍覆沒時回 1 並記一筆 error。"""
    if not sweep_failed_entirely(summary):
        return 0
    (logger or log).error(
        "%s 全軍覆沒：%d 次查詢全部失敗且零記錄（summary=%s）。"
        "workflow 將以非零結束碼失敗，以免這種狀況再次靜默五週。",
        name, int(summary.get("searched") or 0), summary)
    return 1
