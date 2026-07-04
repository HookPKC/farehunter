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

log = logging.getLogger(__name__)


def format_alert(offer: Offer, verdict: Verdict) -> str:
    rt = f" ↩ {offer.return_date}" if offer.return_date else ""
    stops = "直飛" if offer.stops == 0 else f"{offer.stops} 轉"
    if offer.link:
        booking = f"訂票: {offer.link}"
    else:
        booking = (f"查價: https://www.google.com/travel/flights?q=Flights%20from%20"
                   f"{offer.origin}%20to%20{offer.destination}%20on%20{offer.depart_date}")
    return (
        f"✈️ 低價警報 {offer.origin}→{offer.destination}\n"
        f"日期: {offer.depart_date}{rt}\n"
        f"價格: {offer.price:,.0f} {offer.currency}（{offer.carriers}, {stops}）\n"
        f"原因: {verdict.detail}\n"
        f"{booking}"
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


def notify(offer: Offer, verdict: Verdict) -> list[str]:
    """Send to all configured channels; return the list that succeeded."""
    text = format_alert(offer, verdict)
    sent = []
    if send_telegram(text):
        sent.append("telegram")
    if send_line(text):
        sent.append("line")
    if not sent:
        log.info("No notification channel configured; alert printed only:\n%s", text)
        print(text)
    return sent
