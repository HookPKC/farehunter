"""prices.db 封存：出發日早已過去的觀測搬進 archive/，全歷史統計照樣看得到。

存在理由（決策紀錄見 docs/adr/0002-prices-db-archive.md）：prices.db 每小時
隨 commit 進 repo，2026-08→10 從 16 MiB 長到 45 MiB（每月約 +11 MB）。GitHub
拒收超過 100 MiB 的單一檔案——撞到那天，三個 writer 的 commit 全部失敗，整條
抓價停擺。git 歷史本身不是問題：SQLite 原檔的版本間差異很小，50 個版本的
pack 只有 8.25 MiB。卡住的只有「單一檔案大小」。

兩條路徑：

- **寫**：`archive_old()` 把 depart_date 早於（今天 − MARGIN_DAYS）的列從
  prices.db 搬到 archive/observations-NNN.db（滿 ROLL_BYTES 換新檔），每個
  封存檔的筆數記在 prices.db 的 archive_manifest。由 archive-db.yml 執行，
  刻意不在每小時抓價的路徑上：封存壞了，抓價照跑。
- **讀**：`attach(conn)` 掛上 manifest 列出的封存檔，建 TEMP VIEW
  `observations_all` ＝ main.observations ∪ 各封存檔。只有「全歷史」查詢讀它
  （new_low 的航線史上最低、首頁統計與趨勢、FIE 的歷史錨點、航線健康）。
  其餘查詢只看未來出發日或近期觀測，封存的列本來就不在它們的範圍內，
  所以繼續讀 observations，一個字都不用改。

為什麼封存「出發日」而不是「觀測時間」：觀測一定早於出發日，所以出發日已過
30 天的列，觀測時間也一定早於 30 天前——兩種視窗（未來出發日、近期觀測）
都碰不到它們。以觀測時間切則會切到仍在未來的出發日，big_drop 的單日基準就
少了資料。
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

#: 全歷史 view 的名字。SQL 裡直接寫字面量（跟表名一樣可 grep）。
HISTORY = "observations_all"

ARCHIVE_DIRNAME = "archive"
FILE_FMT = "observations-{:03d}.db"

#: 出發日要過多久才封存。理論上 0 就不影響任何視窗查詢（見模組說明），
#: 多留 30 天是保險：時區、時鐘偏移、晚到的回程比對都不會踩到邊界。
#: 代價只是現行檔多放約一個月的資料（約 4 MiB）。
MARGIN_DAYS = 30

#: 封存檔滿這個大小就換新檔。GitHub 50 MiB 起會出警告、100 MiB 拒收；
#: 40 MiB 離警告線還有餘裕，離拒收線一倍以上。
ROLL_BYTES = 40 * 1024 * 1024

#: 也宣告在 storage.SCHEMA 裡（不藏在方法內，long_range 的教訓）。
MANIFEST_DDL = """
CREATE TABLE IF NOT EXISTS archive_manifest (
    file       TEXT PRIMARY KEY,
    rows       INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_CREATE_RE = re.compile(r'^\s*CREATE\s+TABLE\s+"?observations"?', re.IGNORECASE)


class ArchiveIntegrityError(RuntimeError):
    """封存檔缺檔、筆數對不上、或搬移前後指紋不符。

    刻意拋錯而不是默默少算：全歷史變小會讓 new_low（航線史上最低）誤發通知。
    這是 repo 檔案不完整，不是偶發狀況，寧可讓 workflow 變紅。
    """


# ---- 共用小工具 ---------------------------------------------------------------

def archive_dir(db_path: str) -> Path:
    """封存目錄：跟資料庫檔放在同一層的 archive/。"""
    return Path(db_path).resolve().parent / ARCHIVE_DIRNAME


def _main_path(conn: sqlite3.Connection) -> str:
    """conn 的主資料庫檔路徑（記憶體資料庫回空字串）。"""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return row[2] or ""
    return ""


def _manifest(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    exists = conn.execute(
        "SELECT 1 FROM main.sqlite_master WHERE type='table' AND name='archive_manifest'"
    ).fetchone()
    if not exists:
        return []
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT file, rows FROM main.archive_manifest ORDER BY file")]


def _columns(conn: sqlite3.Connection, schema: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info(observations)")]


def _attached(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA database_list")}


def _attach_entries(conn: sqlite3.Connection, base: Path,
                    entries: list[tuple[str, int]]) -> list[str]:
    """掛上 manifest 列出的每個封存檔並核對筆數。回傳 schema 別名清單。"""
    aliases = []
    attached = _attached(conn)
    for i, (fname, rows) in enumerate(entries):
        path = base / fname
        if not path.is_file():
            raise ArchiveIntegrityError(
                f"封存檔 {path} 不見了（manifest 記 {rows} 筆）。不要重跑封存；"
                "照 docs/RUNBOOK_HOOK.md §3b 從 git 歷史還原 archive/")
        alias = f"arc{i}"
        if alias not in attached:
            if conn.in_transaction:
                raise RuntimeError("archive.attach() 必須在交易之外呼叫（ATTACH 的限制）")
            conn.execute(f"ATTACH DATABASE ? AS {alias}", (str(path),))
        actual = conn.execute(f"SELECT COUNT(*) FROM {alias}.observations").fetchone()[0]
        if actual != rows:
            raise ArchiveIntegrityError(
                f"封存檔 {path} 有 {actual} 筆，manifest 記 {rows} 筆。不要重跑封存；"
                "照 docs/RUNBOOK_HOOK.md §3b 從 git 歷史還原 archive/")
        aliases.append(alias)
    return aliases


def _union_sql(conn: sqlite3.Connection, aliases: list[str], cols: list[str]) -> str:
    """main.observations ∪ 各封存檔，欄位以 main 為準。

    封存檔缺少的欄位（封存之後 main 才 migration 新增的）補 NULL——跟 main
    裡 migration 前的舊列讀出來一樣，語意一致。
    """
    selects = [f"SELECT {', '.join(cols)} FROM main.observations"]
    for alias in aliases:
        have = set(_columns(conn, alias))
        sel = ", ".join(c if c in have else f"NULL AS {c}" for c in cols)
        selects.append(f"SELECT {sel} FROM {alias}.observations")
    return " UNION ALL ".join(selects)


# ---- 讀：全歷史 view ---------------------------------------------------------

def attach(conn: sqlite3.Connection) -> int:
    """確保 conn 上有 TEMP VIEW observations_all（現行＋封存）。回傳封存檔數。

    冪等：同一條連線重複呼叫只會建一次。沒有 manifest（還沒封存過、或測試
    用的新資料庫）時，view 就等於 main.observations，行為與封存前完全相同。

    封存檔的位置由 conn 自己的主檔路徑推出（PRAGMA database_list），所以
    任何拿到連線的地方都能呼叫，不必另外傳路徑。
    """
    if conn.execute("SELECT 1 FROM sqlite_temp_master WHERE type='view' AND name=?",
                    (HISTORY,)).fetchone():
        return len(_manifest(conn))
    entries = _manifest(conn)
    aliases = _attach_entries(conn, archive_dir(_main_path(conn)), entries) \
        if entries else []
    union = _union_sql(conn, aliases, _columns(conn, "main"))
    conn.execute(f"CREATE TEMP VIEW {HISTORY} AS {union}")
    return len(aliases)


# ---- 寫：搬移 ------------------------------------------------------------------

def _fingerprint(conn: sqlite3.Connection, aliases: list[str]) -> list[tuple]:
    """全歷史指紋：每航線×艙等的筆數／最低／最高／價格總和／id 總和，外加
    總筆數與相異 id 數。價格以「分」取整後加總，避免浮點加總順序不同造成
    假的不一致（搬移會改變 UNION ALL 的列順序）。"""
    union = _union_sql(conn, aliases, ["id", "origin", "destination",
                                       "fare_class", "price"])
    per_route = conn.execute(
        f"""SELECT origin, destination, fare_class, COUNT(*), MIN(price), MAX(price),
                   SUM(CAST(ROUND(price * 100) AS INTEGER)), SUM(id)
            FROM ({union}) GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""").fetchall()
    totals = conn.execute(
        f"SELECT COUNT(*), COUNT(DISTINCT id) FROM ({union})").fetchone()
    return [tuple(r) for r in per_route] + [tuple(totals)]


def _next_file_name(entries: list[tuple[str, int]]) -> str:
    nums = [int(m.group(1)) for f, _ in entries
            if (m := re.fullmatch(r"observations-(\d+)\.db", f))]
    return FILE_FMT.format(max(nums, default=0) + 1)


def _create_archive_table(conn: sqlite3.Connection, alias: str) -> None:
    """在封存檔建一張跟 main.observations 同結構的表與同一個索引。"""
    sql = conn.execute("SELECT sql FROM main.sqlite_master "
                       "WHERE type='table' AND name='observations'").fetchone()[0]
    if not _CREATE_RE.match(sql):
        raise RuntimeError(f"無法辨識 observations 的建表語法：{sql[:60]!r}")
    conn.execute(_CREATE_RE.sub(f"CREATE TABLE {alias}.observations", sql, count=1))
    conn.execute(f"CREATE INDEX {alias}.idx_obs_route "
                 "ON observations (origin, destination, depart_date)")


def archive_old(db_path: str = "prices.db", now: datetime | None = None,
                margin_days: int = MARGIN_DAYS,
                roll_bytes: int = ROLL_BYTES) -> dict:
    """把 depart_date 早於（now − margin_days）的觀測從 db_path 搬到封存檔。

    整個搬移在單一交易內完成（SQLite 對 ATTACH 的多個檔案做原子提交），提交
    前比對搬移前後的全歷史指紋，任何一項不同就 rollback 並拋錯——所以結果
    只有「搬好」或「完全沒動」兩種。

    冪等：沒有符合條件的列時什麼都不寫，回 moved=0。

    第一次封存（manifest 原本是空的）結束後 VACUUM 一次，把主檔縮小；之後
    不再 VACUUM——刪掉的頁會被新觀測重複使用，檔案停在穩定大小，也避免
    每次整檔重寫讓 git 差異變大。
    """
    now = now or datetime.now(timezone.utc)   # 可注入，供測試固定時鐘
    cutoff = (now.date() - timedelta(days=margin_days)).isoformat()
    base = archive_dir(db_path)
    conn = sqlite3.connect(db_path, isolation_level=None)   # 交易自己管
    created: Path | None = None
    try:
        conn.executescript(MANIFEST_DDL)
        entries = _manifest(conn)
        aliases = _attach_entries(conn, base, entries)       # 順便核對既有封存檔
        n = conn.execute("SELECT COUNT(*) FROM main.observations WHERE depart_date < ?",
                         (cutoff,)).fetchone()[0]
        summary = {"cutoff": cutoff, "moved": n, "file": None, "vacuumed": False}
        if n == 0:
            return _finish(summary, conn, db_path, base)

        last = base / entries[-1][0] if entries else None
        if last is not None and last.stat().st_size < roll_bytes:
            target_name, target_alias = entries[-1][0], aliases[-1]
            check_aliases = aliases
        else:
            target_name = _next_file_name(entries)
            target_path = base / target_name
            if target_path.exists():
                raise ArchiveIntegrityError(
                    f"封存檔 {target_path} 已存在但不在 manifest 裡；請人工確認來源再處理")
            base.mkdir(parents=True, exist_ok=True)
            target_alias = f"arc{len(entries)}"
            conn.execute(f"ATTACH DATABASE ? AS {target_alias}", (str(target_path),))
            created = target_path
            _create_archive_table(conn, target_alias)
            check_aliases = aliases + [target_alias]
        summary["file"] = target_name

        cols = _columns(conn, "main")
        have = set(_columns(conn, target_alias))
        for c in cols:                     # main 後來 migration 新增的欄位補進封存檔
            if c not in have:
                ctype = {r[1]: r[2] for r in conn.execute(
                    "PRAGMA main.table_info(observations)")}[c]
                conn.execute(f"ALTER TABLE {target_alias}.observations "
                             f"ADD COLUMN {c} {ctype}")
        col_list = ", ".join(cols)

        before = _fingerprint(conn, check_aliases)
        conn.execute("BEGIN IMMEDIATE")
        try:
            moved_in = conn.execute(
                f"INSERT INTO {target_alias}.observations ({col_list}) "
                f"SELECT {col_list} FROM main.observations WHERE depart_date < ?",
                (cutoff,)).rowcount
            moved_out = conn.execute(
                "DELETE FROM main.observations WHERE depart_date < ?",
                (cutoff,)).rowcount
            total = conn.execute(
                f"SELECT COUNT(*) FROM {target_alias}.observations").fetchone()[0]
            conn.execute(
                """INSERT INTO main.archive_manifest (file, rows, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(file) DO UPDATE SET
                     rows=excluded.rows, updated_at=excluded.updated_at""",
                (target_name, total, now.isoformat(timespec="seconds")))
            after = _fingerprint(conn, check_aliases)
            if moved_in != n or moved_out != n or after != before:
                raise ArchiveIntegrityError(
                    f"搬移前後不一致（預計 {n}、寫入 {moved_in}、刪除 {moved_out}，"
                    f"指紋{'相同' if after == before else '不同'}）；已 rollback，未做任何改動")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        created = None                     # 已提交，新檔留著

        if not entries:
            conn.execute("VACUUM main")
            summary["vacuumed"] = True
        return _finish(summary, conn, db_path, base)
    finally:
        conn.close()
        if created is not None and created.exists():
            created.unlink()               # 失敗時不留下沒進 manifest 的空殼


def _finish(summary: dict, conn: sqlite3.Connection, db_path: str, base: Path) -> dict:
    entries = _manifest(conn)
    summary.update({
        "live_rows": conn.execute("SELECT COUNT(*) FROM main.observations").fetchone()[0],
        "live_bytes": os.path.getsize(db_path),
        "archives": [{"file": f, "rows": r, "bytes": (base / f).stat().st_size}
                     for f, r in entries],
    })
    return summary


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = archive_old(argv[0] if argv else "prices.db")
    mib = lambda b: b / 1048576
    files = "、".join(f"{a['file']} {a['rows']} 筆 {mib(a['bytes']):.1f} MiB"
                     for a in s["archives"]) or "（無）"
    print(f"封存完成：出發日 < {s['cutoff']} 的 {s['moved']} 筆"
          + (f" → {s['file']}" if s["file"] else "（沒有需要搬的）")
          + ("；已 VACUUM" if s["vacuumed"] else ""))
    print(f"現行檔 {s['live_rows']} 筆 {mib(s['live_bytes']):.1f} MiB；封存檔：{files}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
