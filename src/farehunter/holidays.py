"""台灣連假日曆：某一趟要請幾天假、碰到哪個連假（看板用；不影響價格與通知）。

資料在 `data/tw_holidays.json`，來自人事行政總處「辦公日曆表」（data.gov.tw 資料集 14718，
經 ruyut/TaiwanCalendar 整理）。執行時不連外；每年人事總處公布隔年行事曆後手動更新一次
（docs/RUNBOOK_HOOK.md §3g）。

規則：
- 週六日放假，除非列在 workdays（補班）；平日放假列在 off。
- 連假＝連續 MIN_BLOCK_DAYS 天以上不用上班。一般週末只有 2 天，所以連假一定含國定假日。
- 請假天數＝出發日到回程日（含頭尾）之間的上班日數。
- 碰到資料沒有涵蓋的年份就回 None，不猜。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parents[2] / "data" / "tw_holidays.json"
MIN_BLOCK_DAYS = 3
#: 連假名稱：區間裡有這些節日就用它命名（春節連假裡也有「小年夜」「除夕」）
_PRIMARY = ("春節", "清明節", "端午節", "中秋節", "國慶日", "和平紀念日", "勞動節",
            "開國紀念日", "兒童節")
_FILLER = {"補假", "放假", "小年夜", "農曆除夕", "調整放假"}
_SHORT = {"臺灣光復暨金門古寧頭大捷紀念日": "光復節", "孔子誕辰紀念日/教師節": "教師節"}


@dataclass(frozen=True)
class Block:
    start: date
    end: date
    name: str

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def as_dict(self) -> dict:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(),
                "days": self.days, "name": self.name}


class Calendar:
    def __init__(self, data: dict):
        self.years = frozenset(int(y) for y in data.get("years", []))
        self.off = {date.fromisoformat(k): v for k, v in data.get("off", {}).items()}
        self.workdays = {date.fromisoformat(k) for k in data.get("workdays", {})}

    def covers(self, d: date) -> bool:
        return d.year in self.years

    def is_off(self, d: date) -> bool | None:
        if not self.covers(d):
            return None
        if d in self.workdays:
            return False
        return d in self.off or d.weekday() >= 5

    def blocks(self) -> list[Block]:
        """所有連假（連續 MIN_BLOCK_DAYS 天以上不用上班），依日期排序。"""
        out: list[Block] = []
        if not self.years:
            return out
        d, last = date(min(self.years), 1, 1), date(max(self.years), 12, 31)
        run: list[date] = []
        while d <= last + timedelta(days=1):
            if d <= last and self.is_off(d):
                run.append(d)
            else:
                if len(run) >= MIN_BLOCK_DAYS:
                    out.append(Block(run[0], run[-1], self._name(run)))
                run = []
            d += timedelta(days=1)
        return out

    def _name(self, run: list[date]) -> str:
        names = [self.off[d] for d in run if d in self.off]
        for p in _PRIMARY:
            if any(p in n for n in names):
                return p
        rest = [n for n in names if n not in _FILLER]
        return _SHORT.get(rest[0], rest[0]) if rest else "連假"

    def leave_days(self, depart: str, ret: str | None) -> int | None:
        """出發到回程（含頭尾）要請幾天假；沒有回程日或資料沒涵蓋就 None。"""
        if not ret:
            return None
        try:
            d0, d1 = date.fromisoformat(depart[:10]), date.fromisoformat(ret[:10])
        except ValueError:
            return None
        if d1 < d0 or not (self.covers(d0) and self.covers(d1)):
            return None
        n, d = 0, d0
        while d <= d1:
            n += not self.is_off(d)
            d += timedelta(days=1)
        return n

    def holiday_of(self, depart: str, ret: str | None) -> Block | None:
        """這趟（出發到回程）碰到的連假；碰到兩個時取重疊天數多的。"""
        best, overlap = None, 0
        for b in self.blocks():
            o = overlap_days(depart, ret, b)
            if o > overlap:
                best, overlap = b, o
        return best


def overlap_days(depart: str, ret: str | None, block: Block) -> int:
    """這趟（出發到回程，含頭尾）跟連假重疊幾天；日期壞掉回 0。"""
    try:
        d0 = date.fromisoformat(depart[:10])
        d1 = date.fromisoformat((ret or depart)[:10])
    except ValueError:
        return 0
    return max(0, (min(d1, block.end) - max(d0, block.start)).days + 1)


@lru_cache(maxsize=1)
def default() -> Calendar:
    """repo 內附的日曆；檔案不見或壞掉時回空日曆（所有查詢都是 None），看板照常。"""
    try:
        return Calendar(json.loads(DATA_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return Calendar({})
