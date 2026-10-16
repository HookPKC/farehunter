"""prices.db 封存（archive.py）。

最重要的是 test_outputs_identical_after_archive：封存前後，看板（data.json）、
new_low / big_drop 的統計、FIE 的歷史錨點必須完全相同。這條是「封存不改任何
語意」的證明（ADR 0002），不是實作細節——它紅了就代表通知或首頁數字會變。
"""
import random
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from farehunter import archive, health
from farehunter.archive import ArchiveIntegrityError, archive_old
from farehunter.storage import Store

#: 封存只吃注入的 now、不碰 SQL 的 'now'，所以機制測試可以用固定時間。
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)

ROUTES = (("TPE", "NRT"), ("KHH", "OKA"))


def _seed(db, now, n=600, seed=7, routes=ROUTES):
    """出發日落在 now−150～now+200 天，觀測時間一律早於出發日且不晚於 now
    （真實資料的樣子：沒有人能在出發日之後觀測到那天的票價）。"""
    rng = random.Random(seed)
    store = Store(str(db))
    for i in range(n):
        o, d = routes[i % len(routes)]
        dep = now.date() + timedelta(days=rng.randint(-150, 200))
        dep_dt = datetime(dep.year, dep.month, dep.day, tzinfo=timezone.utc)
        obs = min(dep_dt - timedelta(days=rng.randint(1, 120)),
                  now - timedelta(hours=rng.randint(1, 72)))
        price = rng.randint(5000, 20000) + rng.choice((0, 0, 0.5))
        store.conn.execute(
            """INSERT INTO observations (origin, destination, depart_date, return_date,
                 price, currency, carriers, stops, duration, observed_at, fare_class,
                 source, provider) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (o, d, dep.isoformat(), (dep + timedelta(days=5)).isoformat(), price,
             "TWD", rng.choice(("IT", "JX", "CI", "")), rng.choice((0, 0, 1)), "185",
             obs.isoformat(timespec="seconds"), rng.choice(("any", "any", "full")),
             rng.choice(("aviasales", "aviasales", "google")), "travelpayouts"))
    store.conn.commit()
    store.close()


def _count(db, table="observations"):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _ids(db):
    conn = sqlite3.connect(str(db))
    archive.attach(conn)
    try:
        return sorted(r[0] for r in conn.execute("SELECT id FROM observations_all"))
    finally:
        conn.close()


# ---- 讀：沒有封存時 ---------------------------------------------------------

def test_without_archive_view_is_the_live_table(tmp_path):
    db = tmp_path / "prices.db"
    _seed(db, NOW, n=50)
    conn = sqlite3.connect(str(db))
    assert archive.attach(conn) == 0
    assert archive.attach(conn) == 0                     # 冪等
    assert conn.execute("SELECT COUNT(*) FROM observations_all").fetchone()[0] == 50
    conn.close()
    assert not (tmp_path / "archive").exists()


# ---- 核心保證：封存不改任何輸出 ----------------------------------------------

def test_outputs_identical_after_archive(tmp_path):
    from farehunter.export_web import export
    from farehunter.intelligence import build_ranked

    # export 的視窗查詢用 SQLite 的真實 date('now')，所以這條用真實時鐘建資料；
    # 兩邊在同一刻比較，結果與執行日期無關。
    now = datetime.now(timezone.utc).replace(microsecond=0)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a, b = tmp_path / "a" / "prices.db", tmp_path / "b" / "prices.db"
    _seed(a, now, n=1500)
    shutil.copy(a, b)

    s = archive_old(str(b), now=now)
    assert s["moved"] > 100 and _count(b) < _count(a)    # 真的有搬

    def snapshot(db):
        data = export(str(db), str(db) + ".json", now=now,
                      config_path=str(tmp_path / "no-config.yaml"))
        ranked = build_ranked(str(db))
        st = Store(str(db))
        today = now.date().isoformat()
        stats = {(o, d): (st.route_stats(o, d),
                          {k: v for k, v in st.route_stats_by_date(o, d).items()
                           if k >= today})
                 for o, d in ROUTES}
        st.close()
        anchors = {r["route"]: (r["route_stats"], r["n_offers"]) for r in ranked["routes"]}
        return data, stats, anchors

    data_a, stats_a, anchors_a = snapshot(a)
    data_b, stats_b, anchors_b = snapshot(b)
    assert data_b == data_a              # 看板：統計、趨勢圖、總筆數、健康、日曆
    assert stats_b == stats_a            # new_low（整條航線）與 big_drop（未來出發日）
    assert anchors_b == anchors_a        # FIE 的歷史錨點
    assert data_a["totals"]["observations"] == 1500


# ---- 寫：搬移 ----------------------------------------------------------------

def test_moves_only_rows_past_cutoff_and_keeps_ids(tmp_path):
    db = tmp_path / "prices.db"
    _seed(db, NOW)
    ids_before = _ids(db)
    cutoff = (NOW.date() - timedelta(days=archive.MARGIN_DAYS)).isoformat()

    s = archive_old(str(db), now=NOW)

    assert s["cutoff"] == cutoff and s["file"] == "observations-001.db"
    arc = tmp_path / "archive" / "observations-001.db"
    conn = sqlite3.connect(str(arc))
    assert conn.execute("SELECT COUNT(*) FROM observations WHERE depart_date >= ?",
                        (cutoff,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == s["moved"]
    conn.close()
    conn = sqlite3.connect(str(db))
    assert conn.execute("SELECT COUNT(*) FROM observations WHERE depart_date < ?",
                        (cutoff,)).fetchone()[0] == 0
    assert conn.execute("SELECT file, rows FROM archive_manifest").fetchall() == \
        [("observations-001.db", s["moved"])]
    conn.close()
    assert _ids(db) == ids_before        # 不多、不少、id 不變


def test_rerun_moves_nothing(tmp_path):
    db = tmp_path / "prices.db"
    _seed(db, NOW)
    first = archive_old(str(db), now=NOW)
    second = archive_old(str(db), now=NOW)
    assert first["moved"] > 0 and second["moved"] == 0
    assert second["archives"] == [{"file": "observations-001.db", "rows": first["moved"],
                                   "bytes": first["archives"][0]["bytes"]}]


def test_only_first_archive_vacuums(tmp_path):
    db = tmp_path / "prices.db"
    _seed(db, NOW, n=3000)
    size_before = db.stat().st_size
    first = archive_old(str(db), now=NOW - timedelta(days=60))
    assert first["vacuumed"] and db.stat().st_size < size_before
    second = archive_old(str(db), now=NOW)
    assert second["moved"] > 0 and not second["vacuumed"]


def test_rolls_to_new_file_when_current_is_full(tmp_path):
    db = tmp_path / "prices.db"
    _seed(db, NOW)
    total = _count(db)
    first = archive_old(str(db), now=NOW - timedelta(days=60), roll_bytes=1)
    second = archive_old(str(db), now=NOW, roll_bytes=1)
    assert (first["file"], second["file"]) == ("observations-001.db", "observations-002.db")
    assert [a["rows"] for a in second["archives"]] == [first["moved"], second["moved"]]
    conn = sqlite3.connect(str(db))
    assert archive.attach(conn) == 2
    assert conn.execute("SELECT COUNT(*) FROM observations_all").fetchone()[0] == total
    conn.close()


def test_failed_move_changes_nothing(tmp_path, monkeypatch):
    db = tmp_path / "prices.db"
    _seed(db, NOW)
    before = _count(db)
    real = archive._fingerprint
    calls = []

    def drifting(conn, aliases):            # 第二次（搬移後）回不同的指紋
        calls.append(1)
        fp = real(conn, aliases)
        return fp if len(calls) == 1 else fp + [("drift",)]

    monkeypatch.setattr(archive, "_fingerprint", drifting)
    with pytest.raises(ArchiveIntegrityError, match="rollback"):
        archive_old(str(db), now=NOW)
    assert _count(db) == before
    assert _count(db, "archive_manifest") == 0
    assert not (tmp_path / "archive" / "observations-001.db").exists()


# ---- 完整性：封存檔不見或被改動，一律報錯，絕不默默少算 ------------------

def test_missing_archive_file_raises_everywhere(tmp_path):
    from farehunter.export_web import export
    db = tmp_path / "prices.db"
    _seed(db, NOW)
    archive_old(str(db), now=NOW)
    (tmp_path / "archive" / "observations-001.db").unlink()

    store = Store(str(db))
    with pytest.raises(ArchiveIntegrityError, match="不見了"):
        store.route_stats("TPE", "NRT")              # new_low 的基準
    store.close()
    with pytest.raises(ArchiveIntegrityError):
        export(str(db), str(tmp_path / "d.json"), now=NOW)
    with pytest.raises(ArchiveIntegrityError):
        archive_old(str(db), now=NOW)


def test_archive_row_count_mismatch_raises(tmp_path):
    db = tmp_path / "prices.db"
    _seed(db, NOW)
    archive_old(str(db), now=NOW)
    arc = sqlite3.connect(str(tmp_path / "archive" / "observations-001.db"))
    arc.execute("DELETE FROM observations WHERE id = (SELECT MIN(id) FROM observations)")
    arc.commit()
    arc.close()
    conn = sqlite3.connect(str(db))
    with pytest.raises(ArchiveIntegrityError, match="manifest 記"):
        archive.attach(conn)
    conn.close()


def test_unlisted_file_in_the_way_raises(tmp_path):
    db = tmp_path / "prices.db"
    _seed(db, NOW)
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "observations-001.db").write_bytes(b"")
    with pytest.raises(ArchiveIntegrityError, match="不在 manifest"):
        archive_old(str(db), now=NOW)


# ---- 欄位演進與其他讀者 -----------------------------------------------------

def test_column_added_after_archiving(tmp_path):
    """main 日後 migration 新增欄位：舊封存列讀出 NULL，下次封存把欄位補進封存檔。"""
    db = tmp_path / "prices.db"
    _seed(db, NOW)
    first = archive_old(str(db), now=NOW - timedelta(days=60))
    conn = sqlite3.connect(str(db))
    conn.execute("ALTER TABLE observations ADD COLUMN cabin TEXT")
    conn.commit()
    archive.attach(conn)
    assert conn.execute("SELECT COUNT(*) FROM observations_all WHERE cabin IS NULL"
                        ).fetchone()[0] == _count(db) + first["moved"]
    conn.close()

    second = archive_old(str(db), now=NOW)
    assert second["moved"] > 0 and second["file"] == "observations-001.db"
    arc = sqlite3.connect(str(tmp_path / "archive" / "observations-001.db"))
    assert "cabin" in [r[1] for r in arc.execute("PRAGMA table_info(observations)")]
    arc.close()


def test_health_still_sees_a_route_whose_rows_were_all_archived(tmp_path):
    """斷線很久的航線，現行檔可能一筆不剩；健康報告要說「斷線 N 天」，
    不能變成「從未觀測」。"""
    db = tmp_path / "prices.db"
    _seed(db, NOW, n=40, routes=(("KHH", "NGO"),))
    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM observations WHERE depart_date >= ?",
                 ((NOW.date() - timedelta(days=archive.MARGIN_DAYS)).isoformat(),))
    conn.commit()
    last = conn.execute("SELECT MAX(observed_at) FROM observations").fetchone()[0]
    conn.close()
    archive_old(str(db), now=NOW)
    assert _count(db) == 0

    conn = sqlite3.connect(str(db))
    block = health.build_health(conn, [("KHH", "NGO")], NOW)
    conn.close()
    assert block["routes"][0]["last_observed_at"] == last
    assert block["routes"][0]["status"] == health.DEAD
