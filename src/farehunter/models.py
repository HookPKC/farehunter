"""Shared data models."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Near-term planning window (days ahead) shared by the fare calendar (export_web)
# AND the intelligence ranking (intelligence.py) so Hero / Recommended / Cheapest
# are computed over an IDENTICAL set of offers and can never diverge. Anything
# beyond this window belongs to the year-view monthly strip, not the calendar.
NEAR_TERM_DAYS = 90

# 只通知、只推薦住這麼多晚的來回行程（使用者 2026-11-12 決定）。原本每個出發日只留最便宜的
# 一筆、不看停留天數：近 30 天 50 則警報有 7 則是 14 晚以上（含 2 則「30 天來回」），看板
# 8 條線的推薦有 3 條是 11 晚以上。單一來源：抓價（parse_offers 優先留範圍內的）、警報、
# Bright Data 巡檢、看板的價格格子／目前最低／便宜日／每月最低、推薦排名都用這一組。
# 歷史統計與走勢不受影響。沒有回程日的列（單程、舊資料）無從判斷，一律放行。
MIN_STAY_NIGHTS, MAX_STAY_NIGHTS = 2, 10
#: 同一條規則的 SQL 版本（欄位名固定是 depart_date／return_date；只由常數組成）
STAY_SQL = ("(return_date IS NULL OR return_date = '' OR "
            "julianday(return_date) - julianday(depart_date) "
            f"BETWEEN {MIN_STAY_NIGHTS} AND {MAX_STAY_NIGHTS})")


def stay_ok(depart_date: str, return_date: str | None) -> bool:
    """停留晚數在通知／推薦的範圍內；沒有回程日或日期壞掉時放行（不猜）。"""
    if not return_date:
        return True
    from datetime import date
    try:
        nights = (date.fromisoformat(return_date[:10]) - date.fromisoformat(depart_date[:10])).days
    except ValueError:
        return True
    return MIN_STAY_NIGHTS <= nights <= MAX_STAY_NIGHTS


@dataclass
class Offer:
    """A single fare observation, flattened to the fields we track."""
    origin: str
    destination: str
    depart_date: str          # YYYY-MM-DD
    return_date: Optional[str]
    price: float
    currency: str
    carriers: str             # e.g. "CI" or "CI,BR"
    stops: int
    duration: str             # minutes as string, e.g. "190" (source-dependent)
    link: str = ""            # booking deep link, if the source provides one
    fare_class: str = "any"   # "any" = cheapest overall, "full" = full-service carriers
    source: str = "aviasales" # "aviasales" cache | "google" real-price calendar
    provider: Optional[str] = None  # exact API: serpapi|scrapedo|brightdata|searchapi|travelpayouts
