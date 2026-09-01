"""google_flights_deals 探測程式的測試。零真實 API。

這支程式會花使用者的付費額度（免費層 250 次/月），所以最重要的性質不是
「解析正確」而是**「絕不多打」**：恰好一次計費查詢，沒有迴圈、沒有重試。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from farehunter.deals_probe import probe, _summarise, ENGINE

PAYLOAD = {
    "search_metadata": {"status": "Success", "id": "x"},
    "best_flights": [
        {"price": 7420, "flights": [{"flight_number": "BR 198",
                                     "departure_airport": {"id": "TPE"}}]},
        {"price": 7880, "flights": [{"flight_number": "IT 200"}]},
    ],
    "other_flights": [{"price": 8100}, {"price": 8300}, {"price": 8600}],
}


class _Resp:
    def __init__(self, payload, status=200, text=""):
        self._p, self.status_code, self.text = payload, status, text

    def json(self):
        return self._p


class _Session:
    """記錄每一次呼叫。account.json 回遞減的額度以模擬扣款。"""
    def __init__(self, deals=None, status=200, left_start=250, charge=1):
        self.calls, self.deals = [], deals if deals is not None else PAYLOAD
        self.status, self.left, self.charge = status, left_start, charge

    def get(self, url, params=None, timeout=None):
        engine = (params or {}).get("engine")
        self.calls.append((url, engine))
        if "account.json" in url:
            return _Resp({"plan_name": "Free Plan", "searches_per_month": 250,
                          "total_searches_left": self.left})
        self.left -= self.charge          # 計費查詢
        return _Resp(self.deals, status=self.status,
                     text="" if self.status == 200 else "quota exceeded")


def _billed(s):
    return [c for c in s.calls if c[1] is not None]


# ---- 安全性：絕不多打 ----------------------------------------------------

def test_exactly_one_billed_search():
    """核心安全性質。這支會花真實額度，多打一次就是浪費使用者的錢。"""
    s = _Session()
    probe(api_key="k", session=s)
    assert len(_billed(s)) == 1
    assert _billed(s)[0][1] == ENGINE


def test_http_error_does_not_retry():
    """失敗時不得重試——重試會再扣一次額度，而探測的目的只是知道結果。"""
    s = _Session(status=429)
    r = probe(api_key="k", session=s)
    assert len(_billed(s)) == 1
    assert r["http_status"] == 429 and "error" in r


def test_payload_level_error_is_reported_not_retried():
    s = _Session(deals={"error": "Unsupported engine for your plan"})
    r = probe(api_key="k", session=s)
    assert len(_billed(s)) == 1
    assert "Unsupported engine" in r["error"]
    assert "structure" not in r          # 出錯就不假裝有結構可解析


def test_missing_key_makes_no_calls_at_all(monkeypatch):
    monkeypatch.delenv("SERPAPI_KEY", raising=False)

    class _Exploding:
        def get(self, *a, **kw):
            raise AssertionError("沒有金鑰不得連外")

    try:
        probe(session=_Exploding())
        assert False, "應該拋出"
    except RuntimeError as exc:
        assert "SERPAPI_KEY" in str(exc)


# ---- 量測扣款 ------------------------------------------------------------

def test_measures_how_many_searches_it_actually_costs():
    """整個提案的價值建立在「效率比單日查詢高」上。若一次 deals 扣 5 次，
    它其實比現況更糟——這是寫解析器之前必須先知道的事。"""
    s = _Session(charge=1)
    assert probe(api_key="k", session=s)["searches_charged"] == 1
    s5 = _Session(charge=5)
    assert probe(api_key="k", session=s5)["searches_charged"] == 5


def test_quota_lookup_failure_does_not_abort_the_probe():
    """額度查不到是次要資訊，不該讓已經花掉的那次查詢白費。"""
    class _NoAccount(_Session):
        def get(self, url, params=None, timeout=None):
            if "account.json" in url:
                raise OSError("connection reset")
            return super().get(url, params, timeout)

    s = _NoAccount()
    r = probe(api_key="k", session=s)
    assert len(_billed(s)) == 1
    assert r["top_level_keys"]                    # 主要產出仍在
    assert "searches_charged" not in r


# ---- 產出 ----------------------------------------------------------------

def test_reports_structure_and_row_count():
    r = probe(api_key="k", session=_Session())
    assert r["top_level_keys"] == ["best_flights", "other_flights", "search_metadata"]
    assert r["arrays"] == {"best_flights": 2, "other_flights": 3}
    assert r["rows"] == 3                         # 用最長的陣列估資訊量
    assert "flight_number" in r["structure"]


def test_flexible_date_window_is_actually_sent_as_a_range():
    """要驗證的就是彈性日期區間這個功能，所以必須檢查**實際送出的參數**。

    這條原本只檢查 result 裡回報的區間字串——那是從同一組變數組出來的，
    就算真正送出的 outbound_date 退化成單日也照樣通過。突變測試抓到了：
    把參數改成 start.isoformat() 之後 389 個測試全綠，而探測其實在測
    完全不同的東西。
    """
    captured = {}

    class _Capture(_Session):
        def get(self, url, params=None, timeout=None):
            if (params or {}).get("engine"):
                captured.update(params)
            return super().get(url, params, timeout)

    r = probe(api_key="k", session=_Capture())
    sent = captured["outbound_date"]
    assert "," in sent, f"outbound_date 不是區間: {sent!r}"
    start, end = sent.split(",")
    assert len(start) == 10 and len(end) == 10 and start < end
    assert r["outbound_window"] == f"{start}..{end}"   # 回報要與實際相符
    assert captured["engine"] == ENGINE


def test_summarise_truncates_instead_of_dumping_everything():
    """整包 JSON 可能幾百 KB，倒進 Actions log 只會淹沒重點。"""
    deep = {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}
    out = _summarise(deep)
    assert "f" not in out                          # 超過 max_depth 就收斂
    assert _summarise({"x": []}) .strip().endswith("(空)")
    assert "y" * 200 not in _summarise({"k": "y" * 200})
