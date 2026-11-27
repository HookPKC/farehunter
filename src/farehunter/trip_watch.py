"""使用者指定行程的專屬追蹤（2026-11-27 使用者決定）：每 12 小時查一次 Google 直飛實價，有變化就 Telegram。

**為什麼有這支**：平常的巡檢只看 config 的 8 條線、而且偏重近期日子；使用者 3 月初要去熊本（虎航高雄⇄熊本
週二、五飛，不在監控航線裡），想知道已經看好的那一趟會不會更便宜、或促銷價（回程 2,899）賣完開始漲。

**做什麼**：config.yaml 的 `watch_trips` 每一趟，距離上次**成功**查詢超過 WATCH_EVERY_HOURS 小時就用
Bright Data 抓同一個 Google Flights 頁面（brightdata_flights 的 fetch／parse／select，價格語意相同：
Google 觀測價精確），取直飛最低價，跟上次通知的價格比：
- 第一次查到 → 「開始追蹤」，讓使用者知道有在跑；
- 便宜 DROP_PCT% 以上 → 「變便宜了」；
- 貴 RISE_PCT% 以上 → 「漲價了」（促銷價可能賣完）；
- 頁面正常但查不到直飛 → 「查不到直飛」（可能賣完）。
只發 Telegram 給擁有者（不佔 LINE 額度），半夜靜音送達。出發日到了就不再查。

**不寫 observations**：看板的航線清單取自 observations 的所有航線，熊本會變成第 9 條線；結果另記在
trip_watch_checks。Bright Data 的用量照樣記進 api_usage（provider='brightdata'），配速會算到。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from urllib.parse import quote

from . import brightdata_flights as bd
from .notify import TW, quiet_hours, send_telegram

log = logging.getLogger(__name__)

WATCH_EVERY_HOURS = 12
DROP_PCT = 3.0
RISE_PCT = 5.0
WEEKDAYS = "一二三四五六日"
CITY = {"KHH": "高雄", "TPE": "台北", "KMJ": "熊本", "FUK": "福岡", "NRT": "東京成田", "KIX": "大阪關西",
        "OKA": "沖繩", "NGO": "名古屋", "CTS": "札幌"}


def trips(cfg: dict) -> list[dict]:
    out = []
    for t in cfg.get("watch_trips") or []:
        try:
            out.append({"origin": str(t["origin"]).upper(), "destination": str(t["destination"]).upper(),
                        "depart": str(t["depart"]), "return": str(t["return"]),
                        "note": str(t.get("note") or "")})
        except (KeyError, TypeError):
            log.warning("watch_trips 有一筆格式不對，略過：%s", t)
    return out


def _md(d: str) -> str:
    x = date.fromisoformat(d)
    return f"{x.month}/{x.day}（{WEEKDAYS[x.weekday()]}）"


def _link(t: dict) -> str:
    q = f"Flights from {t['origin']} to {t['destination']} on {t['depart']} through {t['return']}"
    return "https://www.google.com/travel/flights?q=" + quote(q)


def _title(t: dict) -> str:
    nights = (date.fromisoformat(t["return"]) - date.fromisoformat(t["depart"])).days
    o, d = CITY.get(t["origin"], t["origin"]), CITY.get(t["destination"], t["destination"])
    return f"{o}⇄{d} {_md(t['depart'])}–{_md(t['return'])} {nights} 晚"


def message(t: dict, price: float | None, carriers: str | None, last: float | None,
            now: datetime) -> str | None:
    """要發的訊息；不用發回 None。last＝上次通知的價格（None＝還沒通知過，或上次通知的是查不到直飛）。"""
    head = f"🔎 追蹤中的行程：{_title(t)}" + (f"（{t['note']}）" if t["note"] else "")
    stamp = now.astimezone(TW).strftime("%m/%d %H:%M")
    if price is None:
        if last is None:
            return None
        body = f"Google 上查不到直飛（上次通知 {last:,.0f}）——可能賣完了，或暫時沒有座位"
    else:
        now_line = f"Google 直飛實價：{price:,.0f} TWD（{carriers or '—'}）"
        if last is None:
            body = f"開始追蹤。{now_line}\n之後便宜 {DROP_PCT:.0f}% 以上、或貴 {RISE_PCT:.0f}% 以上會再通知你"
        elif price <= last * (1 - DROP_PCT / 100):
            body = f"變便宜了！{now_line}\n比上次通知的 {last:,.0f} 便宜 {last - price:,.0f}（−{(1 - price / last) * 100:.0f}%）"
        elif price >= last * (1 + RISE_PCT / 100):
            body = (f"漲價了。{now_line}\n比上次通知的 {last:,.0f} 貴 {price - last:,.0f}"
                    f"（+{(price / last - 1) * 100:.0f}%），便宜的座位可能賣完了")
        else:
            return None
    return f"{head}\n{body}\n查詢於 {stamp} 台灣時間\n立即比價: {_link(t)}"


def run(store, cfg: dict, now: datetime, *, fetcher=None, sender=None) -> dict:
    """查到期的追蹤行程。任何失敗只記 log，不影響抓價、警報、看板。"""
    fetcher = fetcher or bd.fetch
    sender = sender or (lambda text: send_telegram(text, quiet=quiet_hours(now)))
    stats = {"trips": 0, "checked": 0, "notified": 0, "errors": 0}
    today = now.astimezone(TW).date().isoformat()
    # 每小時的 monitor 開始時間差幾十秒：少算半小時，免得 12 小時的間隔變成 13 小時
    since = (now - timedelta(hours=WATCH_EVERY_HOURS - 0.5)).isoformat(timespec="seconds")
    for t in trips(cfg):
        if t["depart"] <= today:
            continue
        stats["trips"] += 1
        key = (t["origin"], t["destination"], t["depart"], t["return"])
        if store.trip_watch_checked_since(*key, since):
            continue
        ok = False
        try:
            itineraries = bd.parse(fetcher(*key))
            if not itineraries:
                raise bd.BrightDataError("page fetched but no itineraries parsed")
            ok = True
            offers, _ = bd.select(*key, itineraries)
            best = min((o for o in offers if o.fare_class == "any"), key=lambda o: o.price, default=None)
            price, carriers = (best.price, best.carriers) if best else (None, None)
            store.record_trip_watch_check(*key, now.isoformat(timespec="seconds"), price, carriers)
            stats["checked"] += 1
            last = store.trip_watch_last_notified(*key)
            text = message(t, price, carriers, last[0] if last else None, now)
            if text and sender(text):
                store.record_trip_watch_notice(*key, now.isoformat(timespec="seconds"), price)
                stats["notified"] += 1
            log.info("行程追蹤 %s：%s", _title(t), "查不到直飛" if price is None else f"{price:,.0f}")
        except bd.BrightDataError as exc:
            stats["errors"] += 1
            log.warning("行程追蹤 %s 查詢失敗，下一輪再試：%s", _title(t), exc)
        except Exception:                                  # noqa: BLE001
            stats["errors"] += 1
            log.exception("行程追蹤 %s 意外失敗", _title(t))
        finally:
            store.record_api_usage(bd.PROVIDER, now.date().isoformat(), ok=ok)
    return stats
