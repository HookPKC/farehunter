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

def test_the_production_false_alarm_is_gone():
    """2026-09-01 的真實事故：this_month_usage=176、剩 74，因為當天是 1 號，
    舊邏輯算成「每天 176 次」、外推 5,280、報 low 並寫「還需 5104 次」。

    根因：免費層不照日曆月重置（ADR 0001），this_month_usage 是**計費週期**
    的累計，除以 day_of_month 毫無意義。現在只看續航天數，週期起點未知也不
    影響：74 ÷ 6 ≈ 12 天，該是 ok 而不是警報。
    """
    q = Quota(plan_name="Free Plan", searches_per_month=250,
              total_searches_left=74, this_month_usage=176)
    r = assess(q, now=datetime(2026, 9, 1, 11, 33, tzinfo=timezone.utc))
    assert r["status"] == "ok", r
    assert r["runway_days"] == round(74 / 6.0, 1)
    assert "5104" not in r["note"] and "月底" not in r["note"]
    # 舊的錯誤欄位不該再出現，免得有人繼續拿它做判斷
    assert "projected_month_usage" not in r and "daily_rate" not in r


def test_runway_is_independent_of_the_day_of_month():
    """同樣的額度狀態，在月初或月中都該給同一個結論。這正是舊邏輯做不到的。"""
    q = Quota(plan_name="P", searches_per_month=250,
              total_searches_left=74, this_month_usage=176)
    days = [assess(q, now=datetime(2026, 9, d, 12, tzinfo=timezone.utc))
            for d in (1, 8, 15, 28)]
    assert len({r["status"] for r in days}) == 1
    assert len({r["runway_days"] for r in days}) == 1


def test_burn_is_measured_from_the_previous_snapshot():
    """實際跑幾次才算數——專案鐵律：排程表 ≠ 實際執行。

    數字刻意選成「實測值 ≠ EXPECTED_DAILY」：原本用 170→176 跨一天＝6.0/天，
    剛好等於預估值，於是把實測換成預估也照樣通過（突變測試抓到）。這裡兩天
    燒 26 次＝13/天，只有真的做減法才算得出來。
    """
    prev = {"this_month_usage": 150,
            "checked_at": "2026-08-30T11:33:00+00:00"}
    q = Quota(plan_name="P", searches_per_month=250,
              total_searches_left=74, this_month_usage=176)
    r = assess(q, now=datetime(2026, 9, 1, 11, 33, tzinfo=timezone.utc), prev=prev)
    assert r["burn_source"] == "measured"
    assert r["burn_per_day"] == 13.0            # (176-150) / 2 天
    assert r["burn_per_day"] != Q.EXPECTED_DAILY
    assert r["runway_days"] == round(74 / 13.0, 1)
    # 而且實測改變了結論：13/天 → 續航 5.7 天 → low；
    # 若退回預估 6/天 → 12.3 天 → ok。這就是「量測勝過假設」的具體差別。
    assert r["status"] == "low"
    assert assess(q, now=datetime(2026, 9, 1, 11, 33,
                                  tzinfo=timezone.utc))["status"] == "ok"


def test_falls_back_to_expected_burn_without_history():
    r = assess(Quota(total_searches_left=60, this_month_usage=190), now=MID)
    assert r["burn_source"] == "expected" and r["burn_per_day"] == 6.0


def test_period_reset_is_detected_and_recorded():
    """用量倒退 = 計費週期剛重置。這是本專案唯一能觀測到重置日的方式。"""
    prev = {"this_month_usage": 244, "checked_at": "2026-09-03T11:00:00+00:00"}
    q = Quota(plan_name="P", searches_per_month=250,
              total_searches_left=244, this_month_usage=6)
    r = assess(q, now=datetime(2026, 9, 4, 11, 0, tzinfo=timezone.utc), prev=prev)
    assert r["period_reset"] is True
    assert "計費週期已重置" in r["note"]
    # 倒退的差值不得被當成燒用量（會算出負數或荒謬的續航）
    assert r["burn_source"] == "expected" and r["burn_per_day"] > 0


# ---- 下次重置（2026-10-26 起）--------------------------------------------

OCT25 = datetime(2026, 10, 25, 11, 14, 28, tzinfo=timezone.utc)


def test_the_october_false_alarm_knows_the_reset_comes_first():
    """2026-10-25 的真實數字：剩 121、實測每天 7.86 → 續航 15 天，看起來 11 月上旬
    會用完。但 git 歷史顯示每月 3 日前後重置，11/03 只剩 9 天，根本撐得到。"""
    prev = {"this_month_usage": 121, "checked_at": "2026-10-24T10:48:54+00:00"}
    r = assess(Quota(plan_name="Free Plan", searches_per_month=250,
                     total_searches_left=121, this_month_usage=129),
               now=OCT25, prev=prev)
    assert r["runway_days"] == 15.4
    assert r["last_reset_at"] == Q.KNOWN_RESET_AT
    assert r["next_reset_estimate"] == "2026-11-03" and r["days_to_reset"] == 9
    assert r["status"] == "ok"
    assert "11/03" in r["note"] and "撐得到" in r["note"]


def test_short_runway_is_not_low_when_the_reset_comes_first():
    """剩 20 次、每天 6 次 → 3.3 天；但 2 天後就重置，不該警報。"""
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    r = assess(Quota(total_searches_left=20, this_month_usage=230), now=now)
    assert r["days_to_reset"] == 2 and r["runway_days"] == 3.3
    assert r["status"] == "ok"
    # 同樣的額度，重置還遠 → 照樣 low，並說出會早幾天用完
    r = assess(Quota(total_searches_left=20, this_month_usage=230),
               now=datetime(2026, 10, 20, 12, tzinfo=timezone.utc))
    assert r["status"] == "low" and "會早約 11 天用完" in r["note"]


def test_a_detected_reset_becomes_the_new_anchor():
    prev = {"this_month_usage": 244, "checked_at": "2026-11-02T11:00:00+00:00",
            "last_reset_at": "2026-10-03T11:33:23+00:00"}
    now = datetime(2026, 11, 3, 11, 20, tzinfo=timezone.utc)
    r = assess(Quota(total_searches_left=246, this_month_usage=4), now=now, prev=prev)
    assert r["period_reset"] is True
    assert r["last_reset_at"] == "2026-11-03T11:20:00+00:00"
    assert r["next_reset_estimate"] == "2026-12-03"


def test_the_last_reset_is_carried_forward_not_replaced_by_the_seed():
    prev = {"this_month_usage": 50, "checked_at": "2026-11-20T11:00:00+00:00",
            "last_reset_at": "2026-11-05T09:00:00+00:00"}
    now = datetime(2026, 11, 21, 11, tzinfo=timezone.utc)
    r = assess(Quota(total_searches_left=194, this_month_usage=56), now=now, prev=prev)
    assert r["last_reset_at"] == "2026-11-05T09:00:00+00:00"
    assert r["next_reset_estimate"] == "2026-12-05"


def test_a_garbled_last_reset_falls_back_to_the_known_one():
    prev = {"this_month_usage": 120, "checked_at": "2026-10-24T11:00:00+00:00",
            "last_reset_at": "garbage"}
    r = assess(Quota(total_searches_left=121, this_month_usage=129), now=OCT25, prev=prev)
    assert r["last_reset_at"] == Q.KNOWN_RESET_AT
    assert r["next_reset_estimate"] == "2026-11-03"


def test_no_reset_estimate_before_the_seed_or_without_one(monkeypatch):
    """已知重置比「現在」還晚（舊日期的測試、時鐘錯）→ 不估，行為跟以前一樣。"""
    q = Quota(total_searches_left=74, this_month_usage=176)
    early = assess(q, now=datetime(2026, 9, 1, 11, 33, tzinfo=timezone.utc))
    assert early["last_reset_at"] is None and early["next_reset_estimate"] is None
    assert "重置" not in early["note"]
    monkeypatch.setattr(Q, "KNOWN_RESET_AT", None)
    assert assess(q, now=OCT25)["next_reset_estimate"] is None


def test_next_reset_handles_month_ends_and_december():
    now = datetime(2027, 2, 10, tzinfo=timezone.utc)
    assert Q.next_reset("2027-01-31T10:00:00+00:00", now).isoformat() == "2027-02-28"
    # 過了短月不漂移：一律從原日期算，3 月回到 31 日（不是 02-28 接著推的 03-28）
    assert Q.next_reset("2027-01-31T10:00:00+00:00",
                        datetime(2027, 3, 10, tzinfo=timezone.utc)).isoformat() == "2027-03-31"
    assert Q.next_reset("2026-12-15T10:00:00+00:00",
                        datetime(2026, 12, 20, tzinfo=timezone.utc)).isoformat() == "2027-01-15"
    # 好幾個月沒偵測到（例如 FSC 停擺）→ 一路推到今天之後
    assert Q.next_reset("2026-10-03T11:00:00+00:00",
                        datetime(2027, 1, 10, tzinfo=timezone.utc)).isoformat() == "2027-02-03"
    # 重置當天 → 下一次是下個月
    assert Q.next_reset("2026-10-03T11:00:00+00:00",
                        datetime(2026, 11, 3, 23, tzinfo=timezone.utc)).isoformat() == "2026-12-03"
    assert Q.next_reset(None, now) is None and Q.next_reset("garbage", now) is None


def test_a_failed_check_keeps_the_reset_anchor(tmp_path):
    """11-05 偵測到重置 → 11-06 查詢失敗 → 11-07 不能退回 10-03 的已知值。"""
    out = tmp_path / "q.json"
    out.write_text(json.dumps({"this_month_usage": 10, "checked_at": "2026-11-05T11:00:00+00:00",
                               "last_reset_at": "2026-11-05T11:00:00+00:00"}), encoding="utf-8")
    err = snapshot(str(out), api_key="k", session=_Session(exc=RuntimeError("boom")),
                   now=datetime(2026, 11, 6, 11, tzinfo=timezone.utc))
    assert err["status"] == "error" and err["last_reset_at"] == "2026-11-05T11:00:00+00:00"
    acct = dict(ACCOUNT, total_searches_left=230, this_month_usage=20)
    r = snapshot(str(out), api_key="k", session=_Session(_Resp(acct)),
                 now=datetime(2026, 11, 7, 11, tzinfo=timezone.utc))
    assert r["last_reset_at"] == "2026-11-05T11:00:00+00:00"
    assert r["next_reset_estimate"] == "2026-12-05"


def test_snapshot_passes_the_reset_anchor_to_the_next_day(tmp_path):
    out = tmp_path / "q.json"
    day1 = datetime(2026, 11, 3, 11, tzinfo=timezone.utc)
    out.write_text(json.dumps({"this_month_usage": 244,
                               "checked_at": "2026-11-02T11:00:00+00:00"}), encoding="utf-8")
    acct = dict(ACCOUNT, total_searches_left=246, this_month_usage=4)
    snapshot(str(out), api_key="k", session=_Session(_Resp(acct)), now=day1)
    acct = dict(ACCOUNT, total_searches_left=240, this_month_usage=10)
    r = snapshot(str(out), api_key="k", session=_Session(_Resp(acct)),
                 now=datetime(2026, 11, 4, 11, tzinfo=timezone.utc))
    assert r["period_reset"] is False
    assert r["last_reset_at"] == "2026-11-03T11:00:00+00:00"
    assert r["next_reset_estimate"] == "2026-12-03"


def test_low_when_the_runway_is_short():
    """剩 12 次、每天 6 次 → 兩天就見底，該出聲。"""
    r = assess(Quota(plan_name="Free", searches_per_month=250,
                     total_searches_left=12, this_month_usage=238), now=MID)
    assert r["status"] == "low"
    assert "快見底" in r["note"]


def test_exhausted_is_distinct_from_low():
    """SearchApi 那次就是走到這一格：額度歸零後查詢靜默失敗五週。"""
    r = assess(Quota(plan_name="Free", searches_per_month=100,
                     total_searches_left=0, this_month_usage=100), now=MID)
    assert r["status"] == "exhausted"


def test_exhausted_wins_even_if_usage_missing():
    """剩餘為 0 是硬事實，不需要 usage 就能斷定。"""
    assert assess(Quota(total_searches_left=0), now=MID)["status"] == "exhausted"


def test_headroom_when_the_runway_outlasts_a_billing_period():
    """撐得過一整個週期（>30 天）就代表額度沒有在限制系統。"""
    r = assess(Quota(plan_name="Starter", searches_per_month=5000,
                     total_searches_left=4820, this_month_usage=180), now=MID)
    assert r["status"] == "headroom"
    assert "餘裕" in r["note"]


def test_ok_between_the_two_thresholds():
    r = assess(Quota(plan_name="P", searches_per_month=250,
                     total_searches_left=90, this_month_usage=160), now=MID)
    assert r["status"] == "ok"            # 15 天：既不警報也不建議加量


def test_missing_left_cannot_estimate_runway():
    r = assess(Quota(plan_name="P", this_month_usage=100), now=MID)
    assert r["runway_days"] is None
    assert r["status"] == "unknown"


def test_expected_daily_matches_the_real_consumer():
    """EXPECTED_DAILY 刻意不 import SEARCHES_PER_DAY（避免循環依賴），
    所以用測試釘住兩者一致——否則有人改了其中一個就會靜靜地不同步。"""
    from farehunter.serpapi_flights import SEARCHES_PER_DAY
    assert Q.EXPECTED_DAILY == float(SEARCHES_PER_DAY)


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
