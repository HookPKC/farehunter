"""Picker 線索：巡檢優先查「Google 日曆顯示便宜」的日子（ADR 0005）。

google-flights2 的 getCalendarPicker 一次回 62 天、每天一筆「出發日＋N 晚」的最低價。
**它含轉機、沒有航空公司**（2026-11-14 實測：KHH-OKA 13 天裡 11 天等於直飛實價，KHH-NGO 有直飛
的 7 天全等；KHH-FUK 11 天只有 5 天、KHH-CTS 4 天只有 1 天，其餘是便宜很多的轉機價）。所以：

- 線索價**只**存在 calendar_hints 表：不進 observations，不顯示、不發警報、不進統計。
- 用途只有一個：告訴 Bright Data／ChocoData 的巡檢先查哪幾天。那一趟查到直飛實價才寫入
  （照既有流程）；那天沒有直飛就什麼都不寫，那個星期幾之後也不再挑。
- 線索價不會比直飛實價貴（含轉機的最低價 ≤ 直飛價），所以線索價高出門檻一截的日子直飛
  不可能便宜，不挑。

這支只負責「挑」；抓取在 gflights2_flights.refresh_hints。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

#: 抓 Picker 的航線。KHH-CTS 不抓：實測 4 天有 3 天是轉機價，挑出來多半白查。
ROUTES = (("KHH", "OKA"), ("KHH", "NGO"), ("KHH", "FUK"))
STAY_NIGHTS = 5              # 跟巡檢的固定日期一樣住 5 晚
WINDOW_DAYS = 62             # Picker 一次回的天數
WINDOWS = 2                  # 每條線抓幾段（約未來 4 個月）
LEAD_DAYS = 3                # 第一段從幾天後開始
REFRESH_DAYS = 7             # 每段多久重抓一次
STALE_DAYS = 9               # 超過這麼久沒更新的線索不用（重抓失敗時自然退回固定日期）
PER_ROUTE = 6                # 每條線每輪最多排幾個線索日
CAP_RATIO = 1.25             # 線索價超過門檻這個倍數的不挑
MIN_LEAD_DAYS = 2            # 太近的出發日不查（同 brightdata_flights.MIN_LEAD_DAYS）


def windows(today: date) -> list[tuple[date, date]]:
    """要抓的時間段（含頭尾），從 today＋LEAD_DAYS 起連續 WINDOWS 段。"""
    out, start = [], today + timedelta(days=LEAD_DAYS)
    for _ in range(WINDOWS):
        end = start + timedelta(days=WINDOW_DAYS - 1)
        out.append((start, end))
        start = end + timedelta(days=1)
    return out


def pick(store, now: datetime, origin: str, dest: str, *, avoid=frozenset(),
         cap: float | None = None, limit: int = PER_ROUTE) -> list[tuple[str, str]]:
    """這條線要優先查的 (去程日, 回程日)，線索價由低到高。

    avoid：最近查了卻沒有直飛的星期幾（0＝週一），那些日子的線索價是轉機價，不挑。
    cap：線索價上限（門檻 × CAP_RATIO）；沒給就不設上限。
    """
    earliest = (now.date() + timedelta(days=MIN_LEAD_DAYS)).isoformat()
    since = (now - timedelta(days=STALE_DAYS)).isoformat(timespec="seconds")
    out = []
    for dep, ret, price in store.calendar_hints(origin, dest, earliest, since):
        if cap is not None and price > cap:
            break                                  # 已由低到高排序
        if date.fromisoformat(dep).weekday() in avoid:
            continue
        out.append((dep, ret))
        if len(out) >= limit:
            break
    return out
