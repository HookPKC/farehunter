"""「哪天特別便宜」——跨出發日的相對比較（純函式，零 API、零 DB）。

## 為什麼需要跟 big_drop 不同的問法

`analyzer.big_drop` 問的是「這個出發日比**它自己平常**便宜嗎」，所以需要該日累積
足夠的歷史。實測 6 週後只有 60% 的出發日達到 30 筆門檻，而缺口集中在薄航線
——KHH→CTS 只有 8/73 個出發日達標、KHH→NGO 4/33、KHH→FUK 26/104。偏偏那些
是使用者最沒有價格直覺、最需要被告知的航線。

本模組問的是「這個出發日比**鄰近的日子**便宜嗎」。這是跨日期比較，每個出發日
只需要一筆觀測就能參與，因此覆蓋率提升到 86%（CTS 11%→82%、FUK 25%→76%）。

兩者互補，不互相取代：
  big_drop    偵測「變化」——某天的價格掉下來了
  cheap_days  偵測「結構」——某天本來就比鄰居便宜（例如春節後一週、平日二/四）

## 這個規則答得出與答不出的問題

答得出：「我這趟想飛大阪，哪幾天比周圍便宜？」
答不出：「十月比十二月便宜嗎？」——那是跨季節比較，屬於年度視圖的月份長條，
        不是這裡的 ±10 天窗。窗刻意開得窄，才不會把淡旺季混在一起比。
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, timedelta
from statistics import median

#: 與前後各幾天比較。刻意窄——開太寬會把淡旺季混在一起，
#: 那正是 big_drop 原本拿全航線中位數當基準的錯誤。
WINDOW_DAYS = 10

#: 低於鄰近中位數多少百分比才算「特別便宜」。
DROP_PCT = 25.0

#: 鄰近至少要有幾天有資料，中位數才有意義。
MIN_NEIGHBOURS = 6

#: 推播門檻：只有跌幅達這個程度的新進榜日期才值得打斷使用者。
#: 網站列表用 DROP_PCT（看板不吵人），推播用這條更嚴的線。
NOTIFY_PCT = 30.0


@dataclass(frozen=True)
class CheapDay:
    origin: str
    destination: str
    depart_date: str
    price: float
    neighbour_median: float
    discount_pct: float          # 低於鄰近中位數的百分比
    neighbours: int              # 參與比較的鄰近日期數
    notable: bool                # 跌幅是否達到推播門檻

    def to_dict(self) -> dict:
        return asdict(self)


def find_cheap_days(prices_by_date: dict[str, float],
                    origin: str,
                    destination: str,
                    *,
                    today: str | None = None,
                    window_days: int = WINDOW_DAYS,
                    drop_pct: float = DROP_PCT,
                    min_neighbours: int = MIN_NEIGHBOURS,
                    notify_pct: float = NOTIFY_PCT) -> list[CheapDay]:
    """找出相對鄰近日期反常便宜的出發日，跌幅大的排前面。

    prices_by_date: {depart_date: 該日目前最低價}。呼叫端負責決定「目前」的
        定義（例如只取新鮮觀測），本函式不查 DB、不看時鐘以外的東西。
    today: 早於此日期的出發日一律排除——已經飛走的日子不是建議。
        None 時取系統當日。

    價格非正數的日期直接忽略（不參與比較，也不會被列出）：0 或負數不是報價。
    """
    today = today or date.today().isoformat()
    valid = {d: float(p) for d, p in prices_by_date.items()
             if isinstance(p, (int, float)) and not isinstance(p, bool) and p > 0}

    out: list[CheapDay] = []
    for ds, price in valid.items():
        if ds < today:
            continue
        try:
            base = date.fromisoformat(ds)
        except ValueError:
            continue                      # 壞掉的日期字串：跳過，不讓它炸掉整批
        neigh = [valid[k] for k in
                 ((base + timedelta(days=off)).isoformat()
                  for off in range(-window_days, window_days + 1) if off != 0)
                 if k in valid]
        if len(neigh) < min_neighbours:
            continue                      # 樣本不足 → 不下判斷（不是「不便宜」）
        med = median(neigh)
        if not med or price > med * (1 - drop_pct / 100.0):
            continue
        disc = (1 - price / med) * 100.0
        out.append(CheapDay(
            origin=origin, destination=destination, depart_date=ds,
            price=price, neighbour_median=med,
            discount_pct=round(disc, 1), neighbours=len(neigh),
            notable=disc >= notify_pct))

    out.sort(key=lambda c: (-c.discount_pct, c.depart_date))
    return out
