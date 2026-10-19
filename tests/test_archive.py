"""prices.db 封存（archive.py）。

最重要的是 test_outputs_identical_after_archive：封存前後，看板（data.json）、
new_low / big_drop 的統計、FIE 的歷史錨點必須完全相同。這條是「封存不改任何
語意」的證明（ADR 0002），不是實作細節——它紅了就代表通知或首頁數字會變。
"""
import hashlib
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


def _seed(db, now, n=600, seed=7, routes=ROUTES, dep_days=(-150, 200)):
    """出發日落在 now + dep_days 天之間，觀測時間一律早於出發日且不晚於 now
    （真實資料的樣子：沒有人能在出發日之後觀測到那天的票價）。"""
    rng = random.Random(seed)
    store = Store(str(db))
    for i in range(n):
        o, d = routes[i % len(routes)]
        dep = now.date() + timedelta(days=rng.randint(*dep_days))
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


def _manifest_matches_files(db):
    """manifest 記的筆數＝封存檔實際筆數（每次附加之後都要成立）。"""
    conn = sqlite3.connect(str(db))
    rows = conn.execute("SELECT file, rows FROM archive_manifest").fetchall()
    conn.close()
    for f, n in rows:
        assert _count(db.parent / "archive" / f) == n, f
    return rows


def _md5(path):
    return hashlib.md5(path.read_bytes()).hexdigest()


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
    # 第三條航線的出發日全部早於門檻：封存後現行檔一筆不剩，它仍必須留在
    # 首頁的航線清單、統計與趨勢圖裡。
    _seed(a, now, n=60, seed=11, routes=(("KHH", "CTS"),),
          dep_days=(-150, -archive.MARGIN_DAYS - 5))
    shutil.copy(a, b)

    s = archive_old(str(b), now=now)
    assert s["moved"] > 100 and _count(b) < _count(a)    # 真的有搬
    conn = sqlite3.connect(str(b))
    assert conn.execute("SELECT COUNT(*) FROM observations WHERE origin='KHH' "
                        "AND destination='CTS'").fetchone()[0] == 0
    conn.close()

    def snapshot(db):
        data = export(str(db), str(db) + ".json", now=now,
                      config_path=str(tmp_path / "no-config.yaml"))
        ranked = build_ranked(str(db))
        st = Store(str(db))
        today = now.date().isoformat()
        stats = {(o, d): (st.route_stats(o, d),
                          {k: v for k, v in st.route_stats_by_date(o, d).items()
                           if k >= today})
                 for o, d in ROUTES + (("KHH", "CTS"),)}
        st.close()
        anchors = {r["route"]: (r["route_stats"], r["n_offers"]) for r in ranked["routes"]}
        return data, stats, anchors

    # 兩次快照之間若剛好跨過 UTC 午夜，SQL 的 date('now') 視窗會差一天——
    # monitor 是「測試沒過就不抓價」，偶發紅燈等於漏一小時，所以跨日就重拍一次。
    for _ in range(2):
        day = datetime.now(timezone.utc).date()
        snap_a, snap_b = snapshot(a), snapshot(b)
        if datetime.now(timezone.utc).date() == day:
            break
    (data_a, stats_a, anchors_a), (data_b, stats_b, anchors_b) = snap_a, snap_b
    assert data_b == data_a              # 看板：航線清單、統計、趨勢圖、總筆數、健康、日曆
    assert stats_b == stats_a            # new_low（整條航線）與 big_drop（未來出發日）
    assert anchors_b == anchors_a        # FIE 的歷史錨點
    assert data_a["totals"]["observations"] == 1560
    assert "KHH-CTS" in {f"{r['origin']}-{r['destination']}" for r in data_b["routes"]}


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
    conn.close()
    assert _manifest_matches_files(db) == [("observations-001.db", s["moved"])]
    assert _ids(db) == ids_before        # 不多、不少、id 不變


def test_margin_keeps_rows_inside_the_30_day_window(tmp_path):
    """MARGIN_DAYS 是下限（export 的 route_common 回看 30 天觀測），不是保險。"""
    assert archive.MARGIN_DAYS >= 30
    db = tmp_path / "prices.db"
    store = Store(str(db))
    for days_ago in (29, 30, 31):
        dep = (NOW.date() - timedelta(days=days_ago)).isoformat()
        store.conn.execute(
            "INSERT INTO observations (origin, destination, depart_date, price, currency,"
            " observed_at) VALUES ('TPE','NRT',?,9000,'TWD',?)",
            (dep, (NOW - timedelta(days=days_ago + 3)).isoformat(timespec="seconds")))
    store.conn.commit()
    store.close()
    s = archive_old(str(db), now=NOW)
    assert s["moved"] == 1                                # 只有 31 天前那筆
    assert _count(db) == 2


def test_rerun_with_nothing_to_move_writes_nothing(tmp_path):
    db = tmp_path / "prices.db"
    _seed(db, NOW)
    first = archive_old(str(db), now=NOW)
    arc = tmp_path / "archive" / "observations-001.db"
    before = (_md5(db), _md5(arc))
    second = archive_old(str(db), now=NOW)
    assert first["moved"] > 0 and second["moved"] == 0 and second["file"] is None
    assert (_md5(db), _md5(arc)) == before              # 不會產生空 commit


def test_vacuum_policy_first_always_then_only_when_wasteful(tmp_path):
    """不 VACUUM 的話現行檔照樣長（新列接在 B-tree 最右邊，舊列散在各頁，
    騰出的空間幾乎不會被重用）；每次都 VACUUM 又讓 git 差異變大。"""
    db = tmp_path / "prices.db"
    _seed(db, NOW, n=3000)
    ids_before = _ids(db)
    size_before = db.stat().st_size

    first = archive_old(str(db), now=NOW - timedelta(days=90))
    assert first["vacuumed"] and first["compact_bytes"] is None
    assert db.stat().st_size < size_before

    lazy = archive_old(str(db), now=NOW - timedelta(days=45), vacuum_waste_ratio=1e9)
    assert lazy["moved"] > 0 and not lazy["vacuumed"]
    assert lazy["live_bytes"] > lazy["compact_bytes"]    # 搬走的列留下了空洞

    eager = archive_old(str(db), now=NOW, vacuum_waste_ratio=0.0)
    assert eager["moved"] > 0 and eager["vacuumed"]
    assert eager["live_bytes"] <= eager["compact_bytes"]

    # 兩次附加到同一個封存檔之後，manifest 與檔案仍一致、全歷史一筆不差
    assert len(_manifest_matches_files(db)) == 1
    assert _ids(db) == ids_before


def test_rolls_to_new_file_when_current_is_full(tmp_path):
    db = tmp_path / "prices.db"
    _seed(db, NOW)
    ids_before = _ids(db)
    first = archive_old(str(db), now=NOW - timedelta(days=60), roll_bytes=1)
    second = archive_old(str(db), now=NOW, roll_bytes=1, vacuum_waste_ratio=1e9)
    assert (first["file"], second["file"]) == ("observations-001.db", "observations-002.db")
    assert not second["vacuumed"]                         # 換新檔不等於「第一次封存」
    assert [a["rows"] for a in second["archives"]] == [first["moved"], second["moved"]]
    assert len(_manifest_matches_files(db)) == 2
    conn = sqlite3.connect(str(db))
    assert archive.attach(conn) == 2
    conn.close()
    assert _ids(db) == ids_before


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


def test_failed_append_leaves_existing_archive_byte_identical(tmp_path, monkeypatch):
    """補欄位（ALTER）也在交易內：rollback 之後封存檔連 schema 都沒變。"""
    db = tmp_path / "prices.db"
    _seed(db, NOW)
    archive_old(str(db), now=NOW - timedelta(days=60))
    arc = tmp_path / "archive" / "observations-001.db"
    conn = sqlite3.connect(str(db))
    conn.execute("ALTER TABLE observations ADD COLUMN cabin TEXT")
    conn.commit()
    conn.close()
    before = (_md5(db), _md5(arc))
    real = archive._fingerprint
    calls = []
    monkeypatch.setattr(archive, "_fingerprint", lambda c, a: (
        calls.append(1), real(c, a) + ([("drift",)] if len(calls) > 1 else []))[1])
    with pytest.raises(ArchiveIntegrityError, match="rollback"):
        archive_old(str(db), now=NOW)
    assert (_md5(db), _md5(arc)) == before


def test_fingerprint_catches_altered_prices(tmp_path, monkeypatch):
    """指紋不只數筆數：搬移途中價格被改，也要擋下來。"""
    db = tmp_path / "prices.db"
    _seed(db, NOW)
    before = _count(db)
    real = archive._create_archive_table

    def corrupting(conn, alias):
        real(conn, alias)
        conn.execute(f"CREATE TRIGGER {alias}.corrupt AFTER INSERT ON observations "
                     "WHEN NEW.id % 7 = 0 BEGIN "
                     "UPDATE observations SET price = price + 1 WHERE id = NEW.id; END")

    monkeypatch.setattr(archive, "_create_archive_table", corrupting)
    with pytest.raises(ArchiveIntegrityError, match="指紋不同"):
        archive_old(str(db), now=NOW)
    assert _count(db) == before
    assert not (tmp_path / "archive" / "observations-001.db").exists()


# ---- 完整性：封存檔不見或被改動，一律報錯，絕不默默少算 ------------------

def test_missing_archive_file_raises_everywhere(tmp_path):
    from farehunter.export_web import export
    from farehunter.intelligence import build_ranked
    # build_ranked 只替「未來出發日」窗內有報價的航線算錨點，窗用的是 SQLite 的
    # 真實 date('now')——資料必須相對真實時鐘建，否則日子一久窗內沒報價、根本
    # 走不到核對（time_travel_tests 在 2027-04 抓到過）。
    now = datetime.now(timezone.utc).replace(microsecond=0)
    db = tmp_path / "prices.db"
    _seed(db, now)
    archive_old(str(db), now=now)
    (tmp_path / "archive" / "observations-001.db").unlink()

    # Store 一開檔就核對：writer 在打付費 API 之前就停下
    with pytest.raises(ArchiveIntegrityError, match="不見了.*一起複製"):
        Store(str(db))
    with pytest.raises(ArchiveIntegrityError):
        export(str(db), str(tmp_path / "d.json"), now=now)
    with pytest.raises(ArchiveIntegrityError):
        build_ranked(str(db))                            # FIE 的歷史錨點
    with pytest.raises(ArchiveIntegrityError):
        archive_old(str(db), now=now)


@pytest.mark.parametrize("damage", [b"", b"\x00garbage" * 500], ids=["empty", "garbage"])
def test_damaged_archive_file_raises_integrity_error(tmp_path, damage):
    """封存檔被換成空檔或亂碼（合併失誤、編輯器改寫……）也走同一條路：
    ArchiveIntegrityError，RUNBOOK §3b 只要認這一個名字。"""
    db = tmp_path / "prices.db"
    _seed(db, NOW)
    archive_old(str(db), now=NOW)
    (tmp_path / "archive" / "observations-001.db").write_bytes(damage)
    with pytest.raises(ArchiveIntegrityError, match="無法讀取"):
        Store(str(db))


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


def test_unlisted_file_in_the_way_is_refused_and_kept(tmp_path):
    db = tmp_path / "prices.db"
    _seed(db, NOW)
    other = tmp_path / "other.db"
    _seed(other, NOW, n=30, seed=3)
    (tmp_path / "archive").mkdir()
    in_the_way = tmp_path / "archive" / "observations-001.db"
    shutil.copy(other, in_the_way)
    before = _md5(in_the_way)
    with pytest.raises(ArchiveIntegrityError, match="不在 manifest"):
        archive_old(str(db), now=NOW)
    assert _md5(in_the_way) == before                   # 不是自己建的，絕不刪
    assert _count(db) == 600


def test_missing_db_path_creates_nothing(tmp_path):
    with pytest.raises(FileNotFoundError):
        archive_old(str(tmp_path / "price.db"), now=NOW)
    assert list(tmp_path.iterdir()) == []


# ---- 欄位演進與其他讀者 -----------------------------------------------------

def test_column_added_without_default_reads_null(tmp_path):
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


def test_column_added_with_default_reads_the_default(tmp_path):
    """fare_class 當年就是 ADD COLUMN ... DEFAULT 'any'，全歷史查詢都濾它。
    封存列若讀成 NULL，會悄悄掉出統計，筆數卻對得上、不會報錯。"""
    db = tmp_path / "prices.db"
    _seed(db, NOW)
    total = _count(db)
    archive_old(str(db), now=NOW - timedelta(days=60))
    conn = sqlite3.connect(str(db))
    conn.execute("ALTER TABLE observations ADD COLUMN cabin TEXT NOT NULL DEFAULT 'Y'")
    conn.commit()
    archive.attach(conn)
    assert conn.execute("SELECT COUNT(*) FROM observations_all WHERE cabin='Y'"
                        ).fetchone()[0] == total
    conn.close()

    second = archive_old(str(db), now=NOW)               # 附加時把欄位補進封存檔
    assert second["moved"] > 0 and second["file"] == "observations-001.db"
    arc = sqlite3.connect(str(tmp_path / "archive" / "observations-001.db"))
    col = [r for r in arc.execute("PRAGMA table_info(observations)") if r[1] == "cabin"][0]
    assert (col[3], col[4]) == (1, "'Y'")                # NOT NULL DEFAULT 'Y'
    arc.close()
    conn = sqlite3.connect(str(db))
    archive.attach(conn)
    assert conn.execute("SELECT COUNT(*) FROM observations_all WHERE cabin='Y'"
                        ).fetchone()[0] == total
    conn.close()


def test_migration_reaches_every_archive_file(tmp_path):
    """已經滿了、不再寫入的舊封存檔也要補欄位——view 補值只是過渡。"""
    db = tmp_path / "prices.db"
    _seed(db, NOW)
    archive_old(str(db), now=NOW - timedelta(days=90), roll_bytes=1)
    archive_old(str(db), now=NOW - timedelta(days=60), roll_bytes=1)
    conn = sqlite3.connect(str(db))
    conn.execute("ALTER TABLE observations ADD COLUMN cabin TEXT DEFAULT 'Y'")
    conn.commit()
    conn.close()
    third = archive_old(str(db), now=NOW)
    assert third["moved"] > 0 and third["file"] == "observations-002.db"
    for f in ("observations-001.db", "observations-002.db"):
        arc = sqlite3.connect(str(tmp_path / "archive" / f))
        cols = {r[1]: r[4] for r in arc.execute("PRAGMA table_info(observations)")}
        arc.close()
        assert cols.get("cabin") == "'Y'", f


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


# ---- 常數與 CLI（archive-db.yml 實際跑的入口） ------------------------------

def test_roll_size_stays_below_github_warning():
    assert archive.ROLL_BYTES <= 50 * 1024 * 1024        # GitHub 50 MiB 起出警告


def test_cli_archives_the_given_path(tmp_path, capsys):
    db = tmp_path / "prices.db"
    _seed(db, NOW)
    assert archive.main([str(db)]) == 0
    out = capsys.readouterr().out
    assert "封存完成" in out and "observations-001.db" in out and "現行檔" in out
    assert len(_manifest_matches_files(db)) == 1


def test_ten_archive_files_fit_and_the_eleventh_is_refused_cleanly(tmp_path):
    """SQLite 一次最多 ATTACH 10 個檔。VACUUM／VACUUM INTO 在內部各要佔一個名額，
    所以壓實前一定要先卸下封存檔，否則第 10 個檔那天就撞上限（實測過）。
    第 11 個在建檔時就被拒絕，什麼都不動、不留空殼。"""
    db = tmp_path / "prices.db"
    _seed(db, NOW, n=1200, dep_days=(-340, 0))
    start = NOW - timedelta(days=340 - archive.MARGIN_DAYS)
    for k in range(1, 11):
        s = archive_old(str(db), now=start + timedelta(days=31 * k), roll_bytes=1)
        assert s["moved"] > 0 and s["file"] == f"observations-{k:03d}.db", k
        # 壓實失敗只會記警告（見下一個測試），所以要確認每次都真的量到了
        assert k == 1 or s["compact_bytes"] is not None, k
    before = _count(db)
    with pytest.raises(sqlite3.OperationalError, match="too many attached"):
        archive_old(str(db), now=start + timedelta(days=31 * 11), roll_bytes=1)
    assert _count(db) == before
    assert len(_manifest_matches_files(db)) == 10
    assert not (tmp_path / "archive" / "observations-011.db").exists()
    store = Store(str(db))                               # 讀者照樣能掛滿 10 個
    assert store.route_stats("TPE", "NRT")["n"] > 0
    store.close()


def test_compaction_failure_after_commit_is_not_fatal(tmp_path, monkeypatch):
    """搬移提交、核對都過了之後，壓實失敗（例如暫存空間不足）只少一次 VACUUM，
    不能讓 job 變紅、把已經搬好的成果丟掉。"""
    db = tmp_path / "prices.db"
    _seed(db, NOW)
    ids_before = _ids(db)
    archive_old(str(db), now=NOW - timedelta(days=60))

    def full(conn):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(archive, "_compact_bytes", full)
    s = archive_old(str(db), now=NOW)
    assert s["moved"] > 0 and not s["vacuumed"] and s["compact_bytes"] is None
    assert len(_manifest_matches_files(db)) == 1
    assert _ids(db) == ids_before
