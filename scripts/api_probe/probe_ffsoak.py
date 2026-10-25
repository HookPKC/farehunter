"""fast-flights 長時間交錯對照測試（第四輪）。

第三輪：成功的價格全部正確，但約一半回 errorHasStatus（頁面完整 1.9 MB、不是被擋），
而且失敗成群出現——前段 18 次只成功 3 次、後段各寫法都 5/6。順序與時間混在一起，
分不出「hl=zh-TW」「沿用 cookie」有沒有幫助。

這輪把四種寫法（fresh/persistent client × en-US/zh-TW）交錯輪流打 CYCLES 輪，
同一時段內四種都打到，時間因素就被抵銷。每次失敗照「生產可用的重試策略」再試
最多 RETRIES 次（間隔 RETRY_WAIT 秒），量出「單次成功率」與「含重試的成功率」。
"""
from __future__ import annotations

import time

from probe_fastflights import PIP_DEPS, TIMEOUT_S  # noqa: F401
from probe_ffreliab import ITINS, _attempt, _query

PROVIDER = "fast-flights-soak"
CYCLES = 12
SPACING = 5.0
RETRIES = 2
RETRY_WAIT = 15.0
DEADLINE_S = 22 * 60          # 留餘裕給 job 的 timeout-minutes
ARMS = [("fresh", "en-US"), ("fresh", "zh-TW"), ("persist", "en-US"), ("persist", "zh-TW")]


def _new_client():
    import primp
    return primp.Client(impersonate="chrome_145", impersonate_os="macos",
                        referer=True, cookie_store=True, timeout=TIMEOUT_S)


def probe(env, today):
    try:
        import fast_flights  # noqa: F401
        import primp  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return [{"provider": PROVIDER, "case": "import", "route": "-", "ok": False,
                 "results": None, "dates": None, "min_price": None, "currency": None,
                 "carriers": None, "calls": 0,
                 "note": f"SKIPPED: import failed: {type(e).__name__}: {e}"[:300]}]

    start = time.monotonic()
    persistent = {lang: _new_client() for _, lang in ARMS}
    stats = {arm: {"first_ok": 0, "final_ok": 0, "n": 0, "calls": 0, "seq": []} for arm in ARMS}
    timeline = []        # 每輪：四種寫法的單次結果 o/x
    mismatches = []      # 同一行程不同次成功卻價格不同
    seen_price = {}

    for cycle in range(CYCLES):
        if time.monotonic() - start > DEADLINE_S:
            break
        marks = ""
        order = ARMS[cycle % 4:] + ARMS[:cycle % 4]          # 每輪換起手，避免固定先後
        for k, arm in enumerate(order):
            mode, lang = arm
            route, dep, ret, _ref = ITINS[(cycle + k) % len(ITINS)]
            time.sleep(SPACING)
            client = persistent[lang] if mode == "persist" else _new_client()
            code, price, _ = _attempt(client, _query(route, dep, ret, lang=lang))
            st = stats[arm]
            st["n"] += 1
            st["calls"] += 1
            first = code.startswith("OK")
            st["first_ok"] += first
            tries = 0
            while not code.startswith("OK") and tries < RETRIES:
                tries += 1
                time.sleep(RETRY_WAIT)
                client = persistent[lang] if mode == "persist" else _new_client()
                code, price, _ = _attempt(client, _query(route, dep, ret, lang=lang))
                st["calls"] += 1
            st["final_ok"] += code.startswith("OK")
            # o=一次就成功；x1/x2=重試 1/2 次後成功；xF=重試用完仍失敗
            st["seq"].append("o" if first else ("x" + str(tries) if code.startswith("OK") else "xF"))
            marks += "o" if first else "x"
            if price:
                key = (route, dep, ret)
                if key in seen_price and seen_price[key] != price:
                    mismatches.append(f"{route}{dep[5:]}:{seen_price[key]}->{price}")
                seen_price.setdefault(key, price)
        timeline.append(marks)

    rows = []
    for arm in ARMS:
        st = stats[arm]
        n = st["n"] or 1
        rows.append({
            "provider": PROVIDER, "case": f"arm {arm[0]} client, hl={arm[1]}", "route": "*",
            "ok": st["final_ok"] > 0, "results": st["final_ok"], "dates": None,
            "min_price": None, "currency": "TWD", "carriers": None, "calls": st["calls"],
            "note": (f"first-try {st['first_ok']}/{st['n']} ({100 * st['first_ok'] // n}%), "
                     f"with ≤{RETRIES} retries @{RETRY_WAIT:.0f}s {st['final_ok']}/{st['n']} "
                     f"({100 * st['final_ok'] // n}%); seq={' '.join(st['seq'])}")[:300]})
    elapsed = time.monotonic() - start
    rows.append({
        "provider": PROVIDER, "case": "timeline (first tries per cycle, 4 arms)", "route": "*",
        "ok": True, "results": len(timeline), "dates": None, "min_price": None,
        "currency": None, "carriers": None, "calls": 0,
        "note": (f"{elapsed / 60:.1f} min; " + " ".join(timeline)
                 + f"; price changes between successes: {mismatches or 'none'}")[:300]})
    return rows
