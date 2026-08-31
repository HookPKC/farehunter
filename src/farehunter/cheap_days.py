"""「哪天特別便宜」——跨出發日的相對比較。

## 為什麼需要跟 big_drop 不同的問法

`analyzer.big_drop` 問的是「這個出發日比**它自己平常**便宜嗎」，所以需要該日累積
足夠的歷史。實測 6 週後只有 60% 的出發日達到 30 筆門檻，而缺口集中在薄航線
——KHH→CTS 只有 8/73 個出發日達標、KHH→NGO 4/33。偏偏那些是使用者最沒有價格
直覺、最需要被告知的航線。

本模組問的是「這個出發日比**鄰近的日子**便宜嗎」。跨日期比較，每個出發日只要
一筆觀測就能參與。

兩者互補，不互相取代：
  big_drop    偵測「變化」——某天的價格掉下來了
  cheap_days  偵測「結構」——某天本來就比鄰居便宜（例如春節後一週、平日二/四）

## 只用新鮮資料互相比較（這一版的核心）

上一版用「該出發日最新一筆觀測」再加兩個補丁（最多 30 天、候選與鄰近的觀測
時間差 ≤7 天）。實測後改成單一條件：**兩邊都必須是最近 FRESH_HOURS 內觀測到的**。
三個好處：

1. 誠實：列出的每個價格都還訂得到。舊版可能端出 30 天前的價格。
2. 同時期自動成立：兩邊都新鮮，就不可能拿舊價比新價，spread 補丁可以刪掉。
3. 門檻可以更靈敏：實測價格漂移在 ≤7 天間隔的中位數就已經是 0.0%，
   所以用 24 小時內的資料時，15% 的落差是真實結構差異而非雜訊。舊版必須用
   25% 才能蓋掉漂移（≤14 天間隔的漂移在 90 百分位已達 26.2%，超過門檻本身）。

回測過去 12 天：舊設計每天列出 3 天（平均 1.9），新設計平均 10.8（範圍 7–14）。

## 這個規則答得出與答不出的問題

答得出：「我這趟想飛大阪，哪幾天比周圍便宜？」
答不出：「十月比十二月便宜嗎？」——那是跨季節比較，屬於年度視圖的月份長條，
        不是這裡的 ±10 天窗。窗刻意開得窄，才不會把淡旺季混在一起比。
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from statistics import median

#: 與前後各幾天比較。刻意窄——開太寬會把淡旺季混在一起，
#: 那正是 big_drop 原本拿全航線中位數當基準的錯誤。
WINDOW_DAYS = 10

#: 低於鄰近中位數多少百分比才算「特別便宜」。
#: 15% 是搭配 FRESH_HOURS 校準出來的——見模組說明。
DROP_PCT = 15.0

#: 鄰近至少要有幾天有資料，中位數才有意義。
MIN_NEIGHBOURS = 6

#: 推播門檻：只有跌幅達這個程度才值得打斷使用者。
#: 網站列表用 DROP_PCT（看板不吵人），推播用這條更嚴的線。
NOTIFY_PCT = 30.0

#: 只有這麼新的觀測才參與比較。與網站 hero / CTA 的 SLA 一致
#: （current_price.SURFACE_SLA_HOURS），整站對「現價」用同一把尺。
FRESH_HOURS = 24


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
    observed_at: str | None = None   # 這個價格是何時觀測到的，供前端顯示資料齡
    return_date: str | None = None   # 該筆觀測的回程日，供前端組比價連結
    source: str | None = None        # aviasales（快取估價）或 google（實際觀測價）

    def to_dict(self) -> dict:
        return asdict(self)


def find_cheap_days(prices_by_date: dict[str, float],
                    origin: str,
                    destination: str,
                    *,
                    today: str | None = None,
                    observed_at_by_date: dict[str, str] | None = None,
                    return_date_by_date: dict[str, str] | None = None,
                    source_by_date: dict[str, str] | None = None,
                    window_days: int = WINDOW_DAYS,
                    drop_pct: float = DROP_PCT,
                    min_neighbours: int = MIN_NEIGHBOURS,
                    notify_pct: float = NOTIFY_PCT) -> list[CheapDay]:
    """找出相對鄰近日期反常便宜的出發日，跌幅大的排前面。純函式：不查 DB、不看時鐘。

    prices_by_date: {depart_date: 價格}。**呼叫端必須只餵新鮮且同時期的價格**
        （見 build_cheap_days）。本函式不做新鮮度判斷——那是資料選取層的責任。

        為什麼這件事重要：實測 28 個候選日期中有 13 個的「史上最低價」已經消失，
        其中 KHH→FUK 2026-11-27 從 16,173 漲到 54,597（+238%）。把那個數字端到
        使用者面前，他點進去會看到三倍價——比不告訴他更糟。

    observed_at_by_date / return_date_by_date: 選填，原樣帶進結果供前端使用。
    today: 早於此日期的出發日一律排除——已經飛走的日子不是建議。None 時取系統當日。

    價格非正數的日期直接忽略（不參與比較，也不會被列出）：0 或負數不是報價。
    """
    today = today or date.today().isoformat()
    valid = {d: float(p) for d, p in prices_by_date.items()
             if isinstance(p, (int, float)) and not isinstance(p, bool) and p > 0}
    seen = observed_at_by_date or {}
    rets = return_date_by_date or {}
    srcs = source_by_date or {}

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
            notable=disc >= notify_pct,
            observed_at=seen.get(ds), return_date=rets.get(ds),
            source=srcs.get(ds)))

    out.sort(key=lambda c: (-c.discount_pct, c.depart_date))
    return out


#: 每個出發日「最新一筆」觀測。刻意不是 MIN(price)——見 find_cheap_days 的說明。
_LATEST_PER_DATE = """
SELECT o.depart_date, o.price, o.observed_at, o.return_date, o.source
  FROM observations o
  JOIN (SELECT depart_date, MAX(observed_at) AS mx
          FROM observations
         WHERE origin=? AND destination=? AND fare_class='any'
           AND observed_at >= ?
         GROUP BY depart_date) t
    ON t.depart_date = o.depart_date AND t.mx = o.observed_at
 WHERE o.origin=? AND o.destination=? AND o.fare_class='any'
 GROUP BY o.depart_date
"""


def latest_prices_by_date(conn, origin: str, destination: str, *, since: str = ""
                          ) -> tuple[dict[str, float], dict[str, str],
                                     dict[str, str], dict[str, str]]:
    """回傳 (最新價, 觀測時間, 回程日, 來源) 四個以 depart_date 為鍵的字典。

    since: ISO 時間字串，只採計不早於它的觀測。空字串＝不限（測試與診斷用；
        production 一律經由 build_cheap_days 帶入新鮮度下限）。

    存在的理由：把「必須用最新價、不能用史上最低」與新鮮度下限這兩個容易寫錯的
    條件收在一處。同一時刻若有多筆並列（同一輪抓到多個行程），GROUP BY 取其中
    一筆——它們都是當下真實存在的報價，任一筆都誠實。
    """
    prices: dict[str, float] = {}
    seen_at: dict[str, str] = {}
    rets: dict[str, str] = {}
    srcs: dict[str, str] = {}
    for row in conn.execute(_LATEST_PER_DATE,
                            (origin, destination, since, origin, destination)):
        prices[row[0]] = row[1]
        seen_at[row[0]] = row[2]
        if row[3]:
            rets[row[0]] = row[3]
        if row[4]:
            srcs[row[0]] = row[4]
    return prices, seen_at, rets, srcs


def build_cheap_days(conn, routes, *, now: datetime | None = None,
                     fresh_hours: float = FRESH_HOURS, **kw) -> list[dict]:
    """對每條航線跑一次比較，合併後依跌幅排序。routes 為 (origin, destination) 序列。

    只採計最近 fresh_hours 內的觀測。這一個條件同時保證了三件事：列出的價格還
    訂得到、比較的兩邊是同時期、以及門檻可以用更靈敏的 15%（見模組說明）。
    """
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(hours=fresh_hours)).isoformat(timespec="seconds")
    today = now.date().isoformat()

    hits: list[CheapDay] = []
    for origin, destination in routes:
        prices, seen_at, rets, srcs = latest_prices_by_date(
            conn, origin, destination, since=since)
        hits += find_cheap_days(prices, origin, destination, today=today,
                                observed_at_by_date=seen_at,
                                return_date_by_date=rets,
                                source_by_date=srcs, **kw)
    hits.sort(key=lambda c: (-c.discount_pct, c.depart_date))
    return [h.to_dict() for h in hits]
