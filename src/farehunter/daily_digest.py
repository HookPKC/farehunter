"""Telegram 每日行情摘要（2026-11-21 使用者決定）：每天早上一則，只發給擁有者。

警報只在「夠便宜」時才響；摘要讓擁有者每天看得到行情，不用打開看板。試排時就看到警報漏掉的
價格：KHH-NRT 巡檢查到 7,427（門檻 7,500），但 KHH-NRT 沒開 sweep_alerts，不會發警報。

- **何時**：台灣時間 SEND_FROM_HOUR～SEND_UNTIL_HOUR 之間的第一輪（通常是 08:17 那輪）；那輪沒送成，
  下一輪補，過了中午就不補。每天只發一次（digest_sends 表）。
- **發給誰**：只走 Telegram，不用 LINE、不扣任何額度。沒設 Telegram 就不發。
- **內容**：各線近 LOOKBACK_HOURS 小時查到的最低 Google 直飛實價（住 2–10 晚、出發日至少
  MIN_LEAD_DAYS 天後），跟上週同一段時間比；低於門檻的標 ✅、排前面。另列昨天的警報數、LINE 剩餘。
- **傳統航空**（2026-11-22 起）：另列各線傳統航空（非廉航）的最低直飛實價，照價格排；沒有傳統
  航空直飛的線（CTS、NGO）不列。
- **價格語意**：只列 Google 實價（精確數字）。不列快取估價（要寫「約」）、也不列虎航官網價
  （單程未稅，最低價常訂不到，見 ADR 0005）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from . import notify
from .models import MAX_STAY_NIGHTS, MIN_STAY_NIGHTS

log = logging.getLogger(__name__)

SEND_FROM_HOUR, SEND_UNTIL_HOUR = 8, 12      # 台灣時間 [8, 12)
LOOKBACK_HOURS = 48
WEEK_AGO_DAYS = (7, 9)                      # 「上週」＝ 7～9 天前那 48 小時
MIN_LEAD_DAYS = 2
#: 同看板 docs/index.html 的 AIRLINES（只列我們航線會出現的）；對不到就顯示代碼
AIRLINE_NAMES = {
    "IT": "台灣虎航", "TR": "酷航", "MM": "樂桃", "GK": "捷星日本", "AK": "亞洲航空",
    "D7": "全亞洲航空", "FD": "泰亞航", "UO": "香港快運", "HB": "大灣區航空", "CX": "國泰航空",
    "CI": "中華航空", "BR": "長榮航空", "JX": "星宇航空", "JL": "日本航空", "NH": "全日空",
    "BC": "天馬航空", "SL": "泰國獅航", "VZ": "泰越捷", "UA": "聯合航空",
}
WEEKDAYS = "一二三四五六日"


def _tw(now: datetime) -> datetime:
    return now.astimezone(notify.TW)


def due(store, now: datetime) -> bool:
    t = _tw(now)
    return SEND_FROM_HOUR <= t.hour < SEND_UNTIL_HOUR and not store.digest_sent(t.date().isoformat())


def _airlines(carriers: str | None) -> str:
    codes = [c.strip() for c in (carriers or "").split(",") if c.strip()]
    return "、".join(AIRLINE_NAMES.get(c, c) for c in codes)


def _md(day: str) -> str:
    return f"{int(day[5:7])}/{day[8:10]}"


def _cheapest(store, origin: str, dest: str, earliest: str, since: datetime, until: datetime,
              fare_class: str = "any"):
    """這段時間寫入的最低 Google 直飛實價 (price, depart, return, carriers)；沒有就 None。
    fare_class：'any'（全場最低）或 'full'（傳統航空最低）。"""
    return store.conn.execute(
        f"""SELECT price, depart_date, return_date, carriers FROM observations
            WHERE origin=? AND destination=? AND source='google' AND fare_class=?
              AND stops=0 AND depart_date >= ? AND return_date IS NOT NULL AND return_date != ''
              AND julianday(return_date) - julianday(depart_date)
                  BETWEEN {MIN_STAY_NIGHTS} AND {MAX_STAY_NIGHTS}
              AND julianday(observed_at) >= julianday(?) AND julianday(observed_at) < julianday(?)
            ORDER BY price, depart_date LIMIT 1""",
        (origin, dest, fare_class, earliest, since.isoformat(timespec="seconds"),
         until.isoformat(timespec="seconds"))).fetchone()


def route_lows(store, routes: list[dict], now: datetime) -> list[dict]:
    """各線的摘要列；排序：低於門檻的在前，其餘照「價格／門檻」由低到高，沒有實價的放最後。"""
    earliest = (_tw(now).date() + timedelta(days=MIN_LEAD_DAYS)).isoformat()
    since = now - timedelta(hours=LOOKBACK_HOURS)
    w_since, w_until = now - timedelta(days=WEEK_AGO_DAYS[1]), now - timedelta(days=WEEK_AGO_DAYS[0])
    out = []
    for r in routes:
        o, d, thr = r["origin"], r["destination"], r.get("absolute_threshold")
        cur = _cheapest(store, o, d, earliest, since, now)
        prev = _cheapest(store, o, d, earliest, w_since, w_until)
        out.append({"route": f"{o}-{d}", "threshold": thr,
                    "price": cur[0] if cur else None,
                    "depart": cur[1] if cur else None, "return": cur[2] if cur else None,
                    "carriers": cur[3] if cur else None,
                    "week_ago": prev[0] if prev else None})

    def key(x):
        if x["price"] is None:
            return (2, 0.0)
        ratio = x["price"] / x["threshold"] if x["threshold"] else float("inf")
        return (0 if ratio <= 1 else 1, ratio if x["threshold"] else x["price"])
    return sorted(out, key=key)


def full_lows(store, routes: list[dict], now: datetime) -> list[dict]:
    """各線近 LOOKBACK_HOURS 小時的傳統航空最低直飛實價；沒有傳統航空資料的線不列，照價格排。"""
    earliest = (_tw(now).date() + timedelta(days=MIN_LEAD_DAYS)).isoformat()
    since = now - timedelta(hours=LOOKBACK_HOURS)
    w_since, w_until = now - timedelta(days=WEEK_AGO_DAYS[1]), now - timedelta(days=WEEK_AGO_DAYS[0])
    out = []
    for r in routes:
        o, d = r["origin"], r["destination"]
        cur = _cheapest(store, o, d, earliest, since, now, "full")
        if not cur:
            continue
        prev = _cheapest(store, o, d, earliest, w_since, w_until, "full")
        out.append({"route": f"{o}-{d}", "price": cur[0], "depart": cur[1], "return": cur[2],
                    "carriers": cur[3], "week_ago": prev[0] if prev else None})
    return sorted(out, key=lambda x: x["price"])


def _line(x: dict, mark: str = "") -> str:
    who = _airlines(x["carriers"])
    line = (f"{mark}{x['route']} {x['price']:,.0f}　{_md(x['depart'])}–{_md(x['return'])}"
            + (f" {who}" if who else ""))
    if x["week_ago"]:
        pct = round((x["price"] / x["week_ago"] - 1) * 100)
        trend = "持平" if pct == 0 else f"{'↓' if pct < 0 else '↑'}{abs(pct)}%"
        line += f"（上週 {x['week_ago']:,.0f}，{trend}）"
    return line


def _alerts_yesterday(store, now: datetime) -> int:
    start = _tw(now).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    end = start + timedelta(days=1)
    return store.conn.execute(
        """SELECT COUNT(*) FROM alerts
           WHERE julianday(sent_at) >= julianday(?) AND julianday(sent_at) < julianday(?)""",
        (start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"))).fetchone()[0]


def _line_left(store, now: datetime) -> int | None:
    """本月最近一次 LINE 發送後回報的剩餘則數；本月沒發過或查不到時 None。"""
    month = _tw(now).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    row = store.conn.execute(
        """SELECT quota_left FROM line_sends
           WHERE quota_left IS NOT NULL AND julianday(sent_at) >= julianday(?)
           ORDER BY sent_at DESC, rowid DESC LIMIT 1""",
        (month.isoformat(timespec="seconds"),)).fetchone()
    return None if row is None else int(row[0])


def build(store, routes: list[dict], now: datetime) -> str:
    t = _tw(now)
    lines = [f"☀️ FareHunter 每日行情 {t.month}/{t.day:02d}（{WEEKDAYS[t.weekday()]}）",
             f"近 {LOOKBACK_HOURS} 小時查到的最低直飛實價（Google，住 {MIN_STAY_NIGHTS}–{MAX_STAY_NIGHTS} 晚）"]
    for x in route_lows(store, routes, now):
        if x["price"] is None:
            lines.append(f"{x['route']}：近 {LOOKBACK_HOURS} 小時沒有直飛實價")
            continue
        mark = "✅ " if x["threshold"] and x["price"] <= x["threshold"] else ""
        lines.append(_line(x, mark))
    full = full_lows(store, routes, now)
    if full:
        lines.append("傳統航空最低（華航、長榮、星宇等）")
        lines.extend(_line(x) for x in full)
    tail = ["✅ 低於門檻", f"昨天警報 {_alerts_yesterday(store, now)} 則"]
    left = _line_left(store, now)
    if left is not None:
        tail.append(f"LINE 本月約剩 {left} 則")
    lines.append("｜".join(tail))
    return "\n".join(lines)


def maybe_send(store, routes: list[dict], now: datetime) -> dict:
    """該發就組好發出；回傳 {"sent": bool, "skipped": 原因}。送失敗只記 log，下一輪再試。"""
    if not notify.telegram_configured():
        return {"sent": False, "skipped": "no_telegram"}
    if not due(store, now):
        return {"sent": False, "skipped": "not_due"}
    text = build(store, routes, now)
    if not notify.send_telegram(text, quiet=notify.quiet_hours(now)):
        log.warning("每日摘要沒送出，下一輪再試（最晚到台灣時間 %d 點）", SEND_UNTIL_HOUR)
        return {"sent": False, "skipped": "send_failed"}
    store.record_digest(_tw(now).date().isoformat(), now.isoformat(timespec="seconds"))
    log.info("每日摘要已發到 Telegram")
    return {"sent": True, "skipped": ""}
