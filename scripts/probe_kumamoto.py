"""一次性：3 月初福岡 vs 熊本的票價（2026-11-27 使用者要去熊本、福岡）。只讀、不寫 prices.db、不 commit。

1. 虎航官網日曆（TPE、KHH 兩站的原始列）：列出所有跟 KMJ／FUK 有關的航線，以及 2/23～3/15 每天的單程未稅價。
2. Bright Data 抓 Google Flights：幾組 3 月初的來回（高雄⇄福岡、高雄⇄熊本、桃園⇄熊本），印直飛最低價與航空公司；
   沒有直飛時印轉機最低價。金鑰不印（brightdata_flights.fetch 已遮罩）。
"""
import sys
from collections import defaultdict
from datetime import date

sys.path.insert(0, "src")
from farehunter import brightdata_flights as bd          # noqa: E402
from farehunter import tigerair_calendar as tw           # noqa: E402

W = "一二三四五六日"


def day(s):
    d = date.fromisoformat(s)
    return f"{s[5:]}({W[d.weekday()]})"


legs = defaultdict(dict)
for st in ("TPE", "KHH"):
    try:
        rows = tw.fetch(st)
    except Exception as exc:                              # noqa: BLE001
        print(f"虎航 {st}：{exc}")
        continue
    for r in rows:
        try:
            o, d = str(r["origin"]).upper(), str(r["destination"]).upper()
            dep = str(r["pricingDate"])[:10]
            price = float(r["pricingAmount"])
        except (KeyError, TypeError, ValueError):
            continue
        if {"KMJ", "FUK"} & {o, d} and price > 0:
            legs[(o, d)][dep] = min(price, legs[(o, d)].get(dep, price))
print("== 虎航官網：跟熊本／福岡有關的航線 ==", sorted(legs))
for k in sorted(legs):
    sel = sorted((dep, p) for dep, p in legs[k].items() if "2027-02-23" <= dep <= "2027-03-15")
    print(f"{k[0]}→{k[1]}：", "、".join(f"{day(dep)} {p:,.0f}" for dep, p in sel) or "（這段期間沒有）")

trips = [("KHH", "FUK", "2027-03-02", "2027-03-05"), ("KHH", "FUK", "2027-03-03", "2027-03-10"),
         ("KHH", "KMJ", "2027-03-02", "2027-03-06"), ("KHH", "KMJ", "2027-03-03", "2027-03-08"),
         ("TPE", "KMJ", "2027-03-02", "2027-03-06"), ("TPE", "KMJ", "2027-03-03", "2027-03-08")]
print("\n== Google Flights（Bright Data，現在查）==")
for o, d, dep, ret in trips:
    try:
        its = bd.parse(bd.fetch(o, d, dep, ret))
    except Exception as exc:                              # noqa: BLE001
        print(f"{o}⇄{d} {day(dep)}–{day(ret)}：查詢失敗 {type(exc).__name__}")
        continue
    direct = sorted((i for i in its if i.nonstop), key=lambda i: i.price)
    other = sorted((i for i in its if not i.nonstop), key=lambda i: i.price)
    if direct:
        top = "；".join(f"{i.price:,.0f}（{'、'.join(i.airlines)}）" for i in direct[:3])
        print(f"{o}⇄{d} {day(dep)}–{day(ret)}：直飛 {top}")
    else:
        alt = f"{other[0].price:,.0f}（{'、'.join(other[0].airlines)}）" if other else "沒有"
        print(f"{o}⇄{d} {day(dep)}–{day(ret)}：沒有直飛；轉機最低 {alt}（共 {len(its)} 個行程）")
