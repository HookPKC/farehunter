"""快取價「沒變就不重寫」與 guard 的執行紀錄（ADR 0002 2026-11-23）。零真實 API。"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import farehunter.runner as runner_mod                              # noqa: E402
from farehunter.models import Offer                                 # noqa: E402
from farehunter.storage import Store                                # noqa: E402

UTC = timezone.utc


def _now():
    return datetime.now(UTC)


def _at(hours_ago):
    return (_now() - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


def _offer(price=8500.0, carriers="IT", fare_class="any", source="aviasales", ret="2099-09-23"):
    return Offer("TPE", "NRT", "2099-09-18", ret, price, "TWD", carriers, 0, "190",
                 fare_class=fare_class, source=source, provider="travelpayouts")


def _since():
    return _at(runner_mod.CACHE_REWRITE_HOURS)


def test_same_price_within_6_hours_is_not_rewritten(tmp_path):
    st = Store(str(tmp_path / "p.db"))
    assert st.same_cache_price_since(_offer(), _since()) is False          # 沒有前一筆
    st.record(_offer(), observed_at=_at(2))
    assert st.same_cache_price_since(_offer(), _since()) is True           # 同價、2 小時前
    assert st.same_cache_price_since(_offer(price=8400.0), _since()) is False   # 價格變了
    assert st.same_cache_price_since(_offer(carriers="MM"), _since()) is False  # 別家航空
    assert st.same_cache_price_since(_offer(fare_class="full"), _since()) is False
    assert st.same_cache_price_since(_offer(ret="2099-09-24"), _since()) is False


def test_six_hours_later_the_same_price_is_written_again(tmp_path):
    st = Store(str(tmp_path / "p.db"))
    st.record(_offer(), observed_at=_at(runner_mod.CACHE_REWRITE_HOURS + 1))
    assert st.same_cache_price_since(_offer(), _since()) is False


def test_only_the_latest_row_counts_and_google_rows_are_separate(tmp_path):
    st = Store(str(tmp_path / "p.db"))
    st.record(_offer(price=8500.0), observed_at=_at(3))
    st.record(_offer(price=8000.0), observed_at=_at(1))                    # 最新是 8,000
    assert st.same_cache_price_since(_offer(price=8500.0), _since()) is False
    st.record(_offer(price=8000.0, source="google"), observed_at=_at(0.5))
    assert st.same_cache_price_since(_offer(price=8000.0), _since()) is True
    assert st.same_cache_price_since(_offer(price=7000.0, source="google"), _since()) is False


def test_null_carriers_and_one_way_rows_match_themselves(tmp_path):
    st = Store(str(tmp_path / "p.db"))
    o = _offer(carriers=None, ret=None)
    st.record(o, observed_at=_at(1))
    assert st.same_cache_price_since(o, _since()) is True


# ---- runner：兩輪同價只寫一次、計數不變 ------------------------------------------

class _Client:
    prices = [8500.0]

    def __init__(self, *a, **k):
        pass

    def search_month(self, *a, **k):
        return {}


def _run(tmp_path, monkeypatch, price):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("defaults:\n  currency: twd\n  months_ahead: 1\n  pause_seconds: 0\n"
                   "routes:\n  - origin: TPE\n    destination: NRT\n    absolute_threshold: 1000\n",
                   encoding="utf-8")
    monkeypatch.setattr(runner_mod, "TravelpayoutsClient", _Client)
    monkeypatch.setattr(runner_mod, "parse_offers", lambda *a, **k: [_offer(price=price)])
    monkeypatch.setattr(runner_mod, "notify", lambda *a, **k: ["line"])
    monkeypatch.setattr(runner_mod, "channels_configured", lambda: True)
    monkeypatch.setenv("FAREHUNTER_FORCE", "1")
    return runner_mod.run(str(cfg), str(tmp_path / "prices.db"))


def _rows(tmp_path):
    con = sqlite3.connect(tmp_path / "prices.db")
    n = con.execute("SELECT COUNT(*) FROM observations WHERE source='aviasales'").fetchone()[0]
    con.close()
    return n


def test_runner_skips_an_unchanged_price_but_still_counts_the_route(tmp_path, monkeypatch):
    s1 = _run(tmp_path, monkeypatch, 8500.0)
    assert _rows(tmp_path) == 1 and s1["unchanged"] == 0
    s2 = _run(tmp_path, monkeypatch, 8500.0)
    assert _rows(tmp_path) == 1 and s2["unchanged"] == 1
    assert s2["recorded"] == 1 and s2["zero_record_routes"] == []         # 不會誤報「零記錄航線」
    s3 = _run(tmp_path, monkeypatch, 8100.0)                               # 價格變了 → 寫
    assert _rows(tmp_path) == 2 and s3["unchanged"] == 0


# ---- guard 改看執行紀錄 ---------------------------------------------------------

def test_guard_uses_the_run_log_when_no_new_observation_was_written(tmp_path):
    db = tmp_path / "prices.db"
    st = Store(str(db))
    st.record(_offer(), observed_at=_at(3))                                # 最新觀測 3 小時前
    st.record_monitor_run(_at(0.2))                                       # 但 12 分鐘前跑過
    st.close()
    skip, age = runner_mod.guard_decision(str(db), force=False)
    assert skip is True and age < 15


def test_guard_still_works_on_a_database_without_the_run_log(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE observations (observed_at TEXT, source TEXT)")
    con.execute("INSERT INTO observations VALUES (?, 'aviasales')", (_at(0.1),))
    con.commit()
    con.close()
    assert runner_mod.guard_decision(str(db), force=False)[0] is True


def test_config_min_history_matches_the_replay():
    cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text("utf-8"))
    assert cfg["defaults"]["min_history"] == 20
