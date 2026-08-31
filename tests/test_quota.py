"""SerpAPI 額度自我檢查的測試。零真實 API：一律注入假 session。

這支程式的存在理由是「不要再猜額度」，所以測試的重點不只是不炸，而是
**判讀要正確** — 尤其 headroom（有大量餘裕）與 low（撐不到月底）不能弄反，
那兩個結論會導向完全相反的決策。
"""
import sys, json, logging
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from farehunter import quota as Q
from farehunter.quota import Quota, parse_account, assess, fetch_quota, snapshot

# 8 月 31 天。day=16 → 剛過半月，外推最直觀。
MID = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


class _Resp:
    def __init__(self, payload, status=200, text=""):
        self._payload, self.status_code, self.text = payload, status, text

    def json(self):
        return self._payload


class _Session:
    """記錄呼叫並回固定回應。零網路。"""
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp, exc, []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if self.exc:
            raise self.exc
        return self.resp


class _Exploding:
    """任何連網都是測試失敗。"""
    def get(self, *a, **kw):
        raise AssertionError("測試不得連外")


ACCOUNT = {"plan_name": "Starter Plan", "searches_per_month": 5000,
           "plan_searches_left": 4820, "total_searches_left": 4820,
           "this_month_usage": 180, "account_id": "x", "extra_credits": 0}


# ---- parse_account -------------------------------------------------------

def test_parse_account_extracts_the_fields_we_act_on():
    q = parse_account(ACCOUNT)
    assert q.plan_name == "Starter Plan"
    assert q.searches_per_month == 5000
    assert q.total_searches_left == 4820
    assert q.this_month_usage == 180


def test_missing_fields_become_none_not_zero():
    """關鍵：缺欄位必須是 None。填 0 會被讀成「額度用完」——意思完全相反，
    而且會讓 assess 回報 exhausted 觸發假警報。"""
    q = parse_account({"plan_name": "Free"})
    assert q.searches_per_month is None
    assert q.total_searches_left is None
    assert q.this_month_usage is None
    assert assess(q, now=MID)["status"] == "unknown"


def test_parse_account_survives_garbage():
    """SerpAPI 改回應格式時要能降級，不是整支爆掉。"""
    for bad in (None, [], "nope", 42):
        assert parse_account(bad) == Quota()
    assert parse_account({"searches_per_month": "not-a-number"}).searches_per_month is None
    assert parse_account({"searches_per_month": "5000"}).searches_per_month == 5000


# ---- assess：判讀 -------------------------------------------------------

def test_headroom_is_the_whole_point():
    """實際情境：Starter 5,000/月，實測用量約 180/月 → 只用 3.6%。

    這個結論才是加這支程式的原因（原本註解誤寫 ~100/月，整個系統的
    SEARCHES_PER_DAY=6 圍繞錯誤前提設計）。
    """
    r = assess(parse_account(ACCOUNT), now=MID)
    assert r["status"] == "headroom"
    assert r["daily_rate"] == round(180 / 16, 2)
    assert r["projected_month_usage"] == round(180 / 16 * 31, 1)
    assert "餘裕" in r["note"]


def test_low_when_the_month_will_not_finish():
    """免費版 100/月、每日 6 次的情境：月中就該亮燈。"""
    q = Quota(plan_name="Free", searches_per_month=100,
              total_searches_left=4, this_month_usage=96)
    r = assess(q, now=MID)
    assert r["status"] == "low"
    assert "撐不到月底" in r["note"]


def test_exhausted_is_distinct_from_low():
    """SearchApi 那次就是走到這一格：額度歸零後查詢靜默失敗五週。"""
    r = assess(Quota(plan_name="Free", searches_per_month=100,
                     total_searches_left=0, this_month_usage=100), now=MID)
    assert r["status"] == "exhausted"


def test_exhausted_wins_even_if_usage_missing():
    """剩餘為 0 是硬事實，不需要 usage 就能斷定。"""
    r = assess(Quota(total_searches_left=0), now=MID)
    assert r["status"] == "exhausted"


def test_ok_when_tight_but_sufficient():
    """夠用但沒有大量餘裕 → 既不警報也不建議加量。"""
    q = Quota(plan_name="Starter", searches_per_month=5000,
              total_searches_left=2600, this_month_usage=2400)
    r = assess(q, now=MID)
    assert r["status"] == "ok"


def test_zero_usage_is_not_reported_as_headroom():
    """用量 0 時 projected 也是 0，說「餘裕大」沒有資訊量，而且可能只是
    SerpAPI 計數還沒更新——不該拿來當調整額度的依據。"""
    q = Quota(plan_name="Starter", searches_per_month=5000,
              total_searches_left=5000, this_month_usage=0)
    assert assess(q, now=MID)["status"] == "ok"


def test_rate_uses_measured_usage_not_configured_budget():
    """專案鐵律：排程表 ≠ 實際執行。同一個方案、不同實際用量要給不同判讀。"""
    cap = {"plan_name": "P", "searches_per_month": 1000}
    # used + left = 1000，兩組唯一的差別就是實際跑了幾次
    slow = assess(parse_account({**cap, "this_month_usage": 100,
                                 "total_searches_left": 900}), now=MID)
    fast = assess(parse_account({**cap, "this_month_usage": 600,
                                 "total_searches_left": 400}), now=MID)
    assert slow["daily_rate"] < fast["daily_rate"]
    assert slow["status"] == "headroom"     # 預估 194/1000
    assert fast["status"] == "low"          # 每日 37.5 次，剩 400 撐不完 15 天


# ---- fetch_quota --------------------------------------------------------

def test_fetch_hits_the_free_account_endpoint():
    s = _Session(_Resp(ACCOUNT))
    q = fetch_quota(api_key="k", session=s)
    assert q.plan_name == "Starter Plan"
    assert s.calls[0]["url"] == "https://serpapi.com/account.json"
    assert s.calls[0]["params"] == {"api_key": "k"}


def test_fetch_raises_on_http_error():
    s = _Session(_Resp({}, status=401, text="bad key"))
    try:
        fetch_quota(api_key="k", session=s)
        assert False, "應該拋出"
    except RuntimeError as exc:
        assert "401" in str(exc)


def test_fetch_without_key_raises(monkeypatch):
    monkeypatch.delenv("SERPAPI_KEY", raising=False)
    try:
        fetch_quota(session=_Exploding())
        assert False, "應該拋出"
    except RuntimeError as exc:
        assert "SERPAPI_KEY" in str(exc)


# ---- snapshot：fail-soft 與測試隔離 -------------------------------------

def test_no_key_means_no_network_and_no_file(tmp_path, monkeypatch):
    """核心隔離保護：fsc_snapshot 的測試會 mock 掉查詢但不會設金鑰。

    若這裡照跑就會（a）嘗試連外、（b）覆寫 repo 裡真實的 docs/quota.json。
    同樣的隔離漏洞已經在 docs/data.json 上咬過兩次，所以這條不能只靠
    「每個測試都記得傳參數」。
    """
    monkeypatch.delenv("SERPAPI_KEY", raising=False)
    out = tmp_path / "q.json"
    r = snapshot(str(out), session=_Exploding())
    assert r["status"] == "skipped"
    assert not out.exists()


def test_network_failure_is_soft(tmp_path, caplog):
    """額度檢查是維運觀測工具，不是資料來源——絕不能讓抓價流程失敗。"""
    out = tmp_path / "q.json"
    with caplog.at_level(logging.WARNING):
        r = snapshot(str(out), api_key="k",
                     session=_Session(exc=OSError("connection reset")),
                     now=MID)
    assert r["status"] == "error"
    assert "不影響本輪抓價" in caplog.text
    assert json.loads(out.read_text())["status"] == "error"   # 失敗也留紀錄


def test_snapshot_writes_a_dated_record(tmp_path):
    """每天隨 commit 進 repo → 用 git 歷史就有額度的時間序列。"""
    out = tmp_path / "q.json"
    r = snapshot(str(out), api_key="k", session=_Session(_Resp(ACCOUNT)), now=MID)
    on_disk = json.loads(out.read_text(encoding="utf-8"))
    assert on_disk == r
    assert on_disk["checked_at"] == "2026-08-16T12:00:00+00:00"
    assert on_disk["plan_name"] == "Starter Plan"
    assert on_disk["status"] == "headroom"


def test_unwritable_path_does_not_raise(tmp_path):
    r = snapshot(str(tmp_path / "no" / "such" / "dir" / "q.json"),
                 api_key="k", session=_Session(_Resp(ACCOUNT)), now=MID)
    assert r["status"] == "headroom"        # 判讀照回，只是沒寫成檔


def test_low_and_exhausted_are_logged_as_warnings(tmp_path, caplog):
    """夠用時安靜、快沒了才出聲——否則每天一則警報就沒人看了。"""
    out = str(tmp_path / "q.json")
    with caplog.at_level(logging.WARNING):
        snapshot(out, api_key="k", session=_Session(_Resp(
            {"plan_name": "Free", "searches_per_month": 100,
             "total_searches_left": 2, "this_month_usage": 98})), now=MID)
    assert any(r.levelname == "WARNING" for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        snapshot(out, api_key="k", session=_Session(_Resp(ACCOUNT)), now=MID)
    assert not any(r.levelname == "WARNING" for r in caplog.records)


def test_plan_and_usage_appear_in_the_log(tmp_path, caplog):
    """這支程式的首要目的就是讓人看到真實方案，log 一定要印出來。"""
    with caplog.at_level(logging.INFO):
        snapshot(str(tmp_path / "q.json"), api_key="k",
                 session=_Session(_Resp(ACCOUNT)), now=MID)
    assert "Starter Plan" in caplog.text and "5000" in caplog.text


def test_main_never_fails_the_workflow(tmp_path, monkeypatch):
    """額度檢查失敗不該讓抓價 workflow 紅燈。"""
    monkeypatch.delenv("SERPAPI_KEY", raising=False)
    assert Q.main([str(tmp_path / "q.json")]) == 0


# ---- 接線：單元測試全綠但功能沒接上，這個專案已經被咬過一次 -------------

def test_fsc_snapshot_actually_calls_the_quota_check(tmp_path, monkeypatch):
    """回歸型測試：export_web 曾經因為參數名沒同步改，被 try/except 吞掉，
    288 個單元測試全綠但看板是空的。所以「有沒有真的被呼叫」要單獨釘住。"""
    from farehunter import fsc_snapshot as F
    calls = []
    monkeypatch.setattr(F.quota, "snapshot",
                        lambda p, **kw: (calls.append(p), {"status": "headroom"})[1])
    monkeypatch.setattr(F, "load_config", lambda p: {"routes": []})
    monkeypatch.setattr(F, "search_google_flights",
                        lambda *a, **k: {"best_flights": [], "other_flights": []})
    monkeypatch.setattr(F.time, "sleep", lambda s: None)
    qp = str(tmp_path / "q.json")
    s = F.run("x.yaml", str(tmp_path / "t.db"), ranked_path=str(tmp_path / "no.json"),
              data_path=str(tmp_path / "no2.json"), quota_path=qp)
    assert calls == [qp]
    assert s["quota"] == "headroom"


def test_fsc_snapshot_can_switch_the_check_off(tmp_path, monkeypatch):
    """quota_path=None → 完全不呼叫（測試與離線執行用）。"""
    from farehunter import fsc_snapshot as F
    calls = []
    monkeypatch.setattr(F.quota, "snapshot",
                        lambda p, **kw: (calls.append(p), {"status": "x"})[1])
    monkeypatch.setattr(F, "load_config", lambda p: {"routes": []})
    monkeypatch.setattr(F, "search_google_flights",
                        lambda *a, **k: {"best_flights": [], "other_flights": []})
    monkeypatch.setattr(F.time, "sleep", lambda s: None)
    s = F.run("x.yaml", str(tmp_path / "t.db"), ranked_path=str(tmp_path / "no.json"),
              data_path=str(tmp_path / "no2.json"), quota_path=None)
    assert calls == []
    assert s["quota"] == "unchecked"
