"""Notification channels: Telegram Bot API and LINE Messaging API (push).

Both are optional — a channel is active only when its env vars are set:
  Telegram: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  LINE:     LINE_CHANNEL_ACCESS_TOKEN, LINE_USER_ID
"""
from __future__ import annotations

import os
import logging

import requests

from .models import Offer
from .analyzer import Verdict
from .normalize import CACHE_SOURCES

log = logging.getLogger(__name__)


WEEKDAYS = "一二三四五六日"

# 通知價格語意（與看板統一原則一致）：
# 快取/估價來源 → 「約 NT$X」百位四捨五入；google 觀測價 → 精確數字。
# 快取來源清單由 normalize 提供單一定義——通知、網站、評分三處若各自維護
# 一份，新增資料源時漏改任一處就會出現「網站說已驗證、通知說是估計價」。
_SOURCE_LABEL = {
    "aviasales": "Aviasales 快取估價",
    "travelpayouts": "Travelpayouts 快取估價",
    "google": "Google Flights 觀測價",
}


def _tw_now() -> str:
    """Current time formatted for Taiwan (detection ≈ send time, same run)."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
    return now.strftime("%m/%d %H:%M") + " 台灣時間"


def _tw_stamp(ts) -> str:
    """把 datetime 或 ISO 字串轉成台灣時間 MM/DD HH:MM。"""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    if ts is None:
        return ""
    if isinstance(ts, str):
        try:
            ts = _dt.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return ts[:16]
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_tz.utc)
    return (ts.astimezone(_tz(_td(hours=8)))).strftime("%m/%d %H:%M")


def format_alert(offer: Offer, verdict: Verdict, state=None) -> str:
    """組出 LINE／Telegram 文案。

    state 為 price_state.PriceState（可為 None，代表沿用舊的未驗證語意）。
    三種狀態的標題與來源行必須讓使用者一眼分辨:
      VERIFIED   已驗證低價 —— 使用權威價，標示驗證時間
      CONFLICT   同航程價格有落差 —— 同列兩個價格與兩個觀測時間，不宣稱誰對
      UNVERIFIED 疑似低價 —— 明確標示快取估價、非即時成交報價
    """
    from datetime import date as _d
    dep = _d.fromisoformat(offer.depart_date)
    day = f"{offer.depart_date} 週{WEEKDAYS[dep.weekday()]}"
    if offer.return_date:
        nights = (_d.fromisoformat(offer.return_date) - dep).days
        day += f" ↩ {offer.return_date}（{nights} 天來回）"
    who = offer.carriers or "多家航空（點入查看）"
    q = f"Flights from {offer.origin} to {offer.destination} on {offer.depart_date}"
    if offer.return_date:
        q += f" through {offer.return_date}"
    from urllib.parse import quote
    booking = "https://www.google.com/travel/flights?q=" + quote(q)
    route = f"{offer.origin}⇄{offer.destination}"

    st = getattr(state, "state", None)

    if st == "verified":
        return (
            f"✈️ 已驗證低價 {route}・直飛\n"
            f"日期: {day}\n"
            f"Google 實價: {state.selected_price:,.0f} {offer.currency}（{who}）\n"
            f"驗證於 {_tw_stamp(state.selected_observed_at)} 台灣時間\n"
            f"觸發: {verdict.detail}\n"
            f"立即比價: {booking}"
        )

    if st == "conflict":
        return (
            f"⚠️ 同航程價格有落差 {route}・直飛\n"
            f"日期: {day}\n"
            f"Aviasales 快取: 約 {round(state.selected_price / 100) * 100:,.0f} "
            f"{offer.currency}（{_tw_stamp(state.selected_observed_at)}）\n"
            f"Google 近期參考: {state.reference_price:,.0f} {offer.currency}"
            f"（{_tw_stamp(state.reference_observed_at)}）\n"
            f"價差: {abs(state.reference_price - state.selected_price):,.0f}"
            f"（{state.conflict_percentage:.0f}%）\n"
            f"目前售價尚未確認，請立即比價:\n{booking}"
        )

    # UNVERIFIED（或未傳 state 時的預設）：沿用既有的誠實快取語意
    is_cache = (offer.source or "").lower() in CACHE_SOURCES
    if is_cache:
        shown = f"約 {round(offer.price / 100) * 100:,.0f}"
        kind = "快取估價"
    else:
        shown = f"{offer.price:,.0f}"
        kind = "觀測價"
    src = _SOURCE_LABEL.get((offer.source or "").lower(), offer.source or "未知來源")

    return (
        f"✈️ 疑似低價 {route}・直飛・尚未經 Google 驗證\n"
        f"日期: {day}\n"
        f"觀測到: {shown} {offer.currency}（{who}, 直飛）\n"
        f"來源: {src}・偵測於 {_tw_now()}\n"
        f"觸發: {verdict.detail}\n"
        f"比價: {booking}\n"
        f"（此為監控時觀測到的{kind}，非即時報價；"
        f"實際價格以 Google Flights／航空公司為準，低價可能已消失）"
    )


def send_telegram(text: str, session: requests.Session | None = None) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    s = session or requests
    resp = s.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=30,
    )
    if resp.status_code != 200:
        log.error("Telegram send failed %s: %s", resp.status_code, resp.text[:300])
        return False
    return True


def send_line(text: str, session: requests.Session | None = None) -> bool:
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    user_id = os.environ.get("LINE_USER_ID")
    if not token or not user_id:
        return False
    s = session or requests
    resp = s.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {token}"},
        json={"to": user_id, "messages": [{"type": "text", "text": text[:5000]}]},
        timeout=30,
    )
    if resp.status_code != 200:
        log.error("LINE send failed %s: %s", resp.status_code, resp.text[:300])
        return False
    return True


def channels_configured() -> bool:
    return bool(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
                or os.environ.get("TELEGRAM_BOT_TOKEN"))


def notify(offer: Offer, verdict: Verdict, state=None) -> list[str]:
    """Send to all configured channels; return the list that succeeded."""
    text = format_alert(offer, verdict, state)
    sent = []
    if send_telegram(text):
        sent.append("telegram")
    if send_line(text):
        sent.append("line")
    if not sent:
        log.info("No notification channel configured; alert printed only:\n%s", text)
        print(text)
    return sent
