"""ChocoData 一次性探測（ADR 0005）：找出 Google Flights 端點的格式、扣點、有沒有航空公司。

只讀不寫：不碰 prices.db、不 commit。repo 公開、Actions 紀錄公開，而 ChocoData 的金鑰是放在
**網址查詢參數**裡（官方 SDK 0.1.0：`?api_key=...`，header 認證會回 401）——所以這支程式
絕不印網址、也不印連線錯誤原文；所有輸出先經 `red()` 把金鑰（原文與網址編碼）換成 ***。

用法（Actions → Probe ChocoData → Run workflow）：
- mode=discover：每個候選路徑只打一次、不帶航班參數，看哪個不是 404，錯誤訊息通常會列出必填參數。
- mode=search：指定 path 與 params（JSON），打一次真的查詢，印出回傳結構與價格／航空公司欄位。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse

import requests

BASE = "https://api.chocodata.com/api/v1"
CANDIDATES = [
    "google/flights", "google_flights/search", "google-flights/search",
    "googleflights/search", "google/flights_search", "google_flights/flights",
]
KEY = os.environ.get("CHOCODATA_API_KEY", "").strip()


def red(text) -> str:
    s = str(text)
    if KEY:
        for v in {KEY, urllib.parse.quote(KEY, safe=""), urllib.parse.quote_plus(KEY)}:
            s = s.replace(v, "***")
    return s


def scrub(obj):
    """回應若回顯請求參數，把 api_key 欄位拿掉。"""
    if isinstance(obj, dict):
        return {k: ("***" if "key" in k.lower() else scrub(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return obj


def shape(obj, depth=0, max_depth=4) -> str:
    pad = "  " * depth
    if isinstance(obj, dict):
        if depth >= max_depth:
            return f"{pad}{{…{len(obj)} keys}}"
        lines = []
        for k, v in list(obj.items())[:40]:
            if isinstance(v, (dict, list)):
                lines.append(f"{pad}{k}: {type(v).__name__}"
                             + (f"[{len(v)}]" if isinstance(v, list) else ""))
                lines.append(shape(v, depth + 1, max_depth))
            else:
                lines.append(f"{pad}{k}: {str(v)[:80]!s}")
        return "\n".join(lines)
    if isinstance(obj, list):
        return shape(obj[0], depth, max_depth) if obj else f"{pad}(empty)"
    return f"{pad}{str(obj)[:80]}"


def call(path: str, params: dict) -> tuple[int, dict | None, dict]:
    q = {"api_key": KEY, **params}
    try:
        r = requests.get(f"{BASE}/{path}", params=q, timeout=(10, 120),
                         headers={"Accept": "application/json"})
    except requests.RequestException as e:   # 錯誤原文會含網址＝含金鑰，只印類型
        print(f"  [{path}] 連線錯誤：{type(e).__name__}")
        return 0, None, {}
    hdr = {k: v for k, v in r.headers.items()
           if any(t in k.lower() for t in ("credit", "quota", "limit", "remaining", "cost", "usage"))}
    try:
        body = r.json()
    except ValueError:
        body = None
        print(f"  [{path}] HTTP {r.status_code}，非 JSON，{len(r.content)} bytes")
    return r.status_code, scrub(body) if body is not None else None, hdr


def report(path, status, body, hdr, full=False):
    print(f"\n=== {path} → HTTP {status}")
    if hdr:
        print("  額度相關 header：", red(json.dumps(hdr, ensure_ascii=False)))
    if body is None:
        return
    text = red(json.dumps(body, ensure_ascii=False))
    if status != 200 or not full:
        print("  回應（前 1500 字）：", text[:1500])
        return
    print("  回應結構：\n" + red(shape(body)))
    # 價格／航空公司欄位抽樣
    hits = []

    def walk(o, trail=""):
        if isinstance(o, dict):
            for k, v in o.items():
                t = f"{trail}.{k}" if trail else k
                if any(w in k.lower() for w in ("price", "airline", "carrier", "flight_number",
                                                  "stops", "duration", "currency", "credit")):
                    if not isinstance(v, (dict, list)):
                        hits.append(f"{t} = {str(v)[:60]}")
                walk(v, t)
        elif isinstance(o, list):
            for i, v in enumerate(o[:3]):
                walk(v, f"{trail}[{i}]")
    walk(body)
    print("  價格／航空公司欄位抽樣：")
    for h in hits[:60]:
        print("   ", red(h))
    print("  第一筆完整內容（前 3000 字）：")
    first = body
    for k in ("best_flights", "flights", "results", "data", "other_flights", "items"):
        if isinstance(body, dict) and isinstance(body.get(k), list) and body[k]:
            first = body[k][0]
            break
    print("   ", red(json.dumps(first, ensure_ascii=False))[:3000])


def main() -> int:
    if not KEY:
        print("沒有 CHOCODATA_API_KEY（secret 名稱要一模一樣）")
        return 1
    mode = os.environ.get("MODE", "discover")
    if mode == "discover":
        for p in CANDIDATES:
            status, body, hdr = call(p, {})
            report(p, status, body, hdr)
            if status not in (0, 404):
                print(f"\n→ 第一個不是 404 的路徑：{p}（之後的候選不再打，省額度）")
                break
        return 0
    path = os.environ.get("CHOCO_PATH", "").strip().strip("/")
    try:
        params = json.loads(os.environ.get("CHOCO_PARAMS") or "{}")
    except ValueError:
        print("params 不是合法 JSON")
        return 1
    if not path:
        print("search 模式要填 path")
        return 1
    print("查詢參數：", json.dumps(params, ensure_ascii=False))
    status, body, hdr = call(path, params)
    report(path, status, body, hdr, full=True)
    return 0 if status == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
