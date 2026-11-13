"""ChocoData 一次性探測（ADR 0005）：找出 Google Flights 端點的格式、扣點、有沒有航空公司。

只讀不寫：不碰 prices.db、不 commit。repo 公開、Actions 紀錄公開，而 ChocoData 的金鑰是放在
**網址查詢參數**裡（官方 SDK 0.1.0：`?api_key=...`，header 認證會回 401）——所以這支程式
絕不印網址、也不印連線錯誤原文；所有輸出先經 `red()` 把金鑰（原文與網址編碼）換成 ***。

用法（Actions → Probe ChocoData → Run workflow）：
- mode=discover：每個候選路徑只打一次、不帶航班參數，看哪個不是 404，錯誤訊息通常會列出必填參數。
- mode=flights：用 universal.get 抓一頁 Google Flights（Bright Data 同一個網址），用同一個解析器讀價格。
- mode=search：指定 path 與 params（JSON），打一次真的查詢，印出回傳結構與價格／航空公司欄位。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse

import requests

BASE = "https://api.chocodata.com/api/v1"
CANDIDATES = ["google/flights"]  # 第一輪 6 個都 404（不扣點）；只留一個拿 available 清單
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
    avail = body.get("available") if isinstance(body, dict) else None
    if isinstance(avail, list):   # unknown_target：列出全部可用端點裡跟機票／旅遊有關的
        words = ("google", "flight", "air", "travel", "trip", "kayak", "skyscanner", "expedia",
                 "kiwi", "momondo", "booking", "priceline", "hopper", "fare", "agoda")
        rel = [a for a in avail if any(w in str(a).lower() for w in words)]
        print(f"  {body.get('error')}：可用端點共 {len(avail)} 個；相關的 {len(rel)} 個：")
        print("   ", ", ".join(map(str, rel)))
        return
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


def flights() -> int:
    """universal.get 抓一頁 Google Flights，用 Bright Data 同一套網址與解析器（ADR 0004）。"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    from farehunter.brightdata_flights import parse, search_url
    o, d, dep, ret = (os.environ.get(k, "").strip() for k in ("ORIGIN", "DEST", "DEPART", "RET"))
    try:
        extra = json.loads(os.environ.get("CHOCO_PARAMS") or "{}")
    except ValueError:
        print("params 不是合法 JSON")
        return 1
    url = search_url(o, d, dep, ret)
    print(f"行程 {o}-{d} {dep}~{ret}；額外參數 {json.dumps(extra, ensure_ascii=False)}")
    status, body, hdr = call("universal/get", {"url": url, **extra})
    print(f"HTTP {status}；額度相關 header：{red(json.dumps(hdr, ensure_ascii=False))}")
    if not isinstance(body, dict):
        return 1
    if status != 200:
        print("回應：", red(json.dumps(body, ensure_ascii=False))[:1500])
        return 1
    strings = []

    def walk(o_, trail=""):
        if isinstance(o_, dict):
            for k, v in o_.items():
                walk(v, f"{trail}.{k}" if trail else k)
        elif isinstance(o_, list):
            for i, v in enumerate(o_[:5]):
                walk(v, f"{trail}[{i}]")
        elif isinstance(o_, str):
            strings.append((len(o_), trail, o_))
    walk(body)
    strings.sort(reverse=True)
    print("回應結構：\n" + red(shape(body, max_depth=2)))
    if not strings:
        return 1
    n, where, page = strings[0]
    print(f"最長字串欄位 {where}：{n:,} 字元；含 aria-label {page.count('aria-label')} 個；"
          f"含 'round trip total' {page.count('round trip total')} 個")
    its = parse(page)
    print(f"解析出 {len(its)} 個行程")
    nonstop = sorted((i for i in its if i.nonstop), key=lambda i: i.price)
    for i in nonstop[:5]:
        print(f"  直飛 NT${i.price:,.0f}  {i.carriers}")
    if not its:
        print("  頁面開頭（遮罩後前 800 字）：", red(page[:800]))
    return 0 if its else 1


def main() -> int:
    if not KEY:
        print("沒有 CHOCODATA_API_KEY（secret 名稱要一模一樣）")
        return 1
    mode = os.environ.get("MODE", "discover")
    if mode == "discover":
        # 第三輪：印出全部端點名（不含任何金鑰），再對「疑似通用擷取」與旅遊 search 端點
        # 各打一次不帶參數的請求，只看必填參數。缺參數回 400、header Spb-Cost: 0（已驗證）；
        # 萬一某個端點不需要參數而真的執行（200），立刻停手，避免連續扣點。
        status, body, hdr = call(CANDIDATES[0], {})
        avail = body.get("available") if isinstance(body, dict) else None
        if not isinstance(avail, list):
            report(CANDIDATES[0], status, body, hdr)
            return 1
        print(f"全部可用端點 {len(avail)} 個：")
        for i in range(0, len(avail), 12):
            print("  ", ", ".join(map(str, avail[i:i + 12])))
        generic = ("web", "url", "html", "scrape", "render", "browser", "page", "fetch",
                   "proxy", "generic", "universal", "crawl", "screenshot", "markdown", "unlock",
                   "serp", "custom", "any")
        travel = ("expedia.search", "priceline.search", "kayak.search", "agoda.search",
                  "tripadvisor.search")
        targets = [a for a in map(str, avail)
                   if a in travel or any(w in a.lower() for w in generic)]
        print(f"\n要檢查必填參數的端點 {len(targets)} 個：{', '.join(targets)}")
        for t in targets[:40]:
            path = t.replace(".", "/", 1)
            st, b, h = call(path, {})
            issues = b.get("issues") if isinstance(b, dict) else None
            need = [".".join(map(str, i.get("path", []))) + f"({i.get('message')})"
                    for i in issues] if isinstance(issues, list) else None
            print(f"  {t}: HTTP {st}  cost={h.get('Spb-Cost')}  必填={need if need is not None else red(json.dumps(b, ensure_ascii=False))[:300]}")
            if st == 200:
                print("  → 這個端點不帶參數就執行了（可能扣點），停止後續檢查")
                break
        return 0
    if mode == "flights":
        return flights()
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
