"""防重複 guard 測試：fail-open 是鐵律——guard 只能跳過，絕不能擋路。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from datetime import datetime, timedelta, timezone

from farehunter.runner import GUARD_MINUTES, guard_decision, _emit_skip_output, run


def _write_export(path, minutes_ago=None, generated_at=None, raw=None):
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
        return
    if generated_at is None:
        ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        generated_at = ts.isoformat()
    path.write_text(json.dumps({"generated_at": generated_at, "routes": []}),
                    encoding="utf-8")


# ---- 規格要求的四態 ---------------------------------------------------------

def test_fresh_data_skips(tmp_path):
    p = tmp_path / "data.json"
    _write_export(p, minutes_ago=10)
    skip, age = guard_decision(str(p), force=False)
    assert skip is True
    assert 9 <= age <= 11


def test_stale_data_runs(tmp_path):
    p = tmp_path / "data.json"
    _write_export(p, minutes_ago=120)
    skip, age = guard_decision(str(p), force=False)
    assert skip is False
    assert age >= 119


def test_missing_file_runs_fail_open(tmp_path):
    skip, age = guard_decision(str(tmp_path / "nope.json"), force=False)
    assert skip is False and age is None


def test_corrupt_json_or_bad_timestamp_runs_fail_open(tmp_path):
    p = tmp_path / "data.json"
    _write_export(p, raw="{not json!!")
    assert guard_decision(str(p), force=False) == (False, None)
    _write_export(p, generated_at="not-a-timestamp")
    assert guard_decision(str(p), force=False) == (False, None)
    _write_export(p, raw=json.dumps({"routes": []}))  # 缺 generated_at 欄位
    assert guard_decision(str(p), force=False) == (False, None)


# ---- 邊界與旁路 -------------------------------------------------------------

def test_boundary_at_guard_minutes(tmp_path):
    p = tmp_path / "data.json"
    _write_export(p, minutes_ago=GUARD_MINUTES + 1)
    assert guard_decision(str(p), force=False)[0] is False
    _write_export(p, minutes_ago=GUARD_MINUTES - 2)
    assert guard_decision(str(p), force=False)[0] is True


def test_future_timestamp_runs(tmp_path):
    """時鐘漂移/異常的未來時間戳：資料齡為負 → 照常執行，不得永久卡跳過。"""
    p = tmp_path / "data.json"
    _write_export(p, minutes_ago=-30)
    assert guard_decision(str(p), force=False)[0] is False


def test_force_bypasses_guard(tmp_path, monkeypatch):
    p = tmp_path / "data.json"
    _write_export(p, minutes_ago=5)
    assert guard_decision(str(p), force=True)[0] is False
    monkeypatch.setenv("FAREHUNTER_FORCE", "1")
    assert guard_decision(str(p))[0] is False
    monkeypatch.setenv("FAREHUNTER_FORCE", "0")
    assert guard_decision(str(p))[0] is True


def test_run_short_circuits_before_any_side_effect(tmp_path, monkeypatch):
    """run() 在建立 client/store 之前就短路：無網路、無 DB、無 config 也能跳過。"""
    monkeypatch.delenv("FAREHUNTER_FORCE", raising=False)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "gh_out"))  # 隔離，避免污染 CI 測試步驟
    p = tmp_path / "data.json"
    _write_export(p, minutes_ago=10)
    summary = run(config_path=str(tmp_path / "no.yaml"),
                  db_path=str(tmp_path / "no.db"),
                  web_export_path=str(p))
    assert summary["skipped"] is True
    assert summary["searched"] == summary["recorded"] == 0
    assert not (tmp_path / "no.db").exists()


def test_emit_skip_output_writes_github_output(tmp_path, monkeypatch):
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    _emit_skip_output()
    assert "skip=true" in out.read_text(encoding="utf-8")
    monkeypatch.delenv("GITHUB_OUTPUT")
    _emit_skip_output()  # 本地無 GITHUB_OUTPUT：不得拋錯
