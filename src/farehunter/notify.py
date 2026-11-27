"""Notification channels: Telegram Bot API and LINE Messaging API.

Both are optional — a channel is active only when its env vars are set:
  Telegram: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  LINE:     LINE_CHANNEL_ACCESS_TOKEN, LINE_USER_ID（push 給這一個人）；
            LINE_BROADCAST=1 時改成廣播給官方帳號的所有好友（見 send_line）
"""
from __future__ import annotations

import calendar
import os
import logging
from datetime import datetime, timedelta, timezone

import requests

from .models import Offer
from .analyzer import Verdict
from .normalize import CACHE_SOURCES
from .storage import meaningfully_cheaper

log = logging.getLogger(__name__)

#: （連線, 讀取）秒數。原本 30 秒一個數字，requests 會連線、讀取各等 30 秒：管道掛掉時
#: 每則通知最多卡約 1 分鐘，一輪有好幾則警報就拖住整輪。兩邊正常都在 1–2 秒內回應；
#: 送不出去的會留到下一輪重試（見 _log_transport_error），不必在這裡久等。
SEND_TIMEOUT = (5, 20)

#: 台灣時間這段期間送出的通知改成靜音送達：訊息照常到，手機不響（LINE
#: notificationDisabled、Telegram disable_notification）。近 30 天有 12% 的警報落在半夜。
TW = timezone(timedelta(hours=8))
QUIET_START_HOUR, QUIET_END_HOUR = 23, 7


def quiet_hours(now: datetime) -> bool:
    h = now.astimezone(TW).hour
    return h >= QUIET_START_HOUR or h < QUIET_END_HOUR


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


def _log_transport_error(channel: str, exc: Exception, token: str) -> None:
    """逾時／斷線只記 log、當成「沒送出」，不往外丟。

    呼叫端（runner、cheap_day_notify）本來就把 False 當成「保留到下一輪重試」；
    讓例外冒出去的話，monitor 會在迴圈中途死掉、Export／Commit 都不跑，Verify
    Airlines 的推播步驟也排在 Commit 之前——一次推播逾時就丟掉整輪資料。
    例外訊息會帶完整 URL，Telegram 的 token 就在路徑裡，先遮掉再記。
    """
    msg = str(exc).replace(token, "***")[:300]
    log.error("%s send failed (%s): %s", channel, type(exc).__name__, msg)


def send_telegram(text: str, session: requests.Session | None = None, *,
                  quiet: bool = False) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    s = session or requests
    try:
        resp = s.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_notification": quiet},
            timeout=SEND_TIMEOUT,
        )
    except requests.RequestException as exc:
        _log_transport_error("Telegram", exc, token)
        return False
    if resp.status_code != 200:
        log.error("Telegram send failed %s: %s", resp.status_code, resp.text[:300])
        return False
    return True


LINE_API = "https://api.line.me/v2/bot/message"
#: 廣播時，本月剩不到這麼多則就改成只 push 給 LINE_USER_ID。LINE 官方帳號每位收件人算
#: 一則，廣播給 5 個人就用掉 5 則；免費方案每月約 200 則，用完連擁有者都收不到。
LINE_OWNER_RESERVE = 30
#: 有設 Telegram 時的保留額度：擁有者的通知改走 Telegram，LINE 的保留額度只是 Telegram 送不出去時
#: 的備援。2026-11-21 定 10 則；2026-11-26 降到 3 則——朋友只用 LINE，額度要留給便宜的機票
#: （11/26 KHH⇄OKA 已驗證 5,115 因為額度不夠沒廣播出去）。
LINE_OWNER_RESERVE_WITH_TELEGRAM = 3
#: 同一條線這麼多天內只廣播給朋友一次，除非新的比這段時間廣播過的最低價再便宜 10%（storage 的
#: meaningfully_cheaper）。2026-11-26：11/21 起 13 則已驗證警報，照這條只需要廣播 7 則（福岡 11/24 一天 3 則
#: ——12/01、12/02、12/09 出發——朋友收一則就夠）。換算每月約 41 次廣播，免費額度裝得下。
#: Telegram（擁有者）照樣每則都收到。
LINE_ROUTE_SPACING_DAYS = 3


def telegram_configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def owner_reserve() -> int:
    """LINE 要留給擁有者的則數：有 Telegram 時 3，沒有時 30。"""
    return LINE_OWNER_RESERVE_WITH_TELEGRAM if telegram_configured() else LINE_OWNER_RESERVE
#: 讀不到好友統計時，一次廣播當作用掉幾則（擁有者＋4 位朋友）
DEFAULT_RECIPIENTS = 5
LINE_INSIGHT_API = "https://api.line.me/v2/bot/insight/followers"


class LineBudget:
    """LINE 額度配速（使用者說明見 docs/RUNBOOK_HOOK.md §3e）：讓免費額度攤到整個月，而不是前 20 天用完、
    月底朋友完全收不到。

    規則（send_line 廣播前檢查）：廣播完還留得下保留額度（owner_reserve）就廣播，否則只發給擁有者。

    2026-11-27 拿掉「同一天第二則要先留夠月底前每天一則」的預留：11/26 已驗證的 KHH⇄NGO 6,910（比門檻
    7,700 低 10%）因為那天已廣播過一次、月底只剩 17 則而沒發給朋友；沒有好價的日子，預留的額度到月底就
    作廢。現在會廣播的只剩已驗證的警報（未驗證與便宜日推播只發給擁有者），浪費改由「同一條線 3 天只廣播
    一次」（route_recently_told）擋掉——11/21 起的警報重播每月約 164 則，在免費 200 則內。
    """

    def __init__(self, store, now: datetime):
        self.store, self.now = store, now

    def _tw(self) -> datetime:
        return self.now.astimezone(TW)

    def broadcasts_today(self) -> int:
        start = self._tw().replace(hour=0, minute=0, second=0, microsecond=0)
        return self.store.line_sends_since(
            "broadcast", start.astimezone(timezone.utc).isoformat(timespec="seconds"))

    def days_left(self) -> int:
        """本月（台灣日期）含今天還剩幾天。"""
        t = self._tw()
        return calendar.monthrange(t.year, t.month)[1] - t.day + 1

    def allows_broadcast(self, left: int, recipients: int) -> bool:
        return left - recipients >= owner_reserve()

    def route_recently_told(self, origin: str, destination: str, price: float) -> bool:
        """這條線 LINE_ROUTE_SPACING_DAYS 天內已經廣播過、而且這次沒有再便宜 10% 以上。"""
        since = (self.now - timedelta(days=LINE_ROUTE_SPACING_DAYS)).isoformat(timespec="seconds")
        floor = self.store.route_broadcast_floor(origin, destination, since)
        return floor is not None and not meaningfully_cheaper(price, floor)

    def record(self, mode: str, quota_left: int | None = None, *, route=None,
               price: float | None = None) -> None:
        o, d = route if route else (None, None)
        self.store.record_line_send(self.now.isoformat(timespec="seconds"), mode, quota_left,
                                    origin=o, destination=d, price=price)


def _line_recipients(s, headers: dict, token: str, now: datetime) -> int:
    """一次廣播會用掉幾則＝能收到廣播的好友數。讀 LINE 前一天的好友統計；
    讀不到（好友太少時 LINE 不提供、未就緒、連線失敗）就用 LINE_RECIPIENTS 或預設 5。"""
    fallback = DEFAULT_RECIPIENTS
    try:
        fallback = max(1, int(os.environ.get("LINE_RECIPIENTS") or DEFAULT_RECIPIENTS))
    except ValueError:
        pass
    day = (now.astimezone(TW) - timedelta(days=1)).strftime("%Y%m%d")
    try:
        r = s.get(LINE_INSIGHT_API, params={"date": day}, headers=headers,
                  timeout=SEND_TIMEOUT)
        if r.status_code != 200:
            return fallback
        data = r.json()
        if data.get("status") != "ready":
            return fallback
        n = data.get("targetedReaches")
        if isinstance(n, int) and not isinstance(n, bool) and n > 0:
            return n
    except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
        _log_transport_error("LINE insight", exc, token)
    return fallback


def _line_quota_left(s, headers: dict, token: str) -> int | None:
    """本月還剩幾則；不限量或查不到時回 None。查額度不扣則數。"""
    try:
        quota = s.get(f"{LINE_API}/quota", headers=headers, timeout=SEND_TIMEOUT)
        used = s.get(f"{LINE_API}/quota/consumption", headers=headers, timeout=SEND_TIMEOUT)
        if quota.status_code != 200 or used.status_code != 200:
            return None
        q = quota.json()
        if q.get("type") != "limited":
            return None
        return int(q["value"]) - int(used.json()["totalUsage"])
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        _log_transport_error("LINE quota", exc, token)
        return None


def send_line(text: str, session: requests.Session | None = None, *,
              budget: LineBudget | None = None, owner_only: bool = False,
              owner_reached: bool = False, route=None, price: float | None = None) -> bool:
    """LINE_BROADCAST=1：廣播給官方帳號的所有好友；本月額度剩不到保留額度（owner_reserve）
    時改成只 push 給 LINE_USER_ID，至少擁有者不會漏掉通知。有 budget 時再加上配速
    （見 LineBudget），超出今天份額的只發給擁有者。其餘情況照舊 push 給 LINE_USER_ID
    一個人。額度查不到時照樣廣播。台灣時間半夜靜音送達（quiet_hours）。

    owner_only：這則不廣播、只發給擁有者（notify 對沒有經 Google 驗證的警報、同一條線 3 天內已廣播過
    的警報這樣做；便宜日推播一律如此）。沒設 LINE_USER_ID 時無人可發，只好照舊廣播。

    route／price：廣播出去時記進 line_sends，給「同一條線 3 天內只廣播一次」用。

    owner_reached：擁有者已經從 Telegram 收到這則。廣播模式下這則最後只會發給擁有者時，
    就不再用 LINE 發（每則扣 1 則額度，LINE 留給朋友的廣播），回傳 False。Telegram 沒送到
    時呼叫端傳 False，照舊用 LINE 發給擁有者。沒開廣播（例如手動測試通知）時不受影響。"""
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    user_id = os.environ.get("LINE_USER_ID")
    broadcast = os.environ.get("LINE_BROADCAST") == "1"
    if not token or not (user_id or broadcast):
        return False
    s = session or requests
    headers = {"Authorization": f"Bearer {token}"}
    messages = [{"type": "text", "text": text[:5000]}]
    left, n = None, DEFAULT_RECIPIENTS      # 本月剩餘則數（查不到是 None）、一次廣播用掉幾則
    broadcast_mode = broadcast and bool(user_id)
    if broadcast_mode:
        left = _line_quota_left(s, headers, token)
        reserve = owner_reserve()
        if owner_only:
            log.info("LINE：這則只發給擁有者、不廣播（未經 Google 驗證、同線 3 天內已廣播過，或便宜日推播）")
            broadcast = False
        elif left is not None and left < reserve:
            log.warning("LINE 本月只剩 %d 則（保留 %d 則給擁有者）：這則只發給擁有者",
                        left, reserve)
            broadcast = False
        elif left is not None and budget is not None:
            n = _line_recipients(s, headers, token, budget.now)
            if not budget.allows_broadcast(left, n):
                log.info("LINE 額度：本月剩 %d 則，廣播一次 %d 則就會動到保留的 %d 則：這則只發給擁有者",
                         left, n, owner_reserve())
                broadcast = False
    if broadcast_mode and not broadcast and owner_reached:
        log.info("LINE：擁有者已經從 Telegram 收到這則，不再用 LINE 發（省下 1 則額度給朋友）")
        return False
    quiet = quiet_hours(budget.now if budget else datetime.now(timezone.utc))
    url, body = ((f"{LINE_API}/broadcast", {"messages": messages}) if broadcast else
                 (f"{LINE_API}/push", {"to": user_id, "messages": messages}))
    body["notificationDisabled"] = quiet
    try:
        resp = s.post(url, headers=headers, json=body, timeout=SEND_TIMEOUT)
    except requests.RequestException as exc:
        _log_transport_error("LINE", exc, token)
        return False
    if resp.status_code != 200:
        log.error("LINE send failed %s: %s", resp.status_code, resp.text[:300])
        return False
    if budget is not None:
        # 記下送出後大約還剩幾則，給看板的用量面板（廣播時 n 是讀到或預設的好友數）
        after = None if left is None else max(0, left - (n if broadcast else 1))
        if broadcast:
            budget.record("broadcast", after, route=route, price=price)
        else:
            budget.record("owner", after)
    return True


def channels_configured() -> bool:
    return bool(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
                or os.environ.get("TELEGRAM_BOT_TOKEN"))


def notify(offer: Offer, verdict: Verdict, state=None, *,
           line_budget: LineBudget | None = None) -> list[str]:
    """Send to all configured channels; return the list that succeeded.

    LINE 只廣播**經 Google 驗證**（state.state == VERIFIED）的警報；未驗證、價格有落差的只發給
    擁有者。2026-11-01～11-13 的 31 則警報有 10 則未驗證，其中 3 則事後看是錯的（實價高
    25–34%，或 Google 上沒有那家航空）——朋友收到錯價會不再相信警報。state 沒給時照舊。
    """
    text = format_alert(offer, verdict, state)
    owner_only = state is not None and getattr(state, "state", None) != "verified"   # price_state.VERIFIED
    price = getattr(state, "selected_price", None) or offer.price
    route = (offer.origin, offer.destination)
    if not owner_only and line_budget is not None and line_budget.route_recently_told(*route, price):
        log.info("LINE：%s→%s %d 天內已經廣播過、這次沒有再便宜 10%%，只發給擁有者",
                 *route, LINE_ROUTE_SPACING_DAYS)
        owner_only = True
    sent = []
    quiet = quiet_hours(line_budget.now if line_budget else datetime.now(timezone.utc))
    telegram_ok = send_telegram(text, quiet=quiet)
    if telegram_ok:
        sent.append("telegram")
    # Telegram 已經送到擁有者：LINE 只在會廣播給朋友時才發（見 send_line 的 owner_reached）
    if send_line(text, budget=line_budget, owner_only=owner_only, owner_reached=telegram_ok,
                 route=route, price=price):
        sent.append("line")
    if not sent:
        log.info("No notification channel configured; alert printed only:\n%s", text)
        print(text)
    return sent
