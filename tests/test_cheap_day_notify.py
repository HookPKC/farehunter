"""便宜日推播的測試。零真實 API（conftest 已清掉所有金鑰環境變數）。

這支功能的核心規則只有一條，其餘都是為它服務：**只推有 Google 實價背書的**。
使用者的原話是「我要的是 google 實價正確，不然點進去的價格不是正確的也沒有
什麼用」。所以「快取估價被推出去」是這裡最嚴重的失效模式，測試密度也最高。
"""
import sys, json, logging, re
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from farehunter import cheap_day_notify as C
from farehunter.cheap_day_notify import (notifiable, format_cheap_day,
                                         should_notify, run, MAX_AGE_HOURS,
                                         SUPPRESS_DAYS, RENOTIFY_DROP_PCT)
from farehunter.storage import Store

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _item(**kw):
    """一筆「已驗證、夠新鮮、夠便宜」的看板項目。價格與落差互相自洽。"""
    med = kw.pop("neighbour_median", 12000.0)
    price = kw.pop("price", 7200.0)
    try:                       # 允許刻意傳入壞掉的 price 來測健壯性
        disc = round((1 - float(price) / float(med)) * 100, 1)
    except (TypeError, ValueError, ZeroDivisionError):
        disc = 40.0
    base = {
        "origin": "TPE", "destination": "NRT",
        "depart_date": "2026-11-20", "return_date": "2026-11-25",
        "price": price, "neighbour_median": med, "neighbours": 8,
        "discount_pct": disc,
        "notable": True, "source": "google", "carriers": "BR",
        "observed_at": (NOW - timedelta(minutes=5)).isoformat(),
    }
    base.update(kw)
    return base


def _board(tmp_path, *items, name="data.json"):
    p = tmp_path / name
    p.write_text(json.dumps({"cheap_days": list(items)}), encoding="utf-8")
    return str(p)


# ---- 核心規則：只推實價 --------------------------------------------------

def test_verified_real_price_is_notified():
    assert len(notifiable([_item()], now=NOW)) == 1


def test_cache_price_is_never_notified():
    """最重要的一條。推快取估價過去，使用者點進 Google 看到別的數字——
    實測絕對誤差中位數 7.9%、90 百分位 27%——那則推播是負價值。"""
    for src in ("aviasales", "travelpayouts", "", None, "AVIASALES "):
        assert notifiable([_item(source=src)], now=NOW) == [], f"source={src!r}"


def test_source_match_is_exact_not_substring():
    """'not-google' 之類的來源不得被當成 google 放行。"""
    assert notifiable([_item(source="not-google")], now=NOW) == []
    assert notifiable([_item(source="googlecache")], now=NOW) == []
    assert len(notifiable([_item(source="Google")], now=NOW)) == 1   # 只有大小寫差異


def test_not_notable_is_not_notified():
    """看板收 ≥15%，但打斷使用者的門檻是 ≥30%（NOTIFY_PCT）。"""
    assert notifiable([_item(notable=False)], now=NOW) == []


# ---- 新鮮度 --------------------------------------------------------------

def test_stale_real_price_is_not_notified():
    """隔夜的實價已經不是「你點進去會看到的價格」——實測只有 10% 的價格
    能維持 24 小時不變。"""
    old = (NOW - timedelta(hours=MAX_AGE_HOURS + 1)).isoformat()
    assert notifiable([_item(observed_at=old)], now=NOW) == []


def test_freshness_boundary():
    inside = (NOW - timedelta(hours=MAX_AGE_HOURS - 0.1)).isoformat()
    outside = (NOW - timedelta(hours=MAX_AGE_HOURS + 0.1)).isoformat()
    assert len(notifiable([_item(observed_at=inside)], now=NOW)) == 1
    assert notifiable([_item(observed_at=outside)], now=NOW) == []


def test_unparseable_timestamp_is_treated_as_stale():
    """不猜，寧可不推。"""
    for bad in ("", None, "not-a-date", 12345):
        assert notifiable([_item(observed_at=bad)], now=NOW) == [], f"{bad!r}"


def test_naive_timestamp_is_read_as_utc():
    """重點是「沒有時區的字串要當成 UTC」，不是某個特定日期。所以時間戳
    從 NOW 推導，不寫字面值——否則改 NOW 就會誤紅。"""
    naive = (NOW - timedelta(hours=1)).replace(tzinfo=None).isoformat()
    assert "+" not in naive and "Z" not in naive      # 確認真的沒有時區資訊
    assert len(notifiable([_item(observed_at=naive)], now=NOW)) == 1


# ---- 自洽性：訊息裡兩個數字不能互相矛盾 --------------------------------

def test_inconsistent_discount_and_price_is_refused(caplog):
    """訊息會同時秀「實價 Y」和「便宜 X%」。若某處只更新了一半，使用者
    一眼就看得到矛盾——寧可不推。"""
    bad = _item(price=7200.0, neighbour_median=12000.0)
    bad["discount_pct"] = 65.0                      # 實際是 40%
    with caplog.at_level(logging.WARNING):
        assert notifiable([bad], now=NOW) == []
    assert "自相矛盾" in caplog.text


def test_rounding_slack_is_allowed():
    ok = _item(price=7200.0, neighbour_median=12000.0)
    ok["discount_pct"] = 40.5                       # 實際 40.0，在 1pp 容許內
    assert len(notifiable([ok], now=NOW)) == 1


def test_missing_median_does_not_block():
    """沒有中位數就無從檢查，不該因此擋掉一則合格推播。"""
    it = _item(); it["neighbour_median"] = 0
    assert len(notifiable([it], now=NOW)) == 1


# ---- 排序與健壯性 --------------------------------------------------------

def test_biggest_discount_first():
    got = notifiable([_item(price=9600.0, destination="KIX"),   # -20%
                      _item(price=6000.0, destination="CTS"),   # -50%
                      _item(price=8400.0, destination="FUK")],  # -30%
                     now=NOW)
    assert [g["destination"] for g in got] == ["CTS", "FUK", "KIX"]


def test_malformed_entries_are_skipped_not_fatal():
    good = _item()
    for bad in ({}, {"origin": "TPE"}, None, "nope",
                _item(price="abc"), _item(depart_date=None)):
        got = notifiable([bad, good], now=NOW)
        assert [g["depart_date"] for g in got] == ["2026-11-20"], f"{bad!r}"


# ---- 文案 ----------------------------------------------------------------

def test_message_shows_exact_price_never_an_estimate():
    """能走到這裡的都是實價，所以不該出現「約」的估價語意。"""
    t = format_cheap_day(_item(price=7420.0))
    assert "7,420 TWD" in t
    assert "約 7,4" not in t
    assert "Google 實價" in t


def test_message_has_the_frozen_booking_link():
    """q= 的組法與 notify.format_alert 完全一致。專案實測過改了會讓
    Google 的解析退化（HANDOFF_AI §2 明列為禁區）。"""
    t = format_cheap_day(_item())
    assert ("https://www.google.com/travel/flights?q=Flights%20from%20TPE"
            "%20to%20NRT%20on%202026-11-20%20through%202026-11-25") in t


def test_message_covers_dates_airline_and_discount():
    t = format_cheap_day(_item(price=7200.0))
    assert "2026-11-20 週五" in t
    assert "5 天來回" in t
    assert "BR" in t
    assert "便宜 40%" in t


def test_message_survives_a_one_way_entry():
    it = _item(); it["return_date"] = None
    t = format_cheap_day(it)
    assert "↩" not in t and "through" not in t


def test_message_without_carrier_does_not_say_none():
    t = format_cheap_day(_item(carriers=None))
    assert "None" not in t and "多家航空" in t


# ---- 去重 ----------------------------------------------------------------

def _store(tmp_path):
    return Store(str(tmp_path / "p.db"))


def test_first_time_always_notifies(tmp_path):
    st = _store(tmp_path)
    assert should_notify(st, _item(), now=NOW) is True
    st.close()


def test_same_day_is_suppressed_afterwards(tmp_path):
    """便宜日會連續好幾天掛在看板上，沒有這條線會天天轟炸同一個日期。"""
    st = _store(tmp_path)
    it = _item()
    st.record_cheap_day_notice("TPE", "NRT", "2026-11-20", 7200.0,
                               NOW.isoformat())
    assert should_notify(st, it, now=NOW + timedelta(days=1)) is False
    assert should_notify(st, it, now=NOW + timedelta(days=SUPPRESS_DAYS - 1)) is False
    assert should_notify(st, it, now=NOW + timedelta(days=SUPPRESS_DAYS + 1)) is True
    st.close()


def test_a_much_bigger_drop_breaks_the_suppression(tmp_path):
    """又便宜一大截是新消息，值得再說一次。"""
    st = _store(tmp_path)
    st.record_cheap_day_notice("TPE", "NRT", "2026-11-20", 10000.0,
                               NOW.isoformat())
    nxt = NOW + timedelta(days=1)
    just_under = 10000.0 * (1 - RENOTIFY_DROP_PCT / 100.0)
    assert should_notify(st, _item(price=just_under), now=nxt) is True
    assert should_notify(st, _item(price=just_under + 1), now=nxt) is False
    assert should_notify(st, _item(price=9990.0), now=nxt) is False   # 只降 0.1%
    st.close()


def test_a_price_rise_never_renotifies(tmp_path):
    st = _store(tmp_path)
    st.record_cheap_day_notice("TPE", "NRT", "2026-11-20", 7000.0,
                               NOW.isoformat())
    assert should_notify(st, _item(price=9000.0),
                         now=NOW + timedelta(days=1)) is False
    st.close()


def test_suppression_is_per_route_and_date(tmp_path):
    st = _store(tmp_path)
    st.record_cheap_day_notice("TPE", "NRT", "2026-11-20", 7200.0,
                               NOW.isoformat())
    nxt = NOW + timedelta(days=1)
    assert should_notify(st, _item(destination="KIX"), now=nxt) is True
    assert should_notify(st, _item(depart_date="2026-11-21"), now=nxt) is True
    st.close()


def test_notice_row_is_upserted_not_duplicated(tmp_path):
    st = _store(tmp_path)
    for p in (9000.0, 8000.0, 7000.0):
        st.record_cheap_day_notice("TPE", "NRT", "2026-11-20", p, NOW.isoformat())
    rows = st.conn.execute("SELECT price FROM cheap_day_notices").fetchall()
    assert [r["price"] for r in rows] == [7000.0]
    st.close()


def test_broken_stored_timestamp_falls_back_to_notifying(tmp_path):
    st = _store(tmp_path)
    st.record_cheap_day_notice("TPE", "NRT", "2026-11-20", 7200.0, "garbage")
    assert should_notify(st, _item(), now=NOW) is True
    st.close()


# ---- run() 端到端 --------------------------------------------------------

def test_run_records_what_it_sent(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(C, "send_line", lambda t, **k: sent.append(t) or True)
    monkeypatch.setattr(C, "send_telegram", lambda t, **k: False)
    monkeypatch.setattr(C, "channels_configured", lambda: True)
    db = str(tmp_path / "p.db")
    path = _board(tmp_path, _item(), _item(destination="KIX", price=6000.0))
    s = run(db, path, now=NOW)
    assert s["sent"] == 2 and s["failed"] == 0
    assert len(sent) == 2
    st = Store(db)
    assert st.conn.execute("SELECT COUNT(*) FROM cheap_day_notices").fetchone()[0] == 2
    st.close()
    # 第二次跑：全被抑制，不重送
    s2 = run(db, path, now=NOW + timedelta(hours=1))
    assert s2["sent"] == 0 and s2["suppressed"] == 2
    assert len(sent) == 2


def test_run_ignores_a_board_full_of_cache_prices(tmp_path, monkeypatch):
    """真實情境：驗證還沒跑之前，整個看板都是快取估價 → 一則都不推。"""
    monkeypatch.setattr(C, "send_line", lambda t, **k: True)
    monkeypatch.setattr(C, "channels_configured", lambda: True)
    path = _board(tmp_path, _item(source="aviasales"),
                  _item(source="aviasales", destination="KIX"))
    s = run(str(tmp_path / "p.db"), path, now=NOW)
    assert s["board"] == 2 and s["candidates"] == 0 and s["sent"] == 0


def test_run_is_soft_on_a_missing_or_broken_board(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        s = run(str(tmp_path / "p.db"), str(tmp_path / "nope.json"), now=NOW)
    assert s == {"board": 0, "candidates": 0, "suppressed": 0,
                 "sent": 0, "failed": 0, "dry_run": False}
    bad = tmp_path / "bad.json"; bad.write_text("{not json", encoding="utf-8")
    assert run(str(tmp_path / "p.db"), str(bad), now=NOW)["board"] == 0


def test_send_failure_is_counted_and_not_recorded(tmp_path, monkeypatch):
    """送失敗就不能記成已推播，否則抑制期會吃掉一則從沒送出的通知。"""
    monkeypatch.setattr(C, "send_line", lambda t, **k: False)
    monkeypatch.setattr(C, "send_telegram", lambda t, **k: False)
    monkeypatch.setattr(C, "channels_configured", lambda: True)
    db = str(tmp_path / "p.db")
    s = run(db, _board(tmp_path, _item()), now=NOW)
    assert s["failed"] == 1 and s["sent"] == 0
    st = Store(db)
    assert st.conn.execute("SELECT COUNT(*) FROM cheap_day_notices").fetchone()[0] == 0
    st.close()


def test_dry_run_sends_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "send_line",
                        lambda t, **k: (_ for _ in ()).throw(AssertionError("不該送")))
    monkeypatch.setattr(C, "send_telegram",
                        lambda t, **k: (_ for _ in ()).throw(AssertionError("不該送")))
    db = str(tmp_path / "p.db")
    s = run(db, _board(tmp_path, _item()), now=NOW, dry_run=True)
    assert s["sent"] == 1
    st = Store(db)
    assert st.conn.execute("SELECT COUNT(*) FROM cheap_day_notices").fetchone()[0] == 0
    st.close()


def test_main_never_fails_the_workflow(tmp_path):
    """推播跑在抓價與 commit 之後；為了通知失敗讓整輪變紅會遮掉真正的資料問題。"""
    assert C.main([str(tmp_path / "p.db"), str(tmp_path / "nope.json")]) == 0


# ---- 跨接縫的整合測試 ----------------------------------------------------
# 這個專案已經兩次出現「單元測試全綠但接線是死的」：export_web 的參數改名被
# try/except 吞掉（288 測試全過、看板空的），以及 quota 檢查差點沒接上。
# 這裡跨過 verify → export → notify 三段接縫，用真的 DB 和真的 export。

# **跨到 export() 的測試必須注入時鐘：export(..., now=NOW)。**
#
# 這是我自己埋的定時炸彈（2026-09-02 生產紅燈）：種子的 observed_at 釘在
# NOW=2026-09-01 12:00，但 export() 當時是用**真實時鐘**判斷新鮮度
# （cheap_days.FRESH_HOURS = 24）。寫的那天綠，整整 24 小時後必紅——而
# monitor.yml 先跑 pytest 才抓價，於是又賠掉一小時的價格資料。
#
# 正解不是「把種子改成相對真實時鐘」（那樣會綠，但不決定性，且測不了與時間
# 有關的行為），而是用 export() 早就提供的 now= 注入——test_current_price.py
# 的時間戳釘在 2026-07 卻從沒腐爛過，就是因為它一路都傳 now=NOW。
# 同一個教訓也記在 test_fie.py 對「釘死日期 vs 滑動時鐘」的註解裡。

def _ago(**kw):
    """相對注入時鐘 NOW 的時間戳（例：_ago(minutes=5)）。"""
    return (NOW - timedelta(**kw)).isoformat(timespec="seconds")


def _seed(store, o, d, base, *, cheap_index=10, cheap_price=6000,
          normal=10000, observed_at=None, source="aviasales", carriers=""):
    from datetime import timedelta as _td
    at = observed_at or _ago(minutes=10)
    for i in range(21):
        dep = (base + _td(days=i)).isoformat()
        ret = (base + _td(days=i + 5)).isoformat()
        price = cheap_price if i == cheap_index else normal
        store.conn.execute(
            "INSERT INTO observations (origin,destination,depart_date,return_date,"
            "price,currency,carriers,stops,duration,observed_at,fare_class,source,"
            "provider) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (o, d, dep, ret, price, "TWD", carriers, 0, "180", at, "any",
             source, "travelpayouts" if source == "aviasales" else "scrapedo"))
    store.conn.commit()


def test_export_calls_in_this_file_all_inject_the_clock():
    """守住上面那段註解講的規則，而且是在**寫壞的當下**紅，不是 24 小時後
    在生產環境紅。2026-09-02 那次就是這樣賠掉一小時價格資料的。

    做法：掃自己的原始碼。比起「相信註解」，這條會真的抓到漏傳 now= 的
    export() 呼叫——包含以後才加的。
    """
    src = Path(__file__).read_text(encoding="utf-8")
    calls = re.findall(r"export\(db,\s*out[^)]*\)", src)
    assert calls, "找不到 export() 呼叫，這條守衛失效了"
    missing = [c for c in calls if "now=" not in c]
    assert not missing, f"這些 export() 呼叫沒有注入時鐘，會隨時間腐爛: {missing}"


def test_cache_only_board_pushes_nothing_end_to_end(tmp_path, monkeypatch):
    """驗證還沒跑之前：看板有便宜日，但一則都不推。這就是使用者要的行為。"""
    from datetime import timedelta as _td
    from farehunter.export_web import export
    monkeypatch.setattr(C, "send_line", lambda t, **k: True)
    monkeypatch.setattr(C, "channels_configured", lambda: True)
    db = str(tmp_path / "p.db")
    st = Store(db)
    base = NOW.date() + _td(days=60)   # 錨在注入時鐘，不用真實今天
    _seed(st, "TPE", "NRT", base)
    st.close()
    out = str(tmp_path / "data.json")
    payload = export(db, out, now=NOW)
    assert payload["cheap_days"], "看板本身該有便宜日"
    assert any(c["notable"] for c in payload["cheap_days"])
    s = run(db, out, now=NOW)
    assert s["candidates"] == 0 and s["sent"] == 0     # 全是快取估價 → 不推


def test_verified_price_flows_all_the_way_to_a_push(tmp_path, monkeypatch):
    """verify_airlines 寫入實價 → export 重算（source 變 google）→ 推播成立。

    跨三段接縫：storage → export_web.export → cheap_day_notify.run。
    """
    from datetime import timedelta as _td
    from farehunter.export_web import export
    sent = []
    monkeypatch.setattr(C, "send_line", lambda t, **k: sent.append(t) or True)
    monkeypatch.setattr(C, "send_telegram", lambda t, **k: False)
    monkeypatch.setattr(C, "channels_configured", lambda: True)
    db = str(tmp_path / "p.db")
    st = Store(db)
    base = NOW.date() + _td(days=60)   # 錨在注入時鐘，不用真實今天
    cheap_dep = (base + _td(days=10)).isoformat()
    _seed(st, "TPE", "NRT", base)
    # verify_airlines 驗過那一天：較新的 google 觀測，實價 6,200（仍很便宜）
    st.conn.execute(
        "INSERT INTO observations (origin,destination,depart_date,return_date,"
        "price,currency,carriers,stops,duration,observed_at,fare_class,source,"
        "provider) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("TPE", "NRT", cheap_dep, (base + _td(days=15)).isoformat(), 6200.0,
         "TWD", "BR", 0, "180",
         _ago(minutes=1),
         "any", "google", "scrapedo"))
    st.conn.commit(); st.close()

    out = str(tmp_path / "data.json")
    payload = export(db, out, now=NOW)
    hit = [c for c in payload["cheap_days"] if c["depart_date"] == cheap_dep]
    assert hit, "export 沒把驗證過的那天算進看板"
    assert hit[0]["source"] == "google", "export 沒把 source 換成實價"
    assert hit[0]["price"] == 6200.0

    s = run(db, out, now=NOW)
    assert s["sent"] == 1, f"實價便宜日沒有推出去: {s}"
    assert "6,200 TWD" in sent[0] and "Google 實價" in sent[0]


def test_verification_that_kills_the_bargain_stops_the_push(tmp_path, monkeypatch):
    """關鍵行為：若實價其實沒那麼便宜，驗證本身就是過濾器。

    不需要另外寫「別推假便宜」的邏輯——export 用實價重算 discount_pct，
    落差掉下來就自動不再 notable，推播自然不成立。
    """
    from datetime import timedelta as _td
    from farehunter.export_web import export
    monkeypatch.setattr(C, "send_line",
                        lambda t, **k: (_ for _ in ()).throw(AssertionError("不該送")))
    monkeypatch.setattr(C, "channels_configured", lambda: True)
    db = str(tmp_path / "p.db")
    st = Store(db)
    base = NOW.date() + _td(days=60)   # 錨在注入時鐘，不用真實今天
    cheap_dep = (base + _td(days=10)).isoformat()
    _seed(st, "TPE", "NRT", base)
    # 快取說 6,000，實際查出來是 9,800——根本不是特別便宜
    st.conn.execute(
        "INSERT INTO observations (origin,destination,depart_date,return_date,"
        "price,currency,carriers,stops,duration,observed_at,fare_class,source,"
        "provider) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("TPE", "NRT", cheap_dep, (base + _td(days=15)).isoformat(), 9800.0,
         "TWD", "BR", 0, "180",
         _ago(minutes=1),
         "any", "google", "scrapedo"))
    st.conn.commit(); st.close()

    out = str(tmp_path / "data.json")
    payload = export(db, out, now=NOW)
    hit = [c for c in payload["cheap_days"] if c["depart_date"] == cheap_dep]
    assert not (hit and hit[0]["notable"]), "實價 9,800 不該還算 notable 便宜日"
    s = run(db, out, now=NOW)
    assert s["sent"] == 0
