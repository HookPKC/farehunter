"""google-flights2（RapidAPI）一次性探測（ADR 0005）：端點、參數、扣額度方式、價格對不對得上。

只讀不寫：不碰 prices.db、不 commit。金鑰放在 header（X-RapidAPI-Key），不在網址裡；
輸出仍一律先把金鑰換成 ***，連線錯誤只印例外類型。每次執行只打一個請求（免費額度每月約 150 次）。
"""
from __future__ import annotations

import json
import os
import sys

import requests

KEY = os.environ.get("RAPIDAPI_KEY", "").strip()
HOST = (os.environ.get("RAPID_HOST") or "google-flights2.p.rapidapi.com").strip()


def red(s) -> str:
    s = str(s)
    return s.replace(KEY, "***") if KEY else s


def shape(o, depth=0, max_depth=5) -> str:
    pad = "  " * depth
    if isinstance(o, dict):
        if depth >= max_depth:
            return f"{pad}{{…{len(o)} keys}}"
        out = []
        for k, v in list(o.items())[:40]:
            if isinstance(v, (dict, list)):
                out.append(f"{pad}{k}: {type(v).__name__}" + (f"[{len(v)}]" if isinstance(v, list) else ""))
                out.append(shape(v, depth + 1, max_depth))
            else:
                out.append(f"{pad}{k}: {str(v)[:80]}")
        return "\n".join(out)
    if isinstance(o, list):
        return shape(o[0], depth, max_depth) if o else f"{pad}(empty)"
    return f"{pad}{str(o)[:80]}"


def main() -> int:
    if not KEY:
        print("沒有 RAPIDAPI_KEY（secret 名稱要一模一樣）")
        return 1
    path = "/" + os.environ.get("RAPID_PATH", "").strip().lstrip("/")
    try:
        params = json.loads(os.environ.get("RAPID_PARAMS") or "{}")
    except ValueError:
        print("params 不是合法 JSON")
        return 1
    print(f"host={HOST} path={path} params={json.dumps(params, ensure_ascii=False)}")
    try:
        r = requests.get(f"https://{HOST}{path}", params=params, timeout=(10, 90),
                         headers={"X-RapidAPI-Key": KEY, "X-RapidAPI-Host": HOST})
    except requests.RequestException as e:
        print("連線錯誤：", type(e).__name__)
        return 1
    hdr = {k: v for k, v in r.headers.items()
           if any(t in k.lower() for t in ("ratelimit", "quota", "remaining", "limit"))}
    print(f"HTTP {r.status_code}；額度 header：{red(json.dumps(hdr, ensure_ascii=False))}；{len(r.content):,} bytes")
    try:
        body = r.json()
    except ValueError:
        print("非 JSON：", red(r.text[:500]))
        return 1
    data = body.get("data") if isinstance(body, dict) else None
    its = data.get("itineraries") if isinstance(data, dict) else None
    if isinstance(its, dict):          # searchFlights：每個行程一行（價格、停靠、航空、班號）
        print("itineraries keys:", {k: (len(v) if isinstance(v, list) else type(v).__name__) for k, v in its.items()})
        for group, rows in its.items():
            for it in rows if isinstance(rows, list) else []:
                legs = it.get("flights") or []
                print(f"  [{group}] price={it.get('price')} stops={it.get('stops')} "
                      f"airlines={[f.get('airline') for f in legs]} "
                      f"nums={[f.get('flight_number') for f in legs]} "
                      f"logo={str(it.get('airline_logo'))[-6:]} keys={sorted(it)[:14]}")
        ph = data.get("priceHistory") or {}
        print("priceHistory.summary:", json.dumps(ph.get("summary"), ensure_ascii=False),
              "history points:", len(ph.get("history") or []))
        print("data keys:", sorted(data))
    text = red(json.dumps(body, ensure_ascii=False))
    print("結構：\n" + red(shape(body)))
    limit = int(os.environ.get("RAPID_DUMP") or 3000)
    print(f"內容（前 {limit} 字）：", text[:limit])
    return 0 if r.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
