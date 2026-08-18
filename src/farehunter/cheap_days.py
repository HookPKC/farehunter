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
from datetime import date, datetime, timedelta, timezone
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

#: 候選日與鄰近日的觀測時間最多可以相差幾天。
#:
#: 這是比較公不公平的關鍵，不是新鮮度政策。薄航線的某些出發日可能好幾週才被
#: 抓到一次（API 對它們常回「No cached fares」），如果拿六週前的候選價去比昨天
#: 的鄰近價，只要這段期間整體漲價，那天就會顯得假性便宜——實測 KHH→FUK
#: 2026-12-04 的候選價是 6 週前觀測的，而鄰近中位數多數來自本週。
#: 限制兩邊的時間差，比較就一定是同時期的價格，與絕對新鮮度無關。
MAX_SPREAD_DAYS = 7

#: 絕對上限：超過這個歲數的觀測一律不列。不是為了公平（那由 MAX_SPREAD_DAYS
#: 負責），而是太舊的價格連當參考都沒意義。呼叫端仍應把 observed_at 顯示出來，
#: 讓使用者自己判斷要不要相信。
MAX_AGE_DAYS = 30


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
    return_date: str | None = None    # 該筆觀測的回程日，供前端組比價連結

    def to_dict(self) -> dict:
        return asdict(self)


def find_cheap_days(prices_by_date: dict[str, float],
                    origin: str,
                    destination: str,
                    *,
                    today: str | None = None,
                    observed_at_by_date: dict[str, str] | None = None,
                    return_date_by_date: dict[str, str] | None = None,
                    window_days: int = WINDOW_DAYS,
                    drop_pct: float = DROP_PCT,
                    min_neighbours: int = MIN_NEIGHBOURS,
                    notify_pct: float = NOTIFY_PCT,
                    max_spread_days: int = MAX_SPREAD_DAYS,
                    max_age_days: int = MAX_AGE_DAYS) -> list[CheapDay]:
    """找出相對鄰近日期反常便宜的出發日，跌幅大的排前面。

    prices_by_date: {depart_date: 該日**目前**的價格}。

        **必須是現在還訂得到的價格，不能是「史上最低」。** 這不是風格偏好：
        實測 28 個候選日期中有 13 個的史上最低價已經消失，其中
        KHH→FUK 2026-11-27 從 16,173 漲到 54,597（+238%）。把那個數字端到
        使用者面前，他點進去會看到三倍價——這違反全站的誠實語意，比不告訴他
        更糟。呼叫端請取該出發日最新一筆觀測。

    observed_at_by_date: 選填，{depart_date: 該價格的觀測時間}，會原樣帶進
        結果供前端顯示資料齡。本函式不判斷新鮮度——那是呼叫端的政策。
    today: 早於此日期的出發日一律排除——已經飛走的日子不是建議。
        None 時取系統當日。

    價格非正數的日期直接忽略（不參與比較，也不會被列出）：0 或負數不是報價。
    """
    today = today or date.today().isoformat()
    valid = {d: float(p) for d, p in prices_by_date.items()
             if isinstance(p, (int, float)) and not isinstance(p, bool) and p > 0}
    seen = observed_at_by_date or {}

    def _ts(ds: str) -> datetime | None:
        raw = seen.get(ds)
        if not raw:
            return None
        try:
            t = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)

    out: list[CheapDay] = []
    for ds, price in valid.items():
        if ds < today:
            continue
        try:
            base = date.fromisoformat(ds)
        except ValueError:
            continue                      # 壞掉的日期字串：跳過，不讓它炸掉整批

        cand_ts = _ts(ds)
        if cand_ts is not None and (now - cand_ts).days > max_age_days:
            continue                      # 太舊，連當參考都沒意義

        neigh = []
        for off in range(-window_days, window_days + 1):
            if off == 0:
                continue
            k = (base + timedelta(days=off)).isoformat()
            if k not in valid:
                continue
            # 比較必須同時期：兩邊都有時間戳時，時間差超過上限就不採用。
            # 缺時間戳時不強制（呼叫端可能根本沒提供），退回單純的價格比較。
            n_ts = _ts(k)
            if cand_ts is not None and n_ts is not None and \
                    abs((n_ts - cand_ts).days) > max_spread_days:
                continue
            neigh.append(valid[k])
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
            observed_at=seen.get(ds),
            return_date=(return_date_by_date or {}).get(ds)))

    out.sort(key=lambda c: (-c.discount_pct, c.depart_date))
    return out


#: 每個出發日「最新一筆」觀測。刻意不是 MIN(price)——見 find_cheap_days 的說明。
_LATEST_PER_DATE = """
SELECT o.depart_date, o.price, o.observed_at, o.return_date
  FROM observations o
  JOIN (SELECT depart_date, MAX(observed_at) AS mx
          FROM observations
         WHERE origin=? AND destination=? AND fare_class='any'
         GROUP BY depart_date) t
    ON t.depart_date = o.depart_date AND t.mx = o.observed_at
 WHERE o.origin=? AND o.destination=? AND o.fare_class='any'
 GROUP BY o.depart_date
"""


def latest_prices_by_date(conn, origin: str, destination: str
                          ) -> tuple[dict[str, float], dict[str, str], dict[str, str]]:
    """回傳 ({depart_date: 最新價}, {depart_date: 觀測時間}, {depart_date: 回程日})。

    存在的理由：把「必須用最新價、不能用史上最低」這個容易寫錯的查詢收在一處。
    同一時刻若有多筆並列（同一輪抓到多個行程），GROUP BY 取其中一筆——它們都是
    當下真實存在的報價，任一筆都誠實。
    """
    prices: dict[str, float] = {}
    seen_at: dict[str, str] = {}
    ret: dict[str, str] = {}
    for row in conn.execute(_LATEST_PER_DATE,
                            (origin, destination, origin, destination)):
        prices[row[0]] = row[1]
        seen_at[row[0]] = row[2]
        if row[3]:
            ret[row[0]] = row[3]
    return prices, seen_at, ret


def build_cheap_days(conn, routes, *, today: str | None = None,
                     **kw) -> list[dict]:
    """對每條航線跑一次比較，合併後依跌幅排序。routes 為 (origin, destination) 序列。"""
    hits: list[CheapDay] = []
    for origin, destination in routes:
        prices, seen_at, ret = latest_prices_by_date(conn, origin, destination)
        hits += find_cheap_days(prices, origin, destination, today=today,
                                observed_at_by_date=seen_at,
                                return_date_by_date=ret, **kw)
    hits.sort(key=lambda c: (-c.discount_pct, c.depart_date))
    return [h.to_dict() for h in hits]
